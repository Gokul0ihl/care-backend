"""Outreach events (health camp, patient education session, CME, webinar) and
camp-mode capture. hmis_fe docs/outreach-camps.md describes the flow.

An event is created as a draft, sent for approval and approved (or rejected)
by a facility admin. Only an approved CAMP or PES event can capture people.
Changing an approved event makes a new version, which goes back to draft and
needs approving again; capture pauses until it is."""

import re
import secrets
from datetime import date, time
from decimal import Decimal
from typing import Any, Literal

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from pydantic import BaseModel, Field
from rest_framework.exceptions import PermissionDenied, ValidationError

from care.emr.models import ActivityDefinition
from care.emr.models.organization import FacilityOrganizationUser
from care.emr.models.patient import Patient
from care.facility.models import Facility
from care.security.authorization.base import AuthorizationHandler
from hmis.models import (
    CampParticipant,
    CampParticipantTest,
    CampSession,
    OutreachEvent,
    OutreachEventCostLine,
    OutreachEventDecision,
    OutreachEventStaff,
    OutreachEventTest,
    OutreachFormField,
    OutreachFormTemplate,
    PatientNumberSequence,
)
from hmis.services import unit_code

EVENT_TYPES = {
    "CAMP": {
        "label": "Health camp",
        "venue": True,
        "online": False,
        "tests": True,
        "coupon": "required",
        "capture": True,
        "cme": False,
    },
    "PES": {
        "label": "Patient education session",
        "venue": True,
        "online": False,
        "tests": True,
        "coupon": "optional",
        "capture": True,
        "cme": False,
    },
    "CME": {
        "label": "CME (for doctors)",
        "venue": True,
        "online": False,
        "tests": False,
        "coupon": None,
        "capture": False,
        "cme": True,
    },
    "WEB": {
        "label": "Webinar",
        "venue": False,
        "online": True,
        "tests": False,
        "coupon": "optional",
        "capture": False,
        "cme": False,
    },
}

DRAFT, PENDING, APPROVED, REJECTED, CLOSED, CANCELLED = (
    "draft",
    "pending_approval",
    "approved",
    "rejected",
    "closed",
    "cancelled",
)
EDITABLE = {DRAFT, REJECTED}

PLATFORMS = ["Zoom", "Microsoft Teams", "Google Meet", "YouTube Live", "Facebook Live"]
REPORT_DELIVERY = ["whatsapp", "hand_delivery", "both"]
ACCREDITATION_BODIES = [
    "Delhi Medical Council",
    "National Medical Commission",
    "Not accredited",
]
STAFF_ROLES = ["lead", "nurse", "attendant", "promoter"]
COST_CATEGORIES = ["equipment", "consumable", "promotional", "other"]
FIELD_TYPES = [
    "short_text",
    "paragraph",
    "number",
    "date",
    "single_choice",
    "dropdown",
    "checkboxes",
    "yes_no",
    "phone",
    "email",
    "section",
]
CHOICE_TYPES = {"single_choice", "dropdown", "checkboxes"}
GENDERS = ["male", "female", "other"]
BELONGS_TO = ["self", "spouse", "child", "parent", "relative"]
ID_TYPES = ["aadhaar", "pan", "voter_id", "driving_licence", "passport"]
SEND_VIA = ["whatsapp", "sms"]
MAX_FORM_FIELDS = 50
MIN_OPTIONS = 2
MAX_TEXT_LENGTH = 2000
MIN_DESCRIPTION = 20
MAX_TEMPLATE_NAME = 80
MIN_NAME = 2
MAX_AGE = 120
CONFIRM_AGE = 100

PIN_RE = re.compile(r"^[1-9]\d{5}$")
COUPON_RE = re.compile(r"^[A-Z0-9]{4,12}$")
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .]*$")
BP_RE = re.compile(r"^\d{2,3}/\d{2,3}$")
LINK_RE = re.compile(r"^https?://\S+\.\S+")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?\d{6,15}$")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class Errors:
    """Collects field errors and raises them together, as Care's error list
    ({errors: [{type, loc, msg}]}), so the form can show each by its field."""

    def __init__(self):
        self.items = []

    def add(self, loc, msg):
        loc = [loc] if isinstance(loc, str) else list(loc)
        self.items.append({"type": "validation_error", "loc": loc, "msg": msg})

    def check(self, ok, loc, msg):
        if not ok:
            self.add(loc, msg)

    def raise_if_any(self):
        if self.items:
            raise ValidationError({"errors": self.items})


# --- Permissions ---------------------------------------------------------


def has_permission(user, facility, permission):
    """The permission through any role the user holds in the facility (root
    organization or a department). Superusers have every permission."""
    return AuthorizationHandler().check_permission_in_facility_organization(
        [permission], user, facility=facility
    )


def require(user, facility, permission, message):
    if not has_permission(user, facility, permission):
        raise PermissionDenied(message)


def user_facilities(user):
    if user.is_superuser:
        return Facility.objects.filter(deleted=False)
    ids = FacilityOrganizationUser.objects.filter(user=user).values_list(
        "organization__facility_id", flat=True
    )
    return Facility.objects.filter(id__in=ids, deleted=False)


def capabilities(user):
    """What the user can do with outreach in each of their facilities, and
    whether camp mode is all they have (the Camp Staff role and nothing else)."""
    roles = set(
        FacilityOrganizationUser.objects.filter(user=user).values_list(
            "role__name", flat=True
        )
    )
    rows = []
    for facility in user_facilities(user).order_by("name"):
        rows.append(
            {
                "id": str(facility.external_id),
                "name": facility.name,
                "unit_code": facility.unit_code,
                "can_manage": has_permission(
                    user, facility, "can_manage_outreach_event"
                ),
                "can_approve": has_permission(
                    user, facility, "can_approve_outreach_event"
                ),
                "can_capture": has_permission(
                    user, facility, "can_capture_camp_participant"
                ),
            }
        )
    return {
        "camp_only": not user.is_superuser and roles == {"Camp Staff"},
        "facilities": rows,
    }


# --- Write shapes ----------------------------------------------------------


class TestIn(BaseModel):
    name: str = Field(max_length=120)
    lab_test_code: str | None = None
    location: Literal["camp", "hospital"] = "camp"
    fee_type: Literal["free", "paid"] = "free"
    rate: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    cost_to_poh: Decimal | None = Field(
        default=None, ge=0, max_digits=10, decimal_places=2
    )
    quantity_offered: int | None = Field(default=None, ge=0, le=1_000_000)


class StaffIn(BaseModel):
    role: Literal["lead", "nurse", "attendant", "promoter"]
    name: str = Field(max_length=80)
    department: str = Field(default="", max_length=80)


class CostIn(BaseModel):
    category: Literal["equipment", "consumable", "promotional", "other"]
    item_name: str = Field(max_length=120)
    unit: str = Field(default="", max_length=20)
    quantity: Decimal = Field(default=Decimal(0), ge=0, max_digits=10, decimal_places=2)
    unit_cost: Decimal = Field(
        default=Decimal(0), ge=0, max_digits=10, decimal_places=2
    )


class FormFieldIn(BaseModel):
    key: str = Field(max_length=40)
    section: str = Field(default="", max_length=80)
    label: str = Field(max_length=200)
    help_text: str = Field(default="", max_length=300)
    field_type: Literal[
        "short_text",
        "paragraph",
        "number",
        "date",
        "single_choice",
        "dropdown",
        "checkboxes",
        "yes_no",
        "phone",
        "email",
        "section",
    ]
    required: bool = False
    options: list[str] = Field(default_factory=list, max_length=50)
    validation: dict[str, Any] = Field(default_factory=dict)


class EventIn(BaseModel):
    facility: str
    event_type: Literal["CAMP", "PES", "CME", "WEB"]
    name: str = Field(default="", max_length=120)
    theme: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=4000)
    venue_name: str = Field(default="", max_length=120)
    address: str = Field(default="", max_length=255)
    pincode: str = Field(default="", max_length=6)
    platform: str = Field(default="", max_length=40)
    join_link: str = Field(default="", max_length=500)
    event_date: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    expected_attendees: int | None = Field(default=None, ge=0, le=1_000_000)
    has_coupon: bool = False
    coupon_code: str = Field(default="", max_length=12)
    coupon_valid_from: date | None = None
    coupon_valid_to: date | None = None
    discount_registration_pct: int | None = Field(default=None, ge=0, le=100)
    discount_consultation_pct: int | None = Field(default=None, ge=0, le=100)
    discount_tests_pct: int | None = Field(default=None, ge=0, le=100)
    report_delivery: str = ""
    permission_needed: bool = False
    permission_from: str = Field(default="", max_length=120)
    permission_cost: Decimal | None = Field(
        default=None, ge=0, max_digits=10, decimal_places=2
    )
    cme_audience: str = Field(default="", max_length=120)
    cme_accreditation_body: str = Field(default="", max_length=60)
    cme_credit_hours: Decimal | None = Field(
        default=None, ge=0, le=100, max_digits=4, decimal_places=1
    )
    tests: list[TestIn] = Field(default_factory=list, max_length=50)
    staff: list[StaffIn] = Field(default_factory=list, max_length=100)
    cost_lines: list[CostIn] = Field(default_factory=list, max_length=200)
    form_fields: list[FormFieldIn] = Field(
        default_factory=list, max_length=MAX_FORM_FIELDS
    )


class ParticipantIn(BaseModel):
    kind: Literal["new", "existing"] = "new"
    patient: str | None = None
    first_name: str = Field(default="", max_length=40)
    last_name: str = Field(default="", max_length=40)
    gender: str = ""
    date_of_birth: date | None = None
    confirm_age_over_100: bool = False
    mobile_isd: str = Field(default="+91", max_length=5)
    mobile: str = Field(default="", max_length=12)
    mobile_belongs_to: str = "self"
    pincode: str = Field(default="", max_length=6)
    area: str = Field(default="", max_length=80)
    id_type: str = ""
    id_number: str = Field(default="", max_length=30)
    bp: str = Field(default="", max_length=7)
    rbs_mg_dl: int | None = Field(default=None, ge=10, le=1000)
    weight_kg: Decimal | None = Field(
        default=None, gt=0, le=400, max_digits=5, decimal_places=1
    )
    finding: str = Field(default="", max_length=1000)
    refer_to_department: str = Field(default="", max_length=80)
    consent_screening: bool = False
    consent_dpdp_notice: bool = False
    send_via: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list, max_length=50)
    extra_answers: dict[str, Any] = Field(default_factory=dict)


# --- Event validation --------------------------------------------------------


def lab_test_for(facility, code):
    """The facility's lab catalogue entry for a code (Settings → Lab tests)."""
    from hmis.lab_catalogue import facility_slug, lab_slug

    return ActivityDefinition.objects.filter(
        facility=facility, slug=facility_slug(facility, lab_slug(code))
    ).first()


def check_form_fields(fields, errors):
    keys = set()
    for i, field in enumerate(fields):
        loc = ["form_fields", i]
        errors.check(
            KEY_RE.match(field.key),
            [*loc, "key"],
            "Use lower-case letters, digits and _",
        )
        errors.check(
            field.key not in keys, [*loc, "key"], "Each question needs its own key"
        )
        keys.add(field.key)
        errors.check(field.label.strip(), [*loc, "label"], "Give the question a label")
        if field.field_type in CHOICE_TYPES:
            options = [o.strip() for o in field.options]
            errors.check(
                len(options) >= MIN_OPTIONS and all(options),
                [*loc, "options"],
                "Give at least two options, none blank",
            )
            errors.check(
                len(set(options)) == len(options),
                [*loc, "options"],
                "Options must be different",
            )
        validation = field.validation
        if field.field_type == "number":
            low, high = validation.get("min"), validation.get("max")
            for bound, name in ((low, "min"), (high, "max")):
                errors.check(
                    bound is None or isinstance(bound, int | float),
                    [*loc, "validation", name],
                    "Must be a number",
                )
            if isinstance(low, int | float) and isinstance(high, int | float):
                errors.check(
                    low <= high,
                    [*loc, "validation", "max"],
                    "Max must not be below min",
                )
        if field.field_type in {"short_text", "paragraph"}:
            limit = validation.get("max_length")
            errors.check(
                limit is None
                or (isinstance(limit, int) and 1 <= limit <= MAX_TEXT_LENGTH),
                [*loc, "validation", "max_length"],
                "Between 1 and 2000",
            )


def check_event(spec, facility, errors, event=None, submitting=False):  # noqa: PLR0912, PLR0915
    """Every rule for saving. Only the name, date and form questions are
    checked for a draft; submitting checks the rest."""
    kind = EVENT_TYPES[spec.event_type]
    errors.check(spec.name.strip(), "name", "Give the event a name")
    errors.check(spec.event_date, "event_date", "Choose the event date")
    check_form_fields(spec.form_fields, errors)
    if spec.form_fields and not kind["capture"]:
        errors.add(
            "form_fields", "Only health camps and education sessions capture people"
        )
    if spec.has_coupon:
        clash = OutreachEvent.objects.filter(
            has_coupon=True, coupon_code=spec.coupon_code.strip().upper(), deleted=False
        ).exclude(status=CANCELLED)
        if event is not None:
            clash = clash.exclude(pk=event.pk)
        errors.check(
            not clash.exists(),
            "coupon_code",
            "Another event already uses this coupon code",
        )
    if not submitting:
        return

    errors.check(
        len(spec.description.strip()) >= MIN_DESCRIPTION,
        "description",
        "Describe the purpose in at least a sentence (20 characters)",
    )
    if kind["venue"]:
        errors.check(spec.venue_name.strip(), "venue_name", "Enter the location")
        errors.check(spec.address.strip(), "address", "Enter the address")
        errors.check(
            PIN_RE.match(spec.pincode),
            "pincode",
            "PIN code: 6 digits, not starting with 0",
        )
    if kind["online"]:
        errors.check(spec.platform in PLATFORMS, "platform", "Choose the platform")
        errors.check(
            LINK_RE.match(spec.join_link),
            "join_link",
            "Enter the joining link (https://…)",
        )
    errors.check(
        spec.start_time and spec.end_time and spec.end_time > spec.start_time,
        "end_time",
        "End time must be after the start time",
    )
    errors.check(
        spec.expected_attendees and spec.expected_attendees > 0,
        "expected_attendees",
        "Enter how many are expected",
    )
    if kind["cme"]:
        errors.check(spec.cme_audience.strip(), "cme_audience", "Enter the audience")
        errors.check(
            spec.cme_accreditation_body in ACCREDITATION_BODIES,
            "cme_accreditation_body",
            "Choose the accreditation body",
        )
        errors.check(
            spec.cme_credit_hours and spec.cme_credit_hours > 0,
            "cme_credit_hours",
            "Enter the credit hours",
        )
    if kind["coupon"] == "required":
        errors.check(spec.has_coupon, "has_coupon", "A health camp needs a coupon")
    if spec.has_coupon:
        errors.check(kind["coupon"], "has_coupon", "This event type has no coupon")
        errors.check(
            COUPON_RE.match(spec.coupon_code.strip().upper()),
            "coupon_code",
            "4 to 12 letters or digits",
        )
        errors.check(
            spec.coupon_valid_from, "coupon_valid_from", "Choose when the coupon starts"
        )
        errors.check(
            spec.coupon_valid_to, "coupon_valid_to", "Choose when the coupon ends"
        )
        if spec.coupon_valid_from and spec.coupon_valid_to:
            errors.check(
                spec.coupon_valid_to >= spec.coupon_valid_from,
                "coupon_valid_to",
                "Must be on or after the start",
            )
        if spec.coupon_valid_from and spec.event_date:
            errors.check(
                spec.coupon_valid_from >= spec.event_date,
                "coupon_valid_from",
                "Can't start before the event date",
            )
        for name in (
            "discount_registration_pct",
            "discount_consultation_pct",
            "discount_tests_pct",
        ):
            errors.check(
                getattr(spec, name) is not None, name, "Enter a discount (0 to 100)"
            )

    named = [t for t in spec.tests if t.name.strip()]
    if kind["tests"]:
        if spec.event_type == "CAMP":
            errors.check(named, "tests", "Add at least one test")
        for i, test in enumerate(spec.tests):
            loc = ["tests", i]
            errors.check(test.name.strip(), [*loc, "name"], "Name the test")
            if test.lab_test_code:
                errors.check(
                    lab_test_for(facility, test.lab_test_code),
                    [*loc, "lab_test_code"],
                    "Not in this facility's lab catalogue",
                )
            if test.fee_type == "paid":
                errors.check(
                    test.rate and test.rate > 0, [*loc, "rate"], "Enter the rate"
                )
            else:
                errors.check(
                    test.cost_to_poh is not None,
                    [*loc, "cost_to_poh"],
                    "Enter POH's cost per test (0 if covered by consumables)",
                )
            if test.location == "camp":
                errors.check(
                    test.quantity_offered and test.quantity_offered > 0,
                    [*loc, "quantity_offered"],
                    "Enter how many will be offered",
                )
            if test.quantity_offered and spec.expected_attendees:
                errors.check(
                    test.quantity_offered <= spec.expected_attendees,
                    [*loc, "quantity_offered"],
                    "More than the expected attendees",
                )
        if any(t.lab_test_code for t in named):
            errors.check(
                spec.report_delivery in REPORT_DELIVERY,
                "report_delivery",
                "Choose how reports are delivered",
            )
    else:
        errors.check(not named, "tests", "This event type has no tests")

    errors.check(
        any(s.role == "lead" and s.name.strip() for s in spec.staff),
        "staff",
        "Add at least one doctor or speaker",
    )
    for i, member in enumerate(spec.staff):
        errors.check(member.name.strip(), ["staff", i, "name"], "Enter the name")
    for i, line in enumerate(spec.cost_lines):
        errors.check(
            line.item_name.strip(), ["cost_lines", i, "item_name"], "Name the item"
        )
    if spec.permission_needed:
        errors.check(
            spec.permission_from.strip(), "permission_from", "Who gives the permission"
        )
        errors.check(
            spec.permission_cost is not None,
            "permission_cost",
            "Enter the cost (0 if free)",
        )


# --- Event save, numbers and read ------------------------------------------------


def next_sequence(series, facility, period):
    with transaction.atomic():
        PatientNumberSequence.objects.get_or_create(
            series=series, facility=facility, period=period
        )
        sequence = PatientNumberSequence.objects.select_for_update().get(
            series=series, facility=facility, period=period
        )
        sequence.last_value += 1
        sequence.save(update_fields=["last_value"])
    return sequence.last_value


def yymm(day):
    return day.strftime("%y%m")


def allocate_event_number(event):
    """{CAMP|PES|CME|WEB}-<unit>-<YYMM>-<NN>, counted per type, unit and month."""
    code = unit_code(event.facility)
    period = yymm(event.event_date)
    value = next_sequence(event.event_type, event.facility, period)
    return f"{event.event_type}-{code}-{period}-{value:02d}"


def allocate_participant_number(event):
    """CMP-<unit>-<YYMM>-<NNNN>, counted per unit and month of the event."""
    code = unit_code(event.facility)
    period = yymm(event.event_date)
    value = next_sequence("CMP", event.facility, period)
    return f"CMP-{code}-{period}-{value:04d}"


SCALAR_FIELDS = [
    "event_type",
    "name",
    "theme",
    "description",
    "venue_name",
    "address",
    "pincode",
    "platform",
    "join_link",
    "event_date",
    "start_time",
    "end_time",
    "expected_attendees",
    "has_coupon",
    "coupon_code",
    "coupon_valid_from",
    "coupon_valid_to",
    "discount_registration_pct",
    "discount_consultation_pct",
    "discount_tests_pct",
    "report_delivery",
    "permission_needed",
    "permission_from",
    "permission_cost",
    "cme_audience",
    "cme_accreditation_body",
    "cme_credit_hours",
]


def write_event(spec, facility, user, event=None):
    """Creates or replaces the event with its tests, staff, costs and form."""
    kind = EVENT_TYPES[spec.event_type]
    values = {name: getattr(spec, name) for name in SCALAR_FIELDS}
    values["name"] = spec.name.strip()
    values["coupon_code"] = spec.coupon_code.strip().upper() if spec.has_coupon else ""
    if not kind["venue"]:
        values.update(venue_name="", address="", pincode="")
    if not kind["online"]:
        values.update(platform="", join_link="")
    if not kind["cme"]:
        values.update(cme_audience="", cme_accreditation_body="", cme_credit_hours=None)
    if not spec.has_coupon:
        values.update(
            coupon_valid_from=None,
            coupon_valid_to=None,
            discount_registration_pct=None,
            discount_consultation_pct=None,
            discount_tests_pct=None,
        )
    with transaction.atomic():
        if event is None:
            event = OutreachEvent(facility=facility, created_by=user)
        elif event.status == REJECTED:
            event.status = DRAFT
        for name, value in values.items():
            setattr(event, name, value)
        event.updated_by = user
        try:
            event.save()
        except IntegrityError as error:
            raise ValidationError(
                {
                    "errors": [
                        {
                            "type": "validation_error",
                            "loc": ["coupon_code"],
                            "msg": "Another event already uses this coupon code",
                        }
                    ]
                }
            ) from error
        event.tests.all().delete()
        event.staff.all().delete()
        event.cost_lines.all().delete()
        event.form_fields.all().delete()
        if kind["tests"]:
            OutreachEventTest.objects.bulk_create(
                OutreachEventTest(
                    event=event,
                    name=t.name.strip(),
                    lab_test=lab_test_for(facility, t.lab_test_code)
                    if t.lab_test_code
                    else None,
                    location=t.location,
                    fee_type=t.fee_type,
                    rate=t.rate if t.fee_type == "paid" else None,
                    cost_to_poh=t.cost_to_poh if t.fee_type == "free" else None,
                    quantity_offered=t.quantity_offered,
                    order=i,
                )
                for i, t in enumerate(spec.tests)
                if t.name.strip()
            )
        OutreachEventStaff.objects.bulk_create(
            OutreachEventStaff(
                event=event,
                role=s.role,
                name=s.name.strip(),
                department=s.department.strip(),
                order=i,
            )
            for i, s in enumerate(spec.staff)
            if s.name.strip()
        )
        OutreachEventCostLine.objects.bulk_create(
            OutreachEventCostLine(
                event=event,
                category=c.category,
                item_name=c.item_name.strip(),
                unit=c.unit.strip(),
                quantity=c.quantity,
                unit_cost=c.unit_cost,
                order=i,
            )
            for i, c in enumerate(spec.cost_lines)
            if c.item_name.strip()
        )
        if kind["capture"]:
            OutreachFormField.objects.bulk_create(
                OutreachFormField(
                    event=event,
                    key=f.key,
                    section=f.section.strip(),
                    label=f.label.strip(),
                    help_text=f.help_text.strip(),
                    field_type=f.field_type,
                    required=f.required and f.field_type != "section",
                    options=[o.strip() for o in f.options]
                    if f.field_type in CHOICE_TYPES
                    else [],
                    validation=f.validation,
                    order=i,
                )
                for i, f in enumerate(spec.form_fields)
            )
    return event


def event_spec(event):
    """The saved event in the write shape, to check it again on submit."""
    data = {name: getattr(event, name) for name in SCALAR_FIELDS}
    data["facility"] = str(event.facility.external_id)
    data["tests"] = [
        {
            "name": t.name,
            "lab_test_code": lab_code(t.lab_test),
            "location": t.location,
            "fee_type": t.fee_type,
            "rate": t.rate,
            "cost_to_poh": t.cost_to_poh,
            "quantity_offered": t.quantity_offered,
        }
        for t in event.tests.select_related("lab_test")
    ]
    data["staff"] = [
        {"role": s.role, "name": s.name, "department": s.department}
        for s in event.staff.all()
    ]
    data["cost_lines"] = [
        {
            "category": c.category,
            "item_name": c.item_name,
            "unit": c.unit,
            "quantity": c.quantity,
            "unit_cost": c.unit_cost,
        }
        for c in event.cost_lines.all()
    ]
    data["form_fields"] = [form_field_row(f) for f in event.form_fields.all()]
    return EventIn.model_validate(data)


def lab_code(activity):
    if activity is None:
        return None
    from hmis.lab_catalogue import code_from_slug

    return code_from_slug(activity.facility, activity.slug)


def form_field_row(field):
    return {
        "key": field.key,
        "section": field.section,
        "label": field.label,
        "help_text": field.help_text,
        "field_type": field.field_type,
        "required": field.required,
        "options": field.options,
        "validation": field.validation,
    }


def money(value):
    return None if value is None else str(value)


def costs(event):
    """POH's cost and the expected collection from paid tests at the camp."""
    by_category = {name: Decimal(0) for name in COST_CATEGORIES}
    for line in event.cost_lines.all():
        by_category[line.category] += line.quantity * line.unit_cost
    tests = list(event.tests.all())
    free_tests = sum(
        (
            (t.quantity_offered or 0) * (t.cost_to_poh or 0)
            for t in tests
            if t.fee_type == "free"
        ),
        Decimal(0),
    )
    collection = sum(
        (
            (t.quantity_offered or 0) * (t.rate or 0)
            for t in tests
            if t.fee_type == "paid" and t.location == "camp"
        ),
        Decimal(0),
    )
    permission = (
        (event.permission_cost or Decimal(0)) if event.permission_needed else Decimal(0)
    )
    total = sum(by_category.values(), Decimal(0)) + free_tests + permission
    return {
        **{name: str(value) for name, value in by_category.items()},
        "free_tests": str(free_tests),
        "permission": str(permission),
        "total": str(total),
        "expected_collection": str(collection),
    }


def person(user):
    if user is None:
        return None
    return {"id": str(user.external_id), "name": user.get_full_name() or user.username}


def event_summary(event, participants=None):
    return {
        "id": str(event.external_id),
        "number": event.number,
        "event_type": event.event_type,
        "type_label": EVENT_TYPES[event.event_type]["label"],
        "facility": {
            "id": str(event.facility.external_id),
            "name": event.facility.name,
        },
        "name": event.name,
        "venue_name": event.venue_name,
        "platform": event.platform,
        "event_date": event.event_date,
        "start_time": event.start_time,
        "end_time": event.end_time,
        "expected_attendees": event.expected_attendees,
        "status": event.status,
        "version": event.version,
        "participants": participants,
    }


def participant_counts(events):
    from django.db.models import Count

    rows = (
        CampParticipant.objects.filter(event__in=list(events), deleted=False)
        .values("event_id")
        .annotate(n=Count("id"))
    )
    return {row["event_id"]: row["n"] for row in rows}


def event_detail(event, user):
    facility = event.facility
    tests = list(event.tests.select_related("lab_test"))
    decisions = event.decisions.select_related("decided_by").order_by("-decided_at")
    return {
        **event_summary(event, event.participants.filter(deleted=False).count()),
        **{name: getattr(event, name) for name in SCALAR_FIELDS},
        "permission_cost": money(event.permission_cost),
        "cme_credit_hours": money(event.cme_credit_hours),
        "tests": [
            {
                "id": str(t.external_id),
                "name": t.name,
                "lab_test_code": lab_code(t.lab_test),
                "location": t.location,
                "fee_type": t.fee_type,
                "rate": money(t.rate),
                "cost_to_poh": money(t.cost_to_poh),
                "quantity_offered": t.quantity_offered,
            }
            for t in tests
        ],
        "staff": [
            {"role": s.role, "name": s.name, "department": s.department}
            for s in event.staff.all()
        ],
        "cost_lines": [
            {
                "category": c.category,
                "item_name": c.item_name,
                "unit": c.unit,
                "quantity": money(c.quantity),
                "unit_cost": money(c.unit_cost),
            }
            for c in event.cost_lines.all()
        ],
        "form_fields": [form_field_row(f) for f in event.form_fields.all()],
        "costs": costs(event),
        "created_by": person(event.created_by),
        "created_date": event.created_date,
        "submitted_at": event.submitted_at,
        "submitted_by": person(event.submitted_by),
        "decided_at": event.decided_at,
        "decided_by": person(event.decided_by),
        "decision_remarks": event.decision_remarks,
        "decisions": [
            {
                "version": d.version,
                "decision": d.decision,
                "remarks": d.remarks,
                "decided_by": person(d.decided_by),
                "decided_at": d.decided_at,
            }
            for d in decisions
        ],
        "can_edit": event.status in EDITABLE
        and has_permission(user, facility, "can_manage_outreach_event"),
        "can_approve": event.status == PENDING
        and has_permission(user, facility, "can_approve_outreach_event"),
        "can_manage": has_permission(user, facility, "can_manage_outreach_event"),
    }


# --- Status changes ----------------------------------------------------------------


def lock_event(event):
    return (
        OutreachEvent.objects.select_for_update()
        .select_related("facility")
        .get(pk=event.pk)
    )


def submit_event(event, user):
    errors = Errors()
    with transaction.atomic():
        event = lock_event(event)
        if event.status not in EDITABLE:
            raise ValidationError(
                "Only a draft or rejected event can be sent for approval"
            )
        check_event(
            event_spec(event), event.facility, errors, event=event, submitting=True
        )
        errors.raise_if_any()
        if not event.number:
            event.number = allocate_event_number(event)
        event.status = PENDING
        event.submitted_at = timezone.now()
        event.submitted_by = user
        event.decided_at = None
        event.decided_by = None
        event.decision_remarks = ""
        event.updated_by = user
        event.save()
    return event


def decide_event(event, user, decision, remarks):
    remarks = (remarks or "").strip()
    if decision == REJECTED and not remarks:
        raise ValidationError(
            {
                "errors": [
                    {
                        "type": "validation_error",
                        "loc": ["remarks"],
                        "msg": "Give the reason for rejecting",
                    }
                ]
            }
        )
    with transaction.atomic():
        event = lock_event(event)
        if event.status != PENDING:
            raise ValidationError(
                "Only an event waiting for approval can be approved or rejected"
            )
        event.status = decision
        event.decided_at = timezone.now()
        event.decided_by = user
        event.decision_remarks = remarks
        event.updated_by = user
        event.save()
        OutreachEventDecision.objects.create(
            event=event,
            version=event.version,
            decision=decision,
            remarks=remarks,
            decided_by=user,
        )
    return event


def new_version(event, user):
    """An approved event goes back to draft for changes. Capture pauses until
    the new version is approved; people already captured stay on the event."""
    with transaction.atomic():
        event = lock_event(event)
        if event.status not in {APPROVED, PENDING}:
            raise ValidationError(
                "Only an approved or pending event gets a new version"
            )
        event.status = DRAFT
        event.version += 1
        event.decided_at = None
        event.decided_by = None
        event.decision_remarks = ""
        event.updated_by = user
        event.save()
    return event


def close_event(event, user):
    with transaction.atomic():
        event = lock_event(event)
        if event.status != APPROVED:
            raise ValidationError("Only an approved event can be closed")
        event.status = CLOSED
        event.updated_by = user
        event.save()
        CampSession.objects.filter(event=event, ended_at__isnull=True).update(
            ended_at=timezone.now()
        )
    return event


def cancel_event(event, user):
    with transaction.atomic():
        event = lock_event(event)
        if event.status in {CLOSED, CANCELLED}:
            raise ValidationError("This event is already closed")
        if event.participants.filter(deleted=False).exists():
            raise ValidationError(
                "People were captured at this event; close it instead"
            )
        event.status = CANCELLED
        event.updated_by = user
        event.save()
    return event


# --- Form templates ---------------------------------------------------------------


def save_template(facility, user, name, fields):
    errors = Errors()
    name = (name or "").strip()
    errors.check(name, "name", "Name the template")
    errors.check(len(name) <= MAX_TEMPLATE_NAME, "name", "Up to 80 characters")
    specs = [FormFieldIn.model_validate(f) for f in fields]
    errors.check(specs, "fields", "Add at least one question")
    check_form_fields(specs, errors)
    errors.raise_if_any()
    return OutreachFormTemplate.objects.create(
        facility=facility,
        name=name,
        fields=[f.model_dump() for f in specs],
        created_by=user,
        updated_by=user,
    )


def template_row(template):
    return {
        "id": str(template.external_id),
        "name": template.name,
        "fields": template.fields,
        "created_by": person(template.created_by),
        "created_date": template.created_date,
    }


# --- Camp mode --------------------------------------------------------------------


def camp_events(user):
    """Approved CAMP and PES events the user can capture at: today's first,
    then upcoming, then past."""
    facilities = [
        f
        for f in user_facilities(user)
        if has_permission(user, f, "can_capture_camp_participant")
    ]
    events = OutreachEvent.objects.filter(
        facility__in=facilities,
        status=APPROVED,
        event_type__in=["CAMP", "PES"],
        deleted=False,
    ).select_related("facility")
    today = timezone.localdate()

    def order(event):
        if event.event_date == today:
            return (0, 0)
        if event.event_date > today:
            return (1, (event.event_date - today).days)
        return (2, (today - event.event_date).days)

    return sorted(events, key=order)


def start_session(event, user, device_label):
    require(
        user,
        event.facility,
        "can_capture_camp_participant",
        "You can't capture at this facility",
    )
    if event.status != APPROVED or not EVENT_TYPES[event.event_type]["capture"]:
        raise ValidationError(
            "People can only be captured at an approved camp or education session"
        )
    return CampSession.objects.create(
        event=event, user=user, device_label=(device_label or "")[:80]
    )


def open_session(session, user):
    """The session for capture: the user's own, still open, and its event
    still approved."""
    if session.user_id != user.id:
        raise PermissionDenied("This camp session belongs to someone else")
    if session.ended_at:
        raise ValidationError("This camp session has ended; choose the camp again")
    if session.event.status != APPROVED:
        raise ValidationError("This event is no longer approved, so capture is paused")
    require(
        user,
        session.event.facility,
        "can_capture_camp_participant",
        "You can't capture at this facility",
    )
    return session


def mask_aadhaar(number):
    digits = re.sub(r"\D", "", number)
    return f"XXXX XXXX {digits[-4:]}"


def age_on(dob, day):
    return day.year - dob.year - ((day.month, day.day) < (dob.month, dob.day))


def check_answers(fields, answers, errors):  # noqa: PLR0912
    """Each extra answer against its question; returns what is stored."""
    stored = {}
    known = {f.key for f in fields}
    for key in answers:
        errors.check(
            key in known, ["extra_answers", key], "Not a question on this form"
        )
    for field in fields:
        if field.field_type == "section":
            continue
        loc = ["extra_answers", field.key]
        value = answers.get(field.key)
        blank = value is None or value in ("", [])
        if blank:
            errors.check(not field.required, loc, "This question is required")
            continue
        validation = field.validation or {}
        kind = field.field_type
        ok, msg = True, ""
        if kind in {"short_text", "paragraph"}:
            limit = validation.get("max_length") or (
                200 if kind == "short_text" else 2000
            )
            ok = isinstance(value, str) and len(value.strip()) <= limit
            msg = f"Up to {limit} characters"
            value = value.strip() if ok else value
        elif kind == "number":
            ok = isinstance(value, int | float) and not isinstance(value, bool)
            msg = "Enter a number"
            if ok and validation.get("min") is not None and value < validation["min"]:
                ok, msg = False, f"At least {validation['min']}"
            if ok and validation.get("max") is not None and value > validation["max"]:
                ok, msg = False, f"At most {validation['max']}"
        elif kind == "date":
            try:
                date.fromisoformat(value)
            except (TypeError, ValueError):
                ok = False
            msg = "Enter a date"
        elif kind in {"single_choice", "dropdown"}:
            ok, msg = value in field.options, "Choose one of the options"
        elif kind == "checkboxes":
            ok = (
                isinstance(value, list)
                and all(v in field.options for v in value)
                and len(set(value)) == len(value)
            )
            msg = "Choose from the options"
        elif kind == "yes_no":
            ok, msg = isinstance(value, bool), "Answer yes or no"
        elif kind == "phone":
            ok, msg = (
                isinstance(value, str) and bool(PHONE_RE.match(value)),
                "Enter a phone number (6 to 15 digits)",
            )
        elif kind == "email":
            ok, msg = (
                isinstance(value, str) and bool(EMAIL_RE.match(value)),
                "Enter an email address",
            )
        errors.check(ok, loc, msg)
        if ok:
            stored[field.key] = {"label": field.label, "type": kind, "value": value}
    return stored


def check_participant(spec, event, errors):
    today = timezone.localdate()
    patient = None
    if spec.kind == "existing":
        patient = (
            Patient.objects.filter(external_id=spec.patient, deleted=False).first()
            if spec.patient
            else None
        )
        errors.check(patient, "patient", "Find the patient by UHID or mobile")
    first = spec.first_name.strip()
    errors.check(
        len(first) >= MIN_NAME and NAME_RE.match(first),
        "first_name",
        "At least 2 letters; letters, spaces and dots only",
    )
    last = spec.last_name.strip()
    errors.check(
        not last or NAME_RE.match(last), "last_name", "Letters, spaces and dots only"
    )
    errors.check(spec.gender in GENDERS, "gender", "Choose the gender")
    if spec.date_of_birth is None:
        errors.add("date_of_birth", "Enter the date of birth")
    elif spec.date_of_birth > today:
        errors.add("date_of_birth", "Can't be in the future")
    else:
        age = age_on(spec.date_of_birth, today)
        if age > MAX_AGE:
            errors.add("date_of_birth", "Age can't be above 120")
        elif age > CONFIRM_AGE and not spec.confirm_age_over_100:
            errors.add(
                "date_of_birth", "Over 100 — confirm the date of birth with the person"
            )
    errors.check(
        re.match(r"^\+\d{1,4}$", spec.mobile_isd), "mobile_isd", "Country code like +91"
    )
    if spec.mobile_isd == "+91":
        errors.check(
            re.match(r"^[6-9]\d{9}$", spec.mobile),
            "mobile",
            "10 digits, starting 6 to 9",
        )
    else:
        errors.check(
            re.match(r"^\d{6,12}$", spec.mobile),
            "mobile",
            f"6 to 12 digits for {spec.mobile_isd}",
        )
    errors.check(
        spec.mobile_belongs_to in BELONGS_TO,
        "mobile_belongs_to",
        "Choose whose mobile it is",
    )
    errors.check(
        PIN_RE.match(spec.pincode), "pincode", "PIN code: 6 digits, not starting with 0"
    )
    if spec.id_type or spec.id_number:
        errors.check(spec.id_type in ID_TYPES, "id_type", "Choose the ID type")
        errors.check(spec.id_number.strip(), "id_number", "Enter the ID number")
        if spec.id_type == "aadhaar":
            errors.check(
                re.match(r"^\d{12}$", re.sub(r"\s", "", spec.id_number)),
                "id_number",
                "Aadhaar: 12 digits",
            )
    errors.check(not spec.bp or BP_RE.match(spec.bp), "bp", "BP as 150/94")
    errors.check(spec.finding.strip(), "finding", "Enter the finding or concern")
    errors.check(
        spec.consent_screening, "consent_screening", "Consent is needed to capture"
    )
    errors.check(
        spec.consent_dpdp_notice,
        "consent_dpdp_notice",
        "Read out the privacy notice and tick",
    )
    errors.check(
        all(v in SEND_VIA for v in spec.send_via), "send_via", "WhatsApp or SMS"
    )
    offered = {str(t.external_id): t for t in event.tests.all()}
    for i, test_id in enumerate(spec.tests):
        errors.check(
            test_id in offered, ["tests", i], "Not a test offered at this event"
        )
    return patient, offered


def free_used(event, test):
    return CampParticipantTest.objects.filter(
        participant__event=event,
        participant__deleted=False,
        event_test=test,
        fee_type="free",
    ).count()


def capture_participant(session, user, spec):
    event = session.event
    errors = Errors()
    patient, offered = check_participant(spec, event, errors)
    fields = list(event.form_fields.all())
    answers = check_answers(fields, spec.extra_answers, errors)
    errors.raise_if_any()

    first = spec.first_name.strip()
    with transaction.atomic():
        # One capture per person per event, checked under the event's lock.
        OutreachEvent.objects.select_for_update().get(pk=event.pk)
        same = CampParticipant.objects.filter(event=event, deleted=False)
        dup = (
            same.filter(patient=patient).first()
            if patient
            else same.filter(mobile=spec.mobile, first_name__iexact=first).first()
        )
        if dup:
            message = f"Already captured at this event as {dup.number}"
            raise ValidationError(message)
        id_number = spec.id_number.strip()
        if spec.id_type == "aadhaar" and id_number:
            id_number = mask_aadhaar(id_number)
        participant = CampParticipant.objects.create(
            number=allocate_participant_number(event),
            event=event,
            session=session,
            patient=patient,
            kind=spec.kind,
            first_name=first,
            last_name=spec.last_name.strip(),
            gender=spec.gender,
            date_of_birth=spec.date_of_birth,
            mobile_isd=spec.mobile_isd,
            mobile=spec.mobile,
            mobile_belongs_to=spec.mobile_belongs_to,
            pincode=spec.pincode,
            area=spec.area.strip(),
            id_type=spec.id_type if id_number else "",
            id_number=id_number,
            bp=spec.bp,
            rbs_mg_dl=spec.rbs_mg_dl,
            weight_kg=spec.weight_kg,
            finding=spec.finding.strip(),
            refer_to_department=spec.refer_to_department.strip(),
            consent_screening=True,
            consent_dpdp_notice=True,
            consent_channel="tablet",
            send_via=sorted(set(spec.send_via)),
            extra_answers=answers,
            form_version=event.version,
            status="registered" if patient else "open",
            verification_code=secrets.token_hex(3).upper(),
            captured_by=user,
            created_by=user,
            updated_by=user,
        )
        rows = []
        for test_id in dict.fromkeys(spec.tests):
            test = offered[test_id]
            fee_type, rate, source = (
                test.fee_type,
                test.rate or Decimal(0),
                "camp_offer",
            )
            # Once the free quota is used up the test is still done, at the
            # lab catalogue rate (paid at the reception).
            if (
                fee_type == "free"
                and test.quantity_offered
                and free_used(event, test) >= test.quantity_offered
            ):
                fee_type, source = "paid", "beyond_free_quota"
                rate = catalogue_rate(test.lab_test) or Decimal(0)
            rows.append(
                CampParticipantTest.objects.create(
                    participant=participant,
                    event_test=test,
                    lab_test=test.lab_test,
                    name=test.name,
                    fee_type=fee_type,
                    rate=rate if fee_type == "paid" else Decimal(0),
                    source=source,
                    payment_status="due" if fee_type == "paid" else "free",
                    report_status="pending" if test.lab_test_id else "given_at_camp",
                )
            )
    return participant


def catalogue_rate(activity):
    if activity is None:
        return None
    from hmis.lab_catalogue import base_price

    price = base_price(activity)
    return None if price is None else Decimal(str(price))


def participant_row(participant):
    return {
        "id": str(participant.external_id),
        "number": participant.number,
        "kind": participant.kind,
        "patient": str(participant.patient.external_id)
        if participant.patient
        else None,
        "first_name": participant.first_name,
        "last_name": participant.last_name,
        "gender": participant.gender,
        "date_of_birth": participant.date_of_birth,
        "mobile_isd": participant.mobile_isd,
        "mobile": participant.mobile,
        "mobile_belongs_to": participant.mobile_belongs_to,
        "pincode": participant.pincode,
        "area": participant.area,
        "id_type": participant.id_type,
        "id_number": participant.id_number,
        "bp": participant.bp,
        "rbs_mg_dl": participant.rbs_mg_dl,
        "weight_kg": money(participant.weight_kg),
        "finding": participant.finding,
        "refer_to_department": participant.refer_to_department,
        "send_via": participant.send_via,
        "extra_answers": participant.extra_answers,
        "status": participant.status,
        "verification_code": participant.verification_code,
        "captured_by": person(participant.captured_by),
        "captured_at": participant.captured_at,
        "tests": [
            {
                "name": t.name,
                "fee_type": t.fee_type,
                "rate": money(t.rate),
                "source": t.source,
                "payment_status": t.payment_status,
                "report_status": t.report_status,
            }
            for t in participant.tests.all()
        ],
    }


def lookup_patients(query):
    """Existing patients for camp capture, by exact UHID or exact mobile.
    Returns only what the capture form needs."""
    from care.emr.models.patient import PatientIdentifier
    from hmis.services import display_numbers

    query = (query or "").strip()
    if not query:
        return []
    mobile = re.sub(r"[\s-]", "", query).removeprefix("+91")
    if re.match(r"^[6-9]\d{9}$", mobile):
        patients = Patient.objects.filter(
            Q(phone_number=f"+91{mobile}") | Q(phone_number=mobile), deleted=False
        )[:10]
    else:
        ids = PatientIdentifier.objects.filter(value__iexact=query).values_list(
            "patient_id", flat=True
        )
        patients = Patient.objects.filter(id__in=ids, deleted=False)[:10]
    patients = list(patients)
    numbers = display_numbers([p.id for p in patients])
    rows = []
    for p in patients:
        first, _, last = (p.name or "").partition(" ")
        rows.append(
            {
                "id": str(p.external_id),
                "number": numbers.get(p.id),
                "name": p.name,
                "first_name": first,
                "last_name": last,
                "gender": p.gender,
                "date_of_birth": p.date_of_birth,
                "mobile": (p.phone_number or "")[-10:],
                "pincode": str(p.pincode or ""),
            }
        )
    return rows
