"""VIP protocol (D29, R31): a VIP is shown as their VIP code on every staff
list, screen and slip; staff reveal the name on purpose and each reveal is
logged. All patients here are fake."""

import re

from django.urls import reverse
from rest_framework import status

from care.emr.models import Patient
from care.emr.models.patient import PatientUser
from care.utils.tests.base import CareAPITestBase
from hmis.models import VipReveal
from hmis.tests.test_appointment_approvals import make_slot, request_booking
from hmis.vip import vip_code

CODE = re.compile(r"^VIP-[2-9A-HJ-NP-Z]{6}$")


class VipMaskingTests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        self.user = self.create_super_user()
        self.facility = self.create_facility(user=self.user)
        self.patient = self.create_patient(
            name="Testvip Example", phone_number="+919800000042"
        )
        Patient.objects.filter(id=self.patient.id).update(
            is_vip=True,
            vip_note="Test guest",
            vip_authorised_by="Test Manager",
            registered_at=self.patient.created_date,
            registration_facility=self.facility,
            registration_type="domestic",
            first_name="Testvip",
            last_name="Example",
        )
        self.patient.refresh_from_db()
        self.code = vip_code(self.patient)
        self.client.force_authenticate(user=self.user)

    def test_code_is_stable_and_not_the_name(self):
        self.assertRegex(self.code, CODE)
        self.assertEqual(vip_code(Patient.objects.get(id=self.patient.id)), self.code)

    def test_patient_list_and_detail_show_the_code(self):
        listed = self.client.get("/api/v1/patient/", {"name": "Testvip"})
        self.assertEqual(listed.status_code, status.HTTP_200_OK, listed.data)
        row = listed.data["results"][0]
        self.assertEqual(row["name"], self.code)
        self.assertTrue(row["vip"])
        detail = self.client.get(f"/api/v1/patient/{self.patient.external_id}/").data
        self.assertEqual(detail["name"], self.code)
        # Phone stays, so the desk can still check who they are.
        self.assertEqual(detail["phone_number"], "+919800000042")
        # The record itself is unchanged.
        self.assertEqual(Patient.objects.get().name, "Testvip Example")

    def test_a_non_vip_is_unchanged(self):
        other = self.create_patient(name="Plain Example")
        data = self.client.get(f"/api/v1/patient/{other.external_id}/").data
        self.assertEqual(data["name"], "Plain Example")
        self.assertFalse(data["vip"])

    def test_appointments_show_the_code(self):
        booking = request_booking(make_slot(self.user, self.facility), self.patient)
        base = f"/api/hmis/facility/{self.facility.external_id}/appointment-requests/"
        row = self.client.get(base).data["results"][0]
        self.assertEqual(row["patient"]["name"], self.code)
        response = self.client.get(
            f"/api/v1/facility/{self.facility.external_id}/appointments/{booking.external_id}/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["patient"]["name"], self.code)

    def test_registration_masks_the_name_unless_opened_to_edit(self):
        url = reverse("desk-registration-detail", args=[self.patient.external_id])
        data = self.client.get(url).data
        self.assertEqual(data["first_name"], self.code)
        self.assertEqual(data["last_name"], "")
        self.assertEqual(data["vip_code"], self.code)
        self.assertFalse(VipReveal.objects.exists())

        self._vip_access()
        data = self.client.get(url, {"reveal": "edit"}).data
        self.assertEqual(
            (data["first_name"], data["last_name"]), ("Testvip", "Example")
        )
        reveal = VipReveal.objects.get()
        self.assertEqual((reveal.user, reveal.source), (self.user, "edit"))

    def test_reveal_returns_the_name_and_is_logged(self):
        self._vip_access()
        url = reverse("patient-vip-reveal", args=[self.patient.external_id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            response.data, {"name": "Testvip Example", "vip_code": self.code}
        )
        reveal = VipReveal.objects.get()
        self.assertEqual((reveal.patient, reveal.source), (self.patient, "reveal"))

        other = self.create_patient(name="Plain Example")
        PatientUser.objects.create(
            patient=other,
            user=self.user,
            role=self.create_role_with_permissions(["can_list_patients"]),
        )
        response = self.client.post(
            reverse("patient-vip-reveal", args=[other.external_id])
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reveal_needs_access_to_the_patient(self):
        self.client.force_authenticate(user=self.create_user())
        response = self.client.post(
            reverse("patient-vip-reveal", args=[self.patient.external_id])
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(VipReveal.objects.exists())

    def _staff(self, *extra):
        """A desk user who can see and edit the patient, plus any extra permissions."""
        user = self.create_user()
        role = self.create_role_with_permissions(
            ["can_list_patients", "can_write_patient", *extra]
        )
        PatientUser.objects.create(patient=self.patient, user=user, role=role)
        self.client.force_authenticate(user=user)
        return user

    def _vip_access(self):
        """Logs in as a user whose facility role grants VIP access."""
        user = self._staff()
        self.attach_role_facility_organization_user(
            self.create_facility_organization(facility=self.facility),
            user,
            self.create_role_with_permissions(["can_reveal_vip_name"]),
        )
        self.user = user
        return user

    def test_a_superuser_without_vip_access_cannot_reveal(self):
        # setUp's user is a superuser; that alone isn't VIP access.
        me = self.client.get("/api/v1/users/getcurrentuser/").data
        self.assertFalse(me["can_reveal_vip"])
        response = self.client.post(
            reverse("patient-vip-reveal", args=[self.patient.external_id])
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            response.data["detail"], "Only a user with VIP access can see a VIP's name"
        )
        url = reverse("desk-registration-detail", args=[self.patient.external_id])
        self.assertEqual(
            self.client.get(url, {"reveal": "edit"}).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertFalse(VipReveal.objects.exists())

    def test_vip_access_comes_from_a_role(self):
        self._staff()
        self.assertFalse(
            self.client.get("/api/v1/users/getcurrentuser/").data["can_reveal_vip"]
        )
        self._vip_access()
        self.assertTrue(
            self.client.get("/api/v1/users/getcurrentuser/").data["can_reveal_vip"]
        )
        response = self.client.post(
            reverse("patient-vip-reveal", args=[self.patient.external_id])
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(VipReveal.objects.get().user, self.user)

    def test_staff_without_vip_access_cannot_open_a_vip_to_edit(self):
        self._staff()
        url = reverse("desk-registration-detail", args=[self.patient.external_id])
        # The masked view still works for staff.
        masked = self.client.get(url)
        self.assertEqual(masked.status_code, status.HTTP_200_OK, masked.data)
        self.assertEqual(masked.data["first_name"], self.code)
        response = self.client.get(url, {"reveal": "edit"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.put(url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(VipReveal.objects.exists())
        self.assertEqual(Patient.objects.get().name, "Testvip Example")
