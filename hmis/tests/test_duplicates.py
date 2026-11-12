import datetime

from django.urls import reverse
from django.utils import timezone

from care.utils.tests.base import CareAPITestBase

PHONE = "+919812345678"
OTHER_PHONE = "+919898989898"
BORN = datetime.date(1990, 3, 12)


class RegistrationMatchTests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        self.user = self.create_super_user()
        self.client.force_authenticate(user=self.user)
        self.url = reverse("registration-matches")

    def patient(self, **kwargs):
        data = {
            "name": "Neha Sharma",
            "first_name": "Neha",
            "last_name": "Sharma",
            "gender": "female",
            "phone_number": OTHER_PHONE,
            "date_of_birth": BORN,
            "year_of_birth": BORN.year,
            "meta": {},
        }
        data.update(kwargs)
        return self.create_patient(**data)

    def matches(self, **body):
        response = self.client.post(self.url, body, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return {row["id"]: row["reasons"] for row in response.data["results"]}

    def test_same_mobile_matches(self):
        patient = self.patient(name="Ravi Kumar", phone_number=PHONE)
        self.patient(name="Someone Else")
        found = self.matches(phone_number=PHONE, name="Asha Rao")
        self.assertEqual(found, {str(patient.external_id): ["phone"]})

    def test_desk_no_phone_number_never_matches(self):
        self.patient(phone_number=PHONE, mobile_owner="No phone")
        self.assertEqual(self.matches(phone_number=PHONE), {})

    def test_same_name_and_date_of_birth_match(self):
        patient = self.patient()
        self.patient(date_of_birth=datetime.date(1991, 3, 12), year_of_birth=1991)
        found = self.matches(name="neha  sharma", date_of_birth=BORN.isoformat())
        self.assertEqual(found, {str(patient.external_id): ["name_birth"]})

    def test_name_matches_without_the_middle_name(self):
        patient = self.patient(name="Neha Devi Sharma", middle_name="Devi")
        found = self.matches(
            name="Neha Sharma",
            first_name="Neha",
            last_name="Sharma",
            date_of_birth=BORN.isoformat(),
        )
        self.assertIn(str(patient.external_id), found)

    def test_age_matches_the_birth_year(self):
        year = timezone.localdate().year - 30
        exact = self.patient(
            date_of_birth=datetime.date(year, 1, 5), year_of_birth=year
        )
        self.patient(
            date_of_birth=datetime.date(year - 5, 1, 5), year_of_birth=year - 5
        )
        found = self.matches(name="Neha Sharma", age=30)
        self.assertEqual(list(found), [str(exact.external_id)])

    def test_exact_date_matches_an_estimated_birth_year(self):
        estimate = self.patient(
            date_of_birth=datetime.date(1990, 7, 1),
            meta={"date_of_birth_approximate": True},
        )
        exact_other_day = self.patient(date_of_birth=datetime.date(1990, 8, 1))
        found = self.matches(name="Neha Sharma", date_of_birth=BORN.isoformat())
        self.assertIn(str(estimate.external_id), found)
        self.assertNotIn(str(exact_other_day.external_id), found)

    def test_id_last_four_needs_the_first_name_or_birth_year(self):
        same = self.patient(identity_card_type="aadhaar", identity_number_last4="7741")
        stranger = self.patient(
            name="Ravi Kumar",
            first_name="Ravi",
            last_name="Kumar",
            date_of_birth=datetime.date(1970, 1, 1),
            year_of_birth=1970,
            identity_card_type="aadhaar",
            identity_number_last4="7741",
        )
        found = self.matches(
            identity_card_type="aadhaar",
            identity_number_last4="7741",
            first_name="Neha",
            name="Neha Verma",
        )
        self.assertEqual(found, {str(same.external_id): ["identity"]})
        self.assertNotIn(str(stranger.external_id), found)

    def test_reasons_combine_and_strongest_first(self):
        weak = self.patient(name="Ravi Kumar", phone_number=PHONE)
        strong = self.patient(
            phone_number=PHONE,
            identity_card_type="aadhaar",
            identity_number_last4="7741",
        )
        response = self.client.post(
            self.url,
            {
                "phone_number": PHONE,
                "identity_card_type": "aadhaar",
                "identity_number_last4": "7741",
                "name": "Neha Sharma",
                "first_name": "Neha",
                "last_name": "Sharma",
                "date_of_birth": BORN.isoformat(),
            },
            format="json",
        )
        ids = [row["id"] for row in response.data["results"]]
        self.assertEqual(ids, [str(strong.external_id), str(weak.external_id)])
        self.assertEqual(
            response.data["results"][0]["reasons"], ["phone", "identity", "name_birth"]
        )

    def test_merged_away_records_are_left_out(self):
        self.patient(phone_number=PHONE, deleted=True)
        self.assertEqual(self.matches(phone_number=PHONE), {})

    def test_restricted_patients_are_left_out(self):
        self.patient(phone_number=PHONE, restricted_alias="KX4Q7")
        self.assertEqual(self.matches(phone_number=PHONE), {})

    def test_only_patients_the_user_can_view(self):
        self.patient(phone_number=PHONE)
        self.client.force_authenticate(user=self.create_user())
        self.assertEqual(self.matches(phone_number=PHONE), {})

    def test_nothing_to_match_on(self):
        self.patient()
        self.assertEqual(self.matches(), {})
        self.assertEqual(self.matches(name="Neha Sharma"), {})
