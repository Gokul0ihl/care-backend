import datetime

from care.emr.locks.billing import PatientCreateLock
from care.emr.models import Patient
from care.utils.tests.base import CareAPITestBase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from hmis.merge import (
    compare,
    merge_blockers,
    merge_patients,
    planned_moves,
)
from hmis.tests.test_registration import registration_details

PHONE = "+919812345678"


def keep_target(source, target):
    return {x.field.key: "target" for x in compare(source, target) if x.conflict}


class NewbornRegistrationTests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        self.user = self.create_super_user()
        self.facility = self.create_facility(user=self.user)
        self.facility.unit_code = "LN"
        self.facility.save(update_fields=["unit_code"])
        self.client.force_authenticate(user=self.user)
        PatientCreateLock().release()
        self.mother = self.register_mother()

    def register_mother(self, **registration):
        body = {
            "facility": str(self.facility.external_id),
            "documents": [],
            "patient": {
                "name": "Test Mother",
                "gender": "female",
                "phone_number": PHONE,
                "date_of_birth": "1994-04-12",
                "address": "C-22, Ring Road, Lajpat Nagar III, Delhi",
                "pincode": 110024,
            },
            "registration": registration_details(
                first_name="Test",
                last_name="Mother",
                title="Mrs",
                marital_status="married",
                **registration,
            ),
        }
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("desk-registration"), body, format="json"
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        PatientCreateLock().release()
        return Patient.objects.get(external_id=response.data["patient"]["id"])

    def baby(self, **kwargs):
        data = {
            "gender": "female",
            "born_at": (timezone.now() - datetime.timedelta(hours=2)).isoformat(),
            "birth_weight_grams": 3100,
            "length_cm": 49.5,
            "head_circumference_cm": 34,
            "apgar_1_min": 8,
            "apgar_5_min": 9,
        }
        data.update(kwargs)
        return data

    def body(self, babies=None, **kwargs):
        data = {
            "facility": str(self.facility.external_id),
            "mother": str(self.mother.external_id),
            "delivery_type": "Normal vaginal",
            "gestation_weeks": 39,
            "babies": babies if babies is not None else [self.baby()],
        }
        data.update(kwargs)
        return data

    def register(self, body):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse("newborn-register"), body, format="json")

    def assert_rejected(self, body, message):
        before = Patient.objects.count()
        response = self.register(body)
        self.assertIn(response.status_code, (400, 403), response.data)
        self.assertIn(message, str(response.data))
        self.assertEqual(Patient.objects.count(), before)
        self.assertFalse(Patient.objects.filter(born_at__isnull=False).exists())

    def test_single_baby_gets_own_uhid_and_inherited_registration(self):
        response = self.register(self.body())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["inherited"])
        [entry] = response.data["babies"]
        baby = Patient.objects.get(external_id=entry["patient"]["id"])
        self.assertEqual(baby.name, "Baby Of Test Mother")
        self.assertEqual(baby.gender, "female")
        self.assertEqual(baby.phone_number, PHONE)
        self.assertEqual(baby.pincode, 110024)
        number = next(
            item["value"]
            for item in entry["patient"]["instance_identifiers"]
            if item["config"]["config"]["system"] == "care_emergency/uhid"
        )
        self.assertTrue(number.startswith("POH-LN-"))

        record = Patient.objects.get(id=baby.id)
        self.assertEqual(record.registration_facility, self.facility)
        self.assertIsNotNone(record.registered_at)
        self.assertEqual(record.title, "Ms")
        self.assertEqual(record.first_name, "Baby")
        self.assertEqual(record.guardian_relationship, "D/O")
        self.assertEqual(record.mother_name, "Test Mother")
        self.assertEqual(record.mobile_owner, "Mother")
        self.assertEqual(record.city, "New Delhi")
        self.assertEqual(record.payer_type, "Self")
        self.assertEqual(record.consent_given_by, "Parent")
        self.assertTrue(record.general_consent)
        # Never inherited: clinical details and identity.
        self.assertEqual(record.allergy_status, "")
        self.assertFalse(record.desk_allergies.exists())
        self.assertEqual(record.identity_card_type, "")
        self.assertEqual(record.abha_address, "")

        self.assertEqual(record.birth_mother, self.mother)
        self.assertEqual(record.birth_facility, self.facility)
        self.assertEqual(record.birth_weight_grams, 3100)
        self.assertEqual(record.apgar_5_min, 9)
        self.assertEqual(record.delivery_type, "Normal vaginal")
        self.assertEqual(entry["birth"]["length_cm"], 49.5)
        self.assertEqual(entry["birth"]["id"], str(baby.external_id))
        self.assertEqual(entry["birth"]["baby"], str(baby.external_id))
        self.assertEqual(entry["birth"]["mother"], str(self.mother.external_id))
        self.assertEqual(entry["birth"]["facility"], str(self.facility.external_id))
        self.assertEqual(entry["birth"]["birth_order"], 1)
        self.assertEqual(entry["birth"]["number_of_babies"], 1)
        self.assertEqual(entry["birth"]["gestation_weeks"], 39)
        self.assertEqual(entry["birth"]["head_circumference_cm"], 34.0)
        self.assertEqual(list(self.mother.newborn_babies.all()), [record])
        # The mother is not a newborn.
        self.mother.refresh_from_db()
        self.assertIsNone(self.mother.born_at)

        detail = self.client.get(
            reverse("desk-registration-detail", args=[baby.external_id])
        )
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(detail.data["birth"]["mother"], str(self.mother.external_id))

    def test_twins_are_separate_patients_numbered_in_order(self):
        response = self.register(
            self.body(babies=[self.baby(gender="male"), self.baby(birth_weight_grams=2400)])
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        names = [entry["patient"]["name"] for entry in response.data["babies"]]
        self.assertEqual(names, ["Baby 1 Of Test Mother", "Baby 2 Of Test Mother"])
        births = Patient.objects.filter(birth_mother=self.mother).order_by(
            "birth_order"
        )
        self.assertEqual([b.birth_order for b in births], [1, 2])
        self.assertEqual({b.number_of_babies for b in births}, {2})
        first = births[0]
        self.assertEqual(first.title, "Master")
        self.assertEqual(first.guardian_relationship, "S/O")

    def test_staff_mother_makes_baby_a_dependant(self):
        Patient.objects.filter(id=self.mother.id).update(
            payer_type="Staff", payer_name="POH staff — self", employee_id="E123"
        )
        response = self.register(self.body())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        record = Patient.objects.exclude(id=self.mother.id).get()
        self.assertEqual(record.payer_type, "Staff")
        self.assertEqual(record.payer_name, "POH staff — dependant")
        self.assertEqual(record.policy_holder_relationship, "Child")
        self.assertEqual(record.employee_id, "E123")

    def test_mother_without_desk_registration_gets_defaults(self):
        # Registered outside the desk.
        Patient.objects.filter(id=self.mother.id).update(
            registered_at=None, registered_by=None, registration_facility=None
        )
        response = self.register(self.body())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(response.data["inherited"])
        record = Patient.objects.exclude(id=self.mother.id).get()
        self.assertEqual(record.registration_type, "DOM")
        self.assertEqual(record.payer_type, "Self")

    def test_birth_weight_out_of_range_is_rejected(self):
        self.assert_rejected(
            self.body(babies=[self.baby(birth_weight_grams=250)]), "birth_weight_grams"
        )

    def test_baby_older_than_28_days_is_rejected(self):
        old = (timezone.now() - datetime.timedelta(days=29)).isoformat()
        self.assert_rejected(
            self.body(babies=[self.baby(born_at=old)]), "older than 28 days"
        )

    def test_future_birth_time_is_rejected(self):
        later = (timezone.now() + datetime.timedelta(hours=1)).isoformat()
        self.assert_rejected(self.body(babies=[self.baby(born_at=later)]), "future")

    def test_male_mother_is_rejected(self):
        Patient.objects.filter(id=self.mother.id).update(gender="male")
        self.assert_rejected(self.body(), "female patient")

    def test_too_many_babies_is_rejected(self):
        self.assert_rejected(self.body(babies=[self.baby()] * 5), "babies")

    def test_mother_and_baby_cannot_be_merged(self):
        response = self.register(self.body())
        baby = Patient.objects.get(external_id=response.data["babies"][0]["patient"]["id"])
        self.assertIn(
            "These records are a mother and her baby", merge_blockers(baby, self.mother)
        )

    def test_mother_registration_detail_has_no_birth(self):
        detail = self.client.get(
            reverse("desk-registration-detail", args=[self.mother.external_id])
        )
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertIsNone(detail.data["birth"])

    def test_merge_moves_birth_details_and_babies(self):
        # A duplicate baby record (registered as a newborn) merged into a
        # plain record of the same baby takes the birth details along, and a
        # duplicate mother's babies follow her into the kept record.
        response = self.register(self.body())
        baby = Patient.objects.get(external_id=response.data["babies"][0]["patient"]["id"])
        plain = self.register_mother()
        Patient.objects.filter(id=plain.id).update(name="Baby Of Test Mother")
        plain.refresh_from_db()
        baby.refresh_from_db()
        self.assertEqual(planned_moves(baby, plain).get("Birth record"), 1)
        self.assertEqual(merge_blockers(baby, plain), [])
        merge_patients(baby, plain, keep_target(baby, plain), "Same baby", self.user)
        plain.refresh_from_db()
        self.assertEqual(plain.birth_mother, self.mother)
        self.assertEqual(plain.birth_weight_grams, 3100)
        self.assertEqual(plain.birth_order, 1)

        other_mother = self.register_mother()
        self.assertEqual(
            planned_moves(self.mother, other_mother).get("Birth record"), 1
        )
        merge_patients(
            self.mother,
            other_mother,
            keep_target(self.mother, other_mother),
            "Same mother",
            self.user,
        )
        plain.refresh_from_db()
        self.assertEqual(plain.birth_mother, other_mother)

    def test_two_babies_cannot_be_merged(self):
        response = self.register(
            self.body(babies=[self.baby(), self.baby(gender="male")])
        )
        first, second = (
            Patient.objects.get(external_id=entry["patient"]["id"])
            for entry in response.data["babies"]
        )
        self.assertIn("Both records have a birth record", merge_blockers(first, second))
