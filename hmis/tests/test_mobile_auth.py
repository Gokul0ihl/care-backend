from datetime import date, timedelta

from django.test import override_settings
from django.utils import timezone
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APITestCase

from care.emr.models import Patient
from care.facility.models.patient import MobileOTP
from hmis.mobile_auth import PatientAccessToken
from hmis.models import PatientAccount, PatientSession

BASE = "/api/hmis/mobile/api/v1"
MOBILE = "9800000001"
PHONE = f"+91{MOBILE}"
DEV_OTP = "456123"


@override_settings(USE_SMS=False, IS_PRODUCTION=False)
class MobileLoginTests(APITestCase):
    def setUp(self):
        # Clearly fake people sharing one number, plus one on another number.
        self.mother = baker.make(
            Patient,
            name="Testmother Example",
            first_name="Testmother",
            last_name="Example",
            phone_number=PHONE,
            gender="female",
            date_of_birth=date(1980, 5, 1),
            blood_group="O_positive",
        )
        self.child = baker.make(
            Patient,
            name="Testchild Example",
            phone_number=PHONE,
            gender="male",
            year_of_birth=2015,
            blood_group="unknown",
        )
        baker.make(Patient, name="Gone Example", phone_number=PHONE, deleted=True)
        baker.make(Patient, name="Other Example", phone_number="+919800000002")

    def post(self, path, body, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.post(f"{BASE}/{path}", body, format="json", **headers)

    def get(self, path, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.get(f"{BASE}/{path}", **headers)

    def sign_in(self):
        self.post("auth/login/send_otp", {"mobile": MOBILE})
        response = self.post(
            "auth/login/verify_otp", {"mobile": MOBILE, "otp": DEV_OTP}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data["data"]["accounts"]

    def test_send_otp_uses_care_sender_and_app_envelope(self):
        response = self.post("auth/login/send_otp", {"mobile": MOBILE})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "success")
        self.assertEqual(response.data["data"], {"mobile": MOBILE, "otp": DEV_OTP})
        self.assertTrue(MobileOTP.objects.filter(phone_number=PHONE).exists())

    def test_send_otp_rejects_bad_number(self):
        response = self.post("auth/login/send_otp", {"mobile": "12345"})
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(response.data["status"], "error")
        self.assertEqual(response.data["errors"][0]["field"], "mobile")

    def test_verify_offers_every_live_patient_on_the_number(self):
        accounts = self.sign_in()
        self.assertEqual(len(accounts), 2)
        mother, child = (a["user"] for a in accounts)
        self.assertEqual(mother["first_name"], "Testmother")
        self.assertEqual(mother["last_name"], "Example")
        self.assertEqual(mother["mobile"], MOBILE)
        self.assertEqual(mother["dob"], "1980-05-01")
        self.assertEqual(mother["gender"], "Female")
        self.assertEqual(mother["blood_group"], "O+")
        self.assertEqual(child["first_name"], "Testchild")
        self.assertIsNone(child["dob"])
        self.assertIsNone(child["blood_group"])
        self.assertEqual(
            mother["id"], PatientAccount.objects.get(patient=self.mother).id
        )
        self.assertNotEqual(mother["id"], child["id"])
        self.assertEqual(accounts[0]["token_type"], "Bearer")
        self.assertEqual(accounts[0]["expires_in"], 15 * 60)

    def test_wrong_otp_is_counted(self):
        self.post("auth/login/send_otp", {"mobile": MOBILE})
        response = self.post(
            "auth/login/verify_otp", {"mobile": MOBILE, "otp": "000000"}
        )
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(response.data["message"], "Invalid OTP")
        self.assertEqual(MobileOTP.objects.get(phone_number=PHONE).failed_attempts, 1)

    def test_number_with_no_patients_gets_registration_proof(self):
        mobile = "9800000003"
        self.post("auth/login/send_otp", {"mobile": mobile})
        response = self.post(
            "auth/login/verify_otp", {"mobile": mobile, "otp": DEV_OTP}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["data"]["is_new_user"])
        self.assertTrue(response.data["data"]["registration_token"])
        self.assertEqual(response.data["data"]["accounts"], [])

    def test_me_returns_only_the_tokens_patient(self):
        mother, child = self.sign_in()
        response = self.get("users/me", mother["access_token"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["first_name"], "Testmother")
        response = self.get("users/me", child["access_token"])
        self.assertEqual(response.data["data"]["first_name"], "Testchild")

    def test_missing_or_bad_token_is_401_so_the_app_refreshes(self):
        for token in (None, "not-a-token"):
            response = self.get("users/me", token)
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
            self.assertIn("Bearer", response["WWW-Authenticate"])

    def test_patient_token_is_refused_by_staff_endpoints(self):
        (account, _) = self.sign_in()
        response = self.client.get(
            "/api/v1/users/getcurrentuser/",
            HTTP_AUTHORIZATION=f"Bearer {account['access_token']}",
        )
        self.assertIn(response.status_code, (401, 403))

    def test_refresh_rotates_and_old_token_reuse_revokes(self):
        (account, _) = self.sign_in()
        old = account["refresh_token"]
        response = self.post("auth/refresh", {"refresh_token": old})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        new = response.data["data"]
        self.assertNotEqual(new["refresh_token"], old)
        self.assertEqual(self.get("users/me", new["access_token"]).status_code, 200)

        # Past the grace period, reusing the old one ends the session.
        PatientSession.objects.update(rotated_at=timezone.now() - timedelta(hours=1))
        response = self.post("auth/refresh", {"refresh_token": old})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data["status"], "error")
        self.assertEqual(self.get("users/me", new["access_token"]).status_code, 401)
        response = self.post("auth/refresh", {"refresh_token": new["refresh_token"]})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_refresh_retry_within_grace_is_allowed(self):
        (account, _) = self.sign_in()
        self.post("auth/refresh", {"refresh_token": account["refresh_token"]})
        response = self.post(
            "auth/refresh", {"refresh_token": account["refresh_token"]}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_access_token_is_not_a_refresh_token(self):
        (account, _) = self.sign_in()
        response = self.post("auth/refresh", {"refresh_token": account["access_token"]})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_logout_ends_only_that_session(self):
        mother, child = self.sign_in()
        response = self.post(
            "auth/logout",
            {"refresh_token": mother["refresh_token"], "fcm_token": "x"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.get("users/me", mother["access_token"]).status_code, 401)
        response = self.post("auth/refresh", {"refresh_token": mother["refresh_token"]})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.get("users/me", child["access_token"]).status_code, 200)

    def test_changing_the_number_at_the_desk_ends_sessions(self):
        (account, _) = self.sign_in()
        self.mother.phone_number = "+919800000009"
        self.mother.save(update_fields=["phone_number"])
        self.assertEqual(self.get("users/me", account["access_token"]).status_code, 401)

    def test_expired_access_token_is_401(self):
        (account, _) = self.sign_in()
        token = PatientAccessToken(account["access_token"])
        token.set_exp(lifetime=-timedelta(seconds=1))
        self.assertEqual(self.get("users/me", str(token)).status_code, 401)

    def test_fcm_token_is_stored_on_the_session(self):
        (account, _) = self.sign_in()
        response = self.post(
            "users/me/fcm-token", {"fcm_token": "fake-fcm"}, account["access_token"]
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        session = PatientSession.objects.get(account__patient=self.mother)
        self.assertEqual(session.fcm_token, "fake-fcm")
