"""Review pending app bookings on Care's shared schedule (AP6/AP7)."""

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from care.emr.api.viewsets.scheduling.availability import generate_booking_charge_item
from care.emr.api.viewsets.scheduling.booking import TokenBookingViewSet
from care.emr.models import TokenBooking, TokenSlot
from care.security.authorization import AuthorizationController
from care.utils.shortcuts import get_object_or_404


class ReviewConflict(APIException):
    status_code = 409
    default_detail = "This request has already been reviewed. Refresh the list."


def can_review(user, booking):
    return not user.is_service_account and AuthorizationController.call(
        "can_write_booking", booking.token_slot.resource, user
    )


def review_booking(booking_id, decision, reason="", user=None, facility=None):
    """Slot -> booking locks match booking creation. Expiry uses this path too.

    Repeating the same decision is harmless and keeps the original reviewer.
    A conflicting decision returns 409. Never raise inside the transaction after
    expiring a request: the seat release must commit even on a late approval.
    """
    if decision not in {"approved", "rejected", "expired"}:
        raise ValidationError("Unknown review decision")
    if decision != "expired" and user is None:
        raise PermissionDenied("A staff reviewer is required")
    if decision == "rejected" and not reason.strip():
        raise ValidationError("Enter a reason for rejection")
    query = TokenBooking.objects.filter(external_id=booking_id).exclude(
        approval_status=""
    )
    if facility is not None:
        query = query.filter(token_slot__resource__facility=facility)
    original = get_object_or_404(query)
    with transaction.atomic():
        slot = (
            TokenSlot.objects.select_for_update(of=("self",))
            .select_related("resource__facility", "availability__schedule")
            .get(pk=original.token_slot_id)
        )
        booking = (
            query.select_for_update(of=("self",))
            .select_related("patient", "reviewed_by")
            .get(pk=original.pk)
        )
        booking.token_slot = slot
        if user is not None and not can_review(user, booking):
            raise PermissionDenied("You do not have permission to review this booking")
        if booking.approval_status != "pending":
            if booking.approval_status == decision:
                return booking
            raise ReviewConflict
        if booking.status != "pending":
            raise ReviewConflict("The booking is no longer pending. Refresh the list.")
        now = timezone.now()
        expired = slot.start_datetime <= now
        if decision == "expired" and not expired:
            return booking
        outcome = "expired" if expired else decision
        if outcome == "approved":
            booking.status = "booked"
            booking.updated_by = user
            booking.save(update_fields=["status", "updated_by", "modified_date"])
            generate_booking_charge_item(booking, user)
        else:
            TokenBookingViewSet.cancel_appointment_handler(
                booking, {"reason": "cancelled"}, user, allow_pending_review=True
            )
        booking.approval_status = outcome
        booking.approval_reason = (
            "Request expired when the appointment started." if expired else reason
        )
        booking.reviewed_by = None if expired else user
        booking.reviewed_at = now
        booking.save(
            update_fields=[
                "approval_status",
                "approval_reason",
                "reviewed_by",
                "reviewed_at",
                "modified_date",
            ]
        )
    if expired and decision != "expired":
        raise ReviewConflict("This request has expired and its seat has been released.")
    return booking


def expire_pending_bookings():
    due = TokenBooking.objects.filter(
        approval_status="pending",
        status="pending",
        token_slot__start_datetime__lte=timezone.now(),
    ).values_list("external_id", flat=True)
    count = 0
    for booking_id in due.iterator():
        try:
            booking = review_booking(booking_id, "expired")
            count += booking.approval_status == "expired"
        except ReviewConflict:
            # A desk decision won the race; leave its outcome intact.
            continue
    return count
