"""Front-desk registration: create and edit."""

from django.db import transaction
from django.http import Http404
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from care.emr.api.viewsets.patient import PatientViewSet
from care.emr.locks.billing import PatientCreateLock
from care.emr.models import Patient
from care.emr.resources.patient.spec import PatientRetrieveSpec
from care.utils.shortcuts import get_object_or_404
from care.utils.time_util import care_now
from hmis.billing import sync_billing_tags
from hmis.constants import (
    PatientNumberSeries,
)
from hmis.contact import is_desk_phone
from hmis.models import (
    ArtCouple,
    PatientDocument,
    RegistrationAllergy,
)
from hmis.restricted import can_see_identity, is_restricted
from hmis.serializers import (
    serialize_registration,
    serialize_restricted_registration,
)
from hmis.services import (
    attach_documents,
    emergency_desk_phone,
    emergency_encounters,
    issue_patient_number,
    missing_documents,
    staged_documents,
    unit_code,
)
from hmis.specs import (
    DeskRegisterSpec,
    DeskRegistrationUpdateSpec,
)
from hmis.views.base import (
    EmergencyAPIView,
    can,
    care_view,
    get_facility,
)


def registration_columns(registration):
    """Desk registration column values (on Patient) from a RegistrationSpec.
    Phone numbers are optional; the columns keep "" for none."""
    details = registration.model_dump(exclude={"allergies"})
    return {
        key: "" if value is None and key.endswith(("_number", "_phone")) else value
        for key, value in details.items()
    }


DESK_PATIENT_RELATED = ("registration_facility", "vip_flagged_by", "registered_by")


def save_desk_registration(
    patient, details, user, facility, allergies=(), *, replace_allergies=False
):
    """Writes the desk registration columns onto the patient, after Care's own
    create or update so Care's save can't overwrite them (update_fields).
    The first desk write registers the patient: registered_at, registered_by
    and registration_facility are set then and kept on later edits. Then the
    allergies, and the payer tags (billing.sync_billing_tags)."""
    for key, value in details.items():
        setattr(patient, key, value)
    fields = [*details, "vip_flagged_by", "updated_by", "modified_date"]
    if patient.registered_at is None:
        patient.registered_at = care_now()
        patient.registered_by = user
        patient.registration_facility = facility
        fields += ["registered_at", "registered_by", "registration_facility"]
    # Whoever first flagged the VIP stays recorded while the flag is on.
    if not details.get("is_vip"):
        patient.vip_flagged_by = None
    elif patient.vip_flagged_by is None:
        patient.vip_flagged_by = user
    patient.updated_by = user
    patient.save(update_fields=fields)
    if replace_allergies:
        patient.desk_allergies.all().delete()
    RegistrationAllergy.objects.bulk_create(
        RegistrationAllergy(patient=patient, **allergy.model_dump())
        for allergy in allergies
    )
    sync_billing_tags(patient)


def desk_patient(patient_id):
    """The patient with what serialize_registration reads, from the database
    (after Care's save)."""
    return (
        Patient.objects.select_related(*DESK_PATIENT_RELATED)
        .prefetch_related("desk_allergies")
        .get(external_id=patient_id)
    )


class RegistrationView(EmergencyAPIView):
    """Front-desk registration: creates the Care patient, writes its desk
    registration columns, an IVF partner and the desk documents in one
    transaction."""

    def post(self, request):
        spec = DeskRegisterSpec.model_validate(request.data)
        facility = get_facility(spec.facility) if spec.facility else None
        details = registration_columns(spec.registration)
        patient_body = dict(spec.patient)
        if spec.registration.mobile_owner == "No phone":
            if not can("can_register_patient_at_facility_obj", request.user, facility):
                raise PermissionDenied("You do not have access to this facility")
            patient_body["phone_number"] = emergency_desk_phone(facility)
        elif is_desk_phone(patient_body.get("phone_number")):
            raise ValidationError("Select No phone instead of entering the desk number")
        couple = spec.couple
        partner = couple.partner if couple else None
        if set(spec.documents) & set(partner.documents if partner else []):
            raise ValidationError("A document can't belong to both partners")

        if facility:
            unit_code(facility)

        with transaction.atomic():
            documents = staged_documents(request.user, spec.documents)
            partner_documents = (
                staged_documents(request.user, partner.documents) if partner else []
            )
            missing = missing_documents(
                spec.registration, documents, couple, partner_documents
            )
            if missing:
                err = f"Upload {', '.join(missing)}"
                raise ValidationError(err)
            series = (
                PatientNumberSeries.pre_registration
                if spec.pre_registration
                else PatientNumberSeries.uhid
            )
            patient_data = care_view(PatientViewSet, request, "create").handle_create(
                patient_body
            )
            patient = Patient.objects.get(external_id=patient_data["id"])
            if facility:
                issue_patient_number(patient, facility, series)
                patient_data = PatientRetrieveSpec.serialize(patient).to_json()
            patient = Patient.objects.get(id=patient.id)
            save_desk_registration(
                patient,
                details,
                request.user,
                facility,
                spec.registration.allergies,
            )
            attach_documents(documents, patient)
            if couple:
                self.register_couple(
                    request, spec, patient, facility, series, partner_documents
                )
            patient_data = PatientRetrieveSpec.serialize(patient).to_json()
            registration = serialize_registration(desk_patient(patient.external_id))

        return Response({"patient": patient_data, "registration": registration})

    def register_couple(self, request, spec, woman, facility, series, documents):
        """The partner becomes his own Care patient, at the woman's address."""
        couple = spec.couple
        partner_patient = None
        if couple.partner:
            partner = couple.partner
            # Care holds one patient-create lock until commit, so a second
            # create in the same transaction would be refused. Its job is
            # identifier uniqueness checks, and the partner has no
            # staff-entered identifiers.
            PatientCreateLock().release()
            data = care_view(PatientViewSet, request, "create").handle_create(
                {
                    "name": partner.name,
                    "gender": "male",
                    "date_of_birth": partner.date_of_birth.isoformat(),
                    "phone_number": partner.phone_number,
                    "address": spec.patient.get("address"),
                    "permanent_address": spec.patient.get("permanent_address"),
                    "pincode": spec.patient.get("pincode"),
                    "geo_organization": spec.patient.get("geo_organization"),
                }
            )
            partner_patient = Patient.objects.get(external_id=data["id"])
            if facility:
                issue_patient_number(partner_patient, facility, series)
            attach_documents(documents, partner_patient)
        ArtCouple.objects.create(
            woman=woman,
            partner=partner_patient,
            facility=facility,
            single_woman=couple.single_woman,
            art_consent=couple.art_consent,
            partner_identity_card_type=couple.partner.identity_card_type
            if couple.partner
            else "",
            partner_identity_number_last4=couple.partner.identity_number_last4
            if couple.partner
            else "",
            partner_abha_number=couple.partner.abha_number if couple.partner else "",
            partner_abha_address=couple.partner.abha_address if couple.partner else "",
            created_by=request.user,
            updated_by=request.user,
        )


class PatientRegistrationView(EmergencyAPIView):
    def get(self, request, patient_id):
        patient = get_object_or_404(
            Patient.objects.select_related(*DESK_PATIENT_RELATED).prefetch_related(
                "desk_allergies"
            ),
            external_id=patient_id,
        )
        if not can("can_view_patient_obj", request.user, patient):
            raise PermissionDenied("You do not have permission to view this patient")
        if patient.registered_at is None:
            # The frontend reads 404 as "not registered at the desk".
            raise Http404("This patient has no desk registration")
        if is_restricted(patient) and not can_see_identity(request.user, patient):
            return Response(serialize_restricted_registration(patient))
        return Response(serialize_registration(patient))

    def put(self, request, patient_id):
        """Edits a registered patient with the registration form: Care's own
        patient update, then the desk columns (registering a patient the desk
        hasn't, e.g. one registered elsewhere first). The UHID is kept."""
        patient = get_object_or_404(Patient, external_id=patient_id)
        if not can("can_write_patient_obj", request.user, patient):
            raise PermissionDenied("You do not have permission to edit this patient")
        if is_restricted(patient) and not can_see_identity(request.user, patient):
            raise PermissionDenied("This patient's record is restricted")
        if (
            emergency_encounters()
            .filter(patient=patient, converted_at__isnull=True)
            .exists()
        ):
            raise ValidationError("Complete the patient's emergency registration first")
        spec = DeskRegistrationUpdateSpec.model_validate(request.data)
        facility = get_facility(spec.facility) if spec.facility else None
        details = registration_columns(spec.registration)
        patient_body = dict(spec.patient)
        if spec.registration.mobile_owner == "No phone":
            patient_body["phone_number"] = emergency_desk_phone(facility)
        elif is_desk_phone(patient_body.get("phone_number")):
            raise ValidationError("Select No phone instead of entering the desk number")

        with transaction.atomic():
            documents = staged_documents(request.user, spec.documents)
            existing = list(PatientDocument.objects.filter(patient=patient))
            missing = missing_documents(spec.registration, existing + documents)
            if missing:
                err = f"Upload {', '.join(missing)}"
                raise ValidationError(err)
            care_view(
                PatientViewSet, request, "update", external_id=str(patient_id)
            ).handle_update(patient, patient_body)
            patient = Patient.objects.get(id=patient.id)
            save_desk_registration(
                patient,
                details,
                request.user,
                facility,
                spec.registration.allergies,
                replace_allergies=True,
            )
            attach_documents(documents, patient)
            patient.refresh_from_db()
            patient_data = PatientRetrieveSpec.serialize(patient).to_json()
            registration = serialize_registration(desk_patient(patient.external_id))

        return Response({"patient": patient_data, "registration": registration})
