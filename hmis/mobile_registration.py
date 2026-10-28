"""OTP-proven mobile registration and self-profile edits, without staff privileges."""

import hashlib
import re
import secrets
from datetime import date, timedelta

from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import (
    APIException,
    AuthenticationFailed,
    ValidationError,
)

from care.emr.locks.billing import PatientCreateLock
from care.emr.models import Patient
from care.emr.resources.patient_identifier.default_expression_evaluator import (
    evaluate_patient_instance_default_values,
)
from care.facility.models import Facility
from care.utils.models.validators import mobile_validator
from hmis.constants import PatientNumberSeries
from hmis.mobile_auth import session_usable, start_session
from hmis.models import MobileRegistration, PatientAccount, PatientSession
from hmis.profile_photo import set_photo
from hmis.services import issue_patient_number
from hmis.specs import LAST_NAME, NAME_PART, allowed_titles, years_old

MAX_AGE = 120
ADULT_AGE = 18
MIN_PASSWORD_LENGTH = 8
MOBILE_DIGITS = 10
REGISTRATION_LIFETIME = timedelta(minutes=10)
BLOOD_GROUPS = {
    "A+": "A_positive",
    "A-": "A_negative",
    "B+": "B_positive",
    "B-": "B_negative",
    "AB+": "AB_positive",
    "AB-": "AB_negative",
    "O+": "O_positive",
    "O-": "O_negative",
}
GENDERS = ("male", "female", "non_binary", "transgender")


class RegistrationConflict(APIException):
    status_code = 409
    default_detail = "Registration already completed. Sign in again using OTP."


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, dict):
            return super().to_internal_value(data)
        unknown = set(data) - set(self.fields)
        if unknown:
            raise ValidationError(dict.fromkeys(sorted(unknown), "Unsupported field."))
        return super().to_internal_value(data)


class PersonFields(StrictSerializer):
    first_name = serializers.CharField(max_length=40)
    last_name = serializers.CharField(max_length=40)
    dob = serializers.DateField()
    gender = serializers.ChoiceField(choices=GENDERS)
    title = serializers.CharField(max_length=10)
    email = serializers.CharField(max_length=254)
    blood_group = serializers.ChoiceField(choices=list(BLOOD_GROUPS), required=False)

    def validate_first_name(self, value):
        value = " ".join(value.split())
        if not re.fullmatch(NAME_PART, value):
            raise ValidationError("Enter 2-40 letters with single spaces.")
        return value

    def validate_last_name(self, value):
        value = " ".join(value.split())
        if not re.fullmatch(LAST_NAME, value):
            raise ValidationError("Enter 1-40 letters with single spaces.")
        return value

    def validate_dob(self, value):
        today = timezone.localdate()
        if not date(1900, 1, 1) <= value <= today or years_old(value, today) > MAX_AGE:
            raise ValidationError(
                "Date of birth must be from 1900 to today; age at most 120."
            )
        return value

    def validate_email(self, value):
        try:
            validate_email(value)
        except DjangoValidationError as exc:
            raise ValidationError("Enter a valid email address.") from exc
        return value


def validate_title(title, gender, dob):
    if title not in allowed_titles(
        gender, years_old(dob, timezone.localdate()) < ADULT_AGE
    ):
        raise ValidationError({"title": "Choose a title that matches gender and age."})


class RegistrationFields(PersonFields):
    mobile = serializers.CharField(max_length=16)
    registration_token = serializers.CharField(max_length=128, trim_whitespace=False)
    facility = serializers.UUIDField()
    password = serializers.CharField(
        max_length=128, trim_whitespace=False, write_only=True
    )
    confirm_password = serializers.CharField(
        max_length=128, trim_whitespace=False, write_only=True
    )

    def validate_password(self, value):
        if (
            len(value) < MIN_PASSWORD_LENGTH
            or not re.search(r"[A-Z]", value)
            or not re.search(r"[a-z]", value)
            or not re.search(r"[0-9]", value)
            or not re.search(r'[!@#$%^&*(),.?":{}|<>]', value)
        ):
            raise ValidationError(
                "Use 8-128 characters with uppercase, lowercase, digit and special character."
            )
        return value

    def validate(self, attrs):
        validate_title(attrs["title"], attrs["gender"], attrs["dob"])
        if attrs["password"] != attrs["confirm_password"]:
            raise ValidationError({"confirm_password": "Passwords do not match."})
        return attrs


class ProfileFields(PersonFields):
    # partial=True makes inherited person fields optional, but never nullable/blank.
    emergency_contact = serializers.CharField(max_length=16, allow_blank=True)
    address_line1 = serializers.CharField(max_length=500, allow_blank=True)
    city = serializers.CharField(max_length=100, allow_blank=True)
    state = serializers.CharField(max_length=100, allow_blank=True)
    postal_code = serializers.RegexField(
        r"^(?:[1-9][0-9]{5})?$", max_length=6, allow_blank=True
    )
    country = serializers.CharField(max_length=60, allow_blank=True)
    blood_group = serializers.ChoiceField(choices=[*BLOOD_GROUPS, ""], required=False)
    # An upload_id from POST /users/me/profile-photo; null removes the photo.
    profile_photo_id = serializers.UUIDField(allow_null=True)

    def validate_emergency_contact(self, value):
        if not value:
            return ""
        digits = re.sub(r"\D", "", value)
        if len(digits) == MOBILE_DIGITS:
            digits = "91" + digits
        phone = "+" + digits
        try:
            mobile_validator(phone)
        except DjangoValidationError as exc:
            raise ValidationError("Enter a valid Indian mobile number.") from exc
        return phone


def validated(serializer_class, data, **kwargs):
    serializer = serializer_class(data=data, **kwargs)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def registration_facilities():
    return (
        Facility.objects.filter(is_public=True)
        .exclude(unit_code__isnull=True)
        .exclude(unit_code="")
    )


def token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def registration_proof(phone):
    """A new proof invalidates the previous proof for this phone. Store only its hash."""
    token = secrets.token_urlsafe(32)
    MobileRegistration.objects.update_or_create(
        phone_number=phone,
        defaults={
            "token_digest": token_digest(token),
            "expires_at": timezone.now() + REGISTRATION_LIFETIME,
            "consumed_at": None,
        },
    )
    return token


def complete_registration(data, phone):
    if not MobileRegistration.objects.filter(
        phone_number=phone,
        token_digest=token_digest(data["registration_token"]),
        expires_at__gt=timezone.now(),
    ).exists():
        raise ValidationError(
            {"registration_token": "Invalid or expired proof. Verify OTP again."}
        )
    # Hash before taking any database/creation locks.
    password = make_password(data["password"])
    lock = PatientCreateLock()
    lock.acquire()
    try:
        with transaction.atomic():
            proof = (
                MobileRegistration.objects.select_for_update()
                .filter(
                    phone_number=phone,
                    token_digest=token_digest(data["registration_token"]),
                )
                .first()
            )
            if proof is None or proof.expires_at <= timezone.now():
                raise ValidationError(
                    {
                        "registration_token": "Invalid or expired proof. Verify OTP again."
                    }
                )
            if proof.consumed_at is not None:
                raise RegistrationConflict
            if Patient.objects.filter(phone_number=phone).exists():
                raise RegistrationConflict
            facility = (
                registration_facilities()
                .select_for_update()
                .filter(external_id=data["facility"])
                .first()
            )
            if facility is None:
                raise ValidationError(
                    {
                        "facility": "Choose a public facility with a configured unit code."
                    }
                )
            patient = Patient.objects.create(
                name=f"{data['first_name']} {data['last_name']}",
                first_name=data["first_name"],
                last_name=data["last_name"],
                title=data["title"],
                gender=data["gender"],
                date_of_birth=data["dob"],
                phone_number=phone,
                email=data["email"],
                blood_group=BLOOD_GROUPS.get(data.get("blood_group"), "unknown"),
                registration_facility=facility,
                # Full desk registration remains incomplete; there is no staff author.
                mobile_owner="Self",
            )
            evaluate_patient_instance_default_values(patient)
            issue_patient_number(
                patient, facility, PatientNumberSeries.pre_registration
            )
            account = PatientAccount.objects.create(patient=patient, password=password)
            session, tokens = start_session(patient, phone)
            proof.consumed_at = timezone.now()
            proof.save(update_fields=["consumed_at"])
        # ATOMIC_REQUESTS keeps the creation lock until the whole request commits.
        transaction.on_commit(lock.release)
    except Exception:
        lock.release()
        raise
    return patient, account, session, tokens


PROFILE_COLUMNS = {
    "dob": "date_of_birth",
    "emergency_contact": "emergency_phone_number",
    "address_line1": "address",
    "postal_code": "pincode",
}


def update_profile(principal, data):
    with transaction.atomic():
        # Lock the patient, then recheck the live session/phone after a possible desk edit.
        patient = (
            Patient.objects.select_for_update().filter(pk=principal.patient.pk).first()
        )
        session = (
            PatientSession.objects.select_related("account__patient")
            .filter(pk=principal.session.pk)
            .first()
        )
        if patient is None or session is None or not session_usable(session):
            raise AuthenticationFailed("Session expired. Please sign in again.")
        data = dict(data)
        if "profile_photo_id" in data:
            set_photo(patient, data.pop("profile_photo_id"))
        if {"title", "gender", "dob"} & data.keys():
            dob = data.get("dob", patient.date_of_birth)
            title = data.get("title", patient.title)
            gender = data.get("gender", patient.gender)
            # Existing records without titles may still edit DOB/gender; never invent a title.
            if title:
                if dob is None:
                    raise ValidationError(
                        {"dob": "Date of birth is required to validate the title."}
                    )
                validate_title(title, gender, dob)
        for field, value in data.items():
            if field == "country":
                patient.meta = {
                    **patient.meta,
                    "mobile_profile": {
                        **patient.meta.get("mobile_profile", {}),
                        "country": value,
                    },
                }
            elif field == "blood_group":
                patient.blood_group = BLOOD_GROUPS.get(value, "unknown")
            else:
                setattr(
                    patient,
                    PROFILE_COLUMNS.get(field, field),
                    value if field != "postal_code" else int(value) if value else None,
                )
        if "dob" in data:
            patient.year_of_birth = patient.date_of_birth.year
            patient.meta = {**patient.meta}
            patient.meta.pop("date_of_birth_approximate", None)
        if {"first_name", "last_name"} & data.keys():
            patient.name = " ".join(
                filter(
                    None, [patient.first_name, patient.middle_name, patient.last_name]
                )
            )
        # This write was made by the patient, never attribute it to a prior staff editor.
        patient.updated_by = None
        patient.save()
    return patient
