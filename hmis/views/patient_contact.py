"""Changing a patient's main mobile and its owner."""

from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from care.emr.api.viewsets.patient import PatientViewSet
from care.emr.models import Patient
from care.emr.resources.patient.spec import PatientRetrieveSpec
from care.utils.shortcuts import get_object_or_404
from hmis.billing import sync_billing_tags
from hmis.contact import is_desk_phone
from hmis.restricted import can_see_identity, is_restricted
from hmis.serializers import (
    serialize_registration,
)
from hmis.specs import (
    DeskContactSpec,
    DeskRegisterSpec,
    RegistrationSpec,
)
from hmis.views.base import (
    EmergencyAPIView,
    can,
    care_view,
)


class PatientContactView(EmergencyAPIView):
    """Replace a No phone fallback with a real contact, preserving identifiers."""

    def patch(self, request, patient_id):
        spec = DeskContactSpec.model_validate(request.data)
        patient = get_object_or_404(Patient, external_id=patient_id)
        if not can("can_write_patient_obj", request.user, patient):
            raise PermissionDenied("Cannot update this patient")
        if is_restricted(patient) and not can_see_identity(request.user, patient):
            raise PermissionDenied("Identity access is required to update a contact")
        if is_desk_phone(spec.phone_number):
            raise ValidationError("Enter a real contact number, not the desk number")
        with transaction.atomic():
            record = get_object_or_404(
                Patient.objects.select_for_update(),
                id=patient.id,
                registered_at__isnull=False,
            )
            if record.mobile_owner != "No phone":
                raise ValidationError("This patient already has a contact number")
            registration = RegistrationSpec.model_construct(
                **{
                    **serialize_registration(record),
                    "mobile_owner": spec.mobile_owner,
                    "whatsapp_number": record.whatsapp_number or None,
                    "alternate_phone_number": record.alternate_phone_number or None,
                    "next_of_kin_phone": record.next_of_kin_phone or None,
                }
            )
            # Apply contact validation without revalidating unrelated demographics.
            facility = record.registration_facility
            validated = DeskRegisterSpec.model_construct(
                facility=facility.external_id if facility else None,
                patient={"pincode": patient.pincode, "phone_number": spec.phone_number},
                registration=registration,
            )
            try:
                validated.validate_contact()
            except ValueError as error:
                raise ValidationError(str(error)) from error
            if patient.age < 18 and spec.mobile_owner == "Self":
                raise ValidationError(
                    "A minor's mobile must belong to a parent, guardian or relative"
                )
            view = care_view(
                PatientViewSet, request, "update", external_id=str(patient_id)
            )
            # Care's update spec clears PIN when omitted, even for a partial update.
            view.handle_update(
                patient, {"phone_number": spec.phone_number, "pincode": patient.pincode}
            )
            # A fresh instance: Care's update saved its own copy of the patient.
            record = Patient.objects.get(id=patient.id)
            record.mobile_owner = spec.mobile_owner
            record.updated_by = request.user
            record.save(update_fields=["mobile_owner", "updated_by", "modified_date"])
            sync_billing_tags(record)
            patient.refresh_from_db()
        return Response(PatientRetrieveSpec.serialize(patient).to_json())
