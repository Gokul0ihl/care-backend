"""UHID issue, UHID search and the search results' summary."""

from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from care.emr.models import Patient
from care.emr.models.patient import PatientIdentifier
from care.emr.resources.patient.spec import PatientRetrieveSpec
from care.security.authorization import AuthorizationController
from care.utils.shortcuts import get_object_or_404
from hmis.merge import (
    last_visits,
    resolve_merged,
)
from hmis.services import (
    display_numbers,
    emergency_encounters,
    issue_patient_number,
)
from hmis.specs import (
    PatientNumberIssueSpec,
    PatientSummarySpec,
)
from hmis.views.base import (
    EmergencyAPIView,
    can,
    get_facility,
)


class PatientNumberView(EmergencyAPIView):
    """Issues the patient's UHID if they don't have one yet: on payment for a
    pre-registration, or for a patient registered through Care's own form."""

    def post(self, request, patient_id):
        spec = PatientNumberIssueSpec.model_validate(request.data)
        patient = get_object_or_404(Patient, external_id=patient_id)
        if not can("can_write_patient_obj", request.user, patient):
            raise PermissionDenied("You do not have permission to update this patient")
        facility = get_facility(spec.facility)
        if issue_number_blocked(patient):
            raise ValidationError(
                "Complete this emergency patient's registration to issue a UHID"
            )
        with transaction.atomic():
            issue_patient_number(patient, facility)
        return Response(PatientRetrieveSpec.serialize(patient).to_json())


def issue_number_blocked(patient):
    return (
        emergency_encounters()
        .filter(patient=patient, converted_at__isnull=True, is_unidentified=True)
        .exists()
    )


class PatientNumberSearchView(EmergencyAPIView):
    """Exact UHID lookup; return only patients the caller can view."""

    def get(self, request):
        value = request.query_params.get("uhid", "").strip().upper()
        if not value or len(value) > 40:
            raise ValidationError("Enter the full UHID")
        # A merged-away record's UHID is kept (soft-deleted) on it, and leads
        # to the record it was merged into.
        ids = PatientIdentifier._base_manager.filter(  # noqa: SLF001
            config__config__system="care_emergency/uhid", value=value
        ).values_list("patient_id", flat=True)
        results = [
            PatientRetrieveSpec.serialize(patient).to_json()
            for patient in Patient.objects.filter(id__in=resolve_merged(ids))
            if can("can_view_patient_obj", request.user, patient)
        ]
        return Response({"count": len(results), "results": results})


class PatientSummaryView(EmergencyAPIView):
    """The patient number and last visit of each search result, in one call.
    Unknown, deleted and not viewable patients are left out."""

    def post(self, request):
        spec = PatientSummarySpec.model_validate(request.data)
        # Narrow rows (Patient.list_deferred_fields), keeping the desk
        # facility that the desk access check below reads.
        patients = (
            Patient.objects.filter(external_id__in=spec.patients)
            .defer(
                *[
                    x
                    for x in Patient.list_deferred_fields()
                    if x != "registration_facility"
                ]
            )
            .select_related("registration_facility")
        )
        visible = set(
            AuthorizationController.call(
                "get_filtered_patients", patients, request.user
            ).values_list("id", flat=True)
        )
        # Desk access (Patient.registration_facility, contact.DeskPatientAccess)
        # isn't in Care's filter.
        allowed = {
            patient.external_id: patient
            for patient in patients
            if patient.id in visible
            or can("can_view_patient_obj", request.user, patient)
        }
        ids = [x.id for x in allowed.values()]
        numbers = display_numbers(ids)
        visits = last_visits(ids)
        results = [
            {
                "id": str(external_id),
                "patient_number": numbers.get(allowed[external_id].id),
                "last_visit": visits.get(allowed[external_id].id),
            }
            for external_id in dict.fromkeys(spec.patients)
            if external_id in allowed
        ]
        return Response({"results": results})
