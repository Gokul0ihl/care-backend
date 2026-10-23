import datetime

from care.emr.locks.billing import PatientCreateLock
from care.emr.models import Encounter, Patient
from care.emr.models.patient import PatientIdentifier
from care.utils.tests.base import CareAPITestBase
from django.urls import reverse
from rest_framework import status

from care_emergency.constants import PatientNumberSeries
from care_emergency.models import (
    FacilityUnit,
    PatientMerge,
    RestrictedPatient,
)
from care_emergency.services import patient_number
from care_emergency.tests.test_registration import PHONE, registration_details


class PatientMergeTests(CareAPITestBase):
    """Fake patients only: two desk registrations of the same (made-up)
    person, differing in date of birth."""

    def setUp(self):
        super().setUp()
        self.user = self.create_super_user()
        self.facility = self.create_facility(user=self.user)
        self.organization = self.create_facility_organization(facility=self.facility)
        FacilityUnit.objects.create(facility=self.facility, code="LN")
        self.client.force_authenticate(user=self.user)
        PatientCreateLock().release()
        self.target = self.register("1999-04-12")
        self.source = self.register("1999-04-21")
        self.encounter = self.create_encounter(
            self.source, self.facility, self.organization
        )

    def post(self, name, body):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse(name), body, format="json")

    def register(self, date_of_birth, **details):
        PatientCreateLock().release()
        response = self.post(
            "desk-registration",
            {
                "facility": str(self.facility.external_id),
                "patient": {
                    "name": "Neha Sharma",
                    "gender": "female",
                    "phone_number": PHONE,
                    "date_of_birth": date_of_birth,
                    "address": "C-22, Ring Road, Lajpat Nagar III, Delhi",
                    "pincode": 110024,
                },
                "registration": registration_details(**details),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return Patient.objects.get(external_id=response.data["patient"]["id"])

    def walk_in(self, name, date_of_birth):
        """A patient Care created without a desk registration (fake data)."""
        return Patient.objects.create(
            name=name,
            gender="female",
            phone_number=PHONE,
            date_of_birth=date_of_birth,
            address="14 Park Street, Kolkata",
            pincode=700016,
        )

    def preview(self, source=None, target=None):
        return self.client.get(
            reverse("patient-merge"),
            {
                "source": str((source or self.source).external_id),
                "target": str((target or self.target).external_id),
            },
        )

    def merge(self, choices, **kwargs):
        PatientCreateLock().release()
        return self.post(
            "patient-merge",
            {
                "source": str(self.source.external_id),
                "target": str(self.target.external_id),
                "choices": choices,
                "reason": "Registered twice at the desk",
                **kwargs,
            },
        )

    def conflicts(self):
        response = self.preview()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {x["key"] for x in response.data["fields"] if x["conflict"]}

    # Summary

    def test_summary_returns_number_and_last_visit(self):
        response = self.post(
            "patient-summary",
            {
                "patients": [
                    str(self.source.external_id),
                    str(self.target.external_id),
                    "00000000-0000-4000-8000-000000000000",
                ]
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        results = response.data["results"]
        self.assertEqual(
            [x["id"] for x in results],
            [str(self.source.external_id), str(self.target.external_id)],
        )
        self.assertEqual(results[0]["patient_number"]["label"], "UHID")
        self.assertEqual(
            results[0]["patient_number"]["value"],
            patient_number(self.source, PatientNumberSeries.uhid),
        )
        self.assertEqual(results[0]["last_visit"], self.encounter.created_date)
        self.assertIsNone(results[1]["last_visit"])

    def test_summary_leaves_out_patients_the_user_cant_see(self):
        self.client.force_authenticate(user=self.create_user())
        response = self.post(
            "patient-summary", {"patients": [str(self.source.external_id)]}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["results"], [])

    def test_summary_needs_one_to_fifty_patients(self):
        self.assertEqual(
            self.post("patient-summary", {"patients": []}).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    # Preview

    def test_preview_shows_conflicts_and_moves(self):
        response = self.preview()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        fields = {x["key"]: x for x in response.data["fields"]}
        self.assertTrue(fields["date_of_birth"]["conflict"])
        self.assertEqual(fields["date_of_birth"]["source"], "21 Apr 1999")
        self.assertEqual(fields["date_of_birth"]["target"], "12 Apr 1999")
        self.assertFalse(fields["name"]["conflict"])
        self.assertEqual(response.data["moves"]["Visits"], 1)
        self.assertEqual(response.data["blockers"], [])
        self.assertEqual(
            response.data["source"]["patient_number"]["label"], "UHID"
        )

    def test_preview_blocks_merging_a_record_into_itself(self):
        response = self.preview(source=self.target)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["blockers"], ["Choose two different records"])

    def test_unknown_patient_is_404(self):
        response = self.client.get(
            reverse("patient-merge"),
            {
                "source": "00000000-0000-4000-8000-000000000000",
                "target": str(self.target.external_id),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Merge

    def test_merge_moves_records_and_retires_the_duplicate(self):
        choices = {key: "target" for key in self.conflicts()}
        choices["date_of_birth"] = "source"
        old_uhid = patient_number(self.source, PatientNumberSeries.uhid)
        kept_uhid = patient_number(self.target, PatientNumberSeries.uhid)

        response = self.merge(choices)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["patient"]["id"], str(self.target.external_id))
        self.assertEqual(response.data["moved"]["Visits"], 1)
        self.target.refresh_from_db()
        self.assertEqual(self.target.date_of_birth, datetime.date(1999, 4, 21))
        self.encounter.refresh_from_db()
        self.assertEqual(self.encounter.patient_id, self.target.id)
        self.assertTrue(Patient._base_manager.get(id=self.source.id).deleted)  # noqa: SLF001
        self.assertFalse(Encounter.objects.filter(patient=self.source).exists())
        uhids = PatientIdentifier.objects.filter(
            patient=self.target, config__config__system="care_emergency/uhid"
        ).values_list("value", flat=True)
        self.assertEqual(list(uhids), [kept_uhid])
        self.assertIsNotNone(self.target.registered_at)
        merge = PatientMerge.objects.get(source=self.source)
        self.assertEqual(merge.target_id, self.target.id)
        self.assertEqual(merge.choices["date_of_birth"], "source")
        self.assertEqual(merge.source_snapshot["patient"][0]["pk"], self.source.id)
        self.assertNotIn("registration", merge.source_snapshot)
        self.assertEqual(len(merge.source_snapshot["allergies"]), 2)

        # The duplicate's old UHID now finds the kept record.
        found = self.client.get(reverse("patient-number-search"), {"uhid": old_uhid})
        self.assertEqual(found.status_code, status.HTTP_200_OK, found.data)
        self.assertEqual(
            [x["id"] for x in found.data["results"]], [str(self.target.external_id)]
        )

    def test_desk_fields_from_the_duplicate_when_both_registered(self):
        self.source = self.register(
            "1999-04-21",
            religion="Hindu",
            remarks="Needs a ground-floor room",
            allergies=[{"substance": "Sulfa", "reaction": "Rash", "severity": "mild"}],
        )
        conflicts = self.conflicts()
        self.assertTrue({"remarks", "allergies", "date_of_birth"} <= conflicts)
        choices = dict.fromkeys(conflicts, "target")
        choices.update(remarks="source", allergies="source")

        response = self.merge(choices)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.target.refresh_from_db()
        self.assertEqual(self.target.remarks, "Needs a ground-floor room")
        # Only the duplicate had a religion, so it fills the gap.
        self.assertEqual(self.target.religion, "Hindu")
        self.assertEqual(
            list(self.target.desk_allergies.values_list("substance", flat=True)),
            ["Sulfa"],
        )
        self.assertEqual(self.target.date_of_birth, datetime.date(1999, 4, 12))

    def test_only_the_duplicate_registered_at_the_desk(self):
        source = self.source
        registered_at = source.registered_at
        self.target = self.walk_in("Neha Rani Sharma", datetime.date(1999, 4, 12))
        self.assertIsNone(self.target.registered_at)
        preview = self.preview()
        self.assertEqual(preview.data["moves"]["Desk registration"], 1)
        conflicts = self.conflicts()
        self.assertEqual(conflicts, {"name", "date_of_birth", "address"})
        # The kept record's name and address, the duplicate's date of birth.
        response = self.merge(
            {"name": "target", "address": "target", "date_of_birth": "source"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.target.refresh_from_db()
        # The duplicate's desk registration is now the kept record's.
        self.assertEqual(self.target.registered_at, registered_at)
        self.assertEqual(self.target.registration_facility_id, self.facility.id)
        self.assertEqual(self.target.registered_by_id, self.user.id)
        self.assertEqual(self.target.remarks, "Prefers morning slots")
        self.assertEqual(self.target.communication_channels, ["WhatsApp", "SMS"])
        self.assertEqual(
            sorted(self.target.desk_allergies.values_list("substance", flat=True)),
            ["Latex", "Penicillin"],
        )
        self.assertFalse(source.desk_allergies.exists())
        self.assertEqual(self.target.date_of_birth, datetime.date(1999, 4, 21))
        # Derived from the kept record's values, not the copied desk columns.
        self.assertEqual(self.target.name, "Neha Rani Sharma")
        self.assertEqual(
            (self.target.first_name, self.target.middle_name, self.target.last_name),
            ("Neha", "Rani", "Sharma"),
        )
        self.assertEqual(self.target.address, "14 Park Street, Kolkata")
        self.assertEqual((self.target.house, self.target.city), ("", ""))
        # The kept record now opens as a desk registration.
        desk = self.client.get(
            reverse("desk-registration-detail", args=[self.target.external_id])
        )
        self.assertEqual(desk.status_code, status.HTTP_200_OK, desk.data)
        self.assertEqual(desk.data["last_name"], "Sharma")
        self.assertEqual(desk.data["created_date"], registered_at)

    def test_only_the_kept_record_registered_at_the_desk(self):
        self.source = self.walk_in("Neha Rani Sharma", datetime.date(1999, 4, 12))
        conflicts = self.conflicts()
        self.assertEqual(conflicts, {"name", "address"})
        response = self.merge({"name": "source", "address": "target"})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.target.refresh_from_db()
        self.assertEqual(self.target.name, "Neha Rani Sharma")
        self.assertEqual(
            (self.target.first_name, self.target.middle_name, self.target.last_name),
            ("Neha", "Rani", "Sharma"),
        )
        # The kept record's own desk columns stay.
        self.assertEqual(self.target.house, "C-22")
        self.assertEqual(self.target.remarks, "Prefers morning slots")
        self.assertEqual(self.target.desk_allergies.count(), 2)

    def test_quick_registration_moves_with_its_form_link(self):
        self.target = self.walk_in("Neha Sharma", datetime.date(1999, 4, 12))
        self.source = self.walk_in("Neha Sharma", datetime.date(1999, 4, 12))
        Patient.objects.filter(id=self.source.id).update(
            title="Ms",
            first_name="Neha",
            last_name="Sharma",
            quick_registered_at=datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC),
            quick_registered_by=self.user,
            quick_facility=self.facility,
            quick_consultation_type="in_person",
            quick_form_token="fake-token-for-merge-test",
            quick_form_expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.UTC),
        )
        self.source.refresh_from_db()
        self.assertEqual(self.preview().data["moves"]["Quick registration"], 1)

        response = self.merge({})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.target.refresh_from_db()
        self.assertEqual(self.target.quick_form_token, "fake-token-for-merge-test")
        self.assertEqual(self.target.quick_facility_id, self.facility.id)
        self.assertEqual(self.target.first_name, "Neha")
        retired = Patient._base_manager.get(id=self.source.id)  # noqa: SLF001
        self.assertIsNone(retired.quick_form_token)
        self.assertEqual(
            retired.quick_registered_at, self.target.quick_registered_at
        )
        self.client.force_authenticate(user=None)
        form = self.client.get(
            reverse("quick-registration-form", args=["fake-token-for-merge-test"])
        )
        self.assertEqual(form.status_code, status.HTTP_200_OK, form.data)
        self.assertEqual(form.data["first_name"], "Neha")

    def test_merge_needs_a_choice_for_each_conflict(self):
        response = self.merge({})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        locs = [x["loc"] for x in response.data["errors"]]
        self.assertIn(["choices", "date_of_birth"], locs)
        self.assertFalse(PatientMerge.objects.exists())

    def test_merge_refuses_unknown_fields(self):
        choices = {key: "target" for key in self.conflicts()}
        response = self.merge({**choices, "shoe_size": "source"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            ["choices", "shoe_size"], [x["loc"] for x in response.data["errors"]]
        )

    def test_restricted_records_are_blocked(self):
        RestrictedPatient.objects.create(patient=self.source, alias="R-TEST01")
        response = self.preview()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn(
            "Restricted records are merged by a privileged process",
            response.data["blockers"],
        )
        self.assertEqual(response.data["fields"], [])
        merged = self.merge({"date_of_birth": "target"})
        self.assertEqual(merged.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(merged.data["errors"][0]["type"], "merge_blocked")
        self.assertFalse(PatientMerge.objects.exists())

    # Permission

    def test_desk_staff_cant_merge(self):
        desk = self.create_user()
        self.attach_role_facility_organization_user(
            self.organization,
            desk,
            self.create_role_with_permissions(
                ["can_list_patients", "can_create_patient", "can_write_patient"]
            ),
        )
        self.client.force_authenticate(user=desk)
        self.assertEqual(self.preview().status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            self.merge({"date_of_birth": "target"}).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertFalse(PatientMerge.objects.exists())

    def test_facility_supervisor_can_preview(self):
        supervisor = self.create_user()
        self.attach_role_facility_organization_user(
            self.organization,
            supervisor,
            self.create_role_with_permissions(
                [
                    "can_list_patients",
                    "can_create_patient",
                    "can_write_patient",
                    "can_destroy_payment_reconciliation",
                ]
            ),
        )
        self.client.force_authenticate(user=supervisor)
        response = self.preview()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
