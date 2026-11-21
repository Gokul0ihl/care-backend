"""OPD vitals (OPD05-08): Care's questionnaire route with our checks around it.

The vitals form is a Care questionnaire (slug ``opd-vitals``) whose questions
carry LOINC codes and UCUM units, so a save makes ordinary Care Observations
that observation/analyse, patient history and FHIR/ABDM exports already read.
Each vital also has a global ObservationDefinition carrying its adult
reference ranges, which Care's own interpretation code applies.

What Care's questionnaire submit doesn't do, save_vitals() adds:
physical limits, the required list with "can't measure" reasons, server-side
MAP/BMI/BSA, flags from the definitions, the reading source, and the move to
the doctor's queue. docs/opd-vitals-to-rx.md explains the design.
"""

import math
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from care.emr.models.observation import Observation
from care.emr.models.observation_definition import ObservationDefinition
from care.emr.models.questionnaire import Questionnaire
from care.emr.resources.questionnaire.utils import handle_response
from care.emr.resources.questionnaire_response.spec import QuestionnaireSubmitRequest
from care.emr.utils.compute_observation_interpretation import (
    compute_observation_interpretation,
)
from care.security.authorization import AuthorizationController
from hmis.constants import OpdStage, OpdVitalsStatus, VitalsSource

QUESTIONNAIRE_SLUG = "opd-vitals"
QUESTIONNAIRE_VERSION = "1.0"
# styling_metadata.poh.kind: the form builder hides system-owned forms.
SYSTEM_KIND = "opd_vitals"
DEFINITION_PREFIX = "poh-vital-"

LOINC = "http://loinc.org"
UCUM = "http://unitsofmeasure.org"
V3_INTERPRETATION = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"
SOURCE_SYSTEM = "https://pacificonehealth.in/fhir/CodeSystem/vitals-source"
VITAL_SIGNS = {
    "system": "http://terminology.hl7.org/CodeSystem/observation-category",
    "code": "vital-signs",
    "display": "Vital Signs",
}
# Question ids are derived from link ids, so re-seeding keeps them stable and
# old QuestionnaireResponses still match their questions.
QUESTION_NS = uuid.UUID("6f1d0c3e-4b0a-4c55-9a43-2f6f7a0c9b11")

MAX_BP_READINGS = 5
MAX_REASON_LENGTH = 200
ADULT = [
    {
        "metric": "patient_age",
        "operation": "in_range",
        "value": {"min": 18, "max": 150, "value_type": "years"},
    }
]


def question_id(link_id):
    return str(uuid.uuid5(QUESTION_NS, link_id))


def interpretation(code, display):
    return {
        "display": display,
        "highlight": code != "N",
        "code": {"system": V3_INTERPRETATION, "code": code, "display": display},
    }


NORMAL = interpretation("N", "Normal")
LOW = interpretation("L", "Low")
HIGH = interpretation("H", "High")
CRITICAL_LOW = interpretation("LL", "Critically low")
CRITICAL_HIGH = interpretation("HH", "Critically high")


def bands(*items):
    """Ranges for one vital, written highest first. Care's evaluator takes
    the first range that contains the value and its bounds are inclusive, so
    a value on a boundary gets the higher band (SBP 140 is High, SpO2 95 is
    Normal)."""
    return [
        {
            "interpretation": interp,
            **({"min": low} if low is not None else {}),
            **({"max": high} if high is not None else {}),
        }
        for interp, low, high in items
    ]


@dataclass(frozen=True)
class Vital:
    link_id: str
    text: str
    code: str
    display: str
    unit: str = ""
    unit_display: str = ""
    type: str = "decimal"
    # Physical limits: outside them a value is a typing mistake and the save
    # is refused. Flags come from the ranges instead and never block.
    low: float | None = None
    high: float | None = None
    required: bool = False
    read_only: bool = False
    options: tuple = ()
    # Adult ranges (age 18+). Children get no flag until paediatric ranges
    # are signed off; see docs/opd-vitals-to-rx.md.
    ranges: list = field(default_factory=list)

    @property
    def coding(self):
        return {"system": LOINC, "code": self.code, "display": self.display}


SYSTOLIC = Vital(
    "systolic",
    "Systolic",
    "8480-6",
    "Systolic blood pressure",
    "mm[Hg]",
    "mmHg",
    "integer",
    50,
    300,
    ranges=bands(
        (CRITICAL_HIGH, 180, None),
        (HIGH, 140, 180),
        (NORMAL, 90, 140),
        (LOW, 70, 90),
        (CRITICAL_LOW, None, 70),
    ),
)
DIASTOLIC = Vital(
    "diastolic",
    "Diastolic",
    "8462-4",
    "Diastolic blood pressure",
    "mm[Hg]",
    "mmHg",
    "integer",
    20,
    200,
    ranges=bands(
        (CRITICAL_HIGH, 120, None),
        (HIGH, 90, 120),
        (NORMAL, 60, 90),
        (LOW, None, 60),
    ),
)
BP_PANEL = Vital(
    "bp", "Blood pressure", "85354-9", "Blood pressure panel", required=True
)

VITALS = [
    Vital(
        "pulse",
        "Pulse",
        "8867-4",
        "Heart rate",
        "/min",
        "/min",
        "integer",
        20,
        300,
        required=True,
        ranges=bands(
            (CRITICAL_HIGH, 130, None),
            (HIGH, 101, 130),
            (NORMAL, 60, 101),
            (LOW, 40, 60),
            (CRITICAL_LOW, None, 40),
        ),
    ),
    Vital(
        "spo2",
        "SpO₂",
        "59408-5",
        "Oxygen saturation by pulse oximetry",
        "%",
        "%",
        "integer",
        50,
        100,
        required=True,
        ranges=bands((NORMAL, 95, None), (LOW, 90, 95), (CRITICAL_LOW, None, 90)),
    ),
    Vital(
        "temperature",
        "Temperature",
        "8310-5",
        "Body temperature",
        "[degF]",
        "°F",
        "decimal",
        85,
        113,
        required=True,
        ranges=bands(
            (CRITICAL_HIGH, 104, None),
            (HIGH, 100.4, 104),
            (NORMAL, 97, 100.4),
            (LOW, 95, 97),
            (CRITICAL_LOW, None, 95),
        ),
    ),
    Vital(
        "respiratory_rate",
        "Respiratory rate",
        "9279-1",
        "Respiratory rate",
        "/min",
        "/min",
        "integer",
        4,
        80,
        required=True,
        ranges=bands(
            (CRITICAL_HIGH, 30, None),
            (HIGH, 21, 30),
            (NORMAL, 12, 21),
            (LOW, 8, 12),
            (CRITICAL_LOW, None, 8),
        ),
    ),
    Vital(
        "height",
        "Height",
        "8302-2",
        "Body height",
        "cm",
        "cm",
        "decimal",
        30,
        250,
        required=True,
    ),
    Vital(
        "weight",
        "Weight",
        "29463-7",
        "Body weight",
        "kg",
        "kg",
        "decimal",
        0.5,
        350,
        required=True,
    ),
    Vital(
        "pain",
        "Pain score",
        "72514-3",
        "Pain severity - 0-10 verbal numeric rating",
        "{score}",
        "/10",
        "integer",
        0,
        10,
        required=True,
        ranges=bands(
            (interpretation("HH", "Severe pain"), 7, None),
            (interpretation("H", "Moderate pain"), 4, 7),
            (interpretation("N", "Mild pain"), 1, 4),
            (interpretation("N", "No pain"), None, 1),
        ),
    ),
    Vital(
        "glucose",
        "Random blood sugar",
        "2339-0",
        "Glucose [Mass/volume] in Blood",
        "mg/dL",
        "mg/dL",
        "decimal",
        10,
        1000,
        ranges=bands(
            (CRITICAL_HIGH, 400, None),
            (HIGH, 140, 400),
            (NORMAL, 70, 140),
            (LOW, 54, 70),
            (CRITICAL_LOW, None, 54),
        ),
    ),
    Vital(
        "head_circumference",
        "Head circumference",
        "9843-4",
        "Head Occipital-frontal circumference",
        "cm",
        "cm",
        "decimal",
        20,
        70,
    ),
    Vital(
        "blood_group",
        "Blood group",
        "882-1",
        "ABO and Rh group [Type] in Blood",
        type="choice",
        options=("A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"),
    ),
    Vital(
        "lmp",
        "Last menstrual period",
        "8665-2",
        "Last menstrual period start date",
        type="date",
    ),
    # Calculated on the server from the readings above; whatever the browser
    # sends for these is replaced.
    Vital(
        "map",
        "Mean arterial pressure",
        "8478-0",
        "Mean blood pressure",
        "mm[Hg]",
        "mmHg",
        "integer",
        read_only=True,
        ranges=bands((HIGH, 106, None), (NORMAL, 70, 106), (LOW, None, 70)),
    ),
    Vital(
        "bmi",
        "BMI",
        "39156-5",
        "Body mass index (BMI) [Ratio]",
        "kg/m2",
        "kg/m²",
        read_only=True,
        ranges=bands(
            (interpretation("H", "Obese"), 30, None),
            (interpretation("H", "Overweight"), 25, 30),
            (NORMAL, 18.5, 25),
            (interpretation("L", "Underweight"), None, 18.5),
        ),
    ),
    Vital(
        "bsa",
        "Body surface area",
        "8277-6",
        "Body surface area",
        "m2",
        "m²",
        read_only=True,
    ),
]
# Patient condition (OPD05) has no LOINC code: it stays in the
# QuestionnaireResponse and doesn't become an Observation.
CONDITION = Vital(
    "condition",
    "Patient condition",
    "",
    "",
    type="choice",
    options=(
        "Ambulatory",
        "Needs support",
        "Wheelchair",
        "Stretcher",
        "Unwell/distressed",
    ),
)

BY_LINK = {v.link_id: v for v in [*VITALS, SYSTOLIC, DIASTOLIC, BP_PANEL, CONDITION]}
BY_CODE = {v.code: v for v in [*VITALS, SYSTOLIC, DIASTOLIC, BP_PANEL]}
REQUIRED = [BP_PANEL.link_id] + [v.link_id for v in VITALS if v.required]
DEFINED = [v for v in [*VITALS, BP_PANEL] if v.ranges or v is BP_PANEL]


# --- The questionnaire and definitions (seeded by setup_opd_vitals) -------


def _question(vital):
    question = {
        "id": question_id(vital.link_id),
        "link_id": vital.link_id,
        "text": vital.text,
        "type": vital.type,
        "required": False,  # enforced by save_vitals, which allows "can't measure"
        "styling_metadata": {
            "poh": {
                "required": vital.required,
                **({"min": vital.low} if vital.low is not None else {}),
                **({"max": vital.high} if vital.high is not None else {}),
            }
        },
    }
    if vital.code:
        question["code"] = vital.coding
        question["category"] = VITAL_SIGNS
    if vital.unit:
        question["unit"] = {
            "system": UCUM,
            "code": vital.unit,
            "display": vital.unit_display,
        }
    if vital.read_only:
        question["read_only"] = True
    if vital.options:
        question["answer_option"] = [{"value": o} for o in vital.options]
    return question


def questionnaire_questions():
    bp = {
        **_question(BP_PANEL),
        "type": "group",
        "is_component": True,
        "repeats": True,
        "questions": [_question(SYSTOLIC), _question(DIASTOLIC)],
    }
    return [bp, *[_question(v) for v in VITALS], _question(CONDITION)]


def _definition_fields(vital):
    fields = {
        "title": vital.display,
        "status": "active",
        "description": f"POH OPD vitals: {vital.text}",
        "derived_from_uri": "",
        "category": "vital_signs",
        "code": vital.coding,
        "permitted_data_type": "decimal" if vital.type == "integer" else vital.type,
        "permitted_unit": (
            {"system": UCUM, "code": vital.unit, "display": vital.unit_display}
            if vital.unit
            else None
        ),
        "qualified_ranges": (
            [{"title": "Adult", "conditions": ADULT, "ranges": vital.ranges}]
            if vital.ranges
            else []
        ),
        "component": None,
    }
    if vital is BP_PANEL:
        fields["permitted_data_type"] = "decimal"
        fields["component"] = [
            {
                "code": part.coding,
                "permitted_data_type": "decimal",
                "permitted_unit": {
                    "system": UCUM,
                    "code": part.unit,
                    "display": part.unit_display,
                },
                "qualified_ranges": [
                    {"title": "Adult", "conditions": ADULT, "ranges": part.ranges}
                ],
            }
            for part in (SYSTOLIC, DIASTOLIC)
        ]
    return fields


def definition_slug(vital):
    return ObservationDefinition.calculate_slug_from_instance(
        f"{DEFINITION_PREFIX}{vital.code}"
    )


def seed(apply=False, user=None):
    """Creates or refreshes the questionnaire and the definitions. Returns
    the planned changes as lines; writes only with apply=True. Safe to run
    again: the rows are found by slug and overwritten with the catalogue."""
    lines = []
    questionnaire = Questionnaire.objects.filter(slug=QUESTIONNAIRE_SLUG).first()
    lines.append(
        f"{'update' if questionnaire else 'create'} questionnaire {QUESTIONNAIRE_SLUG}"
    )
    definitions = {}
    for vital in DEFINED:
        slug = definition_slug(vital)
        existing = ObservationDefinition.objects.filter(
            slug=slug, facility=None
        ).first()
        definitions[vital.code] = (slug, existing)
        lines.append(
            f"{'update' if existing else 'create'} definition {slug} ({vital.display})"
        )
    if not apply:
        return lines
    with transaction.atomic():
        values = {
            "version": QUESTIONNAIRE_VERSION,
            "title": "OPD vitals",
            "description": "Vitals station form (system-owned; edit in code, not the form builder)",
            "subject_type": "encounter",
            "status": "active",
            "styling_metadata": {"poh": {"kind": SYSTEM_KIND, "system": True}},
            "questions": questionnaire_questions(),
            "updated_by": user,
        }
        if questionnaire:
            for key, value in values.items():
                setattr(questionnaire, key, value)
            questionnaire.save()
        else:
            Questionnaire.objects.create(
                slug=QUESTIONNAIRE_SLUG, created_by=user, **values
            )
        for vital in DEFINED:
            slug, existing = definitions[vital.code]
            fields = _definition_fields(vital)
            if existing:
                for key, value in fields.items():
                    setattr(existing, key, value)
                existing.updated_by = user
                existing.save()
            else:
                ObservationDefinition.objects.create(
                    slug=slug, facility=None, created_by=user, updated_by=user, **fields
                )
    return lines


def get_questionnaire():
    questionnaire = Questionnaire.objects.filter(slug=QUESTIONNAIRE_SLUG).first()
    if questionnaire is None:
        raise ValidationError(
            "The OPD vitals form isn't set up. Run: python manage.py setup_opd_vitals --apply"
        )
    return questionnaire


def form():
    """What the station screen needs to draw the form: the question ids to
    answer with, and the limits, units and required list it checks first."""
    questionnaire = get_questionnaire()
    return {
        "questionnaire": QUESTIONNAIRE_SLUG,
        "version": questionnaire.version,
        "max_bp_readings": MAX_BP_READINGS,
        "sources": [{"value": s.value, "label": s.label} for s in VitalsSource],
        "questions": questionnaire.questions,
        "required": REQUIRED,
    }


# --- Calculations ---------------------------------------------------------


def calculate(bp_readings, height_cm, weight_kg):
    """MAP from the average BP; BMI and BSA (Mosteller) from height and
    weight. Missing inputs leave that value out."""
    out = {}
    if bp_readings:
        systolic = sum(r[0] for r in bp_readings) / len(bp_readings)
        diastolic = sum(r[1] for r in bp_readings) / len(bp_readings)
        out["bp_average"] = {"systolic": round(systolic), "diastolic": round(diastolic)}
        out["map"] = round((systolic + 2 * diastolic) / 3)
    if height_cm and weight_kg:
        metres = height_cm / 100
        out["bmi"] = round(weight_kg / (metres * metres), 1)
        out["bsa"] = round(math.sqrt(height_cm * weight_kg / 3600), 2)
    return out


def _number(raw):
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def check_value(vital, raw):
    """The error for one typed value, or None."""
    if vital.type not in ("integer", "decimal"):
        return None
    value = _number(raw)
    if value is None:
        return "Enter a number"
    if vital.type == "integer" and value != value.to_integral_value():
        return "Enter a whole number"
    if vital.low is not None and value < Decimal(str(vital.low)):
        return (
            f"Below {vital.low} {vital.unit_display} isn't possible; check the reading"
        )
    if vital.high is not None and value > Decimal(str(vital.high)):
        return (
            f"Above {vital.high} {vital.unit_display} isn't possible; check the reading"
        )
    return None


# --- Saving ---------------------------------------------------------------


def _first_value(result):
    for value in result.get("values") or []:
        if value.get("value") not in (None, ""):
            return value["value"]
    return None


def parse_answers(results, questionnaire, errors):
    """Care's results list → {link_id: value} and the BP readings. Answers to
    unknown or calculated questions are dropped."""
    link_of = {}

    def walk(questions):
        for q in questions:
            link_of[q["id"]] = q["link_id"]
            walk(q.get("questions") or [])

    walk(questionnaire.questions)
    answers, readings = {}, []
    for result in results:
        link_id = link_of.get(str(result.get("question_id")))
        vital = BY_LINK.get(link_id)
        # BP parts only count inside a reading; calculated values are ours.
        if vital is None or vital.read_only or vital in (SYSTOLIC, DIASTOLIC):
            continue
        if link_id == BP_PANEL.link_id:
            sub_results = result.get("sub_results") or []
            if len(sub_results) > MAX_BP_READINGS:
                errors.add(["bp"], f"Record at most {MAX_BP_READINGS} BP readings")
            for index, reading in enumerate(sub_results[:MAX_BP_READINGS]):
                parts = {
                    link_of.get(str(r.get("question_id"))): _first_value(r)
                    for r in reading
                }
                readings.append((index, parts.get("systolic"), parts.get("diastolic")))
            continue
        value = _first_value(result)
        if value is not None:
            answers[link_id] = value
    return answers, readings


def _reason_error(link_id, reason, answers, bp):
    if link_id not in REQUIRED:
        return "Only a required vital can be marked as not measured"
    if not str(reason or "").strip():
        return "Say why it couldn't be measured"
    if len(str(reason)) > MAX_REASON_LENGTH:
        return f"Keep the reason under {MAX_REASON_LENGTH} characters"
    if link_id in answers or (link_id == "bp" and bp):
        return "A reading was entered; clear it or the reason"
    return None


def _check_sources(given, errors):
    sources = {}
    for link_id, source in given.items():
        if link_id not in BY_LINK or BY_LINK[link_id].read_only:
            errors.add(["sources", link_id], "Unknown vital")
        elif source not in VitalsSource.values:
            errors.add(["sources", link_id], "Unknown reading source")
        else:
            sources[link_id] = source
    return sources


def _check_reading(index, systolic, diastolic, errors):
    """One BP reading as (systolic, diastolic), or None if blank or wrong."""
    if systolic in (None, "") and diastolic in (None, ""):
        return None
    loc = ["bp", index]
    ok = True
    for part, raw in ((SYSTOLIC, systolic), (DIASTOLIC, diastolic)):
        error = (
            f"Enter the {part.text.lower()} reading"
            if raw in (None, "")
            else check_value(part, raw)
        )
        if error:
            errors.add([*loc, part.link_id], error)
            ok = False
    if not ok:
        return None
    s, d = int(Decimal(str(systolic))), int(Decimal(str(diastolic)))
    if d >= s:
        errors.add([*loc, "diastolic"], "Diastolic must be lower than systolic")
        return None
    return s, d


def validate(answers, readings, not_measured, errors):
    """Physical limits, the required list and the "can't measure" reasons.
    Returns the BP readings as numbers."""
    bp = [r for r in (_check_reading(*r, errors) for r in readings) if r]
    for link_id, raw in answers.items():
        if error := check_value(BY_LINK[link_id], raw):
            errors.add([link_id], error)
    for link_id, reason in not_measured.items():
        if error := _reason_error(link_id, reason, answers, bp):
            errors.add(["not_measured", link_id], error)
    for link_id in REQUIRED:
        if link_id == "bp":
            # An invalid reading already has its own error.
            given = bool(bp) or any(e["loc"][0] == "bp" for e in errors.items)
        else:
            given = link_id in answers or any(
                e["loc"] == [link_id] for e in errors.items
            )
        if not given and link_id not in not_measured:
            errors.add([link_id], "Enter a reading or mark it as not measured")
    return bp


def _result(link_id, value):
    return {"question_id": question_id(link_id), "values": [{"value": str(value)}]}


def build_results(answers, bp, derived):
    """The final results in Care's shape: typed answers, the BP readings as
    sub-results, and the calculated values written by the server."""
    results = [_result(link_id, value) for link_id, value in answers.items()]
    if bp:
        results.append(
            {
                "question_id": question_id("bp"),
                "sub_results": [
                    [_result("systolic", s), _result("diastolic", d)] for s, d in bp
                ],
            }
        )
    for link_id in ("map", "bmi", "bsa"):
        if link_id in derived:
            results.append(_result(link_id, derived[link_id]))
    return results


def _source_coding(source):
    return {
        "system": SOURCE_SYSTEM,
        "code": source,
        "display": VitalsSource(source).label,
    }


def annotate(observations, sources):
    """Links each new Observation to its definition, sets the flag and range
    from it with Care's own evaluator, and records the reading source."""
    definitions = {
        d.code.get("code"): d
        for d in ObservationDefinition.objects.filter(
            facility=None, slug__in=[definition_slug(v) for v in DEFINED]
        )
    }
    metrics_cache = {}
    for observation in observations:
        code = (observation.main_code or {}).get("code")
        vital = BY_CODE.get(code)
        if vital is None:
            continue
        source = sources.get(vital.link_id)
        if source:
            observation.method = _source_coding(source)
        definition = definitions.get(code)
        patient = observation.patient
        if definition and (patient.date_of_birth or patient.year_of_birth):
            observation.observation_definition = definition
            metrics_cache = (
                compute_observation_interpretation(observation, metrics_cache)
                or metrics_cache
            )
        observation.save()


def _flag(interp):
    code = ((interp or {}).get("code") or {}).get("code")
    return {"code": code, "display": interp.get("display")} if code else None


def summarise(observations, derived, not_measured):
    """The readings as the station shows them, with each flag."""
    readings, flags = {}, []
    for observation in observations:
        vital = BY_CODE.get((observation.main_code or {}).get("code"))
        if vital is None:
            continue
        if vital is BP_PANEL:
            parts = {}
            for component in observation.component:
                part = BY_CODE.get((component.get("code") or {}).get("code"))
                if part is None:
                    continue
                parts[part.link_id] = {
                    "value": component.get("value", {}).get("value"),
                    "flag": _flag(component.get("interpretation")),
                }
            readings.setdefault("bp", []).append(parts)
            for part_id, part in parts.items():
                if part["flag"] and part["flag"]["code"] != "N":
                    flags.append({"vital": part_id, **part["flag"]})
            continue
        flag = _flag(observation.interpretation)
        readings[vital.link_id] = {
            "value": (observation.value or {}).get("value"),
            "unit": vital.unit_display,
            "flag": flag,
        }
        if flag and flag["code"] != "N":
            flags.append({"vital": vital.link_id, **flag})
    return {
        "readings": readings,
        "bp_average": derived.get("bp_average"),
        "flags": flags,
        "not_measured": not_measured,
    }


def _mark_entered_in_error(record, user):
    response = record.questionnaire_response
    response.status = "entered_in_error"
    response.updated_by = user
    response.save(update_fields=["status", "updated_by", "modified_date"])
    Observation.objects.filter(questionnaire_response=response).update(
        status="entered_in_error", updated_by=user, modified_date=timezone.now()
    )
    record.status = OpdVitalsStatus.corrected
    record.updated_by = user
    record.save(update_fields=["status", "updated_by", "modified_date"])


def save_vitals(user, spec, visit):
    """One transaction: check, calculate, save through Care's handle_response,
    annotate the Observations, and (first save only) move the visit on to the
    doctor and call the next patient at the station.

    Returns (record, next_visit, created)."""
    from hmis.models import OpdVitalsRecord
    from hmis.opd import call_next, move
    from hmis.outreach import Errors

    encounter = visit.encounter
    if not AuthorizationController.call(
        "can_submit_encounter_questionnaire_obj", user, encounter
    ):
        raise PermissionDenied(
            "You do not have permission to record vitals for this visit"
        )

    existing = OpdVitalsRecord.objects.filter(
        submission_key=spec.submission_key
    ).first()
    if existing:
        if existing.visit_id != visit.id:
            raise ValidationError("This submission key was used for another visit")
        return existing, None, False

    current = visit.vitals_records.filter(status=OpdVitalsStatus.current).first()
    replaces = None
    if spec.replaces:
        if current is None or current.external_id != spec.replaces:
            raise ValidationError("Only the visit's current vitals can be corrected")
        replaces = current
    elif current is not None:
        raise ValidationError(
            "Vitals are already saved for this visit; correct them instead"
        )
    if replaces is None and visit.stage not in (
        OpdStage.waiting_vitals,
        OpdStage.at_vitals,
    ):
        raise ValidationError("This visit is past the vitals station")

    questionnaire = get_questionnaire()
    errors = Errors()
    answers, readings = parse_answers(spec.results, questionnaire, errors)
    not_measured = {k: str(v).strip() for k, v in spec.not_measured.items()}
    bp = validate(answers, readings, not_measured, errors)
    sources = _check_sources(spec.sources, errors)
    errors.raise_if_any()

    height = Decimal(str(answers["height"])) if "height" in answers else None
    weight = Decimal(str(answers["weight"])) if "weight" in answers else None
    derived = calculate(
        bp, float(height) if height else None, float(weight) if weight else None
    )
    request = QuestionnaireSubmitRequest.model_validate(
        {
            "resource_id": str(encounter.external_id),
            "encounter": str(encounter.external_id),
            "patient": str(encounter.patient.external_id),
            "results": build_results(answers, bp, derived),
        }
    )

    with transaction.atomic():
        if replaces is not None:
            _mark_entered_in_error(replaces, user)
        response = handle_response(questionnaire, request, user)
        observations = list(
            Observation.objects.filter(questionnaire_response=response)
            .select_related("patient", "encounter")
            .order_by("id")
        )
        annotate(observations, sources)
        summary = summarise(observations, derived, not_measured)
        record = OpdVitalsRecord.objects.create(
            visit=visit,
            questionnaire_response=response,
            submission_key=spec.submission_key,
            replaces=replaces,
            sources=sources,
            not_measured=not_measured,
            summary=summary,
            created_by=user,
            updated_by=user,
        )
        next_visit = None
        if replaces is None:
            station = visit.station
            visit.vitals_done_at = timezone.now()
            move(visit, OpdStage.waiting_doctor, user, extra=["vitals_done_at"])
            if station is not None:
                next_visit = call_next(station, user, quiet=True)
    return record, next_visit, True
