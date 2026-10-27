"""Mobile API v1: self-only in-person appointments with staff approval."""

from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers

from care.emr.models import SchedulableResource
from care.facility.models import Facility
from care.utils.shortcuts import get_object_or_404
from hmis.appointment_approvals import ReviewConflict, review_booking
from hmis.mobile_appointments import (
    available_slots,
    booking_data,
    booking_queryset,
    public_schedules,
    submit_booking,
)
from hmis.views.mobile import MobileView, ok


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(
                dict.fromkeys(sorted(unknown), "Unsupported field.")
            )
        return super().to_internal_value(data)


class PageQuery(StrictSerializer):
    page = serializers.IntegerField(min_value=1, default=1)
    page_size = serializers.IntegerField(min_value=1, max_value=100, default=20)


class CatalogueQuery(PageQuery):
    search = serializers.CharField(max_length=100, required=False, allow_blank=True)


class SlotQuery(StrictSerializer):
    date = serializers.DateField()


MOBILE_STATUSES = [
    "pending",
    "confirmed",
    "rejected",
    "expired",
    "cancelled",
    "completed",
    "noshow",
    "rescheduled",
    "proposed",
    "waitlist",
]


class BookingQuery(PageQuery):
    status = serializers.ChoiceField(choices=MOBILE_STATUSES, required=False)
    upcoming = serializers.BooleanField(required=False)


class SubmitSpec(StrictSerializer):
    request_id = serializers.UUIDField()
    slot_id = serializers.UUIDField()
    patient_notes = serializers.CharField(max_length=500, allow_blank=True, default="")


def validated(spec, data):
    serializer = spec(data=data.dict() if hasattr(data, "dict") else data)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def page_data(query, params, serialize):
    page, size = params["page"], params["page_size"]
    start = (page - 1) * size
    return {
        "total": query.count(),
        "page": page,
        "page_size": size,
        "items": [serialize(row) for row in query[start : start + size]],
    }


def doctor_data(resource):
    return {
        "id": str(resource.external_id),
        "name": resource.user.get_full_name(),
        "qualification": resource.user.qualification,
        "consultation_fee": None,
    }


def expire_own_pending(patient):
    # Keep the patient's response accurate even between the one-minute beat ticks.
    due = booking_queryset(patient).filter(
        approval_status="pending",
        status="pending",
        token_slot__start_datetime__lte=timezone.now(),
    )
    for booking_id in due.values_list("external_id", flat=True):
        try:
            review_booking(booking_id, "expired")
        except ReviewConflict:
            continue


class MobileAppointmentFacilitiesView(MobileView):
    def get(self, request):
        params = validated(CatalogueQuery, request.query_params)
        query = Facility.objects.filter(
            pk__in=public_schedules().values("resource__facility_id")
        )
        if params.get("search"):
            query = query.filter(name__icontains=params["search"])
        return ok(
            page_data(
                query.order_by("name", "id"),
                params,
                lambda facility: {
                    "id": str(facility.external_id),
                    "name": facility.name,
                    "address": facility.address,
                },
            )
        )


class MobileAppointmentDoctorsView(MobileView):
    def get(self, request, facility_id):
        params = validated(CatalogueQuery, request.query_params)
        facility = get_object_or_404(Facility, external_id=facility_id, is_public=True)
        query = SchedulableResource.objects.filter(
            pk__in=public_schedules()
            .filter(resource__facility=facility)
            .values("resource_id")
        ).select_related("user")
        if params.get("search"):
            query = query.filter(
                Q(user__first_name__icontains=params["search"])
                | Q(user__last_name__icontains=params["search"])
            )
        return ok(
            page_data(
                query.order_by("user__first_name", "user__last_name", "id"),
                params,
                doctor_data,
            )
        )


class MobileAppointmentSlotsView(MobileView):
    def get(self, request, facility_id, doctor_id):
        params = validated(SlotQuery, request.query_params)
        resource = get_object_or_404(
            SchedulableResource.objects.select_related("facility", "user"),
            external_id=doctor_id,
            facility__external_id=facility_id,
            pk__in=public_schedules().values("resource_id"),
        )
        return ok(
            {
                "facility_id": str(facility_id),
                "doctor_id": str(doctor_id),
                "date": params["date"],
                "timezone": timezone.get_current_timezone_name(),
                "items": available_slots(resource, params["date"]),
            }
        )


class MobileAppointmentListCreateView(MobileView):
    def post(self, request):
        params = validated(SubmitSpec, request.data)
        expire_own_pending(request.user.patient)
        booking, created = submit_booking(request.user.account, **params)
        return ok(
            booking_data(booking),
            "Appointment request submitted"
            if created
            else "Appointment request already submitted",
            code=201 if created else 200,
        )

    def get(self, request):
        params = validated(BookingQuery, request.query_params)
        patient = request.user.patient
        expire_own_pending(patient)
        query = booking_queryset(patient)
        state = params.get("status")
        if state == "confirmed":
            query = query.filter(
                status__in=["booked", "arrived", "checked_in", "in_consultation"]
            )
        elif state in {"rejected", "expired"}:
            query = query.filter(status="cancelled", approval_status=state)
        elif state == "cancelled":
            query = query.filter(status__in=["cancelled", "entered_in_error"]).exclude(
                approval_status__in=["rejected", "expired"]
            )
        elif state == "completed":
            query = query.filter(status="fulfilled")
        elif state:
            query = query.filter(status=state)
        if "upcoming" in params:
            query = query.filter(
                status__in=[
                    "pending",
                    "booked",
                    "arrived",
                    "checked_in",
                    "in_consultation",
                ]
            )
            if params["upcoming"]:
                query = query.filter(token_slot__end_datetime__gt=timezone.now())
            else:
                query = query.filter(token_slot__end_datetime__lte=timezone.now())
        return ok(
            page_data(
                query.order_by("-token_slot__start_datetime", "-id"),
                params,
                booking_data,
            )
        )


class MobileAppointmentDetailView(MobileView):
    def get(self, request, booking_id):
        expire_own_pending(request.user.patient)
        return ok(
            booking_data(
                get_object_or_404(
                    booking_queryset(request.user.patient), external_id=booking_id
                )
            )
        )
