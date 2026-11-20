"""Real patient tokens exercise the adapter and shared seat/approval services."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from threading import Barrier
from uuid import uuid4

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from model_bakery import baker
from rest_framework.test import APIClient

from care.emr.models import AvailabilityException, Patient, TokenBooking
from care.users.models import User
from care.utils.tests.base import CareAPITestBase
from hmis.appointment_approvals import review_booking
from hmis.mobile_auth import start_session
from hmis.models import MobileAppointmentSubmission
from hmis.tests.test_appointment_approvals import make_slot

BASE = "/api/hmis/mobile/api/v1/appointments"


def setup_slot(user, facility):
    facility.is_public = True
    facility.save(update_fields=["is_public"])
    slot = make_slot(user, facility)
    day = timezone.localdate() + timedelta(days=1)
    start = timezone.make_aware(datetime.combine(day, time(10)))
    slot.start_datetime = start
    slot.end_datetime = start + timedelta(minutes=15)
    slot.save()
    availability = slot.availability
    availability.slot_type = "appointment"
    availability.slot_size_in_minutes = 15
    availability.availability = [
        {"day_of_week": day.weekday(), "start_time": "10:00:00", "end_time": "10:15:00"}
    ]
    availability.save()
    schedule = availability.schedule
    schedule.valid_from = start - timedelta(days=1)
    schedule.valid_to = start + timedelta(days=7)
    schedule.save()
    return slot


def patient_client(patient):
    _, tokens = start_session(patient, patient.phone_number)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}")
    return client


class MobileAppointmentTests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        self.doctor = self.create_super_user(
            first_name="Testdoctor", last_name="Example"
        )
        self.facility = self.create_facility(user=self.doctor)
        self.slot = setup_slot(self.doctor, self.facility)
        self.patient = self.create_patient(
            name="Testpatient Mobile", phone_number="+919800001111"
        )
        self.client = patient_client(self.patient)
        self.doctors_url = f"{BASE}/facilities/{self.facility.external_id}/doctors"
        self.slots_url = f"{self.doctors_url}/{self.slot.resource.external_id}/slots"
        self.day = timezone.localtime(self.slot.start_datetime).date().isoformat()
        self.payload = {
            "request_id": str(uuid4()),
            "slot_id": str(self.slot.external_id),
            "patient_notes": "Testpatient note",
        }

    def submit(self, payload=None):
        return self.client.post(
            BASE, self.payload if payload is None else payload, format="json"
        )

    def test_catalogue_is_public_and_paginated_without_staff_contact_data(self):
        response = self.client.get(f"{BASE}/facilities")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["total"], 1)
        self.assertEqual(
            response.data["data"]["items"][0]["id"], str(self.facility.external_id)
        )
        response = self.client.get(self.doctors_url)
        doctor = response.data["data"]["items"][0]
        self.assertEqual(doctor["id"], str(self.slot.resource.external_id))
        self.assertEqual(doctor["name"], "Testdoctor Example")
        self.assertNotIn("phone_number", doctor)
        self.assertNotIn("email", doctor)
        self.assertIsNone(doctor["consultation_fee"])
        self.assertEqual(doctor["specializations"], [])
        self.assertIsNone(doctor["photo_url"])
        self.assertEqual(
            self.client.get(self.doctors_url, {"search": "absent"}).data["data"][
                "total"
            ],
            0,
        )
        self.assertEqual(
            self.client.get(self.doctors_url, {"page": 2}).data["data"]["items"], []
        )

    def test_doctor_shows_department_specializations_and_external_photo(self):
        role = self.create_role(name="Test Consultant")
        for name in ("Cardiology", "ENT"):
            dept = self.create_facility_organization(
                self.facility, name=name, org_type="dept"
            )
            self.attach_role_facility_organization_user(dept, self.doctor, role)
        retired = self.create_facility_organization(
            self.facility, name="Retired", org_type="dept", active=False
        )
        self.attach_role_facility_organization_user(retired, self.doctor, role)
        photo = "https://pacificonehealth.com/wp-content/uploads/2026/09/Aashima-1.png"
        self.doctor.profile_picture_url = photo
        self.doctor.save(update_fields=["profile_picture_url"])

        doctor = self.client.get(self.doctors_url).data["data"]["items"][0]
        self.assertEqual(doctor["specializations"], ["Cardiology", "ENT"])
        self.assertEqual(doctor["photo_url"], photo)

    def test_private_or_deleted_schedules_and_inactive_doctors_are_excluded(self):
        schedule = self.slot.availability.schedule
        for obj, field, value in [
            (schedule, "is_public", False),
            (schedule, "deleted", True),
            (self.facility, "is_public", False),
            (self.doctor, "is_active", False),
            (self.doctor, "deleted", True),
            (self.slot.resource, "deleted", True),
        ]:
            original = getattr(obj, field)
            setattr(obj, field, value)
            obj.save(update_fields=[field])
            self.assertEqual(
                self.client.get(f"{BASE}/facilities").data["data"]["total"], 0
            )
            self.assertEqual(
                self.client.get(self.slots_url, {"date": self.day}).status_code, 404
            )
            self.assertEqual(self.submit().status_code, 404)
            setattr(obj, field, original)
            obj.save(update_fields=[field])

    def test_slots_use_live_capacity_and_public_generator(self):
        response = self.client.get(self.slots_url, {"date": self.day})
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(data["date"].isoformat(), self.day)
        self.assertEqual(data["items"][0]["id"], str(self.slot.external_id))
        self.assertTrue(data["items"][0]["available"])
        self.assertEqual(self.submit().status_code, 201)
        row = self.client.get(self.slots_url, {"date": self.day}).data["data"]["items"][
            0
        ]
        self.assertFalse(row["available"])
        self.assertEqual(row["seats_remaining"], 0)

    def test_generation_materializes_slots_without_duplicates(self):
        availability = self.slot.availability
        availability.availability[0]["end_time"] = "10:30:00"
        availability.save()
        one = self.client.get(self.slots_url, {"date": self.day})
        two = self.client.get(self.slots_url, {"date": self.day})
        self.assertEqual(len(one.data["data"]["items"]), 2)
        self.assertEqual(one.data, two.data)

    def test_exception_added_after_materialization_blocks_read_and_create(self):
        baker.make(
            AvailabilityException,
            resource=self.slot.resource,
            valid_from=self.day,
            valid_to=self.day,
            start_time=time(10),
            end_time=time(10, 15),
        )
        self.assertEqual(
            self.client.get(self.slots_url, {"date": self.day}).data["data"]["items"],
            [],
        )
        self.assertEqual(self.submit().status_code, 409)
        self.assertEqual(TokenBooking.objects.count(), 0)

    def test_edited_availability_or_schedule_dates_block_stale_slot(self):
        availability = self.slot.availability
        availability.availability = []
        availability.save()
        self.assertEqual(self.submit().status_code, 409)
        self.assertEqual(
            self.client.get(self.slots_url, {"date": self.day}).data["data"]["items"],
            [],
        )

    def test_walk_in_reserve_is_respected_in_list_and_create(self):
        availability = self.slot.availability
        availability.tokens_per_slot = 2
        availability.walk_in_every = 2
        availability.save()
        self.assertEqual(self.submit().status_code, 201)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.allocated, 1)
        other = self.create_patient(phone_number="+919800001112")
        client = patient_client(other)
        response = client.get(self.slots_url, {"date": self.day})
        self.assertEqual(response.data["data"]["items"][0]["seats_remaining"], 0)
        response = client.post(
            BASE, {**self.payload, "request_id": str(uuid4())}, format="json"
        )
        self.assertEqual(response.status_code, 409, response.data)

    def test_submission_uses_token_patient_holds_seat_and_defers_charge(self):
        response = self.submit()
        self.assertEqual(response.status_code, 201, response.data)
        row = response.data["data"]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["approval_status"], "pending")
        self.assertTrue(row["appointment_number"].startswith("APT-"))
        booking = TokenBooking.objects.get(external_id=row["id"])
        self.assertEqual(booking.patient, self.patient)
        self.assertEqual(booking.booking_channel, "online")
        self.assertEqual(booking.consultation_type, "in_person")
        self.assertIsNone(booking.booked_by)
        self.assertIsNone(booking.charge_item)
        self.assertEqual(MobileAppointmentSubmission.objects.count(), 1)

    def test_retry_returns_same_booking_and_latest_status_without_second_seat(self):
        first = self.submit()
        booking_id = first.data["data"]["id"]
        review_booking(booking_id, "approved", user=self.doctor)
        second = self.submit()
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data["data"]["id"], booking_id)
        self.assertEqual(second.data["data"]["status"], "confirmed")
        self.assertEqual(TokenBooking.objects.count(), 1)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.allocated, 1)

    def test_reuse_with_different_details_is_conflict(self):
        self.submit()
        response = self.submit({**self.payload, "patient_notes": "Different"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("request_id", response.data["message"])

    def test_new_request_for_same_patient_and_slot_is_conflict(self):
        self.slot.availability.tokens_per_slot = 2
        self.slot.availability.save()
        self.submit()
        self.assertEqual(
            self.submit({**self.payload, "request_id": str(uuid4())}).status_code, 409
        )
        self.assertEqual(TokenBooking.objects.count(), 1)

    def test_rejected_request_can_be_retried_but_new_booking_requires_new_request_id(
        self,
    ):
        first = self.submit().data["data"]
        review_booking(first["id"], "rejected", "Test doctor unavailable", self.doctor)
        retry = self.submit()
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data["data"]["status"], "rejected")
        self.assertEqual(
            retry.data["data"]["rejection_reason"], "Test doctor unavailable"
        )
        self.assertEqual(
            self.submit({**self.payload, "request_id": str(uuid4())}).status_code, 201
        )

    def test_validation_envelope_unknown_fields_and_payment_or_family_cannot_be_ignored(
        self,
    ):
        for payload, field in [
            ({}, "request_id"),
            ({**self.payload, "slot_id": "123"}, "slot_id"),
            ({**self.payload, "patient_notes": "x" * 501}, "patient_notes"),
            ({**self.payload, "patient_user_id": 999}, "patient_user_id"),
            ({**self.payload, "pay_now": True}, "pay_now"),
            ({**self.payload, "status": "confirmed"}, "status"),
        ]:
            response = self.submit(payload)
            self.assertEqual(response.status_code, 422, response.data)
            self.assertEqual(response.data["status"], "error")
            self.assertIn(field, [item["field"] for item in response.data["errors"]])
        self.assertEqual(TokenBooking.objects.count(), 0)

    def test_query_validation_and_date_window(self):
        for params in (
            {},
            {"date": "bad"},
            {"date": (timezone.localdate() - timedelta(days=1)).isoformat()},
            {"date": (timezone.localdate() + timedelta(days=91)).isoformat()},
        ):
            self.assertEqual(self.client.get(self.slots_url, params).status_code, 422)
        for params in (
            {"page": 0},
            {"page_size": 101},
            {"status": "madeup"},
            {"upcoming": "madeup"},
        ):
            self.assertEqual(self.client.get(BASE, params).status_code, 422)

    def test_missing_and_staff_tokens_are_401_and_refresh_token_not_accepted(self):
        client = APIClient()
        for url in (BASE, f"{BASE}/facilities", self.doctors_url, self.slots_url):
            response = client.get(url)
            self.assertEqual(response.status_code, 401)
            self.assertIn("Bearer", response["WWW-Authenticate"])
        from rest_framework_simplejwt.tokens import AccessToken

        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(self.doctor)}"
        )
        self.assertEqual(client.get(BASE).status_code, 401)
        _, tokens = start_session(self.patient, self.patient.phone_number)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['refresh_token']}")
        self.assertEqual(client.get(BASE).status_code, 401)

    def test_same_phone_profiles_are_isolated_and_detail_cannot_be_guessed(self):
        booking_id = self.submit().data["data"]["id"]
        other = self.create_patient(phone_number=self.patient.phone_number)
        client = patient_client(other)
        self.assertEqual(client.get(BASE).data["data"]["total"], 0)
        self.assertEqual(client.get(f"{BASE}/{booking_id}").status_code, 404)
        self.assertEqual(self.client.get(f"{BASE}/{booking_id}").status_code, 200)
        self.assertEqual(self.client.get(f"{BASE}/{'-' * 36}").status_code, 404)

    def test_pending_appears_in_staff_queue_and_review_is_visible_to_mobile(self):
        booking_id = self.submit().data["data"]["id"]
        staff = APIClient()
        staff.force_authenticate(self.doctor)
        queue = f"/api/hmis/facility/{self.facility.external_id}/appointment-requests/"
        self.assertEqual(staff.get(queue).data["results"][0]["id"], booking_id)
        self.assertEqual(
            staff.post(f"{queue}{booking_id}/approve/", {}, format="json").status_code,
            200,
        )
        self.assertEqual(
            self.client.get(f"{BASE}/{booking_id}").data["data"]["status"], "confirmed"
        )
        self.assertEqual(
            self.client.get(BASE, {"status": "confirmed"}).data["data"]["total"], 1
        )
        self.assertEqual(
            self.client.get(BASE, {"status": "pending"}).data["data"]["total"], 0
        )

    def test_expired_pending_read_releases_seat_without_waiting_for_beat(self):
        booking_id = self.submit().data["data"]["id"]
        self.slot.start_datetime = timezone.now() - timedelta(seconds=1)
        self.slot.save(update_fields=["start_datetime"])
        row = self.client.get(f"{BASE}/{booking_id}").data["data"]
        self.assertEqual(row["status"], "expired")
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.allocated, 0)
        self.assertEqual(
            self.client.get(BASE, {"status": "expired"}).data["data"]["total"], 1
        )
        self.assertEqual(
            self.client.get(BASE, {"upcoming": True}).data["data"]["total"], 0
        )

    def test_staff_bookings_visible_but_internal_notes_and_other_patient_not_exposed(
        self,
    ):
        booking = baker.make(
            TokenBooking,
            patient=self.patient,
            token_slot=self.slot,
            status="booked",
            note="Internal desk note",
            approval_status="",
        )
        baker.make(
            TokenBooking,
            patient=self.create_patient(),
            token_slot=self.slot,
            status="booked",
        )
        response = self.client.get(BASE)
        self.assertEqual(response.data["data"]["total"], 1)
        self.assertEqual(
            response.data["data"]["items"][0]["id"], str(booking.external_id)
        )
        self.assertIsNone(response.data["data"]["items"][0]["patient_notes"])

    def test_cancelled_after_approval_is_not_shown_as_confirmed_and_fulfilled_is_completed(
        self,
    ):
        booking_id = self.submit().data["data"]["id"]
        review_booking(booking_id, "approved", user=self.doctor)
        for status, expected in [
            ("cancelled", "cancelled"),
            ("fulfilled", "completed"),
            ("rescheduled", "rescheduled"),
            ("noshow", "noshow"),
        ]:
            TokenBooking.objects.filter(external_id=booking_id).update(status=status)
            self.assertEqual(
                self.client.get(f"{BASE}/{booking_id}").data["data"]["status"], expected
            )
            self.assertEqual(
                self.client.get(BASE, {"status": expected}).data["data"]["total"], 1
            )

    @override_settings(MAX_APPOINTMENTS_PER_PATIENT=0)
    def test_quota_failure_rolls_back_receipt_and_capacity(self):
        self.assertEqual(self.submit().status_code, 409)
        self.assertEqual(MobileAppointmentSubmission.objects.count(), 0)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.allocated, 0)

    def test_patient_notes_are_the_submission_snapshot_after_staff_edit(self):
        booking_id = self.submit().data["data"]["id"]
        TokenBooking.objects.filter(external_id=booking_id).update(
            note="Internal staff update"
        )
        row = self.client.get(f"{BASE}/{booking_id}").json()["data"]
        self.assertEqual(row["patient_notes"], "Testpatient note")
        self.assertEqual(row["request_id"], self.payload["request_id"])
        self.assertIsInstance(row["start_datetime"], str)
        self.assertEqual(row["timezone"], "Asia/Kolkata")

    def test_retry_after_deadline_returns_expired_original_request(self):
        booking_id = self.submit().data["data"]["id"]
        self.slot.start_datetime = timezone.now() - timedelta(seconds=1)
        self.slot.save(update_fields=["start_datetime"])
        retry = self.submit()
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data["data"]["id"], booking_id)
        self.assertEqual(retry.data["data"]["status"], "expired")

    def test_trailing_slash_and_unsupported_methods(self):
        self.assertEqual(self.client.get(BASE + "/").status_code, 200)
        booking_id = self.submit().data["data"]["id"]
        self.assertEqual(self.client.delete(f"{BASE}/{booking_id}").status_code, 405)
        self.assertEqual(
            self.client.patch(
                f"{BASE}/{booking_id}", {"status": "confirmed"}, format="json"
            ).status_code,
            405,
        )

    # Longer appointments: back-to-back slots in one session.

    def hour_of_slots(self, tokens_per_slot=1):
        """Opens 10:00-11:00 as four 15-minute slots and returns them as listed."""
        availability = self.slot.availability
        availability.availability[0]["end_time"] = "11:00:00"
        availability.tokens_per_slot = tokens_per_slot
        availability.save()
        return self.client.get(self.slots_url, {"date": self.day}).data["data"]

    def ids(self, items):
        return [item["id"] for item in items]

    def test_longer_appointment_holds_back_to_back_slots(self):
        data = self.hour_of_slots()
        items = data["items"]
        self.assertEqual(data["max_slots_per_booking"], 4)
        self.assertEqual(len(items), 4)
        self.assertEqual(
            {item["session_id"] for item in items},
            {str(self.slot.availability.external_id)},
        )
        response = self.submit({**self.payload, "extra_slot_ids": self.ids(items[1:3])})
        self.assertEqual(response.status_code, 201, response.data)
        row = response.data["data"]
        self.assertEqual(row["slot_id"], items[0]["id"])
        self.assertEqual(self.ids(row["extra_slots"]), self.ids(items[1:3]))
        self.assertEqual(row["end_datetime"].isoformat(), items[2]["end_datetime"])
        self.assertEqual(
            self.client.get(f"{BASE}/{row['id']}").data["data"]["extra_slots"],
            row["extra_slots"],
        )
        listed = self.client.get(self.slots_url, {"date": self.day}).data["data"]
        self.assertEqual(
            [item["available"] for item in listed["items"]], [False, False, False, True]
        )
        review_booking(row["id"], "rejected", "Test doctor unavailable", self.doctor)
        listed = self.client.get(self.slots_url, {"date": self.day}).data["data"]
        self.assertTrue(all(item["available"] for item in listed["items"]))

    def test_extra_slots_must_follow_on_have_seats_and_be_offered(self):
        items = self.hour_of_slots()["items"]

        def submit(extra):
            return self.submit(
                {**self.payload, "request_id": str(uuid4()), "extra_slot_ids": extra}
            )

        self.assertEqual(submit(self.ids(items[2:3])).status_code, 409)  # a gap
        self.assertEqual(submit([items[0]["id"]]).status_code, 422)  # repeats slot_id
        self.assertEqual(submit([str(uuid4())]).status_code, 409)  # unknown
        too_long = submit([*self.ids(items[1:]), str(uuid4())])
        self.assertEqual(too_long.status_code, 422)  # more than an hour

        # A slot closed by an exception after it was listed.
        exception = baker.make(
            AvailabilityException,
            resource=self.slot.resource,
            valid_from=self.day,
            valid_to=self.day,
            start_time=time(10, 30),
            end_time=time(10, 45),
        )
        self.assertEqual(submit(self.ids(items[1:3])).status_code, 409)
        exception.delete()

        other = self.create_patient(phone_number="+919800001115")
        response = patient_client(other).post(
            BASE,
            {"request_id": str(uuid4()), "slot_id": items[1]["id"]},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        full = submit(self.ids(items[1:2]))
        self.assertEqual(full.status_code, 409)
        self.assertIn("full", str(full.data))
        # Nothing but the other patient's booking holds a seat.
        self.assertEqual(TokenBooking.objects.count(), 1)
        listed = self.client.get(self.slots_url, {"date": self.day}).data["data"]
        self.assertEqual(
            [item["available"] for item in listed["items"]], [True, False, True, True]
        )

    def test_retry_must_repeat_the_same_extra_slots(self):
        items = self.hour_of_slots()["items"]
        payload = {**self.payload, "extra_slot_ids": self.ids(items[1:2])}
        first = self.submit(payload)
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(self.submit(payload).status_code, 200)
        self.assertEqual(self.submit(self.payload).status_code, 409)
        self.assertEqual(
            self.submit(
                {**payload, "extra_slot_ids": self.ids(items[1:3])}
            ).status_code,
            409,
        )
        self.assertEqual(TokenBooking.objects.count(), 1)

    def test_upcoming_lasts_until_the_last_slot_ends(self):
        items = self.hour_of_slots()["items"]
        row = self.submit(
            {**self.payload, "extra_slot_ids": self.ids(items[1:2])}
        ).data["data"]
        review_booking(row["id"], "approved", user=self.doctor)
        booking = TokenBooking.objects.get(external_id=row["id"])
        now = timezone.now()
        first, second = booking.token_slot, booking.extra_slots.get()
        first.start_datetime, first.end_datetime = now - timedelta(minutes=20), now
        first.save()
        second.start_datetime = now
        second.end_datetime = now + timedelta(minutes=10)
        second.save()
        self.assertEqual(
            self.client.get(BASE, {"upcoming": True}).data["data"]["total"], 1
        )
        self.assertEqual(
            self.client.get(BASE, {"upcoming": False}).data["data"]["total"], 0
        )

    def test_walk_in_reserve_counts_every_held_slot(self):
        availability = self.slot.availability
        availability.walk_in_every = 2
        availability.save()
        # 8 seats, 4 held for walk-ins.
        items = self.hour_of_slots(tokens_per_slot=2)["items"]
        response = self.submit({**self.payload, "extra_slot_ids": self.ids(items[1:3])})
        self.assertEqual(response.status_code, 201, response.data)
        listed = self.client.get(self.slots_url, {"date": self.day}).data["data"]
        self.assertEqual(
            [item["seats_remaining"] for item in listed["items"]], [1, 1, 1, 1]
        )
        other = self.create_patient(phone_number="+919800001116")
        response = patient_client(other).post(
            BASE,
            {
                "request_id": str(uuid4()),
                "slot_id": items[2]["id"],
                "extra_slot_ids": self.ids(items[3:4]),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertIn("walk-in", str(response.data))


class MobileAppointmentConcurrencyTests(TransactionTestCase):
    def setUp(self):
        from care.facility.models import Facility

        self.doctor = baker.make(
            User, first_name="Testdoctor", is_superuser=True, is_active=True
        )
        facility = baker.make(Facility, created_by=self.doctor)
        self.slot = setup_slot(self.doctor, facility)
        self.patient = baker.make(
            Patient, name="Testpatient Concurrent Mobile", phone_number="+919800001113"
        )

    def run_pair(self, clients, payloads):
        barrier = Barrier(2)

        def run(args):
            client, payload = args
            try:
                barrier.wait(timeout=10)
                response = client.post(BASE, payload, format="json")
                return response.status_code, response.data
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(run, zip(clients, payloads, strict=True)))

    def test_same_request_concurrent_retry_has_one_receipt_and_one_booking(self):
        clients = [patient_client(self.patient), patient_client(self.patient)]
        payload = {"request_id": str(uuid4()), "slot_id": str(self.slot.external_id)}
        results = self.run_pair(clients, [payload, payload])
        self.assertEqual(sorted(code for code, _ in results), [200, 201], results)
        self.assertEqual(results[0][1]["data"]["id"], results[1][1]["data"]["id"])
        self.assertEqual(TokenBooking.objects.count(), 1)
        self.assertEqual(MobileAppointmentSubmission.objects.count(), 1)

    def test_two_patients_last_seat_has_one_winner(self):
        other = baker.make(
            Patient, name="Testother Concurrent Mobile", phone_number="+919800001114"
        )
        clients = [patient_client(self.patient), patient_client(other)]
        payloads = [
            {"request_id": str(uuid4()), "slot_id": str(self.slot.external_id)}
            for _ in clients
        ]
        results = self.run_pair(clients, payloads)
        self.assertEqual(sorted(code for code, _ in results), [201, 409], results)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.allocated, 1)
        self.assertEqual(TokenBooking.objects.count(), 1)
