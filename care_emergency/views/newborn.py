"""Newborn registration."""

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from care.emr.api.viewsets.patient import PatientViewSet
from care.emr.locks.billing import PatientCreateLock
from care.emr.models import Patient
from care.emr.resources.patient.spec import PatientRetrieveSpec
from care.utils.shortcuts import get_object_or_404
from care_emergency.merge import (
    is_unconverted_emergency,
)
from care_emergency.newborn import (
    baby_name,
    baby_registration,
    save_birth,
    serialize_birth,
)
from care_emergency.restricted import can_see_identity, is_restricted
from care_emergency.services import (
    issue_patient_number,
    unit_code,
)
from care_emergency.specs import (
    NewbornRegisterSpec,
)
from care_emergency.views.base import (
    EmergencyAPIView,
    can,
    care_view,
    get_facility,
)
from care_emergency.views.registration import save_desk_registration


class NewbornRegistrationView(EmergencyAPIView):
    """Registers the babies of one delivery: each baby becomes their own Care
    patient with their own UHID and a desk registration inherited from the
    mother's, linked to her by the baby's birth columns (birth_mother). One transaction, so twins
    are registered together or not at all."""

    def post(self, request):
        spec = NewbornRegisterSpec.model_validate(request.data)
        facility = get_facility(spec.facility)
        if not can("can_register_patient_at_facility_obj", request.user, facility):
            raise PermissionDenied("You do not have access to this facility")
        mother = get_object_or_404(Patient, external_id=spec.mother)
        if not can("can_view_patient_obj", request.user, mother):
            raise PermissionDenied("You do not have permission to view the mother")
        if is_restricted(mother) and not can_see_identity(request.user, mother):
            # The baby's name would carry the mother's masked identity.
            raise PermissionDenied(
                "The mother's record is restricted. Ask her clinician to register the baby"
            )
        if mother.gender != "female":
            raise ValidationError("The mother's record must be a female patient")
        if is_unconverted_emergency(mother):
            raise ValidationError(
                "Complete the mother's emergency registration before registering the baby"
            )
        # Her desk registration, if the desk registered her.
        mother_record = mother if mother.registered_at else None
        geo_organization = mother.geo_organization or facility.geo_organization
        unit_code(facility)

        number_of_babies = len(spec.babies)
        babies = []
        with transaction.atomic():
            for order, baby in enumerate(spec.babies, start=1):
                # Care holds one patient-create lock until commit; the babies
                # have no staff-entered identifiers for it to check.
                PatientCreateLock().release()
                data = care_view(PatientViewSet, request, "create").handle_create(
                    {
                        "name": baby_name(mother.name, order, number_of_babies),
                        "gender": baby.gender,
                        "date_of_birth": timezone.localtime(baby.born_at)
                        .date()
                        .isoformat(),
                        "phone_number": mother.phone_number,
                        "address": mother.address,
                        "permanent_address": mother.permanent_address,
                        "pincode": mother.pincode,
                        "geo_organization": str(geo_organization.external_id)
                        if geo_organization
                        else None,
                    }
                )
                patient = Patient.objects.get(external_id=data["id"])
                issue_patient_number(patient, facility)
                patient = Patient.objects.get(id=patient.id)
                save_desk_registration(
                    patient,
                    baby_registration(mother, mother_record, baby.gender),
                    request.user,
                    facility,
                )
                save_birth(
                    patient,
                    {
                        "birth_mother": mother,
                        "birth_facility": facility,
                        "born_at": baby.born_at,
                        "birth_order": order,
                        "number_of_babies": number_of_babies,
                        "delivery_type": spec.delivery_type,
                        "gestation_weeks": spec.gestation_weeks,
                        "birth_weight_grams": baby.birth_weight_grams,
                        "length_cm": baby.length_cm,
                        "head_circumference_cm": baby.head_circumference_cm,
                        "apgar_1_min": baby.apgar_1_min,
                        "apgar_5_min": baby.apgar_5_min,
                    },
                    request.user,
                )
                patient = Patient.objects.select_related(
                    "birth_mother", "birth_facility"
                ).get(id=patient.id)
                babies.append(
                    {
                        "patient": PatientRetrieveSpec.serialize(patient).to_json(),
                        "birth": serialize_birth(patient),
                    }
                )

        return Response(
            {
                "mother": str(mother.external_id),
                "inherited": mother_record is not None,
                "babies": babies,
            }
        )
