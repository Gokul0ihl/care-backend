"""Facility-scoped App requests queue and staff review endpoints."""

from django.utils import timezone
from pydantic import BaseModel, Field
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from care.emr.models import TokenBooking
from hmis.appointment_approvals import can_review, review_booking
from hmis.restricted import can_see_identity, is_restricted
from hmis.services import display_numbers
from hmis.views.base import EmergencyAPIView, can, get_facility


class ReviewSpec(BaseModel):
    reason: str = Field(default="", max_length=500)


def check_access(request, facility):
    if request.user.is_service_account or not can(
        "can_list_booking_on_facility", request.user, facility
    ):
        raise PermissionDenied("You do not have permission to view app requests here")


def serialize_request(booking, user, numbers=None):
    slot = booking.token_slot
    doctor = slot.resource.user
    reviewer = booking.reviewed_by
    # Preserve statutory masking, just like Care's patient serializers.
    masked = is_restricted(booking.patient) and not can_see_identity(
        user, booking.patient
    )
    patient_name = booking.patient.restricted_alias if masked else booking.patient.name
    return {
        "id": str(booking.external_id),
        "appointment_number": booking.appointment_number,
        "status": booking.status,
        "approval_status": booking.approval_status,
        "approval_reason": "Restricted"
        if masked and booking.approval_reason
        else booking.approval_reason,
        "patient": {"id": str(booking.patient.external_id), "name": patient_name},
        "patient_number": None
        if masked
        else (numbers or {}).get(booking.patient_id, {}).get("value"),
        "doctor": doctor.get_full_name() if doctor else slot.availability.schedule.name,
        "facility": {
            "id": str(slot.resource.facility.external_id),
            "name": slot.resource.facility.name,
        },
        "start_datetime": slot.start_datetime,
        "end_datetime": slot.end_datetime,
        "note": "" if masked else booking.note,
        "requested_at": booking.booked_on,
        "reviewed_at": booking.reviewed_at,
        "reviewed_by": reviewer.get_full_name() or reviewer.username
        if reviewer
        else None,
        "can_review": booking.approval_status == "pending"
        and slot.start_datetime > timezone.now()
        and can_review(user, booking),
    }


def request_queryset():
    return TokenBooking.objects.exclude(approval_status="").select_related(
        "patient",
        "reviewed_by",
        "token_slot__resource__user",
        "token_slot__resource__facility",
        "token_slot__availability__schedule",
    )


class AppointmentRequestListView(EmergencyAPIView):
    def get(self, request, facility_id):
        facility = get_facility(facility_id)
        check_access(request, facility)
        state = request.query_params.get("status", "pending")
        if state not in {"pending", "approved", "rejected", "expired"}:
            raise ValidationError("Unknown approval status")
        try:
            limit = min(max(int(request.query_params.get("limit", 20)), 1), 100)
            offset = max(int(request.query_params.get("offset", 0)), 0)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Invalid pagination") from exc
        query = (
            request_queryset()
            .filter(token_slot__resource__facility=facility, approval_status=state)
            .order_by("token_slot__start_datetime", "booked_on", "id")
        )
        rows = list(query[offset : offset + limit])
        numbers = display_numbers([row.patient_id for row in rows])
        return Response(
            {
                "count": query.count(),
                "results": [
                    serialize_request(row, request.user, numbers) for row in rows
                ],
            }
        )


class AppointmentRequestReviewView(EmergencyAPIView):
    def post(self, request, facility_id, booking_id, decision):
        facility = get_facility(facility_id)
        check_access(request, facility)
        spec = ReviewSpec.model_validate(request.data)
        reason = spec.reason.strip()
        if decision == "rejected" and not reason:
            raise ValidationError(
                {"errors": [{"loc": ["reason"], "msg": "Enter a reason for rejection"}]}
            )
        booking = review_booking(booking_id, decision, reason, request.user, facility)
        return Response(
            serialize_request(
                booking, request.user, display_numbers([booking.patient_id])
            )
        )
