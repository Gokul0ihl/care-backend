"""The registration timeline of one patient, and the registration audit trail
across patients (hmis.patient_events, docs/registration-audit.md)."""

import datetime
import uuid

from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from care.emr.models import Patient
from care.utils.pagination.care_pagination import CareLimitOffsetPagination
from care.utils.shortcuts import get_object_or_404
from hmis.models import PatientEvent, PatientEventActions, PatientEventChannels
from hmis.patient_events import (
    search_events,
    serialize_patient_event,
    timeline_patients,
)
from hmis.views.base import EmergencyAPIView, can


class PatientTimelineView(EmergencyAPIView):
    """GET: the patient's registration events, newest first, including those of
    duplicates merged into this record. Anyone who can view the patient."""

    def get(self, request, patient_id):
        patient = get_object_or_404(Patient, external_id=patient_id)
        if not can("can_view_patient_obj", request.user, patient):
            raise PermissionDenied("You do not have permission to view this patient")
        events = PatientEvent.objects.filter(
            patient_id__in=timeline_patients(patient)
        ).select_related("patient", "user", "facility")
        return Response(
            {"results": [serialize_patient_event(event) for event in events]}
        )


def parse_date(value, name):
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as e:
        raise ValidationError({name: "Use YYYY-MM-DD"}) from e


class RegistrationAuditView(EmergencyAPIView):
    """GET: every registration event, newest first, paged with limit/offset.
    Super admins only, like the access audit log. Filters: facility (id),
    action, channel (comma-separated), search (user, patient number,
    facility), date_from and date_to (YYYY-MM-DD, inclusive)."""

    def get(self, request):
        if not request.user.is_superuser:
            raise PermissionDenied("Only super admins can read the audit trail")
        params = request.query_params
        events = PatientEvent.objects.select_related("patient", "user", "facility")
        if params.get("facility"):
            try:
                facility_id = uuid.UUID(params["facility"])
            except ValueError as e:
                raise ValidationError({"facility": "Not a facility id"}) from e
            events = events.filter(facility__external_id=facility_id)
        if params.get("action"):
            actions = params["action"].split(",")
            if set(actions) - set(PatientEventActions.values):
                raise ValidationError({"action": "Unknown action"})
            events = events.filter(action__in=actions)
        if params.get("channel"):
            channels = params["channel"].split(",")
            if set(channels) - set(PatientEventChannels.values):
                raise ValidationError({"channel": "Unknown channel"})
            events = events.filter(channel__in=channels)
        if params.get("search", "").strip():
            events = search_events(events, params["search"].strip())
        if params.get("date_from"):
            events = events.filter(
                created_date__date__gte=parse_date(params["date_from"], "date_from")
            )
        if params.get("date_to"):
            events = events.filter(
                created_date__date__lte=parse_date(params["date_to"], "date_to")
            )
        paginator = CareLimitOffsetPagination()
        page = paginator.paginate_queryset(events, request, view=self)
        return paginator.get_paginated_response(
            [serialize_patient_event(event) for event in page]
        )
