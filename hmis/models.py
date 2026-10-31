from uuid import uuid4

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

    series = models.CharField(max_length=4)
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


# --- Outreach events and camp capture (docs/outreach-camps.md) ---


class OutreachEvent(EMRBaseModel):
    """A health camp, patient education session, CME or webinar. Created as a
    draft, sent for approval and approved by a facility admin. People can only
    be captured at an approved CAMP or PES event."""

    number = models.CharField(max_length=24, unique=True, null=True, blank=True)
    event_type = models.CharField(max_length=4)
    facility = models.ForeignKey(
        "facility.Facility", on_delete=models.PROTECT, related_name="+"
    )
    name = models.CharField(max_length=120)
    theme = models.CharField(max_length=120, blank=True, default="")
    description = models.TextField(blank=True, default="")
    venue_name = models.CharField(max_length=120, blank=True, default="")
    address = models.CharField(max_length=255, blank=True, default="")
    pincode = models.CharField(max_length=6, blank=True, default="")
    platform = models.CharField(max_length=40, blank=True, default="")
    join_link = models.URLField(max_length=500, blank=True, default="")
    event_date = models.DateField()
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    expected_attendees = models.PositiveIntegerField(null=True, blank=True)
    has_coupon = models.BooleanField(default=False)
    coupon_code = models.CharField(max_length=12, blank=True, default="")
    coupon_valid_from = models.DateField(null=True, blank=True)
    coupon_valid_to = models.DateField(null=True, blank=True)
    discount_registration_pct = models.PositiveSmallIntegerField(null=True, blank=True)
    discount_consultation_pct = models.PositiveSmallIntegerField(null=True, blank=True)
    discount_tests_pct = models.PositiveSmallIntegerField(null=True, blank=True)
    report_delivery = models.CharField(max_length=20, blank=True, default="")
    permission_needed = models.BooleanField(default=False)
    permission_from = models.CharField(max_length=120, blank=True, default="")
    permission_cost = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    cme_audience = models.CharField(max_length=120, blank=True, default="")
    cme_accreditation_body = models.CharField(max_length=60, blank=True, default="")
    cme_credit_hours = models.DecimalField(
        max_digits=4, decimal_places=1, null=True, blank=True
    )
    status = models.CharField(max_length=20, default="draft")
    version = models.PositiveIntegerField(default=1)
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        "users.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        "users.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    decision_remarks = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["coupon_code"],
                condition=models.Q(has_coupon=True, deleted=False)
                & ~models.Q(status="cancelled"),
                name="hmis_unique_outreach_coupon",
            )
        ]

    def __str__(self):
        return self.number or f"{self.event_type} draft {self.external_id}"


class OutreachEventDecision(models.Model):
    """Every approval or rejection, per version."""

    event = models.ForeignKey(
        OutreachEvent, on_delete=models.CASCADE, related_name="decisions"
    )
    version = models.PositiveIntegerField()
    decision = models.CharField(max_length=10)
    remarks = models.TextField(blank=True, default="")
    decided_by = models.ForeignKey(
        "users.User", on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.event_id} v{self.version}: {self.decision}"


class OutreachEventTest(models.Model):
    """A test offered at the event. lab_test is null for point-of-care checks
    with no lab report (glucometer RBS, BP)."""

    event = models.ForeignKey(
        OutreachEvent, on_delete=models.CASCADE, related_name="tests"
    )
    external_id = models.UUIDField(default=uuid4, unique=True)
    name = models.CharField(max_length=120)
    lab_test = models.ForeignKey(
        "emr.ActivityDefinition",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    location = models.CharField(max_length=10)
    fee_type = models.CharField(max_length=4)
    rate = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    cost_to_poh = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    quantity_offered = models.PositiveIntegerField(null=True, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.name


class OutreachEventStaff(models.Model):
    """Doctors / speakers, nurses, attendants and promoters."""

    event = models.ForeignKey(
        OutreachEvent, on_delete=models.CASCADE, related_name="staff"
    )
    role = models.CharField(max_length=10)
    user = models.ForeignKey(
        "users.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    name = models.CharField(max_length=80)
    department = models.CharField(max_length=80, blank=True, default="")
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.role}: {self.name}"


class OutreachEventCostLine(models.Model):
    """Equipment, consumables, promotional material and other costs."""

    event = models.ForeignKey(
        OutreachEvent, on_delete=models.CASCADE, related_name="cost_lines"
    )
    category = models.CharField(max_length=12)
    item_name = models.CharField(max_length=120)
    unit = models.CharField(max_length=20, blank=True, default="")
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.item_name


class OutreachFormField(models.Model):
    """An extra question the event's creator added to the camp capture form,
    after the fixed fields. key stays the same when the label is renamed."""

    event = models.ForeignKey(
        OutreachEvent, on_delete=models.CASCADE, related_name="form_fields"
    )
    key = models.SlugField(max_length=40)
    section = models.CharField(max_length=80, blank=True, default="")
    label = models.CharField(max_length=200)
    help_text = models.CharField(max_length=300, blank=True, default="")
    field_type = models.CharField(max_length=20)
    required = models.BooleanField(default=False)
    options = models.JSONField(default=list, blank=True)
    validation = models.JSONField(default=dict, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["event", "key"], name="hmis_unique_outreach_form_key"
            )
        ]

    def __str__(self):
        return self.label


class OutreachFormTemplate(EMRBaseModel):
    """A saved set of extra questions to reuse on other events."""

    facility = models.ForeignKey(
        "facility.Facility", on_delete=models.PROTECT, related_name="+"
    )
    name = models.CharField(max_length=80)
    fields = models.JSONField(default=list)

    def __str__(self):
        return self.name


class CampSession(models.Model):
    """A user capturing at one event, from choosing the camp to leaving it."""

    external_id = models.UUIDField(default=uuid4, unique=True)
    user = models.ForeignKey("users.User", on_delete=models.PROTECT, related_name="+")
    event = models.ForeignKey(
        OutreachEvent, on_delete=models.PROTECT, related_name="camp_sessions"
    )
    device_label = models.CharField(max_length=80, blank=True, default="")
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Camp session {self.external_id}"


class CampParticipant(EMRBaseModel):
    """Someone captured at a camp. number (CMP-<unit>-<YYMM>-<NNNN>) is a camp
    record, not a UHID; an existing patient is linked through patient."""

    number = models.CharField(max_length=24, unique=True)
    event = models.ForeignKey(
        OutreachEvent, on_delete=models.PROTECT, related_name="participants"
    )
    session = models.ForeignKey(
        CampSession,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="participants",
    )
    patient = models.ForeignKey(
        "emr.Patient",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="camp_visits",
    )
    kind = models.CharField(max_length=10)
    first_name = models.CharField(max_length=40)
    last_name = models.CharField(max_length=40, blank=True, default="")
    gender = models.CharField(max_length=10)
    date_of_birth = models.DateField()
    mobile_isd = models.CharField(max_length=5, default="+91")
    mobile = models.CharField(max_length=12)
    mobile_belongs_to = models.CharField(max_length=10)
    pincode = models.CharField(max_length=6)
    area = models.CharField(max_length=80, blank=True, default="")
    id_type = models.CharField(max_length=20, blank=True, default="")
    # Aadhaar is stored masked (XXXX XXXX 1234).
    id_number = models.CharField(max_length=30, blank=True, default="")
    bp = models.CharField(max_length=7, blank=True, default="")
    rbs_mg_dl = models.PositiveSmallIntegerField(null=True, blank=True)
    weight_kg = models.DecimalField(
        max_digits=5, decimal_places=1, null=True, blank=True
    )
    finding = models.TextField()
    refer_to_department = models.CharField(max_length=80, blank=True, default="")
    consent_screening = models.BooleanField(default=False)
    consent_dpdp_notice = models.BooleanField(default=False)
    consent_channel = models.CharField(max_length=30, blank=True, default="")
    send_via = models.JSONField(default=list, blank=True)
    # {key: {"label", "type", "value"}}: the label and type as asked, so old
    # answers stay readable after the form changes.
    extra_answers = models.JSONField(default=dict, blank=True)
    form_version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=12, default="open")
    verification_code = models.CharField(max_length=8)
    captured_by = models.ForeignKey(
        "users.User", on_delete=models.PROTECT, related_name="+"
    )
    captured_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.number


class CampParticipantTest(models.Model):
    participant = models.ForeignKey(
        CampParticipant, on_delete=models.CASCADE, related_name="tests"
    )
    event_test = models.ForeignKey(
        OutreachEventTest,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    lab_test = models.ForeignKey(
        "emr.ActivityDefinition",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    name = models.CharField(max_length=120)
    fee_type = models.CharField(max_length=4)
    rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    source = models.CharField(max_length=20)
    payment_status = models.CharField(max_length=6)
    report_status = models.CharField(max_length=20)

    def __str__(self):
        return self.name
