"""Restricted patients, statutory flags and break-glass access."""

import datetime

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from care.emr.models import Encounter, Patient
from care.emr.resources.encounter.constants import COMPLETED_CHOICES
from care.utils.shortcuts import get_object_or_404
from care_emergency.constants import (
    BREAK_GLASS_HOURS,
)
from care_emergency.models import (
    BreakGlassAccess,
    StatutoryFlag,
)
from care_emergency.restricted import can_see_identity, is_restricted, new_alias
from care_emergency.serializers import (
    serialize_restriction,
)
from care_emergency.specs import (
    BreakGlassSpec,
    StatutoryFlagSpec,
)
from care_emergency.views.base import (
    EmergencyAPIView,
    can,
)


class RestrictionView(EmergencyAPIView):
    """Statutory confidentiality (D30). GET tells anyone who can see the
    patient whether they are restricted; the categories are shown only to
    those who can see the identity."""

    def get(self, request, patient_id):
        patient = get_object_or_404(Patient, external_id=patient_id)
        if not can("can_view_patient_obj", request.user, patient):
            raise PermissionDenied("You do not have permission to view this patient")
        visible = is_restricted(patient) and can_see_identity(request.user, patient)
        return Response(serialize_restriction(patient, request.user, visible))


class StatutoryFlagView(EmergencyAPIView):
    """Raised by the treating clinician in an encounter: needs clinical write
    access to it (doctors and nurses), which the front desk doesn't have."""

    def get(self, request, patient_id):
        patient = get_object_or_404(Patient, external_id=patient_id)
        if not can("can_view_patient_obj", request.user, patient):
            raise PermissionDenied("You do not have permission to view this patient")
        encounters = (
            Encounter.objects.filter(patient=patient)
            .exclude(status__in=[*COMPLETED_CHOICES, "discharged"])
            .select_related("facility", "patient")
            .order_by("-created_date")
        )
        return Response(
            [
                {
                    "id": str(encounter.external_id),
                    "status": encounter.status,
                    "encounter_class": encounter.encounter_class,
                    "created_date": encounter.created_date,
                }
                for encounter in encounters
                if can(
                    "can_write_service_request_in_encounter", request.user, encounter
                )
            ][:20]
        )

    def post(self, request, patient_id):
        spec = StatutoryFlagSpec.model_validate(request.data)
        patient = get_object_or_404(Patient, external_id=patient_id)
        encounter = get_object_or_404(
            Encounter, external_id=spec.encounter, patient=patient
        )
        if not can("can_write_service_request_in_encounter", request.user, encounter):
            raise PermissionDenied(
                "Only the treating clinician can raise a statutory flag"
            )
        if encounter.status in [*COMPLETED_CHOICES, "discharged"]:
            raise ValidationError("Statutory flags require an open encounter")
        with transaction.atomic():
            patient = Patient.objects.select_for_update().get(id=patient.id)
            if not is_restricted(patient):
                patient.restricted_alias = new_alias()
                patient.restricted_at = timezone.now()
                patient.save(update_fields=["restricted_alias", "restricted_at"])
            StatutoryFlag.objects.get_or_create(
                patient=patient,
                encounter=encounter,
                category=spec.category,
                defaults={"created_by": request.user, "updated_by": request.user},
            )
        return Response(serialize_restriction(patient, request.user, visible=True))


class BreakGlassView(EmergencyAPIView):
    """Shows a restricted patient's identity to the user for an hour, with
    the reason kept for audit. Anyone who can view the patient can do it;
    every use is recorded."""

    def post(self, request, patient_id):
        spec = BreakGlassSpec.model_validate(request.data)
        patient = get_object_or_404(Patient, external_id=patient_id)
        if not can("can_view_patient_obj", request.user, patient):
            raise PermissionDenied("You do not have permission to view this patient")
        if not is_restricted(patient):
            raise ValidationError("This patient is not restricted")
        BreakGlassAccess.objects.create(
            patient=patient,
            user=request.user,
            reason=spec.reason.strip(),
            expires_at=timezone.now() + datetime.timedelta(hours=BREAK_GLASS_HOURS),
        )
        return Response(serialize_restriction(patient, request.user, visible=True))
