"""The patient mobile app's sign-in and profile endpoints, in the app's own
contract: its paths, its field names and its {status, message, data}
envelope. See docs/mobile-app-login.md in the frontend repo.

Every patient on the verified number is offered as a profile, each with its
own tokens; a token then reaches only its own patient.
"""

import re

from django.conf import settings
from django.http import Http404
from rest_framework import exceptions, serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from care.emr.api.otp_viewsets.login import LoginOTP, check_otp, send_otp
from care.emr.models import Patient
from care.emr.resources.patient.hooks import patient_phone_allowed
from care.utils.models.validators import mobile_validator
from hmis.mobile_auth import (
    PatientAccessAuthentication,
    end_session,
    rotate,
    start_session,
)
from hmis.mobile_registration import (
    REGISTRATION_LIFETIME,
    ProfileFields,
    StrictSerializer,
    registration_proof,
    update_profile,
    validated,
)
from hmis.profile_photo import PHOTO_TYPES, photo_url, start_upload
from hmis.services import display_numbers

GENDERS = {"male": "Male", "female": "Female"}
BLOOD_GROUPS = {
    "A_positive": "A+",
    "A_negative": "A-",
    "B_positive": "B+",
    "B_negative": "B-",
    "AB_positive": "AB+",
    "AB_negative": "AB-",
    "O_positive": "O+",
    "O_negative": "O-",
}


def ok(data, message="Success", code=status.HTTP_200_OK):
    return Response({"status": "success", "message": message, "data": data}, code)


def error_message(detail):
    if isinstance(detail, dict):
        detail = next(iter(detail.values()), "")
    if isinstance(detail, list):
        detail = detail[0] if detail else ""
    if isinstance(detail, (dict, list)):
        return error_message(detail)
    return str(detail)


def mobile_exception_handler(exc, context):
    """Errors in the app's envelope. Unlike DRF's handler this doesn't roll
    the request back, so a wrong OTP's failure count and a revoked session
    are kept, as they are in Care's own OTP views."""
    if isinstance(exc, Http404):
        exc = exceptions.NotFound()
    if not isinstance(exc, exceptions.APIException):
        return None
    body = {"status": "error", "message": error_message(exc.detail)}
    code = exc.status_code
    if isinstance(exc, exceptions.ValidationError):
        code = status.HTTP_422_UNPROCESSABLE_ENTITY
        if isinstance(exc.detail, dict):
            body["errors"] = [
                {"field": field, "message": error_message(messages)}
                for field, messages in exc.detail.items()
            ]
    headers = {}
    if getattr(exc, "auth_header", None):
        headers["WWW-Authenticate"] = exc.auth_header
    if getattr(exc, "wait", None):
        headers["Retry-After"] = str(int(exc.wait))
    return Response(body, status=code, headers=headers)


class MobileView(APIView):
    authentication_classes = [PatientAccessAuthentication]
    permission_classes = [IsAuthenticated]

    def get_exception_handler(self):
        return mobile_exception_handler

    def get_authenticate_header(self, request):
        # Without one DRF turns a 401 into a 403, which the app never refreshes on.
        return 'Bearer realm="api"'


class PublicMobileView(MobileView):
    authentication_classes = []
    permission_classes = []


def phone_from(request):
    """The app sends the 10-digit number; Care stores +91 numbers."""
    digits = re.sub(r"\D", "", str(request.data.get("mobile") or ""))
    if len(digits) == 12 and digits.startswith("91"):  # noqa: PLR2004
        digits = digits[2:]
    phone = f"+91{digits}"
    try:
        if len(digits) != 10:  # noqa: PLR2004
            raise ValueError
        mobile_validator(phone)
    except Exception as e:
        raise exceptions.ValidationError(
            {"mobile": "Enter a valid 10-digit mobile number."}
        ) from e
    if not patient_phone_allowed(phone):
        raise exceptions.ValidationError(
            {
                "mobile": "This number can't be used to sign in. Use your own mobile number."
            }
        )
    return phone


def app_user(patient, account_id, numbers=None):
    """A Care patient in the app's user shape (usersApi.ts UserProfile),
    plus the number the hospital knows them by."""
    first, last = patient.first_name, patient.last_name
    if not first:
        first, _, last = patient.name.strip().partition(" ")
    if patient.middle_name:
        first = f"{first} {patient.middle_name}"
    number = (numbers or display_numbers([patient.id])).get(patient.id)
    has_birth = patient.date_of_birth or patient.year_of_birth
    return {
        "id": account_id,
        "title": patient.title,
        "first_name": first,
        "last_name": last,
        "email": patient.email,
        "mobile": patient.phone_number.removeprefix("+91"),
        "emergency_contact": patient.emergency_phone_number or None,
        "dob": patient.date_of_birth.isoformat() if patient.date_of_birth else None,
        "age": patient.age if has_birth else None,
        "gender": GENDERS.get(patient.gender, "Other" if patient.gender else ""),
        "gender_code": patient.gender,
        "blood_group": BLOOD_GROUPS.get(patient.blood_group),
        "address_line1": patient.address or None,
        "city": patient.city or None,
        "state": patient.state or None,
        "postal_code": str(patient.pincode) if patient.pincode else None,
        "country": (patient.meta.get("mobile_profile") or {}).get("country") or None,
        "profile_photo": photo_url(patient),
        "is_active": True,
        "patient_number": number["value"] if number else None,
        "patient_number_label": number["label"] if number else None,
    }


class SendOtpView(PublicMobileView):
    def post(self, request):
        phone = phone_from(request)
        send_otp(phone, otp_type=LoginOTP)
        data = {"mobile": phone.removeprefix("+91")}
        # The fixed code is only used when no SMS is sent; never show a real one.
        if not (settings.IS_PRODUCTION or settings.USE_SMS):
            data["otp"] = "456123"
        return ok(data, "OTP sent to your mobile number")


class VerifyOtpView(PublicMobileView):
    def post(self, request):
        phone = phone_from(request)
        otp = str(request.data.get("otp") or "")
        if len(otp) != settings.OTP_LENGTH or not otp.isdigit():
            raise exceptions.ValidationError(
                {"otp": f"Enter the {settings.OTP_LENGTH}-digit OTP."}
            )
        check_otp(phone, otp)
        patients = list(
            Patient.objects.filter(phone_number=phone).order_by("created_date")
        )
        if not patients:
            token = registration_proof(phone)
            return ok(
                {
                    "mobile": phone.removeprefix("+91"),
                    "total_accounts": 0,
                    "accounts": [],
                    "is_new_user": True,
                    "registration_token": token,
                    "expires_in": int(REGISTRATION_LIFETIME.total_seconds()),
                },
                "OTP verified. Complete your profile.",
            )
        numbers = display_numbers([p.id for p in patients])
        accounts = []
        for patient in patients:
            session, tokens = start_session(patient, phone)
            accounts.append(
                {**tokens, "user": app_user(patient, session.account_id, numbers)}
            )
        return ok(
            {
                "mobile": phone.removeprefix("+91"),
                "total_accounts": len(accounts),
                "accounts": accounts,
                "is_new_user": False,
            },
            "OTP verified",
        )


class RefreshView(PublicMobileView):
    def post(self, request):
        tokens = rotate(str(request.data.get("refresh_token") or ""))
        if tokens is None:
            raise exceptions.AuthenticationFailed(
                "Session expired. Please sign in again."
            )
        return ok(tokens, "Token refreshed")


class LogoutView(PublicMobileView):
    """Takes the refresh token rather than the access token, which may already
    have expired when the user signs out."""

    def post(self, request):
        end_session(str(request.data.get("refresh_token") or ""))
        return ok(None, "Logged out")


class MeView(MobileView):
    def patch(self, request):
        data = validated(ProfileFields, request.data, partial=True)
        patient = update_profile(request.user, data)
        return ok(app_user(patient, request.user.account.id), "Profile updated")

    def get(self, request):
        return ok(app_user(request.user.patient, request.user.account.id))


class PhotoUploadFields(StrictSerializer):
    mime_type = serializers.ChoiceField(
        choices=list(PHOTO_TYPES),
        error_messages={"invalid_choice": "Use a JPEG, PNG or WebP image."},
    )


class ProfilePhotoUploadView(MobileView):
    """A presigned PUT for a new photo. The photo shows on the profile once its
    upload_id is saved with PATCH /users/me."""

    def post(self, request):
        data = validated(PhotoUploadFields, request.data)
        upload = start_upload(
            request.user.patient, request.user.account, data["mime_type"]
        )
        return ok(upload, "Upload the photo, then save it", status.HTTP_201_CREATED)


class FcmTokenView(MobileView):
    def post(self, request):
        session = request.user.session
        session.fcm_token = str(request.data.get("fcm_token") or "")[:512]
        session.save(update_fields=["fcm_token"])
        return ok(None, "FCM token saved")
