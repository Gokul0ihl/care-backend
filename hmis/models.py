from django.contrib.auth.hashers import make_password
from django.db import models

from care.emr.models.base import EMRBaseModel
from hmis.constants import (
    CaptureMethods,
    DocumentCategories,
    DocumentSides,
    StatutoryCategories,
)


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
                fields=["facility", "year"], name="hmis_unique_mlc_sequence"
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
                name="hmis_unique_patient_number_sequence",
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
    # The signed consent to merge, filed on the kept record. Null only for
    # merges made before consent was required.
    consent_document = models.ForeignKey(
        "hmis.PatientDocument",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )


class LabTestRule(EMRBaseModel):
    """POH's per-test rules for a lab test (an ActivityDefinition): how long the
    patient fasts before the sample, whether the sample is taken after a meal
    (post-prandial, e.g. PPBS), whether the report is sensitive, so it's
    printed, sealed and handed over in person only (FO-19, D53), and the icon the
    front desk sees on the test (a key from hmis_fe's lab icon set; blank picks one
    from the test's code or sample). A test needs fasting or an after-meal sample,
    never both."""

    activity_definition = models.OneToOneField(
        "emr.ActivityDefinition", on_delete=models.CASCADE, related_name="poh_lab_rule"
    )
    fasting_hours = models.PositiveSmallIntegerField(null=True, blank=True)
    after_meal = models.BooleanField(default=False)
    sensitive_report = models.BooleanField(default=False)
    icon = models.CharField(max_length=40, blank=True, default="")


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


def unusable_patient_password():
    return make_password(None)


class MobileRegistration(models.Model):
    """Latest expiring OTP proof per phone; token plaintext never persists."""

    phone_number = models.CharField(max_length=16, unique=True)
    token_digest = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Mobile registration {self.pk}"


class PatientAccount(models.Model):
    """A Care patient as the patient mobile app sees them. The app expects a
    numeric id for every person, and Care's own Patient.id isn't exposed, so
    the app's user id is this row's id. Created the first time someone signs
    in to the app as that patient. The phone number stays on the patient, so
    changing it at the desk changes who can sign in."""

    patient = models.OneToOneField(
        "emr.Patient", on_delete=models.PROTECT, related_name="mobile_account"
    )
    password = models.CharField(max_length=128, default=unusable_patient_password)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"App account {self.id}"


class PatientSession(models.Model):
    """One signed-in app profile on one device: the refresh token chain behind
    a PatientAccessToken / PatientRefreshToken pair (hmis/mobile_auth.py).
    Revoking it ends every token in the chain."""

    account = models.ForeignKey(
        PatientAccount, on_delete=models.CASCADE, related_name="sessions"
    )
    # The number the OTP was verified on. If the patient's number changes at
    # the desk, the session stops working.
    phone_number = models.CharField(max_length=16)
    # jti of the one refresh token that may be used next. The one before it is
    # kept for a short grace period, for a refresh whose response was lost.
    refresh_jti = models.CharField(max_length=64, blank=True, default="")
    previous_refresh_jti = models.CharField(max_length=64, blank=True, default="")
    rotated_at = models.DateTimeField(null=True, blank=True)
    fcm_token = models.CharField(max_length=512, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"App session {self.id} of account {self.account_id}"


class MobileAppointmentSubmission(models.Model):
    """Durable idempotency receipt; the appointment itself lives in TokenBooking."""

    account = models.ForeignKey(PatientAccount, on_delete=models.PROTECT)
    request_id = models.UUIDField()
    slot_id = models.UUIDField()
    patient_notes = models.CharField(max_length=500, blank=True, default="")
    booking = models.OneToOneField(
        "emr.TokenBooking", on_delete=models.PROTECT, related_name="mobile_submission"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "request_id"],
                name="unique_mobile_appointment_request",
            )
        ]

    def __str__(self):
        return f"Mobile appointment submission {self.request_id}"
