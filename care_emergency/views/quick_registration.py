"""Quick Register and the patient's full-registration form link."""

import datetime
import secrets

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle

from care.emr.api.viewsets.patient import PatientViewSet
from care.emr.models import FacilityOrganization, Patient
from care.emr.models.organization import FacilityOrganizationUser
from care.emr.resources.patient.spec import PatientRetrieveSpec
from care.users.models import User
from care.utils.shortcuts import get_object_or_404
from care.utils.sms import send_text_message
from care_emergency.constants import (
    QUICK_FORM_LINK_DAYS,
    PatientNumberSeries,
)
from care_emergency.contact import is_desk_phone
from care_emergency.pincodes import plug_setting
from care_emergency.serializers import (
    serialize_quick_registration,
)
from care_emergency.services import (
    issue_patient_number,
    unit_code,
)
from care_emergency.specs import (
    QuickFormSpec,
    QuickRegisterSpec,
)
from care_emergency.views.base import (
    EmergencyAPIView,
    can,
    care_view,
    get_facility,
)
from care_emergency.views.pincodes import pincode_response

# Related rows serialize_quick_registration reads.
QUICK_PATIENT_RELATED = (
    "quick_facility",
    "quick_department",
    "quick_doctor",
    "quick_registered_by",
)


def quick_patients():
    """Patients registered through Quick Register."""
    return Patient.objects.filter(quick_registered_at__isnull=False)


def quick_form_link(patient):
    base = plug_setting("REGISTRATION_FORM_BASE_URL") or settings.CURRENT_DOMAIN
    return f"{base.rstrip('/')}/register/complete/{patient.quick_form_token}"


def send_quick_form_link(patient_id):
    """Texts the patient the full registration form. Runs after commit, so a
    rolled-back registration never sends a link."""
    patient = quick_patients().select_related("quick_facility").get(id=patient_id)
    sent = send_text_message(
        content=(
            f"{patient.quick_facility.name}: Hi {patient.first_name}, please "
            f"complete your registration before your visit: "
            f"{quick_form_link(patient)} (valid for {QUICK_FORM_LINK_DAYS} days)"
        ),
        recipients=patient.phone_number,
        fail_silently=True,
    )
    if sent:
        Patient.objects.filter(id=patient.id).update(
            quick_form_link_sent_at=timezone.now()
        )
    return bool(sent)


class QuickRegistrationView(EmergencyAPIView):
    """Quick Register (phone booking): creates the Care patient with a PRE-
    reference, sets the Quick Register columns that mark how they were
    registered, then texts the full registration form if asked."""

    @classmethod
    def as_view(cls, **initkwargs):
        # Commits before responding, so the response says whether the link
        # was actually texted (it is sent once the patient is saved).
        return transaction.non_atomic_requests(super().as_view(**initkwargs))

    def post(self, request):
        spec = QuickRegisterSpec.model_validate(request.data)
        facility = get_facility(spec.facility)
        if not can("can_register_patient_at_facility_obj", request.user, facility):
            raise PermissionDenied("You do not have access to this facility")
        if is_desk_phone(spec.phone_number):
            raise ValidationError("Enter the patient's own mobile number")
        department = None
        if spec.department:
            department = get_object_or_404(
                FacilityOrganization,
                external_id=spec.department,
                facility=facility,
                org_type="dept",
            )
        doctor = None
        if spec.doctor:
            doctor = get_object_or_404(User, external_id=spec.doctor)
            if not FacilityOrganizationUser.objects.filter(
                user=doctor, organization__facility=facility
            ).exists():
                raise ValidationError("The doctor does not work at this facility")
        unit_code(facility)

        with transaction.atomic():
            patient_data = care_view(PatientViewSet, request, "create").handle_create(
                {
                    "name": f"{spec.first_name} {spec.last_name}".strip(),
                    "gender": spec.gender,
                    "phone_number": spec.phone_number,
                    "date_of_birth": spec.date_of_birth.isoformat(),
                }
            )
            patient = Patient.objects.get(external_id=patient_data["id"])
            issue_patient_number(
                patient, facility, PatientNumberSeries.pre_registration
            )
            # A fresh instance, so Care's create can't overwrite the columns.
            patient = Patient.objects.get(id=patient.id)
            columns = {
                "title": spec.title,
                "first_name": spec.first_name,
                "last_name": spec.last_name,
                "quick_registered_at": timezone.now(),
                "quick_registered_by": request.user,
                "quick_facility": facility,
                "quick_consultation_type": spec.consultation_type,
                "quick_department": department,
                "quick_doctor": doctor,
                "quick_whatsapp_same_as_mobile": spec.whatsapp_same_as_mobile,
                "quick_send_form_link": spec.send_form_link,
                "quick_send_payment_reminder": spec.send_payment_reminder,
                "quick_form_token": secrets.token_urlsafe(32),
                "quick_form_expires_at": timezone.now()
                + datetime.timedelta(days=QUICK_FORM_LINK_DAYS),
                "updated_by": request.user,
            }
            for name, value in columns.items():
                setattr(patient, name, value)
            patient.save(update_fields=[*columns, "modified_date"])
            if spec.send_form_link:
                transaction.on_commit(lambda: send_quick_form_link(patient.id))
            patient_data = PatientRetrieveSpec.serialize(patient).to_json()

        return Response(
            {
                "patient": patient_data,
                "quick_registration": serialize_quick_registration(
                    quick_patients()
                    .select_related(*QUICK_PATIENT_RELATED)
                    .get(id=patient.id)
                ),
            }
        )


class PatientQuickRegistrationView(EmergencyAPIView):
    def get_record(self, request, patient_id):
        """The quick-registered patient; 404 for any other patient."""
        patient = get_object_or_404(Patient, external_id=patient_id)
        if not can("can_view_patient_obj", request.user, patient):
            raise PermissionDenied("You do not have permission to view this patient")
        return get_object_or_404(
            quick_patients().select_related(*QUICK_PATIENT_RELATED), id=patient.id
        )

    def get(self, request, patient_id):
        return Response(
            serialize_quick_registration(self.get_record(request, patient_id))
        )


class QuickFormLinkView(PatientQuickRegistrationView):
    """Texts the registration form again (a failed SMS, a lost message) and
    keeps the link open for another QUICK_FORM_LINK_DAYS."""

    http_method_names = ["post"]

    @classmethod
    def as_view(cls, **initkwargs):
        return transaction.non_atomic_requests(super().as_view(**initkwargs))

    def post(self, request, patient_id):
        patient = self.get_record(request, patient_id)
        if patient.quick_form_submitted_at:
            raise ValidationError("The patient has already submitted the form")
        if is_desk_phone(patient.phone_number):
            raise ValidationError("The patient has no mobile number to text")
        patient.quick_send_form_link = True
        patient.quick_form_expires_at = timezone.now() + datetime.timedelta(
            days=QUICK_FORM_LINK_DAYS
        )
        patient.updated_by = request.user
        patient.save(
            update_fields=[
                "quick_send_form_link",
                "quick_form_expires_at",
                "updated_by",
                "modified_date",
            ]
        )
        if not send_quick_form_link(patient.id):
            raise ValidationError("The message couldn't be sent. Try again later")
        patient.refresh_from_db()
        return Response(serialize_quick_registration(patient))


class QuickFormThrottle(AnonRateThrottle):
    rate = "30/min"


class QuickFormView(EmergencyAPIView):
    """The texted registration form. Open to the patient without a login: the
    unguessable token is the credential, it expires, and it takes one answer."""

    authentication_classes = []
    permission_classes = []
    throttle_classes = [QuickFormThrottle]

    def get_record(self, token, *, lock=False):
        """The quick-registered patient the token belongs to."""
        patients = quick_patients()
        if lock:
            # No select_related: FOR UPDATE would lock the facility row too.
            patients = patients.select_for_update()
        else:
            patients = patients.select_related("quick_facility")
        return get_object_or_404(patients, quick_form_token=token)

    def get(self, request, token):
        patient = self.get_record(token)
        if patient.quick_form_submitted_at:
            state = "submitted"
        elif patient.quick_form_expires_at <= timezone.now():
            state = "expired"
        else:
            state = "open"
        phone = patient.phone_number or ""
        return Response(
            {
                "status": state,
                "first_name": patient.first_name,
                "facility_name": patient.quick_facility.name,
                "phone_last4": phone[-4:],
                "whatsapp_same_as_mobile": patient.quick_whatsapp_same_as_mobile,
                "expires_at": patient.quick_form_expires_at,
            }
        )

    def post(self, request, token):
        spec = QuickFormSpec.model_validate(request.data)
        with transaction.atomic():
            patient = self.get_record(token, lock=True)
            if patient.quick_form_submitted_at:
                raise ValidationError("This form has already been submitted")
            if patient.quick_form_expires_at <= timezone.now():
                raise ValidationError(
                    "This link has expired. Please call the hospital for a new one"
                )
            phone = patient.phone_number
            if spec.alternate_phone_number and spec.alternate_phone_number == phone:
                raise ValidationError(
                    "The alternate mobile must differ from the main number"
                )
            details = spec.model_dump(mode="json")
            if patient.quick_whatsapp_same_as_mobile:
                details["whatsapp_number"] = phone
            patient.quick_form_details = details
            patient.quick_form_submitted_at = timezone.now()
            patient.save(
                update_fields=["quick_form_details", "quick_form_submitted_at"]
            )
        return Response({"status": "submitted"})


class QuickFormPincodeView(QuickFormView):
    """The PIN lookup for the texted form, open only to a live link."""

    http_method_names = ["get"]

    def get(self, request, token, pincode):
        patient = self.get_record(token)
        if (
            patient.quick_form_submitted_at
            or patient.quick_form_expires_at <= timezone.now()
        ):
            raise PermissionDenied("This link is no longer open")
        return pincode_response(pincode)
