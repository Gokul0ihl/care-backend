from django.contrib.postgres.fields import ArrayField
from django.db import models

from care.emr.models import EMRBaseModel
from care.emr.models.scheduling.schedule import Availability, SchedulableResource
from care.users.models import User


class TokenSlot(EMRBaseModel):
    resource = models.ForeignKey(
        SchedulableResource, on_delete=models.CASCADE, null=False, blank=False
    )
    availability = models.ForeignKey(
        Availability, on_delete=models.CASCADE, null=True, blank=True
    )
    start_datetime = models.DateTimeField(null=False, blank=False)
    end_datetime = models.DateTimeField(null=False, blank=False)
    allocated = models.IntegerField(null=False, blank=False, default=0)
    # TODO propogate facility to this level or at the booking level to avoid joins


class TokenBooking(EMRBaseModel):
    token_slot = models.ForeignKey(
        TokenSlot, on_delete=models.PROTECT, null=False, blank=False
    )
    patient = models.ForeignKey(
        "emr.Patient",
        on_delete=models.CASCADE,
        null=False,
        blank=False,
    )
    booked_on = models.DateTimeField(auto_now_add=True)
    booked_by = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    status = models.CharField()
    note = models.TextField(null=True, blank=True)
    tags = ArrayField(models.IntegerField(), default=list)
    associated_encounter = models.ForeignKey(
        "emr.Encounter", on_delete=models.PROTECT, null=True, blank=True, default=None
    )
    token = models.ForeignKey(
        "emr.Token",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        default=None,
        related_name="token_booking",
    )
    charge_item = models.ForeignKey(
        "emr.ChargeItem", on_delete=models.CASCADE, null=True, blank=True
    )
    # Desk details (resources/scheduling/slot/appointment.py). Bookings made
    # before these existed have no APT number and a blank channel.
    appointment_number = models.CharField(
        max_length=20, null=True, blank=True, default=None
    )
    consultation_type = models.CharField(max_length=20, default="in_person")
    visit_purpose = models.CharField(max_length=20, blank=True, default="")
    booking_channel = models.CharField(max_length=20, blank=True, default="")
    # Phone bookings only; hmis's reminder task sends them.
    reminder_due_at = models.DateTimeField(null=True, blank=True)
    reminder_sent_at = models.DateTimeField(null=True, blank=True)
    # "I don't know whom to meet" (D50); the remarks print as the chief complaint.
    guidance_area = models.CharField(max_length=40, blank=True, default="")
    guidance_since = models.CharField(max_length=20, blank=True, default="")
    chief_complaint = models.CharField(max_length=500, blank=True, default="")

    # App booking review; blank for bookings that never required approval.
    approval_status = models.CharField(
        max_length=12, blank=True, default="", db_index=True
    )
    approval_reason = models.CharField(max_length=500, blank=True, default="")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        constraints = [
            # A reschedule hands the number to the new booking and the old,
            # rescheduled one keeps a copy.
            models.UniqueConstraint(
                fields=["appointment_number"],
                condition=models.Q(appointment_number__isnull=False)
                & ~models.Q(status="rescheduled"),
                name="unique_appointment_number",
            )
        ]


class AppointmentSequence(models.Model):
    """One counter row per year for APT numbers, incremented under a row lock.
    The number has no site code, so the counter is shared by all facilities."""

    year = models.PositiveIntegerField(unique=True)
    last_value = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.year}: {self.last_value}"
