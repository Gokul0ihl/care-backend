"""Patient app server: link and register app users' patients."""

from django.db import IntegrityError, transaction
from django.db.models import Q
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from care.emr.api.viewsets.patient import PatientViewSet
from care.emr.models import Patient
from care.emr.resources.patient.spec import PatientRetrieveSpec
from care.utils.shortcuts import get_object_or_404
from hmis.app_server import (
    check_service_patient,
    link_app_patient,
)
from hmis.constants import (
    PatientNumberSeries,
)
from hmis.contact import is_desk_phone
from hmis.models import (
    AppPatientLink,
)
from hmis.services import (
    issue_patient_number,
    patient_number,
    unit_code,
)
from hmis.specs import (
    AppRegisterSpec,
)
from hmis.views.base import (
    EmergencyAPIView,
    can,
    care_view,
    get_facility,
)


def abha_matches(abha_number):
    """Live patients with this ABHA number, in the desk column or Care's
    registration details (meta, still written by AppPatientView and older
    records)."""
    return list(
        Patient.objects.filter(
            Q(abha_number=abha_number) | Q(meta__registration__abha_number=abha_number)
        )
    )


def app_patient_body(spec, registration):
    """Care's patient fields from the app profile. The registration details
    the desk may already hold are kept; the app's fields are laid over them."""
    registration = {
        **registration,
        "prefix": spec.title,
        "first_name": spec.first_name,
        # Care's registration wants 2+ letters; a lone initial stays in the name.
        "last_name": spec.last_name if len(spec.last_name) > 1 else "",
        "email": spec.email,
    }
    if spec.abha_number:
        registration.update(
            abha_number=spec.abha_number,
            abha_status="has",
            identity_storage_consent=True,
        )
    return {
        "name": f"{spec.first_name} {spec.last_name}".strip(),
        "gender": spec.gender,
        "phone_number": spec.phone_number,
        "date_of_birth": spec.date_of_birth.isoformat(),
        "address": spec.address or None,
        "pincode": int(spec.pincode) if spec.pincode else None,
        "registration": registration,
    }


def serialize_app_patient(link):
    patient = link.patient
    return {
        "app_user_id": link.app_user_id,
        "matched": link.matched,
        "uhid": patient_number(patient, PatientNumberSeries.uhid),
        "pre_registration_number": patient_number(
            patient, PatientNumberSeries.pre_registration
        ),
        "patient": PatientRetrieveSpec.serialize(patient).to_json(),
    }


class AppPatientView(EmergencyAPIView):
    """The patient app's server registers a person when they finish their
    profile (docs/app-server.md). Safe to repeat: the app's id finds the
    same patient, whose details are then updated. A new patient gets a PRE-
    reference; the UHID comes on payment or at the first visit. A verified
    ABHA number that belongs to exactly one patient links that patient
    instead, without changing their details."""

    def post(self, request):
        if not request.user.is_service_account:
            raise PermissionDenied("Only the app server registers app profiles")
        spec = AppRegisterSpec.model_validate(request.data)
        facility = get_facility(spec.facility)
        if not can("can_register_patient_at_facility_obj", request.user, facility):
            raise PermissionDenied("You do not have access to this facility")
        if is_desk_phone(spec.phone_number):
            raise ValidationError("Enter the person's own mobile number")
        unit_code(facility)

        link = (
            AppPatientLink.objects.select_related("patient")
            .filter(app_user_id=spec.app_user_id)
            .first()
        )
        if link:
            check_service_patient(request.user, link.patient)
            if not link.matched:
                with transaction.atomic():
                    patient = link.patient
                    care_view(
                        PatientViewSet,
                        request,
                        "update",
                        external_id=str(patient.external_id),
                    ).handle_update(
                        patient,
                        app_patient_body(
                            spec, (patient.meta or {}).get("registration") or {}
                        ),
                    )
                link.patient.refresh_from_db()
            return Response(serialize_app_patient(link))

        try:
            with transaction.atomic():
                matches = abha_matches(spec.abha_number) if spec.abha_number else []
                if len(matches) == 1:
                    patient = matches[0]
                else:
                    data = care_view(PatientViewSet, request, "create").handle_create(
                        app_patient_body(spec, {})
                    )
                    patient = Patient.objects.get(external_id=data["id"])
                    issue_patient_number(
                        patient, facility, PatientNumberSeries.pre_registration
                    )
                link = AppPatientLink.objects.create(
                    app_user_id=spec.app_user_id,
                    patient=patient,
                    matched=len(matches) == 1,
                    created_by=request.user,
                    updated_by=request.user,
                )
                link_app_patient(request.user, patient)
        except IntegrityError as e:
            # Two calls for the same new profile at once; the other one won.
            raise ValidationError("This profile is already being registered") from e
        return Response(serialize_app_patient(link), status=status.HTTP_201_CREATED)


class AppPatientDetailView(EmergencyAPIView):
    def get(self, request, app_user_id):
        link = get_object_or_404(
            AppPatientLink.objects.select_related("patient"), app_user_id=app_user_id
        )
        check_service_patient(request.user, link.patient)
        if not can("can_view_patient_obj", request.user, link.patient):
            raise PermissionDenied("You do not have permission to view this patient")
        return Response(serialize_app_patient(link))
