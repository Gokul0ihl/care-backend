"""The lab test catalogue: each lab test is a Care ActivityDefinition (classification
laboratory) linked to its tariff row (ChargeItemDefinition), the sample it needs
(SpecimenDefinition) and the parameters it reports (ObservationDefinitions, LOINC).
POH's own rules (fasting hours, after-meal sample, sensitive report, icon) sit beside it
in LabTestRule.

The front desk reads the catalogue to book lab tests, and facility admins edit it
from Settings → Lab tests in hmis_fe."""

import re
from typing import NamedTuple

from django.db import transaction
from pydantic import BaseModel, Field, field_validator, model_validator
from rest_framework.exceptions import ValidationError

from care.emr.models import ActivityDefinition
from care.emr.models.charge_item_definition import ChargeItemDefinition
from care.emr.models.observation import Observation
from care.emr.models.observation_definition import ObservationDefinition
from care.emr.models.resource_category import ResourceCategory
from care.emr.models.service_request import ServiceRequest
from care.emr.models.specimen import Specimen
from care.emr.models.specimen_definition import SpecimenDefinition
from hmis.billing import CATALOGUE, ensure_definition, slug_for
from hmis.models import LabTestRule

SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"
SPECIMEN_TYPES = "http://terminology.hl7.org/CodeSystem/v2-0487"

# Care requires a procedure code on every activity definition; lab tests share this one.
LAB_PROCEDURE = {"system": SNOMED, "code": "15220000", "display": "Laboratory test"}
# Set on the sample's patient preparation when the test needs fasting, so Care's own
# specimen record says so too. The hours live in LabTestRule.
AFTER_FASTING = {"system": SNOMED, "code": "726054005", "display": "After fasting"}

# The valuesets the admin's codes must come from (checked when they change).
SPECIMEN_VALUESET = "system-specimen_type-code"
PARAMETER_VALUESET = "system-observation"

CODE_PATTERN = r"^[A-Z0-9][A-Z0-9-]{0,19}$"
# An icon key from hmis_fe's lab icon set (lib/lab-icons.ts), e.g. "blood". The app
# owns the set, so the server only checks the key's shape.
ICON_PATTERN = r"^[a-z0-9-]{0,40}$"
MAX_FASTING_HOURS = 24
MAX_PARAMETERS = 60


class CodingSpec(BaseModel):
    system: str
    code: str = Field(min_length=1, max_length=64)
    display: str = Field(min_length=1, max_length=1024)


class ParameterSpec(CodingSpec):
    # The name shown on the test and its report; the LOINC display is often long.
    name: str = Field(min_length=1, max_length=255)

    def coding(self):
        return {"system": self.system, "code": self.code, "display": self.display}


class LabTestWriteSpec(BaseModel):
    facility: str
    title: str = Field(min_length=1, max_length=255)
    status: str = "active"
    charge_item_definition: str
    specimen: CodingSpec
    fasting_hours: int | None = Field(default=None, ge=1, le=MAX_FASTING_HOURS)
    # Taken after a meal (post-prandial), e.g. PPBS. The desk's "After meal" filter.
    after_meal: bool = False
    sensitive_report: bool = False
    # Blank: the app picks an icon from the test's code or sample.
    icon: str = ""
    parameters: list[ParameterSpec] = Field(min_length=1, max_length=MAX_PARAMETERS)

    @field_validator("title")
    @classmethod
    def strip_title(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Enter the test's name")
        return value

    @field_validator("icon")
    @classmethod
    def icon_key(cls, value):
        value = value.strip()
        if not re.match(ICON_PATTERN, value):
            raise ValueError("Choose an icon from the list")
        return value

    @field_validator("status")
    @classmethod
    def known_status(cls, value):
        if value not in ("active", "retired"):
            raise ValueError("Status must be active or retired")
        return value

    @field_validator("specimen")
    @classmethod
    def specimen_system(cls, value):
        if value.system != SPECIMEN_TYPES:
            raise ValueError("Choose the sample type from the list")
        return value

    @model_validator(mode="after")
    def fasting_or_after_meal(self):
        if self.fasting_hours and self.after_meal:
            raise ValueError(
                "A test is either taken fasting or after a meal, not both"
            )
        return self

    @model_validator(mode="after")
    def unique_parameters(self):
        codes = [parameter.code for parameter in self.parameters]
        if any(parameter.system != LOINC for parameter in self.parameters):
            raise ValueError("Parameters must be LOINC codes")
        if len(codes) != len(set(codes)):
            raise ValueError("A parameter is listed twice")
        return self


class LabTestCreateSpec(LabTestWriteSpec):
    code: str

    @field_validator("code")
    @classmethod
    def code_format(cls, value):
        value = value.strip().upper()
        if not re.match(CODE_PATTERN, value):
            raise ValueError(
                "Use up to 20 capital letters, digits and hyphens, e.g. LFT"
            )
        return value


def field_error(field, message):
    """A 400 in the same shape as a Pydantic error, so the form shows it under its field."""
    return ValidationError(
        {"errors": [{"type": "validation_error", "loc": [field], "msg": message}]}
    )


def lab_slug(code):
    return f"poh-lab-{code.lower()}"


def parameter_slug(code):
    return f"loinc-{code.lower()}"


def facility_slug(facility, slug_value):
    # Every definition model shares SlugBaseModel's facility scoping.
    return ActivityDefinition.calculate_slug_from_facility(facility.external_id, slug_value)


def code_from_slug(facility, slug):
    prefix = facility_slug(facility, "poh-lab-")
    return slug[len(prefix) :].upper() if slug.startswith(prefix) else None


def base_price(definition):
    for component in definition.price_components or []:
        if component.get("monetary_component_type") == "base":
            return component.get("amount")
    return None


def serialize_charge_item(definition):
    return {
        "slug": definition.slug,
        "code": definition.purpose,
        "title": definition.title,
        "price": base_price(definition),
        "status": definition.status,
    }


def lab_tests(facility, status=None):
    """The facility's lab tests, each with its fee, sample, rules and parameters,
    loaded in a fixed number of queries."""
    activities = ActivityDefinition.objects.filter(
        facility=facility,
        classification="laboratory",
        slug__startswith=facility_slug(facility, "poh-lab-"),
    ).order_by("title")
    if status:
        activities = activities.filter(status=status)
    activities = list(activities)

    def by_id(model, field):
        ids = {i for activity in activities for i in getattr(activity, field)}
        return {row.id: row for row in model.objects.filter(id__in=ids)}

    charge_items = by_id(ChargeItemDefinition, "charge_item_definitions")
    specimens = by_id(SpecimenDefinition, "specimen_requirements")
    parameters = by_id(ObservationDefinition, "observation_result_requirements")
    rules = {
        rule.activity_definition_id: rule
        for rule in LabTestRule.objects.filter(activity_definition__in=activities)
    }

    rows = []
    for activity in activities:
        charge_item = next(
            (charge_items[i] for i in activity.charge_item_definitions if i in charge_items),
            None,
        )
        specimen = next(
            (specimens[i] for i in activity.specimen_requirements if i in specimens), None
        )
        rule = rules.get(activity.id)
        rows.append(
            {
                "id": str(activity.external_id),
                "code": code_from_slug(facility, activity.slug),
                "title": activity.title,
                "status": activity.status,
                "charge_item_definition": serialize_charge_item(charge_item)
                if charge_item
                else None,
                "specimen": specimen.type_collected if specimen else None,
                "fasting_hours": rule.fasting_hours if rule else None,
                "after_meal": rule.after_meal if rule else False,
                "sensitive_report": rule.sensitive_report if rule else False,
                "icon": rule.icon if rule else "",
                "parameters": [
                    {**parameters[i].code, "name": parameters[i].title}
                    for i in activity.observation_result_requirements
                    if i in parameters
                ],
            }
        )
    return rows


def lab_test_row(facility, activity):
    return next(row for row in lab_tests(facility) if row["id"] == str(activity.external_id))


def lab_tariff(facility):
    """Tariff rows a lab test can be priced from: the facility's active diagnostics."""
    rows = ChargeItemDefinition.objects.filter(
        facility=facility,
        status="active",
        category__slug=facility_slug(facility, slug_for("h2")),
    ).order_by("title")
    return [serialize_charge_item(row) for row in rows]


def check_codes(facility, spec, activity=None):
    """Checks the codes the admin chose against Care's valuesets. Codes Care already
    holds for this facility were checked when they were first saved, so only new
    ones go to the terminology server."""
    from care.emr.registries.care_valueset.care_valueset import validate_valueset
    from care.emr.resources.common import Coding

    def check(valueset, coding, label):
        try:
            validate_valueset(
                "code",
                valueset,
                Coding.model_validate(
                    {"system": coding.system, "code": coding.code, "display": coding.display}
                ),
            )
        except ValueError as error:
            raise field_error(label, f"{coding.display}: {error}") from error

    known_specimen = activity and SpecimenDefinition.objects.filter(
        id__in=activity.specimen_requirements,
        type_collected__code=spec.specimen.code,
    ).exists()
    if not known_specimen:
        check(SPECIMEN_VALUESET, spec.specimen, "specimen")
    known = set(
        ObservationDefinition.objects.filter(
            facility=facility,
            slug__in=[
                facility_slug(facility, parameter_slug(p.code)) for p in spec.parameters
            ],
        ).values_list("code__code", flat=True)
    )
    for parameter in spec.parameters:
        if parameter.code not in known:
            check(PARAMETER_VALUESET, parameter, "parameters")


def ensure_parameter(facility, spec, user=None):
    """The facility's ObservationDefinition for a LOINC code, shared by every test that
    reports it, so renaming it here renames it everywhere."""
    slug = facility_slug(facility, parameter_slug(spec.code))
    parameter = ObservationDefinition.objects.filter(facility=facility, slug=slug).first()
    if parameter is not None and parameter.title != spec.name:
        parameter.title = spec.name
        parameter.updated_by = user
        parameter.save()
    if parameter is None:
        # Results aren't entered in this app yet, so the type defaults to a number with
        # no reference ranges; result entry will refine both.
        parameter = ObservationDefinition(
            facility=facility,
            slug=slug,
            title=spec.name,
            status="active",
            description="",
            derived_from_uri="",
            category="laboratory",
            code=spec.coding(),
            permitted_data_type="quantity",
            qualified_ranges=[],
            created_by=user,
            updated_by=user,
        )
        parameter.save()
    return parameter


@transaction.atomic
def save_lab_test(facility, spec, user=None, activity=None):
    """Creates (activity=None) or updates a lab test with its sample, parameters and rules."""
    charge_item = ChargeItemDefinition.objects.filter(
        facility=facility, slug=spec.charge_item_definition
    ).first()
    if charge_item is None:
        raise field_error("charge_item_definition", "Choose the tariff item")

    if activity is None:
        slug = facility_slug(facility, lab_slug(spec.code))
        if ActivityDefinition.objects.filter(slug=slug).exists():
            raise field_error("code", f"A lab test with code {spec.code} already exists")
        activity = ActivityDefinition(
            facility=facility,
            slug=slug,
            classification="laboratory",
            kind="service_request",
            code=LAB_PROCEDURE,
            description="",
            usage="",
            locations=[],
            created_by=user,
        )
    else:
        activity.version = (activity.version or 1) + 1
    code = code_from_slug(facility, activity.slug)

    specimen_slug = facility_slug(facility, lab_slug(code))
    specimen = SpecimenDefinition.objects.filter(facility=facility, slug=specimen_slug).first()
    if specimen is None:
        specimen = SpecimenDefinition(
            facility=facility, slug=specimen_slug, description="", created_by=user
        )
    specimen.title = f"{spec.title} — {spec.specimen.display}"
    specimen.status = "active"
    specimen.type_collected = spec.specimen.model_dump()
    specimen.patient_preparation = [AFTER_FASTING] if spec.fasting_hours else []
    specimen.updated_by = user
    specimen.save()

    activity.title = spec.title
    activity.status = spec.status
    activity.charge_item_definitions = [charge_item.id]
    activity.specimen_requirements = [specimen.id]
    activity.observation_result_requirements = [
        ensure_parameter(facility, parameter, user).id for parameter in spec.parameters
    ]
    activity.updated_by = user
    activity.save()

    LabTestRule.objects.update_or_create(
        activity_definition=activity,
        defaults={
            "fasting_hours": spec.fasting_hours,
            "after_meal": spec.after_meal,
            "sensitive_report": spec.sensitive_report,
            "icon": spec.icon,
            "updated_by": user,
        },
        create_defaults={
            "fasting_hours": spec.fasting_hours,
            "after_meal": spec.after_meal,
            "sensitive_report": spec.sensitive_report,
            "icon": spec.icon,
            "created_by": user,
            "updated_by": user,
        },
    )
    return activity


# The starting catalogue: the ten tests of the front desk's Add Lab Tests design, each
# billed by its row in the v11 tariff (billing.CATALOGUE, head H2), with the sample,
# preparation and LOINC parameters each reports. Admins edit it from then on;
# setup_lab_catalogue only adds tests that aren't there yet, on startup (--if-empty) only
# seeds a facility that has no lab tests at all, and replace_lab_catalogue swaps a
# facility's whole catalogue for this one.
SAMPLE = {
    "BLD": "Whole blood",
    "SER": "Serum",
    "PLAS": "Plasma",
    "UR": "Urine",
    "SMN": "Seminal fluid",
}


class SeedTest(NamedTuple):
    code: str
    title: str
    sample: str
    parameters: list
    fasting_hours: int | None = None
    after_meal: bool = False
    sensitive: bool = False
    icon: str = ""


LIPID_BASICS = [
    ("2093-3", "Total cholesterol"), ("2571-8", "Triglycerides"), ("2085-9", "HDL cholesterol"),
    ("13457-7", "LDL cholesterol"), ("13458-5", "VLDL cholesterol"),
    ("9830-1", "Total cholesterol/HDL ratio"), ("43396-1", "Non-HDL cholesterol"),
]

LAB_SEED = [
    SeedTest("LFT", "Liver Function Test (LFT) / Liver profile", "SER", [
        ("1975-2", "Bilirubin, total"), ("1968-7", "Bilirubin, direct"), ("1971-1", "Bilirubin, indirect"),
        ("1920-8", "AST (SGOT)"), ("1742-6", "ALT (SGPT)"), ("16325-3", "AST/ALT ratio"),
        ("6768-6", "Alkaline phosphatase"), ("2324-2", "GGT"), ("2885-2", "Total protein"),
        ("1751-7", "Albumin"), ("10834-0", "Globulin"), ("1759-0", "A/G ratio"),
    ], fasting_hours=8, icon="liver"),
    SeedTest("KFT", "Kidney Function Test (KFT) / Renal function", "SER", [
        ("3091-6", "Urea"), ("3094-0", "Blood urea nitrogen"), ("2160-0", "Creatinine"),
        ("3097-3", "BUN/creatinine ratio"), ("3084-1", "Uric acid"), ("2951-2", "Sodium"),
        ("2823-3", "Potassium"), ("2075-0", "Chloride"), ("17861-6", "Calcium"),
        ("2777-1", "Phosphorus"), ("98979-8", "eGFR (CKD-EPI 2021)"),
    ], fasting_hours=8, icon="kidney"),
    SeedTest("PPBS", "Blood Sugar / Blood Glucose / Diabetes Test (PPBS)", "PLAS", [
        ("1521-4", "Post-prandial blood glucose (2 h)"),
    ], after_meal=True, icon="sugar"),
    SeedTest("CBC", "Complete Blood Count (CBC)", "BLD", [
        ("718-7", "Haemoglobin"), ("4544-3", "Haematocrit (PCV)"), ("789-8", "RBC count"),
        ("787-2", "MCV"), ("785-6", "MCH"), ("786-4", "MCHC"), ("788-0", "RDW"),
        ("6690-2", "Total WBC count"), ("777-3", "Platelet count"), ("770-8", "Neutrophils %"),
        ("736-9", "Lymphocytes %"),
    ], icon="blood"),
    SeedTest("LIP", "Lipid Profile / Lipid Test", "SER", [
        *LIPID_BASICS, ("11054-4", "LDL/HDL ratio"), ("70218-3", "Triglycerides/HDL ratio"),
    ], icon="heart"),
    SeedTest("LIPADV", "Lipid Profile - Advanced / Complete Lipid Profile", "SER", [
        *LIPID_BASICS, ("1869-7", "Apolipoprotein A1"), ("1884-6", "Apolipoprotein B"),
        ("10835-7", "Lipoprotein (a)"),
    ], icon="heart"),
    SeedTest("B12", "Vitamin B12 / Cyanocobalamin", "SER", [
        ("2132-9", "Vitamin B12"),
    ], fasting_hours=6, icon="vitamin"),
    SeedTest("URIC", "Uric Acid / Serum Uric Acid", "SER", [
        ("3084-1", "Uric acid"),
    ], fasting_hours=8, icon="bone"),
    SeedTest("TSH", "Thyroid Profile Test / T3 T4 TSH", "SER", [
        ("3053-6", "T3, total"), ("3026-2", "T4, total"), ("3016-3", "TSH"),
    ], icon="thyroid"),
    SeedTest("HBA1C", "HbA1c / Glycated Hemoglobin Test", "BLD", [
        ("4548-4", "HbA1c"), ("59261-8", "HbA1c (IFCC)"),
        ("27353-2", "Estimated average glucose"), ("53553-4", "Estimated average glucose (mmol/L)"),
    ], icon="sugar"),
]


def has_lab_tests(facility):
    """True once the facility has any POH lab test, active or retired."""
    return poh_lab_activities(facility).exists()


def seed_tariff_row(facility, code):
    """The tariff row a seed test is billed by. A row missing from the tariff (a code
    added to billing.CATALOGUE after setup_billing ran, such as PPBS) is created under
    H2; rows that exist are returned as they are, so admin prices are never changed.
    None when billing isn't set up at the facility (no H2 head)."""
    row = ChargeItemDefinition.objects.filter(
        facility=facility, slug=facility_slug(facility, slug_for(code))
    ).first()
    if row is not None:
        return row
    head = ResourceCategory.objects.filter(
        facility=facility, slug=facility_slug(facility, slug_for("h2"))
    ).first()
    entry = next((item for item in CATALOGUE if item[0] == code), None)
    if head is None or entry is None:
        return None
    _, _, title, price, description = entry
    return ensure_definition(facility, head, code, title, price, description)


def seed_spec(facility, test, charge_item):
    return LabTestCreateSpec(
        facility=str(facility.external_id),
        code=test.code,
        title=test.title,
        charge_item_definition=charge_item.slug,
        specimen={"system": SPECIMEN_TYPES, "code": test.sample, "display": SAMPLE[test.sample]},
        fasting_hours=test.fasting_hours,
        after_meal=test.after_meal,
        sensitive_report=test.sensitive,
        icon=test.icon,
        parameters=[
            {"system": LOINC, "code": loinc, "display": name, "name": name}
            for loinc, name in test.parameters
        ],
    )


def poh_lab_activities(facility):
    return ActivityDefinition.objects.filter(
        facility=facility,
        classification="laboratory",
        slug__startswith=facility_slug(facility, "poh-lab-"),
    )


@transaction.atomic
def seed_lab_catalogue(facility, user=None, only_if_empty=False):
    """Adds the starting lab tests that the facility doesn't have yet. Tests already
    there are left exactly as the admin set them. With only_if_empty (the startup
    hook), a facility that already has any lab test is left alone entirely, so tests
    an admin retired or replaced never come back. Needs setup_billing first, for the
    tariff; returns how many tests were added."""
    if only_if_empty and has_lab_tests(facility):
        return 0
    added = 0
    for test in LAB_SEED:
        if ActivityDefinition.objects.filter(
            slug=facility_slug(facility, lab_slug(test.code))
        ).exists():
            continue
        charge_item = seed_tariff_row(facility, test.code)
        if charge_item is None:
            continue
        save_lab_test(facility, seed_spec(facility, test, charge_item), user)
        added += 1
    return added


def unused(queryset, model, field):
    """The definitions in queryset that no patient record refers to, counting records
    Care has soft-deleted: deleting a definition would cascade to them."""
    records = model._base_manager  # noqa: SLF001
    used = records.filter(**{f"{field}__isnull": False}).values(field)
    return queryset.exclude(id__in=used)


@transaction.atomic
def replace_lab_catalogue(facility, user=None):
    """Swaps the facility's POH lab tests for the starting catalogue (LAB_SEED).

    A test that isn't in it is deleted outright, with its sample definition. One that
    a booking already uses can't be (ServiceRequest protects it), so it's retired. A
    test that is in it is rewritten to match, keeping its tariff row and price as they
    are, so bookings made against it stay linked; missing ones are added. Parameters
    no test reports any more are deleted too. Nothing a patient record refers to (a
    specimen or observation) is deleted. Needs setup_billing first. Returns the codes
    in each outcome."""
    seed = {test.code: test for test in LAB_SEED}
    outcome = {"deleted": [], "retired": [], "replaced": [], "added": [], "no_tariff": []}
    old_parameters = set()

    for activity in poh_lab_activities(facility):
        old_parameters.update(activity.observation_result_requirements)
        code = code_from_slug(facility, activity.slug)
        if code in seed:
            continue
        requests = ServiceRequest._base_manager  # noqa: SLF001
        if requests.filter(activity_definition=activity).exists():
            activity.status = "retired"
            activity.updated_by = user
            activity.save(update_fields=["status", "updated_by", "modified_date"])
            outcome["retired"].append(code)
            continue
        specimens = SpecimenDefinition.objects.filter(id__in=activity.specimen_requirements)
        # QuerySet.delete() deletes the rows; Model.delete() only soft-deletes them.
        ActivityDefinition.objects.filter(id=activity.id).delete()
        unused(specimens, Specimen, "specimen_definition").delete()
        outcome["deleted"].append(code)

    for test in LAB_SEED:
        charge_item = seed_tariff_row(facility, test.code)
        if charge_item is None:
            outcome["no_tariff"].append(test.code)
            continue
        spec = seed_spec(facility, test, charge_item)
        activity = ActivityDefinition.objects.filter(
            slug=facility_slug(facility, lab_slug(test.code))
        ).first()
        save_lab_test(facility, spec, user, activity=activity)
        outcome["replaced" if activity else "added"].append(test.code)

    still_reported = set()
    for parameters in poh_lab_activities(facility).values_list(
        "observation_result_requirements", flat=True
    ):
        still_reported.update(parameters)
    orphans = ObservationDefinition.objects.filter(
        facility=facility, id__in=old_parameters - still_reported
    )
    unused(orphans, Observation, "observation_definition").delete()
    return outcome
