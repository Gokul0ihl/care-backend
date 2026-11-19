"""Patient-scoped adapter over Care scheduling, not a second booking store."""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from care.emr.api.viewsets.scheduling.availability import (
    PatientTimeConflictError,
    SlotViewSet,
    convert_availability_and_exceptions_to_slots,
    lock_create_appointment,
)
from care.emr.api.viewsets.scheduling.booking import (
    MOVED_ON_RESCHEDULE,
    TokenBookingViewSet,
)
from care.emr.api.viewsets.scheduling.desk_details import (
    check_walk_in_reserve,
    following_slots,
    seats_for_day,
)
from care.emr.models import AvailabilityException, Schedule, TokenBooking, TokenSlot
from care.emr.resources.scheduling.slot.spec import CANCELLED_STATUS_CHOICES
from care.utils.shortcuts import get_object_or_404
from hmis.models import (
    MobileAppointmentChange,
    MobileAppointmentSubmission,
    PatientAccount,
)

BOOKING_WINDOW_DAYS = 90


class AppointmentConflict(APIException):
    status_code = 409
    default_detail = (
        "This slot is no longer available. Refresh the slots and choose again."
    )


class ChangeNotAllowed(APIException):
    """The app may no longer change this appointment; the desk can."""

    status_code = 409
    default_detail = (
        "This appointment can no longer be changed in the app. "
        "Please contact the hospital."
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
    # A longer appointment takes a seat in each slot it holds, as in
    # desk_details.check_walk_in_reserve.
    bookings = (
        TokenBooking.objects.filter(
            token_slot__availability=availability,
            token_slot__start_datetime__date=day,
        )
        .exclude(status__in=CANCELLED_STATUS_CHOICES)
        .exclude(booking_channel="walk_in")
        .annotate(extra=Count("extra_slots"))
    )
    booked = sum(1 + booking.extra for booking in bookings)
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
                # Slots in one session follow each other, so a longer
                # appointment picks the next ones with the same session_id.
                "session_id": str(availability.external_id),
                "start_datetime": slot.start_datetime.isoformat(),
                "end_datetime": slot.end_datetime.isoformat(),
                "seats_remaining": remaining,
                "available": remaining > 0,
            }
        )
    return result


def booking_queryset(patient):
    return (
        TokenBooking.objects.filter(patient=patient)
        .select_related(
            "token_slot__resource__facility",
            "token_slot__resource__user",
            "mobile_submission",
            "mobile_change",
        )
        .prefetch_related("extra_slots")
    )


def held_extra_slots(booking):
    """The back-to-back slots after token_slot that the booking also holds."""
    return sorted(booking.extra_slots.all(), key=lambda slot: slot.start_datetime)


# Booking statuses the patient may still cancel or move in the app. Once they
# have arrived the desk handles it.
CHANGEABLE_STATUSES = {"pending", "booked"}


def same_appointment(booking):
    """Every booking of this appointment: a reschedule cancels the booking and
    passes its APT number to a new one, at the desk or in the app."""
    if not booking.appointment_number:
        return TokenBooking.objects.filter(pk=booking.pk)
    return TokenBooking.objects.filter(
        patient_id=booking.patient_id,
        appointment_number=booking.appointment_number,
    )


def reschedule_count(booking):
    return same_appointment(booking).filter(status="rescheduled").count()


def change_rules(booking, now=None):
    """What the patient may still change in the app; past these, the desk."""
    now = now or timezone.now()
    start = booking.token_slot.start_datetime
    open_ = booking.status in CHANGEABLE_STATUSES and start > now
    cancel_by = start - settings.MOBILE_CANCEL_CUTOFF
    reschedule_by = start - settings.MOBILE_RESCHEDULE_CUTOFF
    remaining = (
        max(0, settings.MOBILE_RESCHEDULE_LIMIT - reschedule_count(booking))
        if open_
        else 0
    )
    return {
        "can_cancel": open_ and now < cancel_by,
        "cancel_deadline": cancel_by if open_ else None,
        "can_reschedule": open_ and now < reschedule_by and remaining > 0,
        "reschedule_deadline": reschedule_by if open_ else None,
        "reschedules_remaining": remaining,
    }


def origin_submission(booking):
    """The app request this appointment started from, kept across reschedules."""
    if hasattr(booking, "mobile_submission"):
        return booking.mobile_submission
    if not booking.appointment_number:
        return None
    return (
        MobileAppointmentSubmission.objects.filter(
            booking__in=same_appointment(booking)
        )
        .order_by("created_at")
        .first()
    )


def rescheduled_to(booking):
    if booking.status != "rescheduled":
        return None
    change = getattr(booking, "mobile_change", None)
    if change and change.replacement_id:
        replacement = change.replacement
    else:
        replacement = (
            same_appointment(booking)
            .exclude(status="rescheduled")
            .exclude(pk=booking.pk)
            .first()
        )
    return str(replacement.external_id) if replacement else None


def patient_change_data(booking):
    change = getattr(booking, "mobile_change", None)
    if change is None:
        return None
    return {
        "action": change.action,
        "reason": change.reason,
        "note": change.note or None,
        "created_at": change.created_at,
    }


def booking_data(booking):
    slot = booking.token_slot
    resource = slot.resource
    extra_slots = held_extra_slots(booking)
    submission = origin_submission(booking)
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
        "request_id": str(submission.request_id) if submission else None,
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
        "extra_slots": [
            {
                "id": str(extra.external_id),
                "start_datetime": extra.start_datetime,
                "end_datetime": extra.end_datetime,
            }
            for extra in extra_slots
        ],
        "start_datetime": slot.start_datetime,
        # When the whole appointment ends, after any extra slots.
        "end_datetime": (extra_slots[-1] if extra_slots else slot).end_datetime,
        "patient_notes": submission.patient_notes if submission else None,
        "booking_channel": booking.booking_channel,
        "created_at": booking.booked_on,
        "updated_at": booking.modified_date,
        "consultation_type": booking.consultation_type,
        "consultation_fee": None,
        "payment_option": "pay_at_hospital",
        # Set on a rescheduled booking: the booking that replaced it.
        "rescheduled_to": rescheduled_to(booking),
        # Set when the patient cancelled or moved this booking in the app.
        "patient_change": patient_change_data(booking),
        **change_rules(booking),
    }


def submit_booking(account, request_id, slot_id, patient_notes, extra_slot_ids=()):
    """Durable retry protection. Account -> patient -> slot lock; no patient
    principal is ever passed as a Django User to Care's audit foreign keys.
    extra_slot_ids are the back-to-back slots after slot_id that a longer
    appointment also holds (up to MAX_SLOTS_PER_BOOKING in all)."""
    extra_slot_ids = list(dict.fromkeys(extra_slot_ids))
    if slot_id in extra_slot_ids:
        raise ValidationError(
            {"extra_slot_ids": "Don't repeat slot_id in extra_slot_ids."}
        )
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
            booking = booking_queryset(patient).get(pk=previous.booking_id)
            held = {extra.external_id for extra in booking.extra_slots.all()}
            if (
                previous.slot_id != slot_id
                or previous.patient_notes != patient_notes
                or held != set(extra_slot_ids)
            ):
                raise AppointmentConflict(
                    "request_id was already used with different booking details."
                )
            return booking, False
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
        keys = valid_slot_keys(slot.availability, day)
        if not slot_is_current(slot, keys):
            raise AppointmentConflict
        # Care checks the extra slots follow on in this session and have seats;
        # here each must also still be offered to the public.
        extras = TokenSlot.objects.filter(
            external_id__in=extra_slot_ids, resource=slot.resource
        ).select_related("availability__schedule")
        if len(extras) != len(extra_slot_ids) or not all(
            extra.availability_id == slot.availability_id
            and slot_is_current(extra, keys)
            for extra in extras
        ):
            raise AppointmentConflict(
                "One of the chosen slots is no longer available. "
                "Refresh the slots and choose again."
            )
        try:
            booking = SlotViewSet.create_appointment_handler(
                slot,
                {
                    "patient": patient.external_id,
                    "note": patient_notes,
                    "booking_channel": "online",
                    "consultation_type": "in_person",
                    "extra_slots": extra_slot_ids,
                },
                None,
                pending_approval=True,
            )
        except PatientTimeConflictError as exc:
            raise AppointmentConflict(exc.message) from exc
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


def lock_patient_booking(account, booking_id):
    """Account -> patient -> booking -> slot, the same order as submit_booking.
    Returns the patient and the locked booking, its slots locked too."""
    PatientAccount.objects.select_for_update().get(pk=account.pk)
    patient = (
        type(account.patient).objects.select_for_update().get(pk=account.patient_id)
    )
    booking = get_object_or_404(
        booking_queryset(patient).select_for_update(of=("self",)),
        external_id=booking_id,
    )
    return patient, booking


def lock_held_slots(booking):
    """Lock the seats the booking holds before Care's cancel handler gives them
    back, so a concurrent booking can't overwrite the count."""
    held = [booking.token_slot_id, *booking.extra_slots.values_list("pk", flat=True)]
    slots = {
        slot.pk: slot
        for slot in TokenSlot.objects.select_for_update(of=("self",))
        .select_related("resource__facility", "availability__schedule")
        .filter(pk__in=held)
        .order_by("pk")
    }
    booking.token_slot = slots[booking.token_slot_id]


def release_booking(booking, reason):
    """Cancel through Care so seats and any charge item are released the same
    way as at the desk. A pending app request is withdrawn, not reviewed."""
    TokenBookingViewSet.cancel_appointment_handler(
        booking, {"reason": reason}, None, allow_pending_review=True
    )
    if booking.approval_status == "pending":
        booking.approval_status = "withdrawn"
        booking.approval_reason = (
            "Cancelled by the patient in the app."
            if reason == "cancelled"
            else "Moved to another time by the patient in the app."
        )
        booking.save(update_fields=["approval_status", "approval_reason"])


def cancel_booking(account, booking_id, reason, note=""):
    """Returns (booking, cancelled_now). Cancelling an already cancelled
    booking is a harmless replay."""
    with transaction.atomic():
        patient, booking = lock_patient_booking(account, booking_id)
        if booking.status in {"cancelled", "entered_in_error"}:
            return booking, False
        if booking.status == "rescheduled":
            raise ChangeNotAllowed(
                "This appointment was moved to another time. "
                "Cancel the new appointment instead."
            )
        if not change_rules(booking)["can_cancel"]:
            raise ChangeNotAllowed
        lock_held_slots(booking)
        release_booking(booking, "cancelled")
        MobileAppointmentChange.objects.create(
            account=account,
            action=MobileAppointmentChange.CANCEL,
            booking=booking,
            reason=reason,
            note=note,
        )
        return booking_queryset(patient).get(pk=booking.pk), True


def reschedule_booking(account, booking_id, slot_id, reason, note=""):
    """Moves the appointment to another public slot of the same doctor and
    returns (new booking, moved_now). Like a desk reschedule, the old booking
    becomes "rescheduled" and the new one keeps the APT number, length and
    desk details. An approved or desk booking stays confirmed; a pending
    request stays pending at the new time. Changing the doctor is a new
    booking, not a reschedule (Intake Suite)."""
    with transaction.atomic():
        patient, booking = lock_patient_booking(account, booking_id)
        change = getattr(booking, "mobile_change", None)
        if booking.status == "rescheduled":
            if (
                change
                and change.replacement_id
                and change.replacement.token_slot.external_id == slot_id
            ):
                # A retry of the move that already happened.
                return booking_queryset(patient).get(pk=change.replacement_id), False
            raise ChangeNotAllowed(
                "This appointment was already moved. Reschedule the new appointment."
            )
        if booking.token_slot.external_id == slot_id:
            raise ValidationError({"slot_id": "Choose a different time."})
        rules = change_rules(booking)
        if not rules["can_reschedule"]:
            # reschedule_deadline is only set while the booking is still open.
            if rules["reschedule_deadline"] and not rules["reschedules_remaining"]:
                raise ChangeNotAllowed(
                    "This appointment has been moved the most times allowed in "
                    "the app. Please contact the hospital."
                )
            raise ChangeNotAllowed
        new_slot = get_object_or_404(
            TokenSlot.objects.select_for_update(of=("self",)).select_related(
                "resource__facility", "availability__schedule"
            ),
            external_id=slot_id,
            resource_id=booking.token_slot.resource_id,
            availability__deleted=False,
            availability__slot_type="appointment",
            availability__schedule__in=public_schedules(),
        )
        if new_slot.start_datetime <= timezone.now():
            raise AppointmentConflict
        day = timezone.localtime(new_slot.start_datetime).date()
        validate_day(day)
        keys = valid_slot_keys(new_slot.availability, day)
        if not slot_is_current(new_slot, keys):
            raise AppointmentConflict
        lock_held_slots(booking)
        pending = booking.approval_status == "pending"
        try:
            # A longer appointment keeps its length at the new time.
            extra_slots = following_slots(new_slot, booking.extra_slots.count())
            if not all(slot_is_current(extra, keys) for extra in extra_slots):
                raise AppointmentConflict(
                    "The new time doesn't have enough free back-to-back slots. "
                    "Refresh the slots and choose again."
                )
            check_walk_in_reserve(
                new_slot, booking.booking_channel, 1 + len(extra_slots)
            )
            release_booking(booking, "rescheduled")
            replacement = lock_create_appointment(
                new_slot,
                patient,
                None,
                booking.note,
                pending_approval=pending,
                extra_slots=extra_slots,
                **{field: getattr(booking, field) for field in MOVED_ON_RESCHEDULE},
            )
        except PatientTimeConflictError as exc:
            raise AppointmentConflict(exc.message) from exc
        except ValidationError as exc:
            raise AppointmentConflict(exc.detail) from exc
        MobileAppointmentChange.objects.create(
            account=account,
            action=MobileAppointmentChange.RESCHEDULE,
            booking=booking,
            replacement=replacement,
            reason=reason,
            note=note,
        )
        return booking_queryset(patient).get(pk=replacement.pk), True
