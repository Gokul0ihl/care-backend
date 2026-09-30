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
    marital_status: Literal[
        "", "single", "married", "divorced", "widowed", "separated"
    ] = ""
    father_name: str = Field(default="", max_length=100)
    mother_name: str = Field(default="", max_length=100)
    occupation: str = Field(default="", max_length=100)
    country: str = Field(default="", max_length=100)
    state: str = Field(default="", max_length=100)
    locality: str = Field(default="", max_length=100)
    district: str = Field(default="", max_length=100)
    emergency_prefix: str = Field(default="", max_length=20)
    emergency_first_name: str = Field(default="", max_length=75)
    emergency_middle_name: str = Field(default="", max_length=75)
    emergency_last_name: str = Field(default="", max_length=75)
    emergency_relationship: str = Field(default="", max_length=50)
    company: str = Field(default="", max_length=200)
    payer: str = Field(default="", max_length=200)
    lead_source: str = Field(default="", max_length=100)
    referral_type: str = Field(default="", max_length=100)
    referred_by: str = Field(default="", max_length=200)
    # "other" is no longer offered but stays valid for patients registered before it was removed.
    identity_card_type: Literal[
        "", "aadhaar", "abha", "voter_id", "driving_license", "pan", "ration_card", "passport", "other"
    ] = ""
    identity_number_last4: str = Field(default="", pattern=r"^(?:[A-Za-z0-9]{4})?$")
    abha_number: str = Field(default="", pattern=r"^(?:\d{14})?$")
    udid: str = Field(default="", max_length=100)
    identity_storage_consent: bool = False
    identity_verified_physically: bool = False
    terms_accepted: bool = False
    promotions_consent: bool = False

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


class PatientIdentifierConfigRequest(BaseModel):
    config: UUID4
    value: str


class PatientCreateSpec(ExtensionValidator, PatientBaseSpec):
    name: str = Field(max_length=settings.PATIENT_NAME_MAX_LENGTH)
    # Optional: intake records the district as free text in registration.district.
    geo_organization: UUID4 | None = None
    date_of_birth: datetime.date | None = None

    age: int | None = None

    identifiers: list[PatientIdentifierConfigRequest] = []

    tags: list[UUID4] = []

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
        if self.age:
            # override dob if user chooses to update age
            obj.date_of_birth = None
            obj.year_of_birth = timezone.now().date().year - self.age
        else:
            obj.year_of_birth = self.date_of_birth.year
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
    age: int | None = None
    geo_organization: UUID4 | None = None

    identifiers: list[PatientIdentifierConfigRequest] = []

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
            if self.age is not None:
                obj.date_of_birth = None
                obj.year_of_birth = timezone.now().year - self.age
            elif self.date_of_birth:
                obj.year_of_birth = self.date_of_birth.year
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
