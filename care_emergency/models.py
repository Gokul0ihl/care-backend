from care.emr.models.base import EMRBaseModel
from django.db import models
from django.db.models import Q

from care_emergency.constants import (
    CaptureMethods,
    ConsultationTypes,
    DocumentCategories,
    DocumentSides,
    StatutoryCategories,
    TriageChoices,
)


class EmergencyDesk(EMRBaseModel):
    """The facility-owned number used as phone_number for emergency patients."""

    facility = models.OneToOneField(
        "facility.Facility", on_delete=models.PROTECT, related_name="emergency_desk"
    )
    phone_number = models.CharField(max_length=14)


class EmergencyEncounter(EMRBaseModel):
    encounter = models.OneToOneField(
        "emr.Encounter", on_delete=models.PROTECT, related_name="emergency"
    )
    patient = models.ForeignKey(
        "emr.Patient", on_delete=models.PROTECT, related_name="emergency_encounters"
    )
    facility = models.ForeignKey(
        "facility.Facility", on_delete=models.PROTECT, related_name="+"
    )
    triage = models.CharField(max_length=10, choices=TriageChoices.choices)
    # Nullable so an update can set it back to false (Care drops default values).
    is_medico_legal = models.BooleanField(null=True)
    mlc_nature = models.CharField(max_length=50, blank=True, default="")
    police_station = models.CharField(max_length=200, blank=True, default="")
    mlc_number = models.CharField(max_length=30, null=True, blank=True)
    distinguishing_marks = models.TextField(blank=True, default="")
    chief_complaint = models.CharField(max_length=40, blank=True, default="")
    mode_of_arrival = models.CharField(max_length=40, blank=True, default="")
    brought_by_name = models.CharField(max_length=200, blank=True, default="")
    brought_by_phone = models.CharField(max_length=14, blank=True, default="")
    brought_by_relationship = models.CharField(max_length=50, blank=True, default="")
    is_unidentified = models.BooleanField(default=True)
    converted_at = models.DateTimeField(null=True, blank=True)
    converted_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["facility", "mlc_number"],
                condition=Q(mlc_number__isnull=False),
                name="care_emergency_unique_mlc_number",
            )
        ]


class MlcSequence(models.Model):
    """One counter row per facility per year, incremented under a row lock."""

    facility = models.ForeignKey(
        "facility.Facility", on_delete=models.PROTECT, related_name="+"
    )
    year = models.PositiveIntegerField()
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["facility", "year"], name="care_emergency_unique_mlc_sequence"
            )
        ]

    def __str__(self):
        return f"{self.facility_id}/{self.year}: {self.last_value}"


class RegistrationAllergy(models.Model):
    """An allergy recorded at the desk; the patient's other desk details are
    columns of Care's Patient."""

    patient = models.ForeignKey(
        "emr.Patient", on_delete=models.CASCADE, related_name="desk_allergies"
    )
    substance = models.CharField(max_length=100)
    reaction = models.CharField(max_length=100, blank=True, default="")
    severity = models.CharField(max_length=20)

    def __str__(self):
        return self.substance


class FacilityUnit(EMRBaseModel):
    """The facility's unit code (LN, KB, DW), printed in its patient numbers."""

    facility = models.OneToOneField(
        "facility.Facility", on_delete=models.PROTECT, related_name="unit"
    )
    code = models.CharField(max_length=4, unique=True)


class PatientNumberSequence(models.Model):
    """One counter row per series, unit and period, incremented under a row
    lock. period is the day for daily series (UNK), the year for billing
    documents (INV, RCT, ADV, CN, RFD) and "" otherwise. Counters only go up,
    so a number is never issued twice."""

    series = models.CharField(max_length=3)
    facility = models.ForeignKey(
        "facility.Facility", on_delete=models.PROTECT, related_name="+"
    )
    period = models.CharField(max_length=8, blank=True, default="")
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["series", "facility", "period"],
                name="care_emergency_unique_patient_number_sequence",
            )
        ]

    def __str__(self):
        return f"{self.series}/{self.facility_id}/{self.period}: {self.last_value}"


class PatientDocument(EMRBaseModel):
    """A document captured at the desk (D37, D43). The file is a Care
    FileUpload of the patient. Until the registration that uses it is saved,
    patient is null and the file belongs to nobody (staged)."""

    file = models.OneToOneField(
        "emr.FileUpload", on_delete=models.PROTECT, related_name="desk_document"
    )
    patient = models.ForeignKey(
        "emr.Patient",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="desk_documents",
    )
    category = models.CharField(max_length=30, choices=DocumentCategories.choices)
    side = models.CharField(max_length=10, choices=DocumentSides.choices)
    capture_method = models.CharField(max_length=20, choices=CaptureMethods.choices)
    # Where the document came from (e.g. the issuing hospital) and its date.
    source = models.CharField(max_length=100, blank=True, default="")
    document_date = models.DateField(null=True, blank=True)


class ArtCouple(EMRBaseModel):
    """An IVF / ART couple registered together (D45). The woman is the
    registered patient; the partner gets their own Care patient. A single
    woman has no partner and needs no marriage certificate."""

    woman = models.OneToOneField(
        "emr.Patient", on_delete=models.PROTECT, related_name="art_couple"
    )
    partner = models.OneToOneField(
        "emr.Patient",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="art_partner_of",
    )
    facility = models.ForeignKey(
        "facility.Facility",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    single_woman = models.BooleanField(default=False)
    art_consent = models.BooleanField(default=False)
    # The partner's ID summary, kept like the patient's: type and last 4 only.
    partner_identity_card_type = models.CharField(max_length=20, blank=True, default="")
    partner_identity_number_last4 = models.CharField(
        max_length=4, blank=True, default=""
    )
    partner_abha_number = models.CharField(max_length=14, blank=True, default="")
    partner_abha_address = models.CharField(max_length=60, blank=True, default="")


class NewbornBirth(EMRBaseModel):
    """A baby born at the facility, registered as their own patient with
    their own UHID and linked to the mother by relationship, never by
    identifier. Twins and triplets are one row each, sharing the delivery
    details and numbered by birth_order."""

    baby = models.OneToOneField(
        "emr.Patient", on_delete=models.PROTECT, related_name="newborn_birth"
    )
    mother = models.ForeignKey(
        "emr.Patient", on_delete=models.PROTECT, related_name="newborn_babies"
    )
    facility = models.ForeignKey(
        "facility.Facility", on_delete=models.PROTECT, related_name="+"
    )
    born_at = models.DateTimeField()
    birth_order = models.PositiveSmallIntegerField(default=1)
    number_of_babies = models.PositiveSmallIntegerField(default=1)
    delivery_type = models.CharField(max_length=30)
    gestation_weeks = models.PositiveSmallIntegerField()
    birth_weight_grams = models.PositiveSmallIntegerField()
    length_cm = models.DecimalField(
        max_digits=4, decimal_places=1, null=True, blank=True
    )
    head_circumference_cm = models.DecimalField(
        max_digits=4, decimal_places=1, null=True, blank=True
    )
    apgar_1_min = models.PositiveSmallIntegerField(null=True, blank=True)
    apgar_5_min = models.PositiveSmallIntegerField(null=True, blank=True)


class RestrictedPatient(models.Model):
    """A patient under statutory confidentiality (D30). Created with the first
    flag; from then on the patient's identity is masked behind the alias for
    everyone except the flagging clinicians and break-glass access."""

    patient = models.OneToOneField(
        "emr.Patient", on_delete=models.PROTECT, related_name="restriction"
    )
    alias = models.CharField(max_length=9, unique=True)
    created_date = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.alias


class StatutoryFlag(EMRBaseModel):
    """One statutory flag, raised by a clinician in an encounter. The category
    is never shown to the front desk."""

    patient = models.ForeignKey(
        "emr.Patient", on_delete=models.PROTECT, related_name="statutory_flags"
    )
    encounter = models.ForeignKey(
        "emr.Encounter", on_delete=models.PROTECT, related_name="+"
    )
    category = models.CharField(max_length=20, choices=StatutoryCategories.choices)


class BreakGlassAccess(models.Model):
    """An audited, time-limited unmasking of a restricted patient for one
    user. Rows are never edited or deleted."""

    patient = models.ForeignKey(
        "emr.Patient", on_delete=models.PROTECT, related_name="+"
    )
    user = models.ForeignKey("users.User", on_delete=models.PROTECT, related_name="+")
    reason = models.CharField(max_length=500)
    created_date = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    def __str__(self):
        return f"{self.user_id} → {self.patient_id} until {self.expires_at}"


class PostOffice(models.Model):
    """One row of India Post's PIN directory (D17, M01). The whole table is
    replaced on each load, so loaded_at is when the directory was refreshed."""

    pincode = models.CharField(max_length=6, db_index=True)
    name = models.CharField(max_length=100)
    office_type = models.CharField(max_length=10, blank=True, default="")
    delivery = models.BooleanField(default=True)
    district = models.CharField(max_length=100)
    state = models.CharField(max_length=100)
    loaded_at = models.DateTimeField()

    def __str__(self):
        return f"{self.pincode} {self.name}"


class QuickRegistration(EMRBaseModel):
    """Marks a patient registered by phone through Quick Register, with what
    the desk took on the call. The rest of the registration form is texted to
    the patient as a one-time link; their answers wait in form_details until
    the desk completes the registration."""

    patient = models.OneToOneField(
        "emr.Patient", on_delete=models.PROTECT, related_name="quick_registration"
    )
    facility = models.ForeignKey(
        "facility.Facility", on_delete=models.PROTECT, related_name="+"
    )
    title = models.CharField(max_length=10)
    first_name = models.CharField(max_length=40)
    last_name = models.CharField(max_length=40, blank=True, default="")
    consultation_type = models.CharField(
        max_length=20, choices=ConsultationTypes.choices
    )
    department = models.ForeignKey(
        "emr.FacilityOrganization",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    doctor = models.ForeignKey(
        "users.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    whatsapp_same_as_mobile = models.BooleanField(default=True)
    send_form_link = models.BooleanField(default=True)
    send_payment_reminder = models.BooleanField(default=True)

    form_token = models.CharField(max_length=64, unique=True)
    form_expires_at = models.DateTimeField()
    form_link_sent_at = models.DateTimeField(null=True, blank=True)
    form_submitted_at = models.DateTimeField(null=True, blank=True)
    form_details = models.JSONField(default=dict, blank=True)


class PatientMerge(EMRBaseModel):
    """One merge of a duplicate patient (source) into the record kept
    (target), for audit. The source is soft-deleted, and source_snapshot holds
    what is needed to reverse the merge by hand. Rows are never edited."""

    source = models.ForeignKey(
        "emr.Patient", on_delete=models.PROTECT, related_name="+"
    )
    target = models.ForeignKey(
        "emr.Patient", on_delete=models.PROTECT, related_name="merged_from"
    )
    reason = models.CharField(max_length=500)
    # Field key -> "source" | "target", for the fields that conflicted.
    choices = models.JSONField(default=dict)
    source_snapshot = models.JSONField(default=dict)
    # Label -> number of the source's records moved to the target.
    moved = models.JSONField(default=dict)


class LabTestRule(EMRBaseModel):
    """POH's per-test rules for a lab test (an ActivityDefinition): how long the
    patient fasts before the sample, and whether the report is sensitive, so it's
    printed, sealed and handed over in person only (FO-19, D53)."""

    activity_definition = models.OneToOneField(
        "emr.ActivityDefinition", on_delete=models.CASCADE, related_name="poh_lab_rule"
    )
    fasting_hours = models.PositiveSmallIntegerField(null=True, blank=True)
    sensitive_report = models.BooleanField(default=False)


class ApiKey(EMRBaseModel):
    """A long-lived key a client such as the patient mobile app sends instead of
    logging in. Requests made with it act as `user`, so they get exactly that
    user's roles. Only the key's SHA-256 is kept; the key itself is shown once,
    when it is created. Settings → API keys (superusers) creates and revokes them."""

    name = models.CharField(max_length=100)
    user = models.ForeignKey(
        "users.User", on_delete=models.CASCADE, related_name="poh_api_keys"
    )
    # The start of the key, so a key in a client's config can be matched to its row.
    prefix = models.CharField(max_length=16)
    key_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)


class AppPatientLink(EMRBaseModel):
    """The patient app's own id for a person and the Care patient it stands
    for. Each app profile, family members included, has its own row. The app
    server can only see and book for patients linked this way."""

    app_user_id = models.CharField(max_length=64, unique=True)
    patient = models.ForeignKey(
        "emr.Patient", on_delete=models.PROTECT, related_name="app_links"
    )
    # Linked to a patient who already existed (same ABHA number) rather than
    # created by the app. The app doesn't overwrite such a patient's details.
    matched = models.BooleanField(default=False)
