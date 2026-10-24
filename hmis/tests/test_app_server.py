from care.emr.locks.billing import PatientCreateLock
from care.emr.models import (
    Patient,
    PatientUser,
)
from care.emr.models.organization import FacilityOrganizationUser
from care.utils.tests.base import CareAPITestBase
from django.core.management import call_command
from django.urls import reverse
from rest_framework import status

from hmis.app_server import setup_app_server
from hmis.constants import APP_SERVER_ROLE

# Clearly fake people.
PHONE = "+919800000001"
ABHA = "91000000000001"


class AppServerTests(CareAPITestBase):
    @classmethod
    def setUpTestData(cls):
        # The test database starts without Care's permissions.
        call_command("sync_permissions_roles")

    def setUp(self):
        super().setUp()
        PatientCreateLock().release()
        self.admin = self.create_super_user(username="desk-admin")
        self.facility = self.create_facility(user=self.admin)
        self.facility.unit_code = "LN"
        self.facility.save(update_fields=["unit_code"])
        self.app, _ = setup_app_server()
        self.client.force_authenticate(user=self.app)

    def profile(self, **kwargs):
        return {
            "app_user_id": "app-user-1",
            "facility": str(self.facility.external_id),
            "title": "Ms",
            "first_name": "Test",
            "last_name": "Person",
            "date_of_birth": "1990-01-01",
            "gender": "female",
            "phone_number": PHONE,
            **kwargs,
        }

    def register(self, **kwargs):
        PatientCreateLock().release()
        return self.client.post(
            reverse("app-patients"), self.profile(**kwargs), format="json"
        )

    def test_setup_is_idempotent_and_covers_new_facilities(self):
        self.assertTrue(self.app.is_service_account)
        self.assertFalse(self.app.has_usable_password())
        self.assertFalse(self.app.is_superuser)
        user, granted = setup_app_server()
        self.assertEqual((user, granted), (self.app, 0))
        self.facility.refresh_from_db()
        root = FacilityOrganizationUser.objects.get(
            user=self.app, organization=self.facility.default_internal_organization
        )
        self.assertEqual(root.role.name, APP_SERVER_ROLE)

        other = self.create_facility(user=self.admin)
        self.assertTrue(
            FacilityOrganizationUser.objects.filter(
                user=self.app, organization=other.default_internal_organization
            ).exists()
        )

    def test_register_creates_pre_registered_patient_once(self):
        response = self.register(abha_number=ABHA, identity_storage_consent=True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        body = response.json()
        self.assertTrue(body["pre_registration_number"].startswith("PRE-LN-"))
        self.assertIsNone(body["uhid"])
        self.assertFalse(body["matched"])
        patient = Patient.objects.get(external_id=body["patient"]["id"])
        self.assertEqual(patient.name, "Test Person")
        self.assertEqual(patient.meta["registration"]["abha_number"], ABHA)
        self.assertTrue(
            PatientUser.objects.filter(patient=patient, user=self.app).exists()
        )

        again = self.register(
            first_name="Tested", abha_number=ABHA, identity_storage_consent=True
        )
        self.assertEqual(again.status_code, status.HTTP_200_OK, again.data)
        self.assertEqual(again.json()["patient"]["id"], str(patient.external_id))
        self.assertEqual(
            again.json()["pre_registration_number"], body["pre_registration_number"]
        )
        patient.refresh_from_db()
        self.assertEqual(patient.name, "Tested Person")
        self.assertEqual(Patient.objects.count(), 1)

        detail = self.client.get(reverse("app-patient", args=["app-user-1"]))
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(detail.json()["patient"]["id"], str(patient.external_id))
        # Care's own patient API serves it too, through the PatientUser link.
        care = self.client.get(f"/api/v1/patient/{patient.external_id}/")
        self.assertEqual(care.status_code, status.HTTP_200_OK)

    def test_register_validation_and_who_may_call(self):
        response = self.register(abha_number=ABHA)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Consent", str(response.data))
        response = self.register(title="Mr")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.client.force_authenticate(user=self.admin)
        response = self.register()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Patient.objects.count(), 0)

    def test_abha_links_one_existing_patient_without_changing_it(self):
        existing = self.create_patient(
            name="Desk Patient",
            phone_number=PHONE,
            meta={"registration": {"abha_number": ABHA}},
        )
        response = self.register(abha_number=ABHA, identity_storage_consent=True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(response.json()["matched"])
        self.assertEqual(response.json()["patient"]["id"], str(existing.external_id))

        self.register(
            first_name="Changed", abha_number=ABHA, identity_storage_consent=True
        )
        existing.refresh_from_db()
        self.assertEqual(existing.name, "Desk Patient")

        # Two patients share the number: no guess, a new patient.
        self.create_patient(
            phone_number=PHONE, meta={"registration": {"abha_number": "91000000000002"}}
        )
        self.create_patient(
            phone_number=PHONE, meta={"registration": {"abha_number": "91000000000002"}}
        )
        response = self.register(
            app_user_id="app-user-2",
            abha_number="91000000000002",
            identity_storage_consent=True,
        )
        self.assertFalse(response.json()["matched"])
        self.assertEqual(Patient.objects.count(), 4)

    def test_abha_matches_the_desk_column_and_care_meta(self):
        # The desk keeps ABHA in Patient.abha_number; older records and the
        # app keep it in Care's registration meta. Both are checked.
        desk = self.create_patient(name="Desk Patient", phone_number=PHONE)
        Patient.objects.filter(id=desk.id).update(abha_number=ABHA)
        response = self.register(abha_number=ABHA, identity_storage_consent=True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(response.json()["matched"])
        self.assertEqual(response.json()["patient"]["id"], str(desk.external_id))

        number = "91000000000003"
        column = self.create_patient(phone_number=PHONE)
        Patient.objects.filter(id=column.id).update(abha_number=number)
        self.create_patient(
            phone_number=PHONE, meta={"registration": {"abha_number": number}}
        )
        response = self.register(
            app_user_id="app-user-3", abha_number=number, identity_storage_consent=True
        )
        self.assertFalse(response.json()["matched"])
