import datetime
import re
import uuid
from enum import Enum
from typing import Literal

from django.conf import settings
from django.utils import timezone
from pydantic import (
    UUID4,
    BaseModel,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from care.emr.extensions.base import ExtensionResource
from care.emr.extensions.validator import (
    ExtensionListRenderer,
    ExtensionRetrieveRenderer,
    ExtensionValidator,
)
from care.emr.models import Organization
from care.emr.models.patient import (
    Patient,
    PatientIdentifier,
    PatientIdentifierConfigCache,
)
from care.emr.resources.base import EMRResource, PhoneNumber
from care.emr.resources.patient.hooks import run_patient_serialization_hooks
from care.emr.resources.patient_identifier.spec import PatientIdentifierListSpec
from care.emr.resources.permissions import PatientPermissionsMixin
from care.emr.tagging.base import PatientFacilityTagManager, PatientInstanceTagManager
from care.emr.utils.datetime_type import StrictTZAwareDateTime
from care.utils.time_util import care_now


class BloodGroupChoices(str, Enum):
    A_negative = "A_negative"
    A_positive = "A_positive"
    B_negative = "B_negative"
    B_positive = "B_positive"
    AB_negative = "AB_negative"
    AB_positive = "AB_positive"
    O_negative = "O_negative"
    O_positive = "O_positive"
    unknown = "unknown"


class GenderChoices(str, Enum):
    male = "male"
    female = "female"
    non_binary = "non_binary"
    transgender = "transgender"


# Patient name parts and payer IDs follow the POH front-office v12.1 formats (V-name, V-emp).
NAME_PART_PATTERN = r"(?=.{2,40}\Z)[A-Za-z]+( [A-Za-z]+)*"
FULL_NAME_PATTERN = r"[A-Za-z]+( [A-Za-z]+)*"
PAYER_ID_PATTERN = r"[A-Z0-9/-]{1,24}"
PASSPORT_PATTERN = r"[A-Z0-9]{6,15}"


class RegistrationAllergy(BaseModel):
    substance: str = Field(min_length=1, max_length=100)
    reaction: str = Field(default="", max_length=100)
    severity: Literal["", "mild", "moderate", "severe"] = ""

    @field_validator("substance")
    @classmethod
    def validate_substance(cls, value):
        if not value.strip():
            raise ValueError("Enter the allergy's substance")
        return value


class RegistrationDetails(BaseModel):
    """Intake data persisted by EMRResource in Patient.meta["registration"]."""

    patient_type: Literal["domestic", "international", "vip", "newborn"] = "domestic"
    prefix: str = Field(default="", max_length=20)
    first_name: str = Field(default="", max_length=75)
    middle_name: str = Field(default="", max_length=75)
    last_name: str = Field(default="", max_length=75)
    email: str = Field(default="", max_length=254)
    alternate_phone_number: PhoneNumber | None = None
    whatsapp_number: PhoneNumber | None = None
    mobile_owner: str = Field(default="", max_length=50)
    marital_status: Literal[
        "",
        "single",
        "married",
        "divorced",
        "widowed",
        "separated",
        "prefer not to say",
    ] = ""
    father_name: str = Field(default="", max_length=100)
    mother_name: str = Field(default="", max_length=100)
    occupation: str = Field(default="", max_length=100)
    religion: str = Field(default="", max_length=50)
    preferred_language: str = Field(default="", max_length=50)
    interpreter_required: bool = False
    # Collected for patients under 18 until a RelatedPerson model exists (v12.1 D09).
    guardian_name: str = Field(default="", max_length=75)
    guardian_relationship: str = Field(default="", max_length=50)
    guardian_phone_number: PhoneNumber | None = None
    country: str = Field(default="", max_length=100)
    state: str = Field(default="", max_length=100)
    locality: str = Field(default="", max_length=100)
    district: str = Field(default="", max_length=100)
    house_number: str = Field(default="", max_length=50)
    landmark: str = Field(default="", max_length=100)
    emergency_prefix: str = Field(default="", max_length=20)
    emergency_first_name: str = Field(default="", max_length=75)
    emergency_middle_name: str = Field(default="", max_length=75)
    emergency_last_name: str = Field(default="", max_length=75)
    emergency_relationship: str = Field(default="", max_length=50)
    emergency_same_as_alternate: bool = False
    alternate_contact_name: str = Field(default="", max_length=75)
    alternate_contact_relationship: str = Field(default="", max_length=50)
    # Payer type (payer) and payer name (company); free text so payers saved before the list stay valid.
    company: str = Field(default="", max_length=200)
    payer: str = Field(default="", max_length=200)
    payer_member_id: str = Field(default="", pattern=rf"^(?:{PAYER_ID_PATTERN})?$")
    policy_number: str = Field(default="", pattern=rf"^(?:{PAYER_ID_PATTERN})?$")
    policy_valid_till: datetime.date | None = None
    policy_holder_relationship: str = Field(default="", max_length=50)
    lead_source: str = Field(default="", max_length=100)
    camp_name: str = Field(default="", max_length=100)
    camp_patient_id: str = Field(default="", max_length=30)
    referral_type: str = Field(default="", max_length=100)
    referred_by: str = Field(default="", max_length=200)
    referrer_registration_number: str = Field(default="", max_length=30)
    referrer_organisation: str = Field(default="", max_length=200)
    # International patients only.
    nationality: str = Field(default="", max_length=100)
    # Domestic patients are Indian; international ones pick another v12.1 category (D21).
    nationality_category: Literal["", "NRI", "OCI-PIO", "Foreign", "Refugee"] = ""
    passport_number: str = Field(default="", pattern=rf"^(?:{PASSPORT_PATTERN})?$")
    passport_expiry: datetime.date | None = None
    visa_type: Literal[
        "",
        "Medical",
        "Medical attendant",
        "e-Medical",
        "Tourist",
        "Business",
        "Employment",
        "Student",
        "Other",
        "Not required",
    ] = ""
    visa_expiry: datetime.date | None = None
    # "other" is no longer offered but stays valid for patients registered before it was removed.
    identity_card_type: Literal[
        "",
        "aadhaar",
        "abha",
        "voter_id",
        "driving_license",
        "pan",
        "ration_card",
        "passport",
        "nrega",
        "birth_certificate",
        "oci_card",
        "unhcr_card",
        "other",
    ] = ""
    identity_number_last4: str = Field(default="", pattern=r"^(?:[A-Za-z0-9]{4})?$")
    abha_number: str = Field(default="", pattern=r"^(?:\d{14})?$")
    abha_status: Literal["", "has", "declined", "not_now"] = ""
    udid: str = Field(default="", max_length=100)
    allergy_status: Literal["", "nka", "has", "unable"] = ""
    allergies: list[RegistrationAllergy] = Field(default=[], max_length=20)
    assistance_needs: list[
        Literal[
            "Wheelchair",
            "Stretcher",
            "Hearing impairment",
            "Visual impairment",
            "Speech impairment",
            "Attendant required",
        ]
    ] = []
    pregnancy_status: Literal["", "not_pregnant", "pregnant", "unknown"] = ""
    # VIP patients only (D29); vip_flagged_by is set by the patient viewset. Statutory flags (D30) are EHR-only.
    vip_note: str = Field(default="", max_length=200)
    vip_flagged_by: str = Field(default="", max_length=150)
    identity_storage_consent: bool = False
    identity_verified_physically: bool = False
    terms_accepted: bool = False
    data_processing_consent: bool = False
    tariff_consent: bool = False
    consent_language: str = Field(default="", max_length=50)
    consent_method: Literal["", "Signature", "Scanned form", "Thumb impression"] = ""
    consent_witness: str = Field(default="", max_length=75)
    communication_channels: list[Literal["SMS", "WhatsApp", "Email", "Phone call"]] = []
    promotions_consent: bool = False
    # Newborn patients only.
    birth_time: datetime.time | None = None
    delivery_type: Literal["", "normal", "assisted", "caesarean"] = ""
    birth_weight_kg: float | None = Field(default=None, ge=0.3, le=6)
    birth_length_cm: float | None = Field(default=None, ge=20, le=70)
    head_circumference_cm: float | None = Field(default=None, ge=15, le=50)
    apgar_score: int | None = Field(default=None, ge=0, le=10)
    inherit_mother_details: bool = False

    @model_validator(mode="after")
    def validate_identity_summary(self):
        if self.identity_number_last4 and not self.identity_card_type:
            raise ValueError("Choose the government ID type")
        if (
            self.identity_card_type
            or self.identity_number_last4
            or self.abha_number
            or self.udid
        ) and not self.identity_storage_consent:
            raise ValueError("Consent is required to store the identity summary")
        return self

    @model_validator(mode="after")
    def validate_name_parts(self):
        # Emergency, VIP and newborn patients are registered without their own name.
        if self.patient_type not in ("domestic", "international"):
            return self
        for label, value in (
            ("first name", self.first_name),
            ("middle name", self.middle_name),
            ("last name", self.last_name),
        ):
            if value and not re.fullmatch(NAME_PART_PATTERN, value):
                err = f"The {label} must be 2-40 letters with single spaces"
                raise ValueError(err)
        for label, value in (
            ("father's name", self.father_name),
            ("mother's name", self.mother_name),
        ):
            if value and not re.fullmatch(FULL_NAME_PATTERN, value):
                err = f"The {label} can only have letters and single spaces"
                raise ValueError(err)
        return self

    @model_validator(mode="after")
    def validate_travel_documents(self):
        today = timezone.localdate()
        if self.patient_type == "international" and not self.nationality_category:
            raise ValueError("Choose the patient's nationality category")
        if self.nationality_category == "NRI" and not self.passport_number:
            raise ValueError("Enter the passport number for an NRI patient")
        if self.nationality_category == "Foreign":
            if not self.passport_number or not self.passport_expiry:
                raise ValueError(
                    "Enter the passport number and expiry date for a foreign national"
                )
            if not self.visa_type:
                raise ValueError("Choose the visa type for a foreign national")
            if self.visa_type != "Not required" and not self.visa_expiry:
                raise ValueError("Enter the visa expiry date")
        if self.passport_expiry and self.passport_expiry < today:
            raise ValueError("The passport has expired")
        if self.visa_expiry and self.visa_expiry < today:
            raise ValueError("The visa has expired")
        if self.policy_valid_till and self.policy_valid_till < today:
            raise ValueError("The payer card or policy has expired")
        return self

    @model_validator(mode="after")
    def validate_flags(self):
        if self.allergy_status == "has" and not self.allergies:
            raise ValueError("List at least one allergy")
        if self.allergies and self.allergy_status != "has":
            raise ValueError(
                "Allergies can only be listed when the patient has allergies"
            )
        if (
            self.consent_method == "Thumb impression"
            and not self.consent_witness.strip()
        ):
            raise ValueError("Enter the witness for a thumb-impression consent")
        return self

    @field_validator("email")
    @classmethod
    def validate_email(cls, value):
        if value and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Enter a valid email address")
        return value


class PatientBaseSpec(EMRResource):
    __model__ = Patient
    ___extension_resource_type__ = ExtensionResource.patient
    __exclude__ = [
        "geo_organization",
        "instance_identifiers",
        "facility_identifiers",
        "instance_tags",
        "facility_tags",
    ]
    __store_metadata__ = True

    id: UUID4 | None = None
    name: str
    gender: GenderChoices
    phone_number: PhoneNumber = Field(max_length=14)
    emergency_phone_number: PhoneNumber | None = Field(None, max_length=14)
    address: str | None = None
    permanent_address: str | None = None
    pincode: int | None = None
    deceased_datetime: StrictTZAwareDateTime | None = None
    registration: RegistrationDetails | None = None
    blood_group: BloodGroupChoices | None = None

    @field_serializer("registration")
    def serialize_registration(self, value):
        # Preserve explicit false consent values and empty intake fields.
        if isinstance(value, RegistrationDetails):
            return value.model_dump(mode="json")
        return value

    @model_validator(mode="after")
    def validate_alternate_phone(self):
        # v12.1 D14: the alternate number must differ from the main one.
        if (
            self.registration
            and self.registration.alternate_phone_number
            and self.registration.alternate_phone_number == self.phone_number
        ):
            raise ValueError(
                "The alternate mobile number must differ from the main number"
            )
        return self

    @model_validator(mode="after")
    def validate_indian_contact(self):
        # v12.1 VAL-02: an Indian mobile is 10 digits starting 6-9; numbers
        # from other countries keep libphonenumber's check.
        registration = self.registration
        for label, phone in (
            ("mobile", self.phone_number),
            ("emergency mobile", self.emergency_phone_number),
            ("alternate mobile", registration and registration.alternate_phone_number),
            ("WhatsApp mobile", registration and registration.whatsapp_number),
            ("guardian's mobile", registration and registration.guardian_phone_number),
        ):
            phone = str(phone or "")
            if phone.startswith("+91") and not re.fullmatch(r"\+91[6-9]\d{9}", phone):
                err = f"The {label} must have 10 Indian digits starting 6-9"
                raise ValueError(err)
        # v12.1 VAL-03: a PIN is 6 digits and cannot start with 0.
        if self.pincode and not 100000 <= self.pincode <= 999999:  # noqa: PLR2004
            raise ValueError("PIN must be 6 digits and start with 1-9")
        return self

    @field_validator("deceased_datetime")
    @classmethod
    def validate_deceased_datetime(cls, deceased_datetime):
        if deceased_datetime is None:
            return None
        if deceased_datetime > care_now():
            raise ValueError("Deceased datetime cannot be in the future")
        return deceased_datetime


def validate_identifier_config(config, value, obj=None):
    if value:
        queryset = PatientIdentifier.objects.filter(value=value)
        if "config_obj" in config:
            queryset = queryset.filter(config=config["config_obj"])
        else:
            queryset = queryset.filter(config__external_id=config["id"])
        if obj:
            queryset = queryset.exclude(patient=obj)
        if config["config"]["unique"] and queryset.exists():
            err = f"Identifier config {config['config']['system']} is not unique"
            raise ValueError(err)
        if config["config"]["regex"] and not re.match(config["config"]["regex"], value):
            err = f"Identifier config {config['config']['system']} is not valid"
            raise ValueError(err)


def validate_date_of_birth(value):
    # v12.1 D04: 1 Jan 1900 <= date of birth <= today.
    if value and not (datetime.date(1900, 1, 1) <= value <= timezone.localdate()):
        raise ValueError("Date of birth must be between 1 Jan 1900 and today")
    return value


def approximate_date_of_birth(age):
    # v12.1 D05: an age without a date of birth is stored as 1 July, in the
    # year that gives that age today. Patient.meta["date_of_birth_approximate"]
    # marks it as an estimate.
    today = timezone.localdate()
    year = today.year - age - (1 if (today.month, today.day) < (7, 1) else 0)
    return datetime.date(year, 7, 1)


def set_date_of_birth(obj, date_of_birth, age):
    obj.meta.pop("age", None)
    if age is not None:
        obj.date_of_birth = approximate_date_of_birth(age)
        obj.meta["date_of_birth_approximate"] = True
    else:
        obj.date_of_birth = date_of_birth
        obj.meta.pop("date_of_birth_approximate", None)
    obj.year_of_birth = obj.date_of_birth.year


class PatientIdentifierConfigRequest(BaseModel):
    config: UUID4
    value: str


class PatientCreateSpec(ExtensionValidator, PatientBaseSpec):
    name: str = Field(max_length=settings.PATIENT_NAME_MAX_LENGTH)
    # Optional: intake records the district as free text in registration.district.
    geo_organization: UUID4 | None = None
    date_of_birth: datetime.date | None = None

    # v12.1 D05: age 0-120 when the date of birth is unknown.
    age: int | None = Field(default=None, ge=0, le=120)

    identifiers: list[PatientIdentifierConfigRequest] = []

    tags: list[UUID4] = []

    _validate_date_of_birth = field_validator("date_of_birth")(validate_date_of_birth)

    @field_validator("geo_organization")
    @classmethod
    def validate_geo_organization(cls, geo_organization):
        if geo_organization is None:
            return None
        if not Organization.objects.filter(
            org_type="govt", external_id=geo_organization
        ).exists():
            raise ValueError("Geo Organization does not exist")
        return geo_organization

    @model_validator(mode="after")
    def validate_birth(self):
        if self.date_of_birth is None and self.age is None:
            raise ValueError("Enter the date of birth or the age")
        return self

    @model_validator(mode="after")
    def validate_identifiers(self):
        instance_identifier_configs = PatientIdentifierConfigCache.get_instance_config()
        configs = {str(x.config): x for x in self.identifiers}
        for identifier_config in instance_identifier_configs:
            if str(identifier_config["id"]) in configs:
                value = configs[str(identifier_config["id"])].value
                if identifier_config["config"]["required"] and not value:
                    err = f"Identifier config {identifier_config['config']['system']} is required"
                    raise ValueError(err)
                validate_identifier_config(identifier_config, value)
        return self

    def perform_extra_deserialization(self, is_update, obj):
        if self.geo_organization:
            obj.geo_organization = Organization.objects.get(
                external_id=self.geo_organization
            )
        set_date_of_birth(obj, self.date_of_birth, self.age)
        obj._identifiers = self.identifiers  # noqa: SLF001
        obj._tags = self.tags  # noqa: SLF001
        if not self.pincode:
            obj.pincode = None


class PatientUpdateSpec(ExtensionValidator, PatientBaseSpec):
    name: str | None = Field(default=None, max_length=settings.PATIENT_NAME_MAX_LENGTH)
    gender: GenderChoices | None = None
    phone_number: PhoneNumber | None = Field(default=None, max_length=14)
    emergency_phone_number: PhoneNumber | None = Field(default=None, max_length=14)
    address: str | None = None
    permanent_address: str | None = None
    pincode: int | None = None
    blood_group: BloodGroupChoices | None = None
    date_of_birth: datetime.date | None = None
    age: int | None = Field(default=None, ge=0, le=120)
    geo_organization: UUID4 | None = None

    identifiers: list[PatientIdentifierConfigRequest] = []

    _validate_date_of_birth = field_validator("date_of_birth")(validate_date_of_birth)

    @field_validator("geo_organization")
    @classmethod
    def validate_geo_organization(cls, geo_organization):
        if geo_organization is None:
            return None
        if not Organization.objects.filter(
            org_type="govt", external_id=geo_organization
        ).exists():
            raise ValueError("Geo Organization does not exist")
        return geo_organization

    def perform_extra_deserialization(self, is_update, obj):
        if is_update:
            obj._identifiers = self.identifiers  # noqa: SLF001
            if self.geo_organization:
                obj.geo_organization = Organization.objects.get(
                    external_id=self.geo_organization
                )
            if self.age is not None or self.date_of_birth:
                set_date_of_birth(obj, self.date_of_birth, self.age)
        if not self.pincode:
            obj.pincode = None

    @field_validator("identifiers")
    @classmethod
    def validate_identifiers(cls, identifiers, info):
        instance_identifier_configs = PatientIdentifierConfigCache.get_instance_config()
        configs = {str(x.config): x for x in identifiers}
        for identifier_config in instance_identifier_configs:
            if str(identifier_config["id"]) in configs:
                value = configs[str(identifier_config["id"])].value
                if identifier_config["config"]["required"] and not value:
                    err = f"Identifier config {identifier_config['config']['system']} is required"
                    raise ValueError(err)
                validate_identifier_config(
                    identifier_config, value, info.context.get("object")
                )
        return identifiers


class PatientListSpec(ExtensionListRenderer, PatientBaseSpec):
    date_of_birth: datetime.date | None = None
    year_of_birth: datetime.date | None = None
    # Read from Patient.meta; set by set_date_of_birth, never by the client.
    date_of_birth_approximate: bool = False

    created_date: datetime.datetime
    modified_date: datetime.datetime

    instance_tags: list[dict] = []
    facility_tags: list[dict] = []

    @classmethod
    def perform_extra_serialization(cls, mapping, obj, *args, **kwargs):
        mapping["id"] = obj.external_id
        mapping["instance_tags"] = PatientInstanceTagManager().render_tags(obj)
        if kwargs.get("facility"):
            mapping["facility_tags"] = PatientFacilityTagManager(
                kwargs["facility"]
            ).render_tags(obj)
        super().perform_extra_serialization(mapping, obj, *args, **kwargs)
        run_patient_serialization_hooks(mapping, obj)


class PatientPartialSpec(EMRResource):
    __model__ = Patient

    id: UUID4 | None = None
    name: str
    gender: GenderChoices
    phone_number: str
    partial_id: str

    @classmethod
    def perform_extra_serialization(cls, mapping, obj):
        mapping["partial_id"] = str(obj.external_id)[:5]
        mapping["id"] = str(uuid.uuid4())
        run_patient_serialization_hooks(mapping, obj)


class PatientIdentifierResponse(BaseModel):
    config: PatientIdentifierListSpec
    value: str


class PatientRetrieveSpec(
    ExtensionRetrieveRenderer, PatientListSpec, PatientPermissionsMixin
):
    geo_organization: dict = {}

    created_by: dict | None = None
    updated_by: dict | None = None

    instance_identifiers: list[PatientIdentifierResponse] = []
    facility_identifiers: list[PatientIdentifierResponse] = []

    extensions: dict

    @classmethod
    def perform_extra_serialization(cls, mapping, obj, *args, **kwargs):
        from care.emr.resources.organization.spec import OrganizationReadSpec

        super().perform_extra_serialization(mapping, obj, *args, **kwargs)
        if obj.geo_organization:
            mapping["geo_organization"] = OrganizationReadSpec.serialize(
                obj.geo_organization
            ).to_json()
        cls.serialize_audit_users(mapping, obj)
        if obj.instance_identifiers:
            mapping["instance_identifiers"] = [
                {
                    "config": PatientIdentifierConfigCache.get_config(x["config"]),
                    "value": x["value"],
                }
                for x in obj.instance_identifiers
            ]
        if kwargs.get("facility"):
            facility = kwargs.get("facility")
            if facility and obj.facility_identifiers:
                mapping["facility_identifiers"] = [
                    {
                        "config": PatientIdentifierConfigCache.get_config(x["config"]),
                        "value": x["value"],
                    }
                    for x in obj.facility_identifiers.get(str(facility.id), [])
                ]
