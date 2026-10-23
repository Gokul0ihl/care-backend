"""Booking rules for the desk details on TokenBooking: the APT number, the
walk-in reserve, the default visit purpose and the phone reminder."""

import datetime

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from care.emr.models import AvailabilityException, TokenBooking
from care.emr.models.scheduling.booking import AppointmentSequence
from care.emr.models.scheduling.schedule import Availability
from care.emr.resources.scheduling.slot.appointment import (
    REMINDER_LEAD_TIME_HOURS,
    BookingChannels,
    VisitPurposes,
)
from care.emr.resources.scheduling.slot.spec import CANCELLED_STATUS_CHOICES
from care.utils.time_util import care_now


def allocate_appointment_number():
    """Returns the next APT-<yy>-<nnnnnn>."""
    year = care_now().year
    with transaction.atomic():
        AppointmentSequence.objects.get_or_create(year=year)
        sequence = AppointmentSequence.objects.select_for_update().get(year=year)
        sequence.last_value += 1
        sequence.save(update_fields=["last_value"])
    return f"APT-{year % 100:02d}-{sequence.last_value:06d}"


def seats_for_day(availability, day):
    """Seats the availability offers on the day, from the slot generator so
    availability exceptions are left out the same way."""
    from care.emr.api.viewsets.scheduling.availability import (
        convert_availability_and_exceptions_to_slots,
    )

    windows = [
        {
            "availability": window,
            "slot_size_in_minutes": availability.slot_size_in_minutes,
            "availability_id": availability.id,
        }
        for window in availability.availability
        if window["day_of_week"] == day.weekday()
    ]
    exceptions = AvailabilityException.objects.filter(
        resource=availability.schedule.resource,
        valid_from__lte=day,
        valid_to__gte=day,
    )
    slots = convert_availability_and_exceptions_to_slots(windows, exceptions, day)
    return len(slots) * (availability.tokens_per_slot or 0)


def check_walk_in_reserve(token_slot, booking_channel):
    """Refuses a scheduled booking that would eat into the day's walk-in
    seats. Must run inside the booking's transaction: the availability row
    stays locked until it commits, so concurrent bookings are counted one at
    a time."""
    day = timezone.localtime(token_slot.start_datetime).date()
    if booking_channel == BookingChannels.walk_in.value:
        if day != timezone.localdate():
            raise ValidationError("Walk-in bookings can only be made for today")
        return
    if token_slot.availability_id is None:
        return
    availability = (
        Availability.objects.select_for_update()
        .select_related("schedule__resource")
        .filter(pk=token_slot.availability_id, walk_in_every__isnull=False)
        .first()
    )
    if availability is None:
        return
    seats = seats_for_day(availability, day)
    scheduled = (
        TokenBooking.objects.filter(
            token_slot__availability=availability,
            token_slot__start_datetime__date=day,
        )
        .exclude(status__in=CANCELLED_STATUS_CHOICES)
        .exclude(booking_channel=BookingChannels.walk_in.value)
        .count()
    )
    if scheduled >= seats - seats // availability.walk_in_every:
        raise ValidationError(
            "The remaining seats in this session are held for walk-ins"
        )


def default_visit_purpose(token_slot, patient):
    """Follow-up when the patient saw the same resource within the schedule's
    revisit window, otherwise a new consultation."""
    schedule = token_slot.availability.schedule
    if not schedule.revisit_allowed_days:
        return VisitPurposes.new.value
    since = token_slot.start_datetime - datetime.timedelta(
        days=schedule.revisit_allowed_days
    )
    earlier = (
        TokenBooking.objects.filter(
            patient=patient,
            token_slot__resource=token_slot.resource,
            token_slot__start_datetime__gte=since,
            token_slot__start_datetime__lt=token_slot.start_datetime,
        )
        .exclude(status__in=[*CANCELLED_STATUS_CHOICES, "noshow"])
        .exists()
    )
    return VisitPurposes.follow_up.value if earlier else VisitPurposes.new.value


def reminder_due(token_slot, booking_channel):
    """Phone bookings get an SMS before the slot, if that is still ahead."""
    if booking_channel != BookingChannels.phone.value:
        return None
    due = token_slot.start_datetime - datetime.timedelta(hours=REMINDER_LEAD_TIME_HOURS)
    return due if due > care_now() else None
