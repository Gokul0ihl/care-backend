from care.emr.locks.billing import PatientCreateLock
from care.emr.models import Encounter, Patient
from care.utils.tests.base import CareAPITestBase
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from hmis.services import emergency_encounters, is_emergency_desk_phone

DESK_NUMBER = "+919876543210"
# Queries for the emergency list, whatever the number of records (fold run 5a).
LIST_QUERIES = 3
REAL_NUMBER = "+919812345678"
COMPANION_NUMBER = "+919898989898"


class EmergencyAPITests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        self.user = self.create_super_user()
        self.facility = self.create_facility(user=self.user)
        self.facility.unit_code = "LN"
        self.facility.save(update_fields=["unit_code"])
        self.client.force_authenticate(user=self.user)
        PatientCreateLock().release()

    def set_desk(self):
        self.facility.emergency_desk_phone = DESK_NUMBER
        self.facility.save(update_fields=["emergency_desk_phone"])

    def register_data(self, **kwargs):
        data = {
            "facility": str(self.facility.external_id),
            "patient": {"name": "Unknown Male", "gender": "male", "age": 35},
            "triage": "red",
            "is_medico_legal": True,
            "mlc_nature": "Road accident",
            "police_station": "Central",
            "distinguishing_marks": "Scar on left arm",
            "brought_by": {
                "name": "Ravi",
                "phone_number": COMPANION_NUMBER,
                "relationship": "Other",
            },
        }
        data.update(kwargs)
        return data

    def register(self, **kwargs):
        # Care releases its create locks on commit; run those callbacks here.
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                reverse("emergency-register"),
                self.register_data(**kwargs),
                format="json",
            )

    def test_register_requires_desk_number(self):
        response = self.register()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Patient.objects.exists())

    def test_register_creates_patient_encounter_and_mlc_number(self):
        self.set_desk()
        response = self.register()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        emergency = response.data["emergency"]
        self.assertRegex(emergency["mlc_number"], r"^MLC-\d{4}-0001$")
        self.assertEqual(emergency["brought_by"]["phone_number"], COMPANION_NUMBER)

        patient = Patient.objects.get(external_id=response.data["patient"]["id"])
        self.assertEqual(patient.phone_number, DESK_NUMBER)
        encounter = Encounter.objects.get(external_id=response.data["encounter"]["id"])
        self.assertEqual(encounter.encounter_class, "emer")
        self.assertEqual(encounter.priority, "emergency")
        self.assertEqual(encounter.hospitalization["admit_source"], "emd")

    def test_mlc_numbers_are_sequential(self):
        self.set_desk()
        first = self.register().data["emergency"]["mlc_number"]
        response = self.register()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        second = response.data["emergency"]["mlc_number"]
        self.assertTrue(first.endswith("0001"))
        self.assertTrue(second.endswith("0002"))

    def test_non_medico_legal_gets_no_mlc_number(self):
        self.set_desk()
        response = self.register(
            is_medico_legal=False, mlc_nature="Assault", police_station="X"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIsNone(response.data["emergency"]["mlc_number"])
        self.assertEqual(response.data["emergency"]["mlc_nature"], "")

    def test_medico_legal_requires_nature_and_police_station(self):
        self.set_desk()
        response = self.register(mlc_nature="", police_station="")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_age_zero_is_accepted(self):
        self.set_desk()
        response = self.register(
            patient={"name": "Unknown Female", "gender": "female", "age": 0}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        patient = Patient.objects.get()
        self.assertLessEqual(patient.date_of_birth, timezone.localdate())
        self.assertTrue(patient.meta["date_of_birth_approximate"])

    def test_age_over_120_is_rejected(self):
        self.set_desk()
        response = self.register(
            patient={"name": "Unknown Female", "gender": "female", "age": 121}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Patient.objects.exists())

    def test_medico_legal_defaults_to_no(self):
        self.set_desk()
        data = self.register_data()
        for field in ("is_medico_legal", "mlc_nature", "police_station"):
            del data[field]
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("emergency-register"), data, format="json"
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(response.data["emergency"]["is_medico_legal"])
        self.assertIsNone(response.data["emergency"]["mlc_number"])

    def test_quick_registration_needs_chief_complaint(self):
        self.set_desk()
        patient = {"name": "Arun Kumar", "gender": "male", "age": 40}
        response = self.register(patient=patient, is_unidentified=False)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Patient.objects.exists())

        response = self.register(
            patient=patient,
            is_unidentified=False,
            chief_complaint="Chest pain",
            mode_of_arrival="108 ambulance",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        emergency = response.data["emergency"]
        self.assertEqual(emergency["chief_complaint"], "Chest pain")
        self.assertEqual(emergency["mode_of_arrival"], "108 ambulance")
        self.assertFalse(emergency["is_unidentified"])
        self.assertEqual(Patient.objects.get().name, "Arun Kumar")

    def test_unknown_options_are_rejected(self):
        self.set_desk()
        self.assertEqual(
            self.register(chief_complaint="Headache").status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.register(mode_of_arrival="Helicopter").status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertFalse(Patient.objects.exists())

    def test_brought_by_needs_name_or_phone(self):
        self.set_desk()
        response = self.register(brought_by={"phone_number": COMPANION_NUMBER})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["emergency"]["brought_by"]["name"], "")
        response = self.register(brought_by={"name": " "})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_chief_complaint_and_arrival(self):
        self.set_desk()
        encounter_id = self.register().data["encounter"]["id"]
        url = reverse(
            "emergency-encounter-detail", kwargs={"external_id": encounter_id}
        )
        response = self.client.patch(
            url,
            {"chief_complaint": "Seizure", "mode_of_arrival": "Police"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["chief_complaint"], "Seizure")
        self.assertEqual(response.data["mode_of_arrival"], "Police")
        response = self.client.patch(url, {"chief_complaint": "Cough"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_register_without_permission(self):
        self.set_desk()
        self.client.force_authenticate(user=self.create_user())
        response = self.register()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_update_can_clear_medico_legal(self):
        self.set_desk()
        encounter_id = self.register().data["encounter"]["id"]
        url = reverse(
            "emergency-encounter-detail", kwargs={"external_id": encounter_id}
        )
        response = self.client.patch(url, {"is_medico_legal": False}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(response.data["is_medico_legal"])
        # The issued number is kept so it is never reused.
        self.assertIsNotNone(response.data["mlc_number"])

    def test_list_filters(self):
        self.set_desk()
        self.register()
        self.register(is_medico_legal=False, triage="green", chief_complaint="Burns")
        url = reverse("emergency-encounter-list")
        facility = str(self.facility.external_id)
        response = self.client.get(url, {"facility": facility, "is_medico_legal": "true"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        response = self.client.get(url, {"facility": facility, "chief_complaint": "Burns"})
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["triage"], "green")

    def test_convert_to_normal_patient(self):
        self.set_desk()
        patient_id = self.register().data["patient"]["id"]
        url = reverse("emergency-convert", kwargs={"patient_id": patient_id})
        response = self.client.post(
            url, {"name": "Arun Kumar", "phone_number": REAL_NUMBER}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        patient = Patient.objects.get(external_id=patient_id)
        self.assertEqual(patient.phone_number, REAL_NUMBER)
        self.assertEqual(patient.name, "Arun Kumar")
        record = Encounter.objects.get(patient=patient)
        self.assertIsNotNone(record.converted_at)
        self.assertFalse(record.is_unidentified)

    def test_convert_rejects_desk_number(self):
        self.set_desk()
        patient_id = self.register().data["patient"]["id"]
        url = reverse("emergency-convert", kwargs={"patient_id": patient_id})
        response = self.client.post(url, {"phone_number": DESK_NUMBER}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIsNone(
            Encounter.objects.get(patient__external_id=patient_id).converted_at
        )

    def test_desk_number_admin_only(self):
        url = reverse("emergency-desk")
        body = {"facility": str(self.facility.external_id), "phone_number": DESK_NUMBER}
        self.assertEqual(
            self.client.put(url, body, format="json").status_code, status.HTTP_200_OK
        )
        self.client.force_authenticate(user=self.create_user())
        self.assertEqual(
            self.client.put(url, body, format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_desk_and_unit_are_facility_columns(self):
        other = self.create_facility(user=self.user)
        query = {"facility": str(other.external_id)}
        for name in ("emergency-desk", "facility-unit"):
            with self.subTest(name=name):
                response = self.client.get(reverse(name), query)
                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        # A facility without a desk number doesn't make "" a desk number.
        self.assertFalse(is_emergency_desk_phone(""))
        desk = self.client.put(
            reverse("emergency-desk"),
            {**query, "phone_number": DESK_NUMBER},
            format="json",
        )
        unit = self.client.put(
            reverse("facility-unit"), {**query, "code": "KB"}, format="json"
        )
        other.refresh_from_db()
        self.assertEqual(other.emergency_desk_phone, DESK_NUMBER)
        self.assertEqual(other.unit_code, "KB")
        for name, written, key, value in (
            ("emergency-desk", desk, "phone_number", DESK_NUMBER),
            ("facility-unit", unit, "code", "KB"),
        ):
            with self.subTest(name=name):
                read = self.client.get(reverse(name), query)
                self.assertEqual(read.status_code, status.HTTP_200_OK)
                for response in (written, read):
                    self.assertEqual(
                        set(response.data), {"facility", key, "modified_date"}
                    )
                    self.assertEqual(response.data["facility"], query["facility"])
                    self.assertEqual(response.data[key], value)
        self.assertTrue(is_emergency_desk_phone(DESK_NUMBER))

    def test_emergency_columns_on_encounter(self):
        self.set_desk()
        data = self.register().data
        encounter = Encounter.objects.get(external_id=data["encounter"]["id"])
        self.assertEqual(data["emergency"]["id"], data["encounter"]["id"])
        self.assertEqual(data["emergency"]["encounter"], data["encounter"]["id"])
        self.assertEqual(encounter.triage, "red")
        self.assertEqual(encounter.mlc_number, data["emergency"]["mlc_number"])
        self.assertEqual(encounter.brought_by_name, "Ravi")
        self.assertTrue(encounter.is_unidentified)

    def test_list_and_detail_skip_other_encounters(self):
        self.set_desk()
        patient_id = self.register().data["patient"]["id"]
        patient = Patient.objects.get(external_id=patient_id)
        organization = self.create_facility_organization(facility=self.facility)
        other = self.create_encounter(patient, self.facility, organization)
        self.assertEqual(other.triage, "")
        self.assertFalse(other.is_unidentified)
        self.assertEqual(emergency_encounters().count(), 1)
        url = reverse("emergency-encounter-list")
        response = self.client.get(url, {"facility": str(self.facility.external_id)})
        self.assertEqual(response.data["count"], 1)
        detail = reverse(
            "emergency-encounter-detail", kwargs={"external_id": other.external_id}
        )
        self.assertEqual(self.client.get(detail).status_code, 404)

    def test_mlc_number_unique_per_facility(self):
        self.set_desk()
        number = self.register().data["emergency"]["mlc_number"]
        patient = Patient.objects.first()
        organization = self.create_facility_organization(facility=self.facility)
        other = self.create_encounter(patient, self.facility, organization)
        other.mlc_number = number
        with self.assertRaises(IntegrityError), transaction.atomic():
            other.save(update_fields=["mlc_number"])

    def test_list_query_count(self):
        """The list costs the same number of queries for 2 and 5 records, and
        doesn't load the patients' desk columns."""
        self.set_desk()
        url = reverse("emergency-encounter-list")
        facility = str(self.facility.external_id)
        counts = []
        for total in (2, 5):
            while emergency_encounters().count() < total:
                self.register()
            with CaptureQueriesContext(connection) as context:
                response = self.client.get(url, {"facility": facility})
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data["count"], total)
            counts.append(len(context.captured_queries))
            rows = [
                x["sql"] for x in context.captured_queries if "mlc_number" in x["sql"]
            ]
            self.assertTrue(rows)
            for sql in rows:
                self.assertNotIn('"emr_patient"."first_name"', sql)
        self.assertEqual(counts[0], counts[1])
        self.assertEqual(counts[0], LIST_QUERIES)
