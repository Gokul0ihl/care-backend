from datetime import date

from dateutil.relativedelta import relativedelta
from django.contrib.postgres.fields import ArrayField
from django.core.validators import MinValueValidator
from django.db import models
from django.template.defaultfilters import pluralize
from django.utils import timezone

from care.emr.models import EMRBaseModel
from care.emr.resources.base import model_from_cache
from care.users.models import User
from care.utils.models.validators import mobile_or_landline_number_validator


class Patient(EMRBaseModel):
    name = models.CharField(max_length=200, default="")
    gender = models.CharField(max_length=35, default="")

    phone_number = models.CharField(
        max_length=14, validators=[mobile_or_landline_number_validator], default=""
    )

    emergency_phone_number = models.CharField(
        max_length=14, validators=[mobile_or_landline_number_validator], default=""
    )
    address = models.TextField(default="")
    permanent_address = models.TextField(default="")

    pincode = models.IntegerField(default=0, blank=True, null=True)

    date_of_birth = models.DateField(default=None, null=True)
    year_of_birth = models.IntegerField(validators=[MinValueValidator(1900)], null=True)
    deceased_datetime = models.DateTimeField(default=None, null=True, blank=True)

    blood_group = models.CharField(max_length=16)

    geo_organization = models.ForeignKey(
        "emr.Organization", on_delete=models.SET_NULL, null=True, blank=True
    )

    organization_cache = ArrayField(models.IntegerField(), default=list)

    users_cache = ArrayField(models.IntegerField(), default=list)

    instance_identifiers = models.JSONField(default=list, null=True, blank=True)
    facility_identifiers = models.JSONField(default=dict, null=True, blank=True)

    instance_tags = ArrayField(models.IntegerField(), default=list)
    facility_tags = models.JSONField(default=dict, null=True, blank=True)

    extensions = models.JSONField(default=dict)

    # Front-desk registration (care_emergency's desk form). The same names as
    # the plug's former PatientRegistration columns; "" or null for patients
    # the desk never registered. registered_at is set only by the desk writes
    # and marks a desk registration. Field map: hmis_fe
    # docs/patient-registration.md.
    registration_facility = models.ForeignKey(
        "facility.Facility",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    registered_at = models.DateTimeField(null=True, blank=True)
    registered_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    registration_type = models.CharField(max_length=10, blank=True, default="")

    # Identity
    title = models.CharField(max_length=10, blank=True, default="")
    first_name = models.CharField(max_length=40, blank=True, default="")
    middle_name = models.CharField(max_length=40, blank=True, default="")
    last_name = models.CharField(max_length=40, blank=True, default="")
    marital_status = models.CharField(max_length=20, blank=True, default="")
    father_name = models.CharField(max_length=100, blank=True, default="")
    mother_name = models.CharField(max_length=100, blank=True, default="")
    guardian_name = models.CharField(max_length=40, blank=True, default="")
    guardian_relationship = models.CharField(max_length=10, blank=True, default="")
    occupation = models.CharField(max_length=50, blank=True, default="")
    religion = models.CharField(max_length=30, blank=True, default="")
    preferred_language = models.CharField(max_length=30, blank=True, default="")

    # Contact
    mobile_owner = models.CharField(max_length=20, blank=True, default="")
    whatsapp_number = models.CharField(max_length=16, blank=True, default="")
    whatsapp_owner = models.CharField(max_length=20, blank=True, default="")
    alternate_phone_number = models.CharField(max_length=16, blank=True, default="")
    email = models.CharField(max_length=254, blank=True, default="")
    house = models.CharField(max_length=100, blank=True, default="")
    street = models.CharField(max_length=150, blank=True, default="")
    locality = models.CharField(max_length=100, blank=True, default="")
    city = models.CharField(max_length=100, blank=True, default="")
    district = models.CharField(max_length=100, blank=True, default="")
    state = models.CharField(max_length=100, blank=True, default="")
    permanent_house = models.CharField(max_length=100, blank=True, default="")
    permanent_street = models.CharField(max_length=150, blank=True, default="")
    permanent_locality = models.CharField(max_length=100, blank=True, default="")
    permanent_city = models.CharField(max_length=100, blank=True, default="")
    permanent_district = models.CharField(max_length=100, blank=True, default="")
    permanent_state = models.CharField(max_length=100, blank=True, default="")
    permanent_pincode = models.CharField(max_length=12, blank=True, default="")
    permanent_country = models.CharField(max_length=60, blank=True, default="")
    next_of_kin_title = models.CharField(max_length=10, blank=True, default="")
    next_of_kin_name = models.CharField(max_length=40, blank=True, default="")
    next_of_kin_relationship = models.CharField(max_length=30, blank=True, default="")
    next_of_kin_phone = models.CharField(max_length=16, blank=True, default="")

    # Documentation. Only the ID type and its last 4 characters are kept.
    nationality_category = models.CharField(max_length=20, blank=True, default="")
    identity_storage_consent = models.BooleanField(default=False)
    identity_card_type = models.CharField(max_length=20, blank=True, default="")
    identity_number_last4 = models.CharField(max_length=4, blank=True, default="")
    abha_status = models.CharField(max_length=10, blank=True, default="")
    abha_number = models.CharField(
        max_length=14, blank=True, default="", db_index=True
    )
    abha_address = models.CharField(max_length=60, blank=True, default="")

    # Travel and residency (international patients)
    nationality = models.CharField(max_length=60, blank=True, default="")
    passport_number = models.CharField(max_length=15, blank=True, default="")
    passport_expiry = models.DateField(null=True, blank=True)
    visa_type = models.CharField(max_length=30, blank=True, default="")
    visa_expiry = models.DateField(null=True, blank=True)
    frro_c_form = models.CharField(max_length=50, blank=True, default="")
    address_in_india = models.CharField(max_length=300, blank=True, default="")
    interpreter_language = models.CharField(max_length=30, blank=True, default="")

    # Safety flags; allergies are care_emergency RegistrationAllergy rows.
    allergy_status = models.CharField(max_length=10, blank=True, default="")
    assistance_needs = ArrayField(
        models.CharField(max_length=30), default=list, blank=True
    )
    pregnancy_status = models.CharField(max_length=20, blank=True, default="")

    # VIP protocol (discreet handling only; no effect on price or care)
    is_vip = models.BooleanField(default=False)
    vip_note = models.CharField(max_length=200, blank=True, default="")
    vip_authorised_by = models.CharField(max_length=100, blank=True, default="")
    handle_with_care = models.BooleanField(default=False)
    vip_flagged_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    # Payer
    payer_type = models.CharField(max_length=20, blank=True, default="")
    payer_name = models.CharField(max_length=200, blank=True, default="")
    staff_category = models.CharField(max_length=20, blank=True, default="")
    employee_id = models.CharField(max_length=24, blank=True, default="")
    policy_number = models.CharField(max_length=24, blank=True, default="")
    policy_valid_till = models.DateField(null=True, blank=True)
    policy_holder_relationship = models.CharField(max_length=30, blank=True, default="")
    concession = models.CharField(max_length=60, blank=True, default="")
    concession_approved_by = models.CharField(max_length=100, blank=True, default="")

    # Source and referral
    arrival_source = models.CharField(max_length=40, blank=True, default="")
    lead_source = models.CharField(max_length=60, blank=True, default="")
    camp_name = models.CharField(max_length=100, blank=True, default="")
    camp_patient_id = models.CharField(max_length=30, blank=True, default="")
    referring_provider = models.CharField(max_length=200, blank=True, default="")
    referrer_registration_number = models.CharField(
        max_length=30, blank=True, default=""
    )
    referrer_organisation = models.CharField(max_length=200, blank=True, default="")
    remarks = models.TextField(blank=True, default="")

    # Consent and communication
    consent_given_by = models.CharField(max_length=20, blank=True, default="")
    consent_method = models.CharField(max_length=20, blank=True, default="")
    consent_witness = models.CharField(max_length=40, blank=True, default="")
    consent_language = models.CharField(max_length=30, blank=True, default="")
    general_consent = models.BooleanField(default=False)
    data_processing_consent = models.BooleanField(default=False)
    tariff_consent = models.BooleanField(default=False)
    # International patients: the international tariff and currency explained.
    international_tariff_consent = models.BooleanField(default=False)
    share_with_referrer_consent = models.BooleanField(default=False)
    abdm_link_consent = models.BooleanField(default=False)
    teleconsultation_consent = models.BooleanField(default=False)
    clinical_photography_consent = models.BooleanField(default=False)
    research_consent = models.BooleanField(default=False)
    communication_channels = ArrayField(
        models.CharField(max_length=20), default=list, blank=True
    )
    do_not_contact = models.BooleanField(default=False)
    promotions_consent = models.BooleanField(default=False)
    promotion_purposes = ArrayField(
        models.CharField(max_length=30), default=list, blank=True
    )

    # Quick Register (care_emergency): a patient registered by phone, with what
    # the desk took on the call. The name parts are the desk columns above
    # (title, first_name, last_name). The rest of the registration form is
    # texted as a one-time link (quick_form_token); the answers wait in
    # quick_form_details until the desk completes the registration.
    # quick_registered_at marks a quick registration. Field map: hmis_fe
    # docs/patient-registration.md.
    quick_registered_at = models.DateTimeField(null=True, blank=True)
    quick_registered_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    quick_facility = models.ForeignKey(
        "facility.Facility",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    quick_consultation_type = models.CharField(max_length=20, blank=True, default="")
    quick_department = models.ForeignKey(
        "emr.FacilityOrganization",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    quick_doctor = models.ForeignKey(
        "users.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    quick_whatsapp_same_as_mobile = models.BooleanField(default=False)
    quick_send_form_link = models.BooleanField(default=False)
    quick_send_payment_reminder = models.BooleanField(default=False)
    quick_form_token = models.CharField(
        max_length=64, unique=True, null=True, blank=True
    )
    quick_form_expires_at = models.DateTimeField(null=True, blank=True)
    quick_form_link_sent_at = models.DateTimeField(null=True, blank=True)
    quick_form_submitted_at = models.DateTimeField(null=True, blank=True)
    quick_form_details = models.JSONField(default=dict, blank=True)

    def get_age(self) -> str:
        start = self.date_of_birth or date(self.year_of_birth, 1, 1)
        end = (self.deceased_datetime or timezone.now()).date()

        delta = relativedelta(end, start)

        if delta.years > 0:
            year_str = f"{delta.years} year{pluralize(delta.years)}"
            return f"{year_str}"

        if delta.months > 0:
            month_str = f"{delta.months} month{pluralize(delta.months)}"
            day_str = (
                f" {delta.days} day{pluralize(delta.days)}" if delta.days > 0 else ""
            )
            return f"{month_str}{day_str}"

        if delta.days > 0:
            return f"{delta.days} day{pluralize(delta.days)}"

        return "0 days"

    @property
    def age(self) -> int:
        start = self.date_of_birth or date(self.year_of_birth, 1, 1)
        end = (self.deceased_datetime or timezone.now()).date()
        delta = relativedelta(end, start)
        return delta.years

    def rebuild_organization_cache(self):
        organization_parents = []
        if self.geo_organization:
            organization_parents.extend(self.geo_organization.parent_cache)
            organization_parents.append(self.geo_organization.id)
        if self.id:
            for patient_organization in PatientOrganization.objects.filter(
                patient_id=self.id
            ):
                organization_parents.extend(
                    patient_organization.organization.parent_cache
                )
                organization_parents.append(patient_organization.id)

        self.organization_cache = list(set(organization_parents))

    def rebuild_users_cache(self):
        if self.id:
            users = list(
                PatientUser.objects.filter(patient=self).values_list(
                    "user_id", flat=True
                )
            )
            self.users_cache = users

    def build_instance_identifiers(self):
        self.instance_identifiers = [
            {"config": str(x.config.external_id), "value": x.value}
            for x in PatientIdentifier.objects.filter(
                patient=self, config__facility__isnull=True
            )
        ]

    def build_facility_identifiers(self, facility_id):
        if not self.facility_identifiers:
            self.facility_identifiers = {}
        self.facility_identifiers[facility_id] = [
            {"config": str(x.config.external_id), "value": x.value}
            for x in PatientIdentifier.objects.filter(
                patient=self, config__facility_id=facility_id
            )
        ]

    def save(self, *args, **kwargs) -> None:
        if self.date_of_birth and not self.year_of_birth:
            self.year_of_birth = self.date_of_birth.year
        super().save(*args, **kwargs)
        self.rebuild_organization_cache()
        self.rebuild_users_cache()
        super().save(update_fields=["organization_cache", "users_cache"])

    def __str__(self):
        return self.name


class PatientOrganization(EMRBaseModel):
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE)
    organization = models.ForeignKey("emr.Organization", on_delete=models.CASCADE)
    # TODO : Add Role here to deny certain permissions for certain organizations

    def save(self, *args, **kwargs) -> None:
        super().save(*args, **kwargs)
        self.patient.save()


class PatientUser(EMRBaseModel):
    """
    Add a user that can access the patient
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE)
    role = models.ForeignKey("security.RoleModel", on_delete=models.PROTECT)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.patient.save()


class PatientIdentifierConfig(EMRBaseModel):
    status = models.CharField(max_length=255)
    facility = models.ForeignKey(
        "facility.Facility", on_delete=models.CASCADE, null=True, blank=True
    )
    config = models.JSONField(default=dict, null=True, blank=True)


class PatientIdentifier(EMRBaseModel):
    facility = models.ForeignKey(
        "facility.Facility", on_delete=models.CASCADE, null=True, blank=True
    )
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE)
    config = models.ForeignKey(PatientIdentifierConfig, on_delete=models.CASCADE)
    value = models.CharField(max_length=1024, db_index=True)


class PatientIdentifierConfigCache:
    """
    Configs can be cached because it changes very rarely,
    Redis based alternative for this class can be implemented later if needed.
    """

    # TODO : Switch to Redis
    configs = {}
    instance_configs = None
    facility_configs = {}

    @classmethod
    def get_config(cls, config_id) -> PatientIdentifierConfig:
        from care.emr.resources.patient_identifier.spec import PatientIdentifierListSpec

        if config_id not in cls.configs:
            cls.configs[config_id] = PatientIdentifierListSpec.serialize(
                PatientIdentifierConfig.objects.get(external_id=config_id)
            ).to_json()
        return cls.configs[config_id]

    @classmethod
    def clear_cache(cls, config_id: int | None = None):
        if config_id is None:
            cls.configs = {}
        else:
            cls.configs.pop(config_id, None)

    @classmethod
    def get_instance_config(cls) -> list[dict]:
        from care.emr.resources.patient_identifier.spec import (
            PatientIdentifierListSpec,
            PatientIdentifierStatus,
        )

        return [
            model_from_cache(PatientIdentifierListSpec, id=x.id)
            for x in PatientIdentifierConfig.objects.filter(
                facility__isnull=True, status=PatientIdentifierStatus.active.value
            ).only("id")
        ]

    @classmethod
    def get_facility_config(cls, facility_id):
        from care.emr.resources.patient_identifier.spec import (
            PatientIdentifierListSpec,
            PatientIdentifierStatus,
        )

        return [
            model_from_cache(PatientIdentifierListSpec, id=x.id)
            for x in PatientIdentifierConfig.objects.filter(
                facility_id=facility_id, status=PatientIdentifierStatus.active.value
            ).only("id")
        ]
