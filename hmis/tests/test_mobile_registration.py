"""Direct phone registration, atomicity, proof security and self-profile editing."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.hashers import check_password, is_password_usable
from django.db import connection
from django.test import override_settings
from django.utils import timezone
from model_bakery import baker
from rest_framework.test import APIClient, APITransactionTestCase

from care.emr.models import Patient
from care.facility.models import Facility
from care.users.models import User
from hmis.constants import PatientNumberSeries
from hmis.mobile_registration import registration_proof, token_digest
from hmis.models import MobileRegistration, PatientAccount, PatientSession
from hmis.services import patient_number
from hmis.tests.test_mobile_appointments import setup_slot

BASE = "/api/hmis/mobile/api/v1"
PHONE = "+919800003333"
MOBILE = PHONE.removeprefix("+91")
PASSWORD = "FakeTesting#123"


@override_settings(USE_SMS=False, IS_PRODUCTION=False)
class MobileRegistrationTests(APITransactionTestCase):
    def setUp(self):
        self.staff = baker.make(User)
        self.facility = baker.make(
            Facility,
            name="Test Hospital",
            is_public=True,
            unit_code="TST",
            created_by=self.staff,
        )
        self.proof = registration_proof(PHONE)
        self.payload = {
            "mobile": MOBILE,
            "registration_token": self.proof,
            "facility": str(self.facility.external_id),
            "title": "Ms",
            "first_name": "Test",
            "last_name": "Patient",
            "email": "test@example.invalid",
            "dob": "1995-05-01",
            "gender": "female",
            "blood_group": "O+",
            "password": PASSWORD,
            "confirm_password": PASSWORD,
        }

    def complete(self, payload=None):
        return self.client.post(
            f"{BASE}/auth/login/complete_registration",
            self.payload if payload is None else payload,
            format="json",
        )

    def signed_in(self):
        response = self.complete()
        self.assertEqual(response.status_code, 201, response.data)
        self.client.credentials(
            HTTP_AUTHORIZATION="Bearer " + response.data["data"]["access_token"]
        )
        return Patient.objects.get(phone_number=PHONE), response.data["data"]

    def test_complete_creates_pre_patient_hashed_password_and_usable_session(self):
        patient, data = self.signed_in()
        self.assertEqual(patient.name, "Test Patient")
        self.assertEqual(patient.year_of_birth, 1995)
        self.assertEqual(patient.blood_group, "O_positive")
        self.assertEqual(patient.registration_facility, self.facility)
        self.assertIsNone(patient.registered_at)
        self.assertIsNone(patient.created_by)
        self.assertIsNone(patient.registered_by)
        account = PatientAccount.objects.get(patient=patient)
        self.assertNotEqual(account.password, PASSWORD)
        self.assertTrue(check_password(PASSWORD, account.password))
        self.assertEqual(data["user"]["id"], account.pk)
        self.assertTrue(data["user"]["patient_number"].startswith("PRE-TST-"))
        self.assertIsNone(patient_number(patient, PatientNumberSeries.uhid))
        self.assertEqual(self.client.get(f"{BASE}/users/me").status_code, 200)
        self.assertEqual(PatientSession.objects.filter(account=account).count(), 1)
        self.assertNotIn("password", data["user"])
        staff = self.client.get("/api/v1/users/getcurrentuser/")
        self.assertIn(staff.status_code, (401, 403))
        response = self.client.post(
            f"{BASE}/auth/refresh",
            {"refresh_token": data["refresh_token"]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_otp_flow_generates_hashed_proof_without_creating_patient(self):
        self.client.post(
            f"{BASE}/auth/login/send_otp", {"mobile": MOBILE}, format="json"
        )
        response = self.client.post(
            f"{BASE}/auth/login/verify_otp",
            {"mobile": MOBILE, "otp": "456123"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertTrue(data["is_new_user"])
        self.assertEqual(data["expires_in"], 600)
        proof = MobileRegistration.objects.get(phone_number=PHONE)
        self.assertEqual(proof.token_digest, token_digest(data["registration_token"]))
        self.assertNotEqual(proof.token_digest, data["registration_token"])
        self.assertFalse(Patient.objects.filter(phone_number=PHONE).exists())
        self.assertEqual(self.complete().status_code, 422)  # Prior proof invalidated.
        self.client.credentials(
            HTTP_AUTHORIZATION="Bearer " + data["registration_token"]
        )
        self.assertEqual(self.client.get(f"{BASE}/users/me").status_code, 401)
        self.client.credentials()
        self.payload["registration_token"] = data["registration_token"]
        self.assertEqual(self.complete().status_code, 201)

    def test_required_fields_and_unknown_fields(self):
        for field in self.payload:
            if field == "blood_group":
                continue
            with self.subTest(field=field):
                body = {k: v for k, v in self.payload.items() if k != field}
                response = self.complete(body)
                self.assertEqual(response.status_code, 422, response.data)
                self.assertIn(field, [e["field"] for e in response.data["errors"]])
        response = self.complete({**self.payload, "is_staff": True})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(Patient.objects.filter(phone_number=PHONE).exists())

    def test_name_limits_and_backend_character_rules(self):
        for field, value in [
            ("first_name", "A"),
            ("first_name", "A" * 41),
            ("last_name", "B" * 41),
            ("first_name", "Anne-Marie"),
            ("last_name", "O'Example"),
        ]:
            with self.subTest(field=field, value=value):
                self.assertEqual(
                    self.complete({**self.payload, field: value}).status_code, 422
                )
        response = self.complete(
            {**self.payload, "first_name": "A" * 40, "last_name": "B" * 40}
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_demographic_email_password_validation(self):
        cases = [
            {"gender": "Other"},
            {"gender": "other"},
            {"gender": ""},
            {"title": "Mr"},
            {"title": "Baby"},
            {"dob": "1899-01-01"},
            {"dob": (timezone.localdate() + timedelta(days=1)).isoformat()},
            {"dob": "2025-02-30"},
            {"email": "bad"},
            {"blood_group": "XX"},
            {"password": "fake", "confirm_password": "fake"},
            {"password": "NoDigits#", "confirm_password": "NoDigits#"},
            {"confirm_password": "different"},
        ]
        for values in cases:
            with self.subTest(values=values):
                self.assertEqual(
                    self.complete({**self.payload, **values}).status_code, 422
                )

    def test_all_backend_genders_and_optional_blood_group(self):
        for index, gender in enumerate(("male", "female", "non_binary", "transgender")):
            phone = f"+91980000400{index}"
            body = {
                **self.payload,
                "mobile": phone[3:],
                "registration_token": registration_proof(phone),
                "gender": gender,
                "title": "Mx",
            }
            body.pop("blood_group")
            response = self.complete(body)
            self.assertEqual(response.status_code, 201, response.data)
            self.assertEqual(
                Patient.objects.get(phone_number=phone).blood_group, "unknown"
            )

    def test_wrong_expired_and_wrong_phone_proofs(self):
        for values in [{"registration_token": "invalid"}, {"mobile": "9800003334"}]:
            self.assertEqual(self.complete({**self.payload, **values}).status_code, 422)
        MobileRegistration.objects.update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(self.complete().status_code, 422)
        self.assertFalse(PatientAccount.objects.exists())

    def test_replay_requires_otp_login_and_never_duplicates(self):
        self.assertEqual(self.complete().status_code, 201)
        self.assertEqual(self.complete().status_code, 409)
        self.assertEqual(Patient.objects.filter(phone_number=PHONE).count(), 1)
        self.assertEqual(PatientAccount.objects.count(), 1)
        self.assertEqual(PatientSession.objects.count(), 1)
        response = self.client.post(
            f"{BASE}/auth/login/send_otp", {"mobile": MOBILE}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        response = self.client.post(
            f"{BASE}/auth/login/verify_otp",
            {"mobile": MOBILE, "otp": "456123"},
            format="json",
        )
        self.assertFalse(response.data["data"]["is_new_user"])
        self.assertEqual(len(response.data["data"]["accounts"]), 1)

    def test_patient_added_after_otp_does_not_get_overwritten(self):
        patient = baker.make(Patient, name="Existing Test", phone_number=PHONE)
        self.assertEqual(self.complete().status_code, 409)
        patient.refresh_from_db()
        self.assertEqual(patient.name, "Existing Test")
        self.assertFalse(PatientAccount.objects.exists())

    def test_public_facility_catalogue_and_validation(self):
        private = baker.make(
            Facility, is_public=False, unit_code="PRV", created_by=self.staff
        )
        no_code = baker.make(
            Facility, is_public=True, unit_code=None, created_by=self.staff
        )
        deleted = baker.make(
            Facility,
            is_public=True,
            unit_code="DEL",
            deleted=True,
            created_by=self.staff,
        )
        response = self.client.get(
            f"{BASE}/auth/registration/facilities", {"search": "Test", "page_size": 1}
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["data"]["items"][0]["id"], str(self.facility.external_id)
        )
        self.assertEqual(
            self.client.get(f"{BASE}/appointments/facilities").status_code, 401
        )
        for facility in [private, no_code, deleted]:
            self.assertEqual(
                self.complete(
                    {**self.payload, "facility": str(facility.external_id)}
                ).status_code,
                422,
            )
        self.assertEqual(
            self.complete({**self.payload, "facility": str(uuid4())}).status_code, 422
        )
        self.assertEqual(
            self.client.get(
                f"{BASE}/auth/registration/facilities", {"page": 0}
            ).status_code,
            422,
        )

    def test_registration_rolls_back_patient_proof_and_session_on_failure(self):
        with (
            patch(
                "hmis.mobile_registration.start_session",
                side_effect=RuntimeError("test rollback"),
            ),
            self.assertRaisesMessage(RuntimeError, "test rollback"),
        ):
            self.complete()
        self.assertFalse(Patient.objects.filter(phone_number=PHONE).exists())
        self.assertFalse(PatientAccount.objects.exists())
        self.assertFalse(PatientSession.objects.exists())
        self.assertIsNone(
            MobileRegistration.objects.get(phone_number=PHONE).consumed_at
        )
        self.assertEqual(self.complete().status_code, 201)

    def test_existing_otp_account_has_unusable_password(self):
        patient = baker.make(Patient, phone_number="+919800003339")
        account = PatientAccount.objects.create(patient=patient)
        self.assertFalse(is_password_usable(account.password))

    def test_concurrent_completion_creates_one_patient(self):
        barrier = Barrier(2)
        payload = self.payload.copy()

        def submit():
            connection.close()
            try:
                barrier.wait(timeout=10)
                return (
                    APIClient()
                    .post(
                        f"{BASE}/auth/login/complete_registration",
                        payload,
                        format="json",
                    )
                    .status_code
                )
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(codes.count(201), 1, codes)
        self.assertTrue(set(codes) <= {201, 409, 423}, codes)
        self.assertEqual(Patient.objects.filter(phone_number=PHONE).count(), 1)
        self.assertEqual(PatientAccount.objects.count(), 1)

    def test_profile_update_roundtrip_and_same_phone_isolation(self):
        patient, data = self.signed_in()
        other = baker.make(Patient, phone_number=PHONE, name="Other Test")
        response = self.client.patch(
            f"{BASE}/users/me",
            {
                "first_name": "Updated",
                "last_name": "Example",
                "dob": "1996-06-01",
                "email": "updated@example.invalid",
                "blood_group": "AB-",
                "emergency_contact": "9800009999",
                "address_line1": "Test address",
                "city": "Test City",
                "state": "Test State",
                "postal_code": "110001",
                "country": "India",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        profile = response.data["data"]
        self.assertEqual(profile["id"], data["user"]["id"])
        self.assertEqual(profile["city"], "Test City")
        self.assertEqual(profile["country"], "India")
        self.assertEqual(profile["postal_code"], "110001")
        self.assertEqual(profile["blood_group"], "AB-")
        self.assertEqual(self.client.get(f"{BASE}/users/me").data["data"], profile)
        patient.refresh_from_db()
        self.assertEqual(patient.name, "Updated Example")
        self.assertEqual(patient.year_of_birth, 1996)
        self.assertEqual(patient.emergency_phone_number, "+919800009999")
        self.assertIsNone(patient.registered_at)
        other.refresh_from_db()
        self.assertEqual(other.name, "Other Test")

    def test_profile_rejects_mobile_password_privileges_and_invalid_fields_atomically(
        self,
    ):
        patient, _ = self.signed_in()
        for field, value in [
            ("mobile", "9800009999"),
            ("password", PASSWORD),
            ("facility", str(uuid4())),
            ("is_active", False),
            ("id", 99),
            ("dob", "2100-01-01"),
            ("gender", "other"),
            ("postal_code", "001234"),
            ("first_name", "A" * 41),
            ("emergency_contact", "123"),
        ]:
            with self.subTest(field=field):
                response = self.client.patch(
                    f"{BASE}/users/me",
                    {"last_name": "Changed", field: value},
                    format="json",
                )
                self.assertEqual(response.status_code, 422, response.data)
        patient.refresh_from_db()
        self.assertEqual(patient.last_name, "Patient")
        response = self.client.patch(
            f"{BASE}/users/me", {"gender": "male"}, format="json"
        )
        self.assertEqual(response.status_code, 422)  # Ms no longer fits.
        response = self.client.patch(
            f"{BASE}/users/me", {"gender": "male", "title": "Mr"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_profile_clear_optional_values_and_keep_other_meta(self):
        patient, _ = self.signed_in()
        patient.meta = {"test_marker": True, "date_of_birth_approximate": True}
        patient.save()
        response = self.client.patch(
            f"{BASE}/users/me",
            {
                "country": "",
                "postal_code": "",
                "emergency_contact": "",
                "blood_group": "",
                "dob": "1995-05-01",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["data"]["postal_code"])
        patient.refresh_from_db()
        self.assertTrue(patient.meta["test_marker"])
        self.assertNotIn("date_of_birth_approximate", patient.meta)

    def test_profile_requires_active_own_session(self):
        self.assertEqual(
            self.client.patch(f"{BASE}/users/me", {}, format="json").status_code, 401
        )
        patient, _ = self.signed_in()
        patient.phone_number = "+919800003338"
        patient.save()
        self.assertEqual(
            self.client.patch(
                f"{BASE}/users/me", {"first_name": "Changed"}, format="json"
            ).status_code,
            401,
        )

    def test_registered_patient_can_submit_an_appointment(self):
        self.staff.is_active = True
        self.staff.is_service_account = False
        self.staff.save()
        slot = setup_slot(self.staff, self.facility)
        _patient, _tokens = self.signed_in()
        response = self.client.post(
            f"{BASE}/appointments",
            {
                "request_id": str(uuid4()),
                "slot_id": str(slot.external_id),
                "patient_notes": "Fake registration integration test",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["status"], "pending")
        self.assertEqual(
            self.client.get(f"{BASE}/appointments").data["data"]["total"], 1
        )
