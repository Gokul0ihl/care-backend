"""The registration timeline and audit trail (hmis.patient_events)."""

from unittest import mock

from django.core.cache import cache
from django.urls import reverse
from rest_framework import status

from care.emr.locks.billing import PatientCreateLock
from care.emr.models import Patient
from care.utils.tests.base import CareAPITestBase
from hmis.models import PatientEvent, PatientMerge
from hmis.patient_events import record_patient_event
from hmis.tests.test_registration import PHONE, registration_details, stage_document

DESK_NUMBER = "+919876543210"
OTHER_PHONE = "+919812300000"
DESK_PERMISSIONS = [
    "can_create_patient",
    "can_list_patients",
    "can_write_patient",
]


class PatientEventTests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.admin = self.create_super_user(username="admin")
        self.facility = self.create_facility(user=self.admin, name="Lajpat Nagar")
        self.facility.unit_code = "LN"
        self.facility.emergency_desk_phone = DESK_NUMBER
        self.facility.save(update_fields=["unit_code", "emergency_desk_phone"])
        self.org = self.create_facility_organization(facility=self.facility)
        self.desk = self.create_user(
            username="ritu", first_name="Ritu", last_name="Sharma"
        )
        self.attach_role_facility_organization_user(
            self.org,
            self.desk,
            self.create_role_with_permissions(DESK_PERMISSIONS, "Front desk"),
        )
        self.client.force_authenticate(user=self.desk)
        PatientCreateLock().release()

    def body(self, patient=None, **registration):
        return {
            "facility": str(self.facility.external_id),
            "documents": [],
            "patient": {
                "name": "Neha Sharma",
                "gender": "female",
                "phone_number": PHONE,
                "date_of_birth": "1999-04-12",
                "address": "C-22, Ring Road, Lajpat Nagar III, Delhi",
                "pincode": 110024,
                **(patient or {}),
            },
            "registration": registration_details(**registration),
        }

    def post(self, name, body, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse(name, **kwargs), body, format="json")

    def register(self, **registration):
        response = self.post("desk-registration", self.body(**registration))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return Patient.objects.get(external_id=response.data["patient"]["id"])

    def timeline(self, patient):
        return self.client.get(reverse("patient-timeline", args=[patient.external_id]))

    def test_desk_registration_records_user_role_facility_and_time(self):
        patient = self.register()
        event = PatientEvent.objects.get()
        self.assertEqual(event.patient, patient)
        self.assertEqual(event.action, "registered")
        self.assertEqual(event.channel, "desk")
        self.assertEqual(event.user, self.desk)
        self.assertEqual(event.username, "ritu")
        self.assertEqual(event.user_full_name, "Ritu Sharma")
        self.assertEqual(event.roles, ["Front desk"])
        self.assertEqual(event.facility, self.facility)
        self.assertEqual(event.facility_name, "Lajpat Nagar")
        self.assertTrue(event.patient_number.startswith("POH-LN-"))
        self.assertIsNotNone(event.created_date)

        rows = self.timeline(patient).data["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user"]["username"], "ritu")
        self.assertEqual(rows[0]["roles"], ["Front desk"])
        self.assertEqual(rows[0]["action_label"], "Registered")
        self.assertEqual(rows[0]["channel_label"], "Front desk")
        self.assertEqual(rows[0]["facility"]["name"], "Lajpat Nagar")

    def test_edit_records_changed_field_names_but_not_values(self):
        patient = self.register()
        response = self.client.put(
            reverse("desk-registration-detail", args=[patient.external_id]),
            self.body(patient={"name": "Neha Verma"}, last_name="Verma"),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        update = PatientEvent.objects.get(action="registration_updated")
        self.assertEqual(update.user, self.desk)
        self.assertIn("last_name", update.details["fields"])
        self.assertIn("name", update.details["fields"])
        self.assertNotIn("Verma", str(update.details))
        rows = self.timeline(patient).data["results"]
        self.assertEqual(
            [row["action"] for row in rows], ["registration_updated", "registered"]
        )

    def test_rejected_registration_leaves_no_event(self):
        response = self.post("desk-registration", self.body(first_name=""))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(PatientEvent.objects.exists())

    def test_ivf_partner_gets_their_own_event(self):
        body = self.body(registration_type="CPL")
        body["documents"] = [
            stage_document(self.desk, "government_id", "front"),
            stage_document(self.desk, "marriage_certificate"),
        ]
        body["couple"] = {
            "art_consent": True,
            "partner": {
                "first_name": "Rohit",
                "last_name": "Sharma",
                "date_of_birth": "1988-02-10",
                "phone_number": OTHER_PHONE,
                "identity_card_type": "pan",
                "identity_number_last4": "234F",
                "documents": [stage_document(self.desk, "government_id", "front")],
            },
        }
        response = self.post("desk-registration", body)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            sorted(PatientEvent.objects.values_list("channel", flat=True)),
            ["desk", "ivf_partner"],
        )

    def test_timeline_needs_patient_access(self):
        patient = self.register()
        self.client.force_authenticate(user=self.create_user())
        self.assertEqual(self.timeline(patient).status_code, status.HTTP_403_FORBIDDEN)

    def test_audit_trail_is_super_admin_only_and_filters(self):
        self.register()
        self.client.force_authenticate(user=self.admin)
        self.post(
            "emergency-register",
            {
                "facility": str(self.facility.external_id),
                "patient": {"name": "Unknown Male", "gender": "male", "age": 35},
                "triage": "red",
                "is_medico_legal": False,
            },
        )
        url = reverse("registration-audit")
        everything = self.client.get(url)
        self.assertEqual(everything.data["count"], 2)
        emergency = self.client.get(url, {"channel": "emergency"}).data
        self.assertEqual(emergency["count"], 1)
        self.assertEqual(emergency["results"][0]["user"]["username"], "admin")
        self.assertIn("unidentified", emergency["results"][0]["details"])
        self.assertEqual(self.client.get(url, {"search": "ritu"}).data["count"], 1)
        self.assertEqual(
            self.client.get(url, {"facility": str(self.facility.external_id)}).data[
                "count"
            ],
            2,
        )
        self.assertEqual(
            self.client.get(url, {"channel": "nope"}).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.client.get(url, {"facility": "nope"}).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.client.force_authenticate(user=self.desk)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_403_FORBIDDEN)

    def test_emergency_register_and_convert(self):
        # A super admin with no role at this facility (the facility's creator
        # holds Facility Admin there).
        self.client.force_authenticate(user=self.create_super_user(username="root"))
        response = self.post(
            "emergency-register",
            {
                "facility": str(self.facility.external_id),
                "patient": {"name": "Unknown Male", "gender": "male", "age": 35},
                "triage": "red",
                "is_medico_legal": False,
            },
        )
        patient_id = response.data["patient"]["id"]
        converted = self.client.post(
            reverse("emergency-convert", kwargs={"patient_id": patient_id}),
            {"name": "Arun Kumar", "phone_number": OTHER_PHONE},
            format="json",
        )
        self.assertEqual(converted.status_code, status.HTTP_200_OK, converted.data)
        events = list(PatientEvent.objects.order_by("id"))
        self.assertEqual(
            [(e.action, e.channel) for e in events],
            [("registered", "emergency"), ("registration_completed", "emergency")],
        )
        self.assertTrue(events[0].patient_number.startswith("UNK-LN-"))
        self.assertTrue(events[1].patient_number.startswith("POH-LN-"))
        self.assertIn("phone_number", events[1].details["fields"])
        self.assertEqual(events[0].roles, ["Super admin"])

    def test_quick_registration_and_patient_form(self):
        department = self.create_facility_organization(
            facility=self.facility, org_type="dept", name="Cardiology"
        )
        self.client.force_authenticate(user=self.admin)
        with mock.patch(
            "hmis.views.quick_registration.send_text_message", return_value=1
        ):
            response = self.post(
                "quick-registration",
                {
                    "facility": str(self.facility.external_id),
                    "title": "Ms",
                    "first_name": "Neha",
                    "last_name": "Sharma",
                    "date_of_birth": "1999-04-12",
                    "gender": "female",
                    "phone_number": PHONE,
                    "consultation_type": "in_person",
                    "department": str(department.external_id),
                    "send_form_link": True,
                },
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        patient = Patient.objects.get(external_id=response.data["patient"]["id"])
        self.client.force_authenticate(user=None)
        submitted = self.client.post(
            reverse("quick-registration-form", args=[patient.quick_form_token]),
            {
                "email": "neha@example.com",
                "house": "C-22",
                "street": "Ring Road",
                "pincode": "110024",
                "preferred_language": "Hindi",
                "next_of_kin_name": "Ravi Sharma",
                "next_of_kin_relationship": "Brother",
            },
            format="json",
        )
        self.assertEqual(submitted.status_code, status.HTTP_200_OK, submitted.data)
        quick, form = PatientEvent.objects.order_by("id")
        self.assertEqual((quick.action, quick.channel), ("registered", "quick"))
        self.assertTrue(quick.patient_number.startswith("PRE-LN-"))
        # The facility's creator holds Facility Admin there.
        self.assertEqual(quick.roles, ["Facility Admin"])
        self.assertEqual(
            (form.action, form.channel), ("form_submitted", "patient_form")
        )
        self.assertIsNone(form.user)
        self.assertEqual(form.roles, ["Patient"])
        self.assertIn("email", form.details["fields"])
        self.assertNotIn("neha@example.com", str(form.details))

    def test_merged_duplicate_events_show_on_the_kept_record(self):
        kept = self.register()
        duplicate = self.create_patient(phone_number=OTHER_PHONE)
        record_patient_event(duplicate, "registered", "camp", self.desk, self.facility)
        PatientMerge.objects.create(source=duplicate, target=kept, reason="Same person")
        channels = [row["channel"] for row in self.timeline(kept).data["results"]]
        self.assertEqual(sorted(channels), ["camp", "desk"])

    def test_events_are_append_only(self):
        self.register()
        event = PatientEvent.objects.get()
        event.username = "someone-else"
        with self.assertRaises(ValueError):
            event.save()
        with self.assertRaises(ValueError):
            event.delete()
        self.assertEqual(PatientEvent.objects.get().username, "ritu")
