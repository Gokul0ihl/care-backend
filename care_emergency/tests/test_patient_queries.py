"""Pins the query counts of Care's patient list and search and the desk's
patient summary (fold run 4b). Patient carries the desk, Quick Register,
restriction and newborn columns; list and search defer the ones Care never
reads (Patient.list_deferred_fields), and the serialization hooks read only
loaded columns, so a row costs no extra query. Each count is checked with
two page sizes: the same number of queries for 2 and for 5 patients."""

from care.emr.models import Patient
from care.emr.models.patient import PatientIdentifier, PatientIdentifierConfig
from care.utils.tests.base import CareAPITestBase
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

PHONE = "+919800000001"

# Queries per request, whatever the number of patients. See the doc
# (hmis_fe docs/patient-registration.md, "Patient width").
LIST_QUERIES = 7
PHONE_SEARCH_QUERIES = 3
IDENTIFIER_SEARCH_QUERIES = 4
SUMMARY_QUERIES = 7


class PatientQueryCountTests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        admin = self.create_super_user()
        self.facility = self.create_facility(user=admin)
        organization = self.create_facility_organization(facility=self.facility)
        self.desk = self.create_user()
        self.attach_role_facility_organization_user(
            organization,
            self.desk,
            self.create_role_with_permissions(
                ["can_list_patients", "can_create_patient", "can_write_patient"]
            ),
        )
        self.config = PatientIdentifierConfig.objects.create(
            config={
                "system": "test/uhid",
                "unique": True,
                "regex": "",
                "display": "UHID",
                "retrieve_config": {},
            },
        )
        self.geo = self.create_organization(org_type="govt")
        self.patients = []
        self.client.force_authenticate(user=self.desk)

    def add_patients(self, count):
        """Desk-registered patients (all fold column groups filled on some),
        one with no phone and one restricted, so both hooks have work."""
        start = len(self.patients)
        for index in range(start, start + count):
            patient = self.create_patient(
                name=f"Test Patient {index}",
                gender="female",
                phone_number=PHONE,
                year_of_birth=1990,
                geo_organization=self.geo,
                created_by=self.desk,
                updated_by=self.desk,
                registration_facility=self.facility,
                registered_at=timezone.now(),
                registered_by=self.desk,
                first_name="Test",
                last_name=f"Patient{index}",
                mobile_owner="No phone" if index == 0 else "Self",
                restricted_alias="PT-ABCDEF" if index == 1 else None,
                quick_registered_at=timezone.now(),
                quick_facility=self.facility,
                born_at=timezone.now(),
                birth_facility=self.facility,
                birth_weight_grams=3000,
            )
            identifier = PatientIdentifier.objects.create(
                patient=patient, config=self.config, value=f"UH{index:04d}"
            )
            patient.instance_identifiers = [
                {"config": str(self.config.external_id), "value": identifier.value}
            ]
            patient.save(update_fields=["instance_identifiers"])
            self.patients.append(patient)

    def assert_flat(self, expected, request):
        """The same query count for 2 and for 5 patients."""
        counts = []
        for total in (2, 5):
            self.add_patients(total - len(self.patients))
            # Warm the process caches (identifier configs, users, roles)
            # first, so only the per-request queries are counted.
            request()
            with CaptureQueriesContext(connection) as queries:
                response = request()
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            counts.append(len(queries))
            self.patient_sql = [
                x["sql"]
                for x in queries.captured_queries
                if '"emr_patient"."name"' in x["sql"]
            ]
        self.assertEqual(
            counts,
            [expected, expected],
            "\n".join(x["sql"][:300] for x in queries.captured_queries),
        )
        return response

    def assert_narrow(self):
        """The patient rows were read without the deferred fold columns, but
        with the ones the hooks read."""
        self.assertTrue(self.patient_sql)
        for sql in self.patient_sql:
            self.assertNotIn('"emr_patient"."quick_form_details"', sql)
            self.assertNotIn('"emr_patient"."first_name"', sql)
            self.assertIn('"emr_patient"."restricted_alias"', sql)
            self.assertIn('"emr_patient"."mobile_owner"', sql)

    def test_list(self):
        response = self.assert_flat(
            LIST_QUERIES,
            lambda: self.client.get(
                reverse("patient-list"),
                {"phone_number": PHONE, "limit": 20, "ordering": "-created_date"},
            ),
        )
        results = {x["name"]: x for x in response.data["results"]}
        self.assertEqual(len(results), 5)
        # The hooks still apply: masked alias, hidden desk number.
        self.assertIn("PT-ABCDEF", results)
        self.assertEqual(results["Test Patient 0"]["phone_number"], "")
        self.assert_narrow()

    def test_phone_search(self):
        response = self.assert_flat(
            PHONE_SEARCH_QUERIES,
            lambda: self.client.post(
                reverse("patient-search"), {"phone_number": PHONE}, format="json"
            ),
        )
        self.assertTrue(response.data["partial"])
        self.assertEqual(len(response.data["results"]), 5)
        self.assert_narrow()

    def test_identifier_search(self):
        self.config.config["retrieve_config"] = {"retrieve_partial_search": True}
        self.config.save()
        response = self.assert_flat(
            IDENTIFIER_SEARCH_QUERIES,
            lambda: self.client.post(
                reverse("patient-search"),
                {"config": str(self.config.external_id), "value": "UH"},
                format="json",
            ),
        )
        self.assertFalse(response.data["partial"])
        names = {x["name"] for x in response.data["results"]}
        self.assertEqual(len(names), 5)
        self.assertIn("PT-ABCDEF", names)
        self.assert_narrow()

    def test_summary(self):
        self.assert_flat(
            SUMMARY_QUERIES,
            lambda: self.client.post(
                reverse("patient-summary"),
                {"patients": [str(x.external_id) for x in self.patients]},
                format="json",
            ),
        )
        self.assert_narrow()

    def test_list_defers_fold_columns_but_not_hook_columns(self):
        deferred = set(Patient.list_deferred_fields())
        self.assertIn("first_name", deferred)
        self.assertIn("quick_form_details", deferred)
        self.assertIn("born_at", deferred)
        self.assertIn("registration_facility", deferred)
        self.assertFalse({"mobile_owner", "restricted_alias"} & deferred)
        # None of Care's own columns.
        self.assertFalse({"name", "phone_number", "meta", "extensions"} & deferred)
