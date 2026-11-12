"""Outreach events (health camp, patient education session, CME, webinar) and
camp-mode capture. hmis_fe docs/outreach-camps.md describes the flow.

An event is created as a draft, sent for approval and approved (or rejected)
by a facility admin. Only an approved CAMP or PES event can capture people.
Changing an approved event makes a new version, which goes back to draft and
needs approving again; capture pauses until it is."""

import re
import secrets
import uuid
from datetime import date, time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Literal

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from pydantic import BaseModel, Field
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework_simplejwt.tokens import RefreshToken

from care.emr.models import ActivityDefinition, FileUpload, Questionnaire
from care.emr.models.organization import FacilityOrganizationUser
from care.emr.models.patient import Patient
from care.emr.resources.patient.spec import approximate_date_of_birth
from care.emr.resources.patient_identifier.default_expression_evaluator import (
    evaluate_patient_instance_default_values,
)
from care.facility.models import Facility
from care.security.authorization.base import AuthorizationHandler
from care.users.models import User
from hmis.constants import PatientNumberSeries
from hmis.models import (
    CampParticipant,
    CampParticipantTest,
    CampSession,
    OutreachEvent,
    OutreachEventCostLine,
    OutreachEventDecision,
    OutreachEventLogin,
    OutreachEventStaff,
    OutreachEventTest,
    OutreachFormField,
    OutreachFormTemplate,
    PatientNumberSequence,
    RegistrationAllergy,
)
from hmis.services import display_numbers, issue_patient_number, unit_code
from hmis.specs import LAST_NAME, NAME_PART

EVENT_TYPES = {
    "CAMP": {
        "label": "Health camp",
        "venue": True,
        "online": False,
        "tests": True,
        "coupon": "optional",
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
# The alternative mobile's owner, as the desk registration names them (ALT_MOBILE_OWNERS).
ALT_BELONGS_TO = ["Self", "Spouse", "Son/Daughter", "Parent", "Relative"]
# The camp spec's Government ID list (lib/outreach.ts ID_TYPES): Indian IDs, a
# child's birth certificate, and the IDs a foreign national shows.
ID_TYPES = [
    "aadhaar",
    "pan",
    "passport",
    "voter_id",
    "driving_licence",
    "nrega_job_card",
    "birth_certificate",
    "oci_card",
    "unhcr_card",
]
SEND_VIA = ["whatsapp", "sms", "phone", "do_not_contact"]
DO_NOT_CONTACT = "do_not_contact"
TITLES = ["Mr", "Ms", "Mrs", "Dr", "Master", "Mx"]
BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-", "Unknown"]
ALLERGY_STATUSES = ["nka", "has", "unknown"]
ALLERGY_SEVERITIES = ["mild", "moderate", "severe", "life_threatening"]
CONSENT_BY = ["The patient", "Spouse", "Parent", "Guardian", "Next of kin"]
# How consent was taken, as on the desk registration (lib/desk-registration.ts).
TABLET_SIGNATURE = "Signature"
THUMB_IMPRESSION = "Thumb impression"
CONSENT_METHODS = [TABLET_SIGNATURE, "Scanned form", THUMB_IMPRESSION]
TEST_LOCATIONS = ["camp", "hospital"]
IMAGE_MIME_TYPES = ["image/jpeg", "image/png", "image/webp"]
FILE_MIME_TYPES = [*IMAGE_MIME_TYPES, "application/pdf"]
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
# A name part as the desk's Domestic form takes it (hmis.specs): letters, an
# apostrophe inside a word (D'Souza), single spaces; 2-50 (last name 1-50).
NAME_PART_RE = re.compile(NAME_PART)
LAST_NAME_RE = re.compile(LAST_NAME)
NAME_MESSAGE = "Letters, an apostrophe inside a word and single spaces"
MAX_NAME = 50
MINOR_AGE = 18
# Full formats of the government IDs a camp takes (as the desk's checks), by
# camp ID type. Spaces and hyphens are ignored; Aadhaar also has a check digit.
ID_FORMATS = {
    "pan": (r"[A-Z]{5}\d{4}[A-Z]", "PAN like ABCDE1234F"),
    "passport": (r"[A-Z0-9]{6,15}", "Passport: 6 to 15 letters and digits"),
    "voter_id": (r"[A-Z]{3}\d{7}", "Voter ID like ABC1234567"),
    "driving_licence": (r"[A-Z]{2}\d{13}", "Driving licence like DL0420110012345"),
    "oci_card": (r"[A-Z0-9]{6,15}", "OCI card: 6 to 15 letters and digits"),
}
VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]  # fmt: skip
VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]  # fmt: skip


def valid_aadhaar(number):
    """12 digits, not starting 0 or 1, with a correct Verhoeff check digit."""
    if not re.fullmatch(r"[2-9]\d{11}", number):
        return False
    check = 0
    for i, digit in enumerate(reversed(number)):
        check = VERHOEFF_D[check][VERHOEFF_P[i % 8][int(digit)]]
    return check == 0


def id_number_problem(id_type, raw):
    """Why an ID number doesn't fit its type, or ""; as the desk checks it."""
    number = re.sub(r"[\s-]", "", raw).upper()
    if not number:
        return "Enter the ID number"
    if id_type == "aadhaar":
        return "" if valid_aadhaar(number) else "Aadhaar: 12 digits with a valid check digit"
    pattern, message = ID_FORMATS.get(id_type, (r"[A-Z0-9]{4,20}", "4 to 20 letters and digits"))
    return "" if re.fullmatch(pattern, number) else message
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
    login_user = OutreachEventLogin.objects.filter(user=user).exists()
    return {
        "camp_only": not user.is_superuser
        and (roles == {"Camp Staff"} or (login_user and not roles)),
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


MAX_LOGINS = 20


def clean_emails(emails):
    return [e.strip().lower() for e in emails if e.strip()]


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
    login_emails: list[str] = Field(default_factory=list, max_length=MAX_LOGINS)
    # Slug of the CARE questionnaire built for the event; blank for none.
    questionnaire: str = Field(default="", max_length=50)


class AddressIn(BaseModel):
    address: str = Field(default="", max_length=100)
    country: str = Field(default="", max_length=60)
    state: str = Field(default="", max_length=100)
    city: str = Field(default="", max_length=100)
    district: str = Field(default="", max_length=100)
    pincode: str = Field(default="", max_length=12)


class TestPickIn(BaseModel):
    id: str
    location: Literal["camp", "hospital"] | None = None
    result: str = Field(default="", max_length=20)


class ParticipantIn(BaseModel):
    kind: Literal["new", "existing"] = "new"
    patient: str | None = None
    title: str = Field(default="", max_length=8)
    first_name: str = Field(default="", max_length=50)
    middle_name: str = Field(default="", max_length=50)
    last_name: str = Field(default="", max_length=50)
    gender: str = ""
    date_of_birth: date | None = None
    # Typed when the date of birth isn't known (as on the desk's Domestic
    # form); saved as an approximate date of birth, 1 July of that year.
    age: int | None = Field(default=None, ge=0, le=MAX_AGE)
    confirm_age_over_100: bool = False
    preferred_language: str = Field(default="", max_length=40)
    nationality: str = ""
    blood_group: str = Field(default="", max_length=8)
    mobile_isd: str = Field(default="+91", max_length=5)
    mobile: str = Field(default="", max_length=12)
    mobile_belongs_to: str = "self"
    whatsapp: str = Field(default="", max_length=12)
    alt_mobile_isd: str = Field(default="+91", max_length=5)
    alt_mobile: str = Field(default="", max_length=12)
    alt_mobile_belongs_to: str = ""
    address: str = Field(default="", max_length=200)
    country: str = Field(default="", max_length=60)
    state: str = Field(default="", max_length=100)
    city: str = Field(default="", max_length=100)
    district: str = Field(default="", max_length=100)
    pincode: str = Field(default="", max_length=6)
    area: str = Field(default="", max_length=80)
    permanent_same_as_current: bool = False
    permanent_address: AddressIn | None = None
    id_type: str = ""
    id_number: str = Field(default="", max_length=30)
    allergy_status: str = ""
    allergen: str = Field(default="", max_length=60)
    allergy_reaction: str = Field(default="", max_length=60)
    allergy_severity: str = ""
    bp: str = Field(default="", max_length=7)
    rbs_mg_dl: int | None = Field(default=None, ge=10, le=1000)
    height_cm: Decimal | None = Field(
        default=None, gt=0, le=250, max_digits=4, decimal_places=1
    )
    weight_kg: Decimal | None = Field(
        default=None, gt=0, le=400, max_digits=5, decimal_places=1
    )
    finding: str = Field(default="", max_length=1000)
    refer_to_department: str = Field(default="", max_length=80)
    consent_screening: bool = False
    consent_dpdp_notice: bool = False
    consent_tariff: bool = False
    consent_by: str = ""
    consent_method: str = ""
    consent_witness: str = Field(default="", max_length=50)
    send_via: list[str] = Field(default_factory=list)
    # Test ids, or {id, location, result} for a result read at the camp.
    tests: list[str | TestPickIn] = Field(default_factory=list, max_length=50)
    extra_answers: dict[str, Any] = Field(default_factory=dict)

    def picks(self):
        """The tests as {id: TestPickIn}, in order, without repeats."""
        result = {}
        for item in self.tests:
            pick = TestPickIn(id=item) if isinstance(item, str) else item
            result.setdefault(pick.id, pick)
        return result


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
    if spec.questionnaire:
        if not kind["capture"]:
            errors.add(
                "questionnaire",
                "Only health camps and education sessions capture people",
            )
        else:
            form = Questionnaire.objects.filter(slug=spec.questionnaire).first()
            if not form:
                errors.add(
                    "questionnaire",
                    "The form wasn't found. Open the form builder and choose it again.",
                )
            elif form.status != "active":
                # CARE takes answers only to an active questionnaire.
                errors.add("questionnaire", "Publish the form before using it")
    seen = set()
    for i, raw in enumerate(spec.login_emails if kind["capture"] else []):
        email = raw.strip().lower()
        if not email:
            continue
        try:
            validate_email(email)
        except DjangoValidationError:
            errors.add(["login_emails", i], "Enter a valid email address")
        errors.check(email not in seen, ["login_emails", i], "Already added")
        seen.add(email)
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

    if kind["capture"]:
        # Every capturing event asks its own form template (built in the form builder).
        errors.check(
            spec.questionnaire,
            "questionnaire",
            "Choose or build a capture form for this event",
        )
        errors.check(
            clean_emails(spec.login_emails),
            "login_emails",
            "Add at least one email staff sign in to the camp with",
        )
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
        event.questionnaire = (
            Questionnaire.objects.filter(slug=spec.questionnaire).first()
            if spec.questionnaire and kind["capture"]
            else None
        )
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
        save_logins(event, clean_emails(spec.login_emails) if kind["capture"] else [])
    return event


def save_logins(event, emails):
    """The event's sign-in emails: kept ones keep their account, removed ones
    lose access, new ones get an account at their first sign-in."""
    emails = list(dict.fromkeys(emails))
    event.logins.exclude(email__in=emails).delete()
    have = set(event.logins.values_list("email", flat=True))
    OutreachEventLogin.objects.bulk_create(
        OutreachEventLogin(event=event, email=e) for e in emails if e not in have
    )


def login_emails(event):
    return list(event.logins.order_by("id").values_list("email", flat=True))


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
    data["login_emails"] = login_emails(event)
    data["questionnaire"] = questionnaire_slug(event) or ""
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
        "show_when": getattr(field, "show_when", None),
        "place": getattr(field, "place", ""),
        # A form builder reading's unit (mmHg, kg…); capture shows it as a vitals card.
        "unit": getattr(field, "unit", ""),
        # Merged into this patient record field (father_name…); "" for an extra answer.
        "patient_field": getattr(field, "patient_field", ""),
        "required_unless": getattr(field, "required_unless", ""),
    }


# Column names the camp capture form always asks itself (CampParticipant
# fields). A form built from the Standard template has these; capture asks
# only the form's other fields as extra questions, so nothing is asked twice.
CAPTURE_KEYS = {
    "title", "first_name", "middle_name", "last_name", "gender", "date_of_birth",
    "preferred_language", "nationality", "blood_group", "id_type", "id_number",
    "mobile", "mobile_belongs_to", "same_as_whatsapp", "whatsapp", "alt_mobile",
    "alt_mobile_belongs_to", "address", "area", "city", "district", "state",
    "country", "pincode", "allergy_status", "allergen", "allergy_reaction",
    "allergy_severity", "bp", "rbs_mg_dl", "height_cm", "weight_kg", "bmi",
    "finding", "refer_to_department", "consent_by", "consent_method",
    "consent_witness", "consent_screening", "consent_dpdp_notice",
    "consent_tariff", "send_via", "photo",
}  # fmt: skip

# The form builder's field types (styling_metadata.poh.type) as capture's
# question types. Consent is a one-option checkbox, so "required" means ticked.
BUILDER_TO_CAPTURE = {
    "short_text": "short_text", "govt_id": "short_text", "pincode": "short_text",
    "long_text": "paragraph", "email": "email", "phone": "phone",
    "integer": "number", "decimal": "number", "reading": "number",
    "dropdown": "dropdown", "single_choice": "single_choice",
    "multi_choice": "checkboxes", "yes_no": "yes_no", "consent": "checkboxes",
    "date": "date", "datetime": "date", "note": "note",
}  # fmt: skip
CARE_TO_CAPTURE = {
    "string": "short_text", "text": "paragraph", "integer": "number",
    "decimal": "number", "quantity": "number", "boolean": "yes_no",
    "date": "date", "dateTime": "date", "display": "note",
}  # fmt: skip
CONSENT_OPTION = "Yes"
# Formats the form builder checks, kept in capture (pattern, default message).
BUILDER_PATTERNS = {
    "phone": (r"^[6-9]\d{9}$", "10 digits, starting 6–9"),  # noqa: RUF001
    "pincode": (r"^[1-9]\d{5}$", "6 digits, not starting with 0"),
}
# The labels of the coded values capture stores, so a "show only when" rule on
# a fixed field can use the label the form builder showed (Known allergy, has).
FIXED_VALUE_LABELS = {
    "gender": {"female": "Female", "male": "Male", "other": "Other"},
    "allergy_status": {
        "nka": "No known allergy (NKA)",
        "has": "Known allergy",
        "unknown": "Unable to ascertain now",
    },
    "allergy_severity": {
        "mild": "Mild",
        "moderate": "Moderate",
        "severe": "Severe",
        "life_threatening": "Life-threatening",
    },
    "mobile_belongs_to": {
        "self": "Self",
        "spouse": "Spouse",
        "child": "Child",
        "parent": "Parent",
        "relative": "Relative",
    },
    "nationality": {"Indian resident": "Indian Resident", "OCI-PIO": "OCI/PIO"},
    "id_type": {
        "aadhaar": "Aadhaar",
        "pan": "PAN card",
        "passport": "Passport",
        "voter_id": "Voter ID (EPIC)",
        "driving_licence": "Driving licence",
        "nrega_job_card": "NREGA job card",
        "birth_certificate": "Birth certificate",
        "oci_card": "OCI card",
        "unhcr_card": "UNHCR refugee card",
    },
    "consent_method": {
        "Signature": "Signed on the tablet",
        "Scanned form": "Signed on paper — scanned",
        "Thumb impression": "Thumb impression + witness",
    },
    "send_via": {
        "whatsapp": "WhatsApp",
        "sms": "SMS",
        "phone": "Phone call",
        "do_not_contact": "Do not contact",
    },
}


def number_or_none(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


# Patient record fields a form builder field can be merged into ("Save to
# the patient record"), with their length limits. Camp capture copies the
# answer into the patient's own field instead of only keeping it as an extra
# answer. Only free-text fields the Edit Patient form has.
PATIENT_FIELD_TARGETS = {"father_name": 100, "mother_name": 100, "email": 254}


def mapped_patient_values(answers):
    """The answers saved to the patient record, by patient field."""
    return {
        target: str(answer["value"]).strip()[:PATIENT_FIELD_TARGETS[target]]
        for answer in (answers or {}).values()
        if (target := answer.get("patient_field")) in PATIENT_FIELD_TARGETS
        and str(answer.get("value", "")).strip()
    }


def question_field(q, section):  # noqa: PLR0912
    """One CARE question of the event's form as a capture question, or None
    for a field nobody answers (calculated)."""
    meta = (q.get("styling_metadata") or {}).get("poh") or {}
    builder = meta.get("type")
    if builder == "calculated" or (not builder and q.get("read_only")):
        return None
    if builder in BUILDER_TO_CAPTURE:
        kind = BUILDER_TO_CAPTURE[builder]
    elif q.get("type") == "choice":
        kind = "checkboxes" if q.get("repeats") else "dropdown"
    else:
        kind = CARE_TO_CAPTURE.get(q.get("type"), "short_text")
    rules = meta.get("rules") or {}
    validation = {}
    if kind == "number":
        validation = {
            "min": number_or_none(rules.get("min")),
            "max": number_or_none(rules.get("max")),
        }
        if builder == "integer" or (not builder and q.get("type") == "integer"):
            validation["integer"] = True
    elif kind in {"short_text", "paragraph"} and (
        q.get("max_length") or rules.get("maxLength")
    ):
        validation = {"max_length": int(q.get("max_length") or rules.get("maxLength"))}
    if builder in BUILDER_PATTERNS:
        validation["pattern"], validation["message"] = BUILDER_PATTERNS[builder]
    if rules.get("message"):
        validation["message"] = rules["message"]
    label = q.get("text", "")
    if builder == "reading" and meta.get("unit"):
        label = f"{label} ({meta['unit']})"
    options = [str(o.get("value")) for o in q.get("answer_option") or []]
    if builder == "consent":
        options = [CONSENT_OPTION]
    show_when = None
    condition = (q.get("enable_when") or [None])[0]
    if condition and condition.get("operator") in {"equals", "not_equals"}:
        answer = condition.get("answer")
        if isinstance(answer, bool):
            answer = "Yes" if answer else "No"
        show_when = {
            "key": condition.get("question"),
            "op": "is" if condition["operator"] == "equals" else "is_not",
            "value": str(answer),
        }
    return SimpleNamespace(
        key=q.get("link_id", ""),
        section=section,
        label=label,
        help_text=q.get("description") or "",
        field_type=kind,
        required=bool(q.get("required")) and kind not in {"section", "note"},
        options=options,
        validation=validation,
        show_when=show_when,
        place="",
        unit=meta.get("unit", "") if builder == "reading" else "",
        patient_field=meta.get("patient_field", "")
        if meta.get("patient_field") in PATIENT_FIELD_TARGETS
        else "",
        # A required field not needed once this other one is answered (the
        # form builder's "Not needed when"; the date of birth beside the age).
        required_unless=str(meta.get("required_unless") or "")
        if q.get("required")
        else "",
    )


# The camp capture form's own sections, by the standard template's section
# column names and by their titles. A form field in one of these sections is
# shown inside that section of the capture form (its `place`).
CAPTURE_SECTIONS = {
    "section_identity": "identity", "identity details": "identity",
    "section_contact": "contact", "contact details": "contact",
    "section_allergies": "allergies", "allergies": "allergies",
    "section_vitals": "vitals", "collect vitals & readings": "vitals",
    "section_consent": "consent", "consent & communication": "consent",
    "section_referral": "referral", "referral info": "referral",
}  # fmt: skip


def capture_place(group):
    """The capture section a form group matches, or "" for one of its own."""
    return CAPTURE_SECTIONS.get(group.get("link_id", "")) or CAPTURE_SECTIONS.get(
        group.get("text", "").strip().lower(), ""
    )


def capture_fields(event):
    """The extra questions camp capture asks: the event's form (its CARE
    questionnaire) without the fields capture always asks; or, for an event
    without a form, its older question list. A field in a group matching one
    of capture's sections gets that section as its `place`; any other group
    becomes a section heading when it has questions left."""
    if not event.questionnaire_id:
        return list(event.form_fields.all())

    def walk(questions, section):
        found = []
        for q in questions or []:
            if q.get("type") == "group":
                inner = walk(q.get("questions"), q.get("text", ""))
                place = capture_place(q)
                if inner and place:
                    for field in inner:
                        field.place = field.place or place
                    found += inner
                elif inner:
                    heading = SimpleNamespace(
                        key=q.get("link_id", ""), section="", label=q.get("text", ""),
                        help_text="", field_type="section", required=False,
                        options=[], validation={}, show_when=None, place="",
                    )  # fmt: skip
                    found += [heading, *inner]
            elif q.get("link_id") not in CAPTURE_KEYS:
                field = question_field(q, section)
                if field:
                    found.append(field)
        return found

    return walk(event.questionnaire.questions, "")


# The capture form's section titles, as the patient page shows them.
PLACE_TITLES = {
    "identity": "Identity details", "contact": "Contact details",
    "allergies": "Allergies", "vitals": "Vitals & readings",
    "consent": "Consent & communication", "referral": "Referral info",
}  # fmt: skip
HEALTH_SECTION = "Health questions"
# The capture screen's order: its own sections, with Health questions and the
# form's own sections between the vitals and consent.
SECTION_ORDER = {
    title: i
    for i, title in enumerate(
        [*(PLACE_TITLES[p] for p in ["identity", "contact", "allergies", "vitals"]),
         HEALTH_SECTION, PLACE_TITLES["consent"], PLACE_TITLES["referral"]]
    )
}  # fmt: skip
# An existing person's capture has no identity, contact or allergy section;
# questions placed there are asked under Health questions.
NEW_ONLY_PLACES = {"identity", "contact", "allergies"}


def answer_sections(fields, kind="new"):
    """Each question's section as camp capture showed it: its capture
    section, else the form's own section heading above it, else Health
    questions. By question key."""
    sections, heading = {}, HEALTH_SECTION
    for field in fields:
        if field.field_type == "section":
            heading = field.label or HEALTH_SECTION
            continue
        place = getattr(field, "place", "") or ""
        if place and not (kind == "existing" and place in NEW_ONLY_PLACES):
            sections[field.key] = PLACE_TITLES.get(place, HEALTH_SECTION)
        else:
            sections[field.key] = HEALTH_SECTION if place else heading
    return sections


def answer_words(value):
    """An answer as the words a "show only when" rule compares."""
    if isinstance(value, bool):
        return ["Yes" if value else "No"]
    if isinstance(value, list):
        return [str(v) for v in value]
    return [] if value in (None, "") else [str(value)]


def question_shown(field, answers, by_key, participant=None):
    """Whether a question is asked, following the form's "show only when"
    rule as CARE does: "is not" holds only once the other field is answered.
    A rule on a field capture asks itself reads the participant's value."""
    rule = getattr(field, "show_when", None)
    if not rule:
        return True
    on = by_key.get(rule["key"])
    if on is not None:
        if not question_shown(on, answers, by_key, participant):
            return False
        values = answer_words(answers.get(rule["key"]))
    else:
        raw = (
            getattr(participant, rule["key"], None) if participant is not None else None
        )
        values = answer_words(raw)
        labels = FIXED_VALUE_LABELS.get(rule["key"], {})
        values += [labels[v] for v in values if v in labels]
    values = [v.lower() for v in values]
    want = str(rule["value"]).lower()
    if rule["op"] == "is":
        return want in values
    return bool(values) and want not in values


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


def questionnaire_slug(event):
    """The slug of the event's form (a CARE questionnaire), or None."""
    return event.questionnaire.slug if event.questionnaire_id else None


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
        "questionnaire": questionnaire_slug(event),
        # The form's name and version, and what capture asks from it (the
        # questions without the fixed fields), for the event page.
        "questionnaire_title": event.questionnaire.title
        if event.questionnaire_id
        else None,
        "questionnaire_version": (
            (event.questionnaire.styling_metadata or {}).get("poh", {}).get("version")
            if event.questionnaire_id
            else None
        ),
        "capture_questions": [form_field_row(f) for f in capture_fields(event)],
        "login_emails": login_emails(event),
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
    events = (
        OutreachEvent.objects.filter(
            Q(facility__in=facilities) | Q(logins__user=user),
            status=APPROVED,
            event_type__in=["CAMP", "PES"],
            deleted=False,
        )
        .select_related("facility")
        .distinct()
    )
    today = timezone.localdate()

    def order(event):
        if event.event_date == today:
            return (0, 0)
        if event.event_date > today:
            return (1, (event.event_date - today).days)
        return (2, (today - event.event_date).days)

    return sorted(events, key=order)


def is_login_user(user, event):
    """Signed in with one of the camp's emails (camp_login): capture at this
    event only, while that email is still on the event."""
    return event.logins.filter(user=user).exists()


def require_capture(user, event):
    if not is_login_user(user, event):
        require(
            user,
            event.facility,
            "can_capture_camp_participant",
            "You can't capture at this facility",
        )


def start_session(event, user, device_label):
    require_capture(user, event)
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
    require_capture(user, session.event)
    return session


# --- Camp sign-in with the camp's email ------------------------------------------

LOGIN_ATTEMPTS = 10
LOGIN_WINDOW = 15 * 60


def camp_login_header(event):
    """What the sign-in page shows before anyone signs in."""
    return {
        "id": str(event.external_id),
        "number": event.number,
        "name": event.name,
        "type_label": EVENT_TYPES[event.event_type]["label"],
        "venue_name": event.venue_name,
        "event_date": event.event_date,
    }


def camp_login_user(login):
    """The account an email signs in as: one per email per event, with no
    password and no facility role, so it can do nothing in the app except
    capture at this event (is_login_user). Its name and email are the
    email, which camp lists show as "captured by"."""
    user = login.user
    if user is None:
        user = User(
            username=f"camp-{uuid.uuid4().hex[:11]}",
            first_name=login.email[:150],
            email=login.email,
            user_type="Staff",
            phone_number="",
        )
        user.set_unusable_password()
        user.save()
        login.user = user
        login.save(update_fields=["user"])
    return user


def email_error(msg):
    errors = Errors()
    errors.add("email", msg)
    errors.raise_if_any()


def camp_login(event_id, email, client):
    """Signs in with one of the camp's emails: tokens for that email's
    account. Ten wrong emails from one address lock that address out of the
    camp for 15 minutes."""
    key = f"hmis:camp-login:{event_id}:{client}"
    if cache.get(key, 0) >= LOGIN_ATTEMPTS:
        email_error("Too many tries. Wait 15 minutes and try again.")
    event = OutreachEvent.objects.filter(external_id=event_id, deleted=False).first()
    if event is None or not EVENT_TYPES[event.event_type]["capture"]:
        raise ValidationError("This camp link isn't valid")
    email = (email or "").strip().lower()
    login = event.logins.filter(email=email).first() if email else None
    if login is None:
        cache.set(key, cache.get(key, 0) + 1, LOGIN_WINDOW)
        email_error("This email can't open this camp")
    if event.status != APPROVED:
        raise ValidationError(
            "This camp isn't approved yet, so it can't capture people"
        )
    with transaction.atomic():
        login = OutreachEventLogin.objects.select_for_update().get(pk=login.pk)
        user = camp_login_user(login)
    refresh = RefreshToken.for_user(user)
    User.objects.filter(pk=user.pk).update(last_login=timezone.now())
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


def mask_aadhaar(number):
    digits = re.sub(r"\D", "", number)
    return f"XXXX XXXX {digits[-4:]}"


def age_on(dob, day):
    return day.year - dob.year - ((day.month, day.day) < (dob.month, dob.day))


def check_answers(fields, answers, errors, participant=None):  # noqa: PLR0912, PLR0915
    """Each extra answer against its question; returns what is stored.
    Questions hidden by their "show only when" rule are skipped."""
    stored = {}
    known = {f.key for f in fields}
    sections = answer_sections(fields, getattr(participant, "kind", "new"))
    by_key = {f.key: f for f in fields if f.field_type not in {"section", "note"}}
    for key in answers:
        errors.check(
            key in known, ["extra_answers", key], "Not a question on this form"
        )
    for field in fields:
        # Headings and notes for staff aren't answered.
        if field.field_type in {"section", "note"}:
            continue
        if not question_shown(field, answers, by_key, participant):
            continue
        loc = ["extra_answers", field.key]
        value = answers.get(field.key)
        blank = value is None or value in ("", [])
        if blank:
            other = getattr(field, "required_unless", "")
            if other in by_key:
                excused = answers.get(other) not in (None, "", [])
            else:
                excused = bool(other) and getattr(participant, other, None) not in (None, "", [])
            errors.check(
                not field.required or excused, loc, "This question is required"
            )
            continue
        validation = field.validation or {}
        kind = field.field_type
        ok, msg = True, ""
        custom = validation.get("message")
        if kind in {"short_text", "paragraph"}:
            limit = validation.get("max_length") or (
                200 if kind == "short_text" else 2000
            )
            ok = isinstance(value, str) and len(value.strip()) <= limit
            msg = custom or f"Up to {limit} characters"
            value = value.strip() if ok else value
            if ok and validation.get("pattern"):
                ok = bool(re.fullmatch(validation["pattern"], value))
        elif kind == "number":
            ok = isinstance(value, int | float) and not isinstance(value, bool)
            msg = "Enter a number"
            if ok and validation.get("integer") and value != int(value):
                ok, msg = False, custom or "Whole numbers only"
            if ok and validation.get("min") is not None and value < validation["min"]:
                ok, msg = False, custom or f"At least {validation['min']:g}"
            if ok and validation.get("max") is not None and value > validation["max"]:
                ok, msg = False, custom or f"At most {validation['max']:g}"
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
            pattern = validation.get("pattern")
            ok = isinstance(value, str) and bool(
                re.fullmatch(pattern, value) if pattern else PHONE_RE.match(value)
            )
            msg = custom or "Enter a phone number (6 to 15 digits)"
        elif kind == "email":
            ok, msg = (
                isinstance(value, str) and bool(EMAIL_RE.match(value)),
                "Enter an email address",
            )
        errors.check(ok, loc, msg)
        if ok:
            stored[field.key] = {
                "label": field.label,
                "type": kind,
                "value": value,
                "section": sections.get(field.key, HEALTH_SECTION),
            }
            if getattr(field, "patient_field", ""):
                stored[field.key]["patient_field"] = field.patient_field
    return stored


# A camp's nationalities (as on the Domestic form), in the patient record's
# nationality_category values.
CAMP_NATIONALITIES = ["Indian resident", "NRI", "OCI-PIO"]


PATIENT_TO_CAMP_GENDER = {"male": "male", "female": "female", "non_binary": "other"}


HIDDEN_FOR_EXISTING = (
    "preferred_language",
    "nationality",
    "blood_group",
    "whatsapp",
    "alt_mobile_isd",
    "alt_mobile",
    "alt_mobile_belongs_to",
    "address",
    "country",
    "state",
    "city",
    "district",
    "area",
    "permanent_same_as_current",
    "permanent_address",
    "id_type",
    "id_number",
    "allergy_status",
    "allergen",
    "allergy_reaction",
    "allergy_severity",
)


def take_patient_identity(spec, patient):
    """An existing patient's identity and contact come from their hospital
    record, not the form (Existing Person shows only the search). Returns
    False when the record lacks a date or year of birth."""
    first, _, last = (patient.name or "").strip().partition(" ")
    phone = patient.phone_number or ""
    born = patient.date_of_birth or (
        date(patient.year_of_birth, 1, 1) if patient.year_of_birth else None
    )
    spec.title = patient.title if patient.title in TITLES else ""
    spec.first_name = (patient.first_name or first)[:40]
    spec.middle_name = (patient.middle_name or "")[:40]
    spec.last_name = (patient.last_name or last)[:40]
    spec.gender = PATIENT_TO_CAMP_GENDER.get(patient.gender, "other")
    spec.date_of_birth = born
    spec.mobile_isd = "+91" if phone.startswith("+91") or not phone else phone[:-10]
    spec.mobile = phone[-10:]
    spec.mobile_belongs_to = "self"
    spec.pincode = str(patient.pincode or "")[:6]
    # Fields the Existing Person form doesn't show: whatever was left from New
    # Person or Capture Id is dropped, not saved on the visit.
    for name in HIDDEN_FOR_EXISTING:
        setattr(spec, name, ParticipantIn.model_fields[name].get_default())
    return born is not None


def check_participant(spec, event, errors):
    today = timezone.localdate()
    patient = None
    if spec.kind == "existing":
        patient = (
            Patient.objects.filter(external_id=spec.patient, deleted=False).first()
            if spec.patient
            else None
        )
        errors.check(patient, "patient", "Find the patient by name, mobile or UHID")
        if patient is not None:
            errors.check(
                take_patient_identity(spec, patient),
                "patient",
                "This patient's record has no date of birth; add it at the desk first",
            )
        return patient, check_visit(spec, event, errors)
    first = spec.first_name.strip()
    errors.check(
        NAME_PART_RE.fullmatch(first), "first_name", f"2 to 50 characters. {NAME_MESSAGE}"
    )
    errors.check(
        LAST_NAME_RE.fullmatch(spec.last_name.strip()),
        "last_name",
        f"Enter the last name. {NAME_MESSAGE}",
    )
    errors.check(spec.gender in GENDERS, "gender", "Choose the gender")
    if spec.date_of_birth is None and spec.age is not None:
        # An age alone is an approximate date of birth; a child needs the exact one.
        errors.check(
            spec.age >= MINOR_AGE,
            "date_of_birth",
            "Under 18: enter the exact date of birth",
        )
    elif spec.date_of_birth is None:
        errors.add("date_of_birth", "Enter the date of birth or the age")
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
    # A new person's mandatory details: everything below, and the title,
    # language, nationality, address and locality in check_details.
    errors.check(spec.id_type in ID_TYPES, "id_type", "Choose the government ID")
    problem = id_number_problem(spec.id_type, spec.id_number)
    errors.check(not problem, "id_number", problem)
    check_details(spec, errors)
    return patient, check_visit(spec, event, errors)


def check_visit(spec, event, errors):
    """What's checked for everyone captured: vitals, finding, consent and
    tests. Returns the event's tests by id."""
    errors.check(spec.bp, "bp", "Enter the BP")
    errors.check(not spec.bp or BP_RE.match(spec.bp), "bp", "BP as 150/94")
    errors.check(spec.height_cm is not None, "height_cm", "Enter the height")
    errors.check(spec.weight_kg is not None, "weight_kg", "Enter the weight")
    errors.check(spec.finding.strip(), "finding", "Enter the finding or concern")
    check_consent(spec, errors)
    offered = {str(t.external_id): t for t in event.tests.all()}
    for i, test_id in enumerate(spec.picks()):
        errors.check(
            test_id in offered, ["tests", i], "Not a test offered at this event"
        )
    return offered


def mobile_problem(isd, number):
    if isd == "+91":
        return (
            None if re.match(r"^[6-9]\d{9}$", number) else "10 digits, starting 6 to 9"
        )
    if re.match(r"^\d{6,12}$", number):
        return None
    return f"6 to 12 digits for {isd}"


def check_details(spec, errors):
    """The identity, contact and allergy details beyond the camp's basics."""
    errors.check(spec.title in TITLES, "title", "Choose the title")
    errors.check(
        spec.preferred_language.strip(),
        "preferred_language",
        "Choose the preferred language",
    )
    errors.check(
        spec.nationality in CAMP_NATIONALITIES,
        "nationality",
        "Choose the nationality",
    )
    errors.check(spec.address.strip(), "address", "Enter the house, flat or building")
    errors.check(spec.city.strip(), "city", "Choose the post office or locality")
    middle = spec.middle_name.strip()
    errors.check(
        not middle or NAME_PART_RE.fullmatch(middle),
        "middle_name",
        f"2 to 50 characters. {NAME_MESSAGE}",
    )
    errors.check(
        not spec.blood_group or spec.blood_group in BLOOD_GROUPS,
        "blood_group",
        "Choose the blood group",
    )
    if spec.whatsapp:
        problem = mobile_problem(spec.mobile_isd, spec.whatsapp)
        errors.check(not problem, "whatsapp", problem)
    if spec.alt_mobile:
        errors.check(
            re.match(r"^\+\d{1,4}$", spec.alt_mobile_isd),
            "alt_mobile_isd",
            "Country code like +91",
        )
        problem = mobile_problem(spec.alt_mobile_isd, spec.alt_mobile)
        errors.check(not problem, "alt_mobile", problem)
        errors.check(
            (spec.alt_mobile_isd, spec.alt_mobile) != (spec.mobile_isd, spec.mobile),
            "alt_mobile",
            "Must differ from the mobile number",
        )
        errors.check(
            spec.alt_mobile_belongs_to in ALT_BELONGS_TO,
            "alt_mobile_belongs_to",
            "Choose whose number it is",
        )
    perm = spec.permanent_address
    if perm and not spec.permanent_same_as_current and perm.pincode:
        india = not perm.country.strip() or perm.country.strip().lower() == "india"
        errors.check(
            not india or PIN_RE.match(perm.pincode),
            ["permanent_address", "pincode"],
            "PIN code: 6 digits, not starting with 0",
        )
    errors.check(
        not spec.allergy_status or spec.allergy_status in ALLERGY_STATUSES,
        "allergy_status",
        "Choose the allergy status",
    )
    if spec.allergy_status == "has":
        errors.check(spec.allergen.strip(), "allergen", "Choose the allergen")
        errors.check(
            spec.allergy_reaction.strip(), "allergy_reaction", "Choose the reaction"
        )
        errors.check(
            spec.allergy_severity in ALLERGY_SEVERITIES,
            "allergy_severity",
            "Choose the severity",
        )


def check_consent(spec, errors):
    errors.check(
        spec.consent_screening, "consent_screening", "Consent is needed to capture"
    )
    errors.check(
        spec.consent_dpdp_notice,
        "consent_dpdp_notice",
        "Read out the privacy notice and tick",
    )
    errors.check(spec.consent_tariff, "consent_tariff", "Explain the tariff and tick")
    errors.check(spec.consent_by in CONSENT_BY, "consent_by", "Who gives consent")
    errors.check(
        spec.consent_method in CONSENT_METHODS,
        "consent_method",
        "Choose how consent was taken",
    )
    if spec.consent_method == THUMB_IMPRESSION:
        witness = spec.consent_witness.strip()
        errors.check(
            NAME_PART_RE.fullmatch(witness),
            "consent_witness",
            f"Enter the witness's name. {NAME_MESSAGE}",
        )
    errors.check(
        all(v in SEND_VIA for v in spec.send_via),
        "send_via",
        "WhatsApp, SMS, phone call or do not contact",
    )
    errors.check(
        DO_NOT_CONTACT not in spec.send_via or len(set(spec.send_via)) == 1,
        "send_via",
        "Do not contact can't be chosen with a channel",
    )


def check_files(spec, files, errors):
    """The photo (an image) and the consent form (needed unless signed on the
    tablet), each within the upload limit."""
    limit = settings.MAX_FILE_UPLOAD_SIZE * 1024 * 1024
    for key, types in (("photo", IMAGE_MIME_TYPES), ("consent_file", FILE_MIME_TYPES)):
        upload = files.get(key)
        if upload is None:
            continue
        errors.check(
            upload.size <= limit,
            key,
            f"Files can be up to {settings.MAX_FILE_UPLOAD_SIZE} MB",
        )
        errors.check(
            upload.content_type in types,
            key,
            "Upload a JPEG, PNG or WebP"
            if key == "photo"
            else "Upload a JPEG, PNG, WebP or PDF",
        )
    if (
        spec.consent_method in CONSENT_METHODS
        and spec.consent_method != TABLET_SIGNATURE
    ):
        errors.check(
            files.get("consent_file"), "consent_file", "Upload the consent form"
        )


def bmi_of(height_cm, weight_kg):
    if not height_cm or not weight_kg:
        return None
    metres = Decimal(height_cm) / 100
    return (Decimal(weight_kg) / (metres * metres)).quantize(Decimal("0.1"))


def store_participant_file(participant, user, upload, category, name):
    file = FileUpload(
        name=name,
        internal_name=upload.name,
        associating_id=str(participant.external_id),
        file_type="camp_participant",
        file_category=category,
        meta={"mime_type": upload.content_type},
        created_by=user,
        updated_by=user,
    )
    file.save()
    file.files_manager.put_object(file, upload.read())
    file.upload_completed = True
    file.save(skip_internal_name=True)
    return file


def free_used(event, test):
    return CampParticipantTest.objects.filter(
        participant__event=event,
        participant__deleted=False,
        event_test=test,
        fee_type="free",
    ).count()


# Camp values as the desk registration stores them on Care's Patient.
PATIENT_GENDERS = {"male": "male", "female": "female", "other": "non_binary"}
PATIENT_BLOOD_GROUPS = {
    "A+": "A_positive",
    "A-": "A_negative",
    "B+": "B_positive",
    "B-": "B_negative",
    "AB+": "AB_positive",
    "AB-": "AB_negative",
    "O+": "O_positive",
    "O-": "O_negative",
}
PATIENT_MOBILE_OWNERS = {
    "self": "Self",
    "spouse": "Spouse",
    "child": "Son",
    "parent": "Parent",
    "relative": "Relative",
}
PATIENT_ALLERGY_STATUSES = {"nka": "nka", "has": "has", "unknown": "unable"}
PATIENT_CHANNELS = {"whatsapp": "WhatsApp", "sms": "SMS", "phone": "Phone call"}
PATIENT_ALLERGY_SEVERITIES = {"mild", "moderate", "severe", "life_threatening"}


def create_camp_patient(event, spec, user, number, answers=None):
    """A new person captured at a camp becomes a Care patient with a PRE-
    number (pre-registration) at the camp's facility, so they're in the
    hospital's patient list. The desk converts them to a UHID when it
    completes their registration (Edit Patient). Called inside the capture's
    transaction. Care's PatientCreateLock isn't taken: it guards staff-entered
    identifiers, which a camp has none of, and it refuses (rather than waits
    for) a second capture saved at the same moment."""
    facility = event.facility
    first = spec.first_name.strip()
    middle = spec.middle_name.strip()
    last = spec.last_name.strip()
    address = spec.address.strip()
    perm = spec.permanent_address
    permanent = perm if perm and not spec.permanent_same_as_current else None
    has_allergy = spec.allergy_status == "has"
    channels = [PATIENT_CHANNELS[x] for x in spec.send_via if x in PATIENT_CHANNELS]
    mobile_owner = PATIENT_MOBILE_OWNERS.get(spec.mobile_belongs_to, "")
    if mobile_owner == "Self" and age_on(spec.date_of_birth, timezone.localdate()) < MINOR_AGE:
        # The desk refuses "Self" for a child's mobile; at a camp it's a parent's.
        mobile_owner = "Parent"
    patient = Patient.objects.create(
        name=" ".join(x for x in (first, middle, last) if x),
        title=spec.title,
        first_name=first,
        middle_name=middle,
        last_name=last,
        gender=PATIENT_GENDERS.get(spec.gender, spec.gender),
        date_of_birth=spec.date_of_birth,
        phone_number=f"{spec.mobile_isd}{spec.mobile}",
        mobile_owner=mobile_owner,
        # As the desk saves it: "same as mobile" is the mobile number.
        whatsapp_number=f"{spec.mobile_isd}{spec.whatsapp or spec.mobile}",
        whatsapp_owner=mobile_owner
        if not spec.whatsapp or spec.whatsapp == spec.mobile
        else "",
        alternate_phone_number=f"{spec.alt_mobile_isd}{spec.alt_mobile}"
        if spec.alt_mobile
        else "",
        alternate_phone_owner=spec.alt_mobile_belongs_to if spec.alt_mobile else "",
        address=address,
        house=address[:100],
        city=spec.city.strip(),
        district=spec.district.strip(),
        state=spec.state.strip(),
        pincode=int(spec.pincode) if spec.pincode.isdigit() else None,
        permanent_address=permanent.address.strip() if permanent else address,
        permanent_house=permanent.address.strip()[:100] if permanent else "",
        permanent_city=permanent.city.strip() if permanent else "",
        permanent_district=permanent.district.strip() if permanent else "",
        permanent_state=permanent.state.strip() if permanent else "",
        permanent_pincode=permanent.pincode.strip() if permanent else "",
        permanent_country=permanent.country.strip() if permanent else "",
        blood_group=PATIENT_BLOOD_GROUPS.get(spec.blood_group, "unknown"),
        preferred_language=spec.preferred_language.strip()[:30],
        nationality_category=spec.nationality,
        allergy_status=PATIENT_ALLERGY_STATUSES.get(spec.allergy_status, ""),
        arrival_source="Health camp",
        camp_name=event.name[:100],
        camp_patient_id=number,
        consent_given_by=spec.consent_by,
        consent_method=spec.consent_method,
        consent_witness=spec.consent_witness.strip()[:50]
        if spec.consent_method == THUMB_IMPRESSION
        else "",
        general_consent=True,
        data_processing_consent=True,
        tariff_consent=True,
        do_not_contact=DO_NOT_CONTACT in spec.send_via,
        communication_channels=[] if DO_NOT_CONTACT in spec.send_via else channels,
        registration_facility=facility,
        created_by=user,
        updated_by=user,
        # A typed age is an estimate, marked as Care marks it (set_date_of_birth).
        meta={"date_of_birth_approximate": True} if spec.age is not None else {},
        # Answers to form fields merged into the patient record (father's name…).
        **mapped_patient_values(answers),
    )
    evaluate_patient_instance_default_values(patient)
    issue_patient_number(patient, facility, PatientNumberSeries.pre_registration)
    if has_allergy and spec.allergen.strip():
        severity = spec.allergy_severity.lower().replace(" ", "_").replace("-", "_")
        RegistrationAllergy.objects.create(
            patient=patient,
            substance=spec.allergen.strip()[:100],
            reaction=spec.allergy_reaction.strip()[:100],
            severity=severity if severity in PATIENT_ALLERGY_SEVERITIES else "mild",
        )
    return patient


def capture_participant(session, user, spec, files=None):
    """Captures a person; files may hold the photo and the consent form
    (uploads keyed photo and consent_file). A new person is also created as
    a pre-registered Care patient (create_camp_patient)."""
    files = files or {}
    event = session.event
    errors = Errors()
    patient, offered = check_participant(spec, event, errors)
    check_files(spec, files, errors)
    fields = capture_fields(event)
    answers = check_answers(fields, spec.extra_answers, errors, participant=spec)
    errors.raise_if_any()
    if spec.kind == "new" and spec.date_of_birth is None and spec.age is not None:
        # An age alone becomes an approximate date of birth, as at the desk.
        spec.date_of_birth = approximate_date_of_birth(spec.age)
    elif spec.date_of_birth is not None:
        spec.age = None
    perm = spec.permanent_address
    permanent = (
        {k: v.strip() for k, v in perm.model_dump().items()}
        if perm and not spec.permanent_same_as_current
        else {}
    )
    has_allergy = spec.allergy_status == "has"
    send_via = (
        [DO_NOT_CONTACT]
        if DO_NOT_CONTACT in spec.send_via
        else sorted(set(spec.send_via))
    )

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
        number = allocate_participant_number(event)
        participant = CampParticipant.objects.create(
            number=number,
            event=event,
            session=session,
            patient=patient or create_camp_patient(event, spec, user, number, answers),
            kind=spec.kind,
            title=spec.title,
            first_name=first,
            middle_name=spec.middle_name.strip(),
            last_name=spec.last_name.strip(),
            gender=spec.gender,
            date_of_birth=spec.date_of_birth,
            date_of_birth_approximate=spec.age is not None,
            preferred_language=spec.preferred_language.strip(),
            nationality=spec.nationality,
            blood_group=spec.blood_group,
            mobile_isd=spec.mobile_isd,
            mobile=spec.mobile,
            mobile_belongs_to=spec.mobile_belongs_to,
            whatsapp=spec.whatsapp if spec.whatsapp != spec.mobile else "",
            alt_mobile_isd=spec.alt_mobile_isd if spec.alt_mobile else "",
            alt_mobile=spec.alt_mobile,
            alt_mobile_belongs_to=spec.alt_mobile_belongs_to if spec.alt_mobile else "",
            address=spec.address.strip(),
            country=spec.country.strip(),
            state=spec.state.strip(),
            city=spec.city.strip(),
            district=spec.district.strip(),
            pincode=spec.pincode,
            area=spec.area.strip(),
            permanent_same_as_current=spec.permanent_same_as_current,
            permanent_address=permanent,
            id_type=spec.id_type if id_number else "",
            id_number=id_number,
            allergy_status=spec.allergy_status,
            allergen=spec.allergen.strip() if has_allergy else "",
            allergy_reaction=spec.allergy_reaction.strip() if has_allergy else "",
            allergy_severity=spec.allergy_severity if has_allergy else "",
            bp=spec.bp,
            rbs_mg_dl=spec.rbs_mg_dl,
            height_cm=spec.height_cm,
            weight_kg=spec.weight_kg,
            bmi=bmi_of(spec.height_cm, spec.weight_kg),
            finding=spec.finding.strip(),
            refer_to_department=spec.refer_to_department.strip(),
            consent_screening=True,
            consent_dpdp_notice=True,
            consent_tariff=True,
            consent_by=spec.consent_by,
            consent_channel=spec.consent_method,
            consent_witness=spec.consent_witness.strip()
            if spec.consent_method == THUMB_IMPRESSION
            else "",
            send_via=send_via,
            extra_answers=answers,
            form_version=event.version,
            status="registered" if patient else "open",
            verification_code=secrets.token_hex(3).upper(),
            captured_by=user,
            created_by=user,
            updated_by=user,
        )
        updates = []
        # Existing Person has no photo field; a photo left from New Person is dropped.
        if files.get("photo") and spec.kind != "existing":
            participant.photo = store_participant_file(
                participant, user, files["photo"], "profile_photo", "Photo"
            )
            updates.append("photo")
        if files.get("consent_file") and spec.consent_method != TABLET_SIGNATURE:
            participant.consent_file = store_participant_file(
                participant,
                user,
                files["consent_file"],
                "consent_attachment",
                "Consent form",
            )
            updates.append("consent_file")
        if updates:
            participant.save(update_fields=updates)
        if patient:
            # An existing patient: merged answers fill only what their record leaves blank.
            blank = {
                f: v for f, v in mapped_patient_values(answers).items()
                if not getattr(patient, f, "")
            }  # fmt: skip
            if blank:
                for f, v in blank.items():
                    setattr(patient, f, v)
                patient.save(update_fields=list(blank))
        rows = []
        for test_id, pick in spec.picks().items():
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
                    location=pick.location or test.location,
                    result=pick.result.strip(),
                )
            )
    return participant


def catalogue_rate(activity):
    if activity is None:
        return None
    from hmis.lab_catalogue import base_price

    price = base_price(activity)
    return None if price is None else Decimal(str(price))


def participant_patient_number(participant):
    """The linked patient's UHID or, until they have one, PRE number:
    {"label", "value"}."""
    if participant.patient_id is None:
        return None
    return display_numbers([participant.patient_id]).get(participant.patient_id)


def camp_visit_row(participant):
    """A camp visit for the patient page: the participant and its camp."""
    event = participant.event
    answers = participant.extra_answers or {}
    if answers:
        fields = capture_fields(event)
        # Captured before answers recorded their section: from the event's form.
        sections = answer_sections(fields, participant.kind)
        answers = {
            key: {"section": sections.get(key, HEALTH_SECTION), **a}
            for key, a in answers.items()
        }
        # In the order capture asked them (JSONB keeps no key order): by
        # section as the capture screen runs, then by the form's order.
        position = {f.key: i for i, f in enumerate(fields)}
        answers = dict(
            sorted(
                answers.items(),
                key=lambda item: (
                    SECTION_ORDER.get(
                        item[1]["section"], SECTION_ORDER[HEALTH_SECTION]
                    ),
                    position.get(item[0], len(position)),
                ),
            )
        )
    return {
        **participant_row(participant),
        "extra_answers": answers,
        "event": {
            "id": str(event.external_id),
            "number": event.number,
            "name": event.name,
            "type_label": EVENT_TYPES[event.event_type]["label"],
            "venue_name": event.venue_name,
            "event_date": event.event_date,
        },
    }


def captured_by(user):
    """Who captured: their name and email (a camp email account's name is
    its email)."""
    if user is None:
        return None
    return {**person(user), "email": user.email or ""}


def participant_row(participant):
    return {
        "id": str(participant.external_id),
        "number": participant.number,
        "kind": participant.kind,
        "patient_number": participant_patient_number(participant),
        "patient": str(participant.patient.external_id)
        if participant.patient
        else None,
        "title": participant.title,
        "first_name": participant.first_name,
        "middle_name": participant.middle_name,
        "last_name": participant.last_name,
        "gender": participant.gender,
        "date_of_birth": participant.date_of_birth,
        "date_of_birth_approximate": participant.date_of_birth_approximate,
        "preferred_language": participant.preferred_language,
        "nationality": participant.nationality,
        "blood_group": participant.blood_group,
        "has_photo": participant.photo_id is not None,
        "mobile_isd": participant.mobile_isd,
        "mobile": participant.mobile,
        "mobile_belongs_to": participant.mobile_belongs_to,
        "whatsapp": participant.whatsapp,
        "alt_mobile_isd": participant.alt_mobile_isd,
        "alt_mobile": participant.alt_mobile,
        "alt_mobile_belongs_to": participant.alt_mobile_belongs_to,
        "address": participant.address,
        "country": participant.country,
        "state": participant.state,
        "city": participant.city,
        "district": participant.district,
        "pincode": participant.pincode,
        "area": participant.area,
        "permanent_same_as_current": participant.permanent_same_as_current,
        "permanent_address": participant.permanent_address,
        "id_type": participant.id_type,
        "id_number": participant.id_number,
        "allergy_status": participant.allergy_status,
        "allergen": participant.allergen,
        "allergy_reaction": participant.allergy_reaction,
        "allergy_severity": participant.allergy_severity,
        "bp": participant.bp,
        "rbs_mg_dl": participant.rbs_mg_dl,
        "height_cm": money(participant.height_cm),
        "weight_kg": money(participant.weight_kg),
        "bmi": money(participant.bmi),
        "finding": participant.finding,
        "refer_to_department": participant.refer_to_department,
        "consent_by": participant.consent_by,
        "consent_method": participant.consent_channel,
        "consent_witness": participant.consent_witness,
        "has_consent_file": participant.consent_file_id is not None,
        "send_via": participant.send_via,
        "extra_answers": participant.extra_answers,
        "status": participant.status,
        "verification_code": participant.verification_code,
        "captured_by": captured_by(participant.captured_by),
        "captured_at": participant.captured_at,
        "tests": [
            {
                "name": t.name,
                "fee_type": t.fee_type,
                "rate": money(t.rate),
                "source": t.source,
                "payment_status": t.payment_status,
                "report_status": t.report_status,
                "location": t.location,
                "result": t.result,
            }
            for t in participant.tests.all()
        ],
    }


NAME_SEARCH_RE = re.compile(r"^[A-Za-z][A-Za-z .]{2,}$")


def lookup_patients(query, facility=None):
    """Existing patients for camp capture, by exact UHID, exact mobile or
    (3 letters or more) name. A name search covers only patients registered
    at the camp's facility. Returns only what the capture form needs."""
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
        patients = list(Patient.objects.filter(id__in=ids, deleted=False)[:10])
        name = " ".join(query.split())
        if not patients and facility is not None and NAME_SEARCH_RE.match(name):
            patients = Patient.objects.filter(
                name__icontains=name, registration_facility=facility, deleted=False
            ).order_by("name")[:10]
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
