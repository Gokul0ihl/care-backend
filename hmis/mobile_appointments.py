"""Patient-scoped adapter over Care scheduling, not a second booking store."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from care.emr.api.viewsets.scheduling.availability import (
    SlotViewSet,
    convert_availability_and_exceptions_to_slots,
)
from care.emr.api.viewsets.scheduling.desk_details import seats_for_day
from care.emr.models import AvailabilityException, Schedule, TokenBooking, TokenSlot
from care.emr.resources.scheduling.slot.spec import CANCELLED_STATUS_CHOICES
from care.utils.shortcuts import get_object_or_404
from hmis.models import MobileAppointmentSubmission, PatientAccount

BOOKING_WINDOW_DAYS = 90


class AppointmentConflict(APIException):
    status_code = 409
    default_detail = (
        "This slot is no longer available. Refresh the slots and choose again."
    )


def public_schedules():
    return Schedule.objects.filter(
        is_public=True,
        resource__deleted=False,
        resource__resource_type="practitioner",
        resource__facility__deleted=False,
        resource__facility__is_public=True,
        resource__user__deleted=False,
        resource__user__is_active=True,
        resource__user__is_service_account=False,
        valid_to__gt=timezone.now(),
    )


def validate_day(day):
    today = timezone.localdate()
    if not today <= day <= today + timedelta(days=BOOKING_WINDOW_DAYS):
        raise ValidationError(
            {"date": "Choose today or a date within the next 90 days."}
        )
    return day


def valid_slot_keys(availability, day):
    """Use the generator's exact windows and exception rules, including edits
    made after TokenSlot rows were materialized."""
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
    return convert_availability_and_exceptions_to_slots(windows, exceptions, day)


def slot_is_current(slot, keys):
    start = timezone.localtime(slot.start_datetime)
    end = timezone.localtime(slot.end_datetime)
    schedule = slot.availability.schedule
    return (
        start.date() == end.date()
        and timezone.localtime(schedule.valid_from).date()
        <= start.date()
        <= timezone.localtime(schedule.valid_to).date()
        and f"{slot.availability_id}-{start.time()}-{end.time()}" in keys
    )


def scheduled_seats_remaining(availability, day):
    if not availability.walk_in_every:
        return None
    seats = seats_for_day(availability, day)
    booked = (
        TokenBooking.objects.filter(
            token_slot__availability=availability,
            token_slot__start_datetime__date=day,
        )
        .exclude(status__in=CANCELLED_STATUS_CHOICES)
        .exclude(booking_channel="walk_in")
        .count()
    )
    return max(0, seats - seats // availability.walk_in_every - booked)


def available_slots(resource, day):
    validate_day(day)
    SlotViewSet.get_slots_for_day_handler(
        resource.facility.external_id,
        {
            "resource_type": "practitioner",
            "resource_id": resource.user.external_id,
            "day": day,
        },
        is_public=True,
    )
    schedules = public_schedules().filter(resource=resource)
    slots = (
        TokenSlot.objects.filter(
            resource=resource,
            availability__deleted=False,
            availability__slot_type="appointment",
            availability__schedule__in=schedules,
            start_datetime__date=day,
            start_datetime__gt=timezone.now(),
        )
        .select_related("availability__schedule")
        .order_by("start_datetime", "id")
    )
    rules = {}
    result = []
    for slot in slots:
        availability = slot.availability
        if availability.pk not in rules:
            rules[availability.pk] = (
                valid_slot_keys(availability, day),
                scheduled_seats_remaining(availability, day),
            )
        keys, allowance = rules[availability.pk]
        if not slot_is_current(slot, keys):
            continue
        remaining = max(0, (availability.tokens_per_slot or 0) - slot.allocated)
        if allowance is not None:
            remaining = min(remaining, allowance)
        result.append(
            {
                "id": str(slot.external_id),
                "start_datetime": slot.start_datetime.isoformat(),
                "end_datetime": slot.end_datetime.isoformat(),
                "seats_remaining": remaining,
                "available": remaining > 0,
            }
        )
    return result


def booking_queryset(patient):
    return TokenBooking.objects.filter(patient=patient).select_related(
        "token_slot__resource__facility",
        "token_slot__resource__user",
        "mobile_submission",
    )


def booking_data(booking):
    slot = booking.token_slot
    resource = slot.resource
    # Clinical status wins after approval: cancelling an approved booking must
    # never leave a patient seeing "confirmed" just because its audit says approved.
    if booking.status in {
        "cancelled",
        "entered_in_error",
        "rescheduled",
        "noshow",
        "fulfilled",
    }:
        state = {"fulfilled": "completed", "entered_in_error": "cancelled"}.get(
            booking.status, booking.status
        )
        if booking.approval_status in {"rejected", "expired"}:
            state = booking.approval_status
    elif booking.approval_status == "pending":
        state = "pending"
    elif booking.status in {"booked", "arrived", "checked_in", "in_consultation"}:
        state = "confirmed"
    else:
        state = booking.status
    return {
        "id": str(booking.external_id),
        "appointment_number": booking.appointment_number,
        "request_id": str(booking.mobile_submission.request_id)
        if hasattr(booking, "mobile_submission")
        else None,
        "timezone": timezone.get_current_timezone_name(),
        "status": state,
        "booking_status": booking.status,
        "approval_status": booking.approval_status or None,
        "rejection_reason": booking.approval_reason
        if state in {"rejected", "expired"}
        else None,
        "reviewed_at": booking.reviewed_at,
        "facility": {
            "id": str(resource.facility.external_id),
            "name": resource.facility.name,
            "address": resource.facility.address,
        },
        "doctor": {
            "id": str(resource.external_id),
            "name": resource.user.get_full_name() if resource.user_id else None,
        },
        "slot_id": str(slot.external_id),
        "start_datetime": slot.start_datetime,
        "end_datetime": slot.end_datetime,
        "patient_notes": booking.mobile_submission.patient_notes
        if hasattr(booking, "mobile_submission")
        else None,
        "booking_channel": booking.booking_channel,
        "created_at": booking.booked_on,
        "updated_at": booking.modified_date,
        "consultation_type": booking.consultation_type,
        "consultation_fee": None,
        "payment_option": "pay_at_hospital",
    }


def submit_booking(account, request_id, slot_id, patient_notes):
    """Durable retry protection. Account -> patient -> slot lock; no patient
    principal is ever passed as a Django User to Care's audit foreign keys."""
    with transaction.atomic():
        PatientAccount.objects.select_for_update().get(pk=account.pk)
        # Serialize mobile submissions for this patient, including quota checks.
        patient = (
            type(account.patient).objects.select_for_update().get(pk=account.patient_id)
        )
        previous = MobileAppointmentSubmission.objects.filter(
            account=account, request_id=request_id
        ).first()
        if previous:
            if previous.slot_id != slot_id or previous.patient_notes != patient_notes:
                raise AppointmentConflict(
                    "request_id was already used with different booking details."
                )
            return booking_queryset(patient).get(pk=previous.booking_id), False
        slot = get_object_or_404(
            TokenSlot.objects.select_for_update(of=("self",)).select_related(
                "resource__facility", "availability__schedule"
            ),
            external_id=slot_id,
            availability__deleted=False,
            availability__slot_type="appointment",
            availability__schedule__in=public_schedules(),
        )
        day = timezone.localtime(slot.start_datetime).date()
        validate_day(day)
        if not slot_is_current(slot, valid_slot_keys(slot.availability, day)):
            raise AppointmentConflict
        try:
            booking = SlotViewSet.create_appointment_handler(
                slot,
                {
                    "patient": patient.external_id,
                    "note": patient_notes,
                    "booking_channel": "online",
                    "consultation_type": "in_person",
                },
                None,
                pending_approval=True,
            )
        except ValidationError as exc:
            raise AppointmentConflict(exc.detail) from exc
        MobileAppointmentSubmission.objects.create(
            account=account,
            request_id=request_id,
            slot_id=slot_id,
            patient_notes=patient_notes,
            booking=booking,
        )
        return booking_queryset(patient).get(pk=booking.pk), True
