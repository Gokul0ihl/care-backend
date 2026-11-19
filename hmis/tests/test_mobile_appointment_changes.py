"""Patient self-service cancel and reschedule in the mobile app."""

from datetime import timedelta
from uuid import uuid4

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from care.emr.models import TokenBooking, TokenSlot
from care.utils.tests.base import CareAPITestBase
from hmis.appointment_approvals import review_booking
from hmis.models import MobileAppointmentChange
from hmis.tests.test_mobile_appointments import BASE, patient_client, setup_slot


class MobileAppointmentChangeTests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        self.doctor = self.create_super_user(
            first_name="Testdoctor", last_name="Example"
        )
        self.facility = self.create_facility(user=self.doctor)
        self.slot = setup_slot(self.doctor, self.facility)
        self.patient = self.create_patient(
            name="Testpatient Mobile", phone_number="+919800002222"
        )
        self.client = patient_client(self.patient)
        doctor = self.slot.resource.external_id
        self.slots_url = (
            f"{BASE}/facilities/{self.facility.external_id}/doctors/{doctor}/slots"
        )
        self.day = timezone.localtime(self.slot.start_datetime).date().isoformat()
        # 10:00-11:00 as four 15-minute slots, one seat each.
        availability = self.slot.availability
        availability.availability[0]["end_time"] = "11:00:00"
        availability.save()
        self.items = self.client.get(self.slots_url, {"date": self.day}).data["data"][
            "items"
        ]
        self.slot_ids = [item["id"] for item in self.items]

    def book(self, slot=0, extra=(), approve=False):
        response = self.client.post(
            BASE,
            {
                "request_id": str(uuid4()),
                "slot_id": self.slot_ids[slot],
                "extra_slot_ids": [self.slot_ids[i] for i in extra],
                "patient_notes": "Testpatient note",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        booking_id = response.data["data"]["id"]
        if approve:
            review_booking(booking_id, "approved", user=self.doctor)
        return booking_id

    def cancel(self, booking_id, **body):
        return self.client.post(
            f"{BASE}/{booking_id}/cancel", {"reason": "unwell", **body}, format="json"
        )

    def move(self, booking_id, slot, **body):
        return self.client.post(
            f"{BASE}/{booking_id}/reschedule",
            {"slot_id": self.slot_ids[slot], "reason": "schedule_clash", **body},
            format="json",
        )

    def allocated(self):
        return [
            TokenSlot.objects.get(external_id=slot_id).allocated
            for slot_id in self.slot_ids
        ]

    def staff_queue(self, state="pending"):
        staff = APIClient()
        staff.force_authenticate(self.doctor)
        url = f"/api/hmis/facility/{self.facility.external_id}/appointment-requests/"
        return [row["id"] for row in staff.get(url, {"status": state}).data["results"]]

    # Cancel

    def test_cancel_pending_request_withdraws_it_and_frees_the_seat(self):
        booking_id = self.book()
        row = self.client.get(f"{BASE}/{booking_id}").data["data"]
        self.assertTrue(row["can_cancel"])
        self.assertTrue(row["can_reschedule"])
        self.assertEqual(row["reschedules_remaining"], 2)
        response = self.cancel(booking_id, note="Feeling better")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["message"], "Appointment cancelled")
        row = response.data["data"]
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["approval_status"], "withdrawn")
        self.assertIsNone(row["rejection_reason"])
        self.assertFalse(row["can_cancel"])
        self.assertFalse(row["can_reschedule"])
        self.assertEqual(row["patient_change"]["action"], "cancel")
        self.assertEqual(row["patient_change"]["reason"], "unwell")
        self.assertEqual(row["patient_change"]["note"], "Feeling better")
        self.assertEqual(self.allocated(), [0, 0, 0, 0])
        self.assertEqual(self.staff_queue(), [])
        self.assertEqual(
            self.client.get(BASE, {"status": "cancelled"}).data["data"]["total"], 1
        )
        # A retry changes nothing.
        retry = self.cancel(booking_id)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data["message"], "Appointment already cancelled")
        self.assertEqual(MobileAppointmentChange.objects.count(), 1)

    def test_cancel_confirmed_booking_frees_every_held_slot(self):
        booking_id = self.book(extra=[1], approve=True)
        self.assertEqual(self.allocated(), [1, 1, 0, 0])
        row = self.cancel(booking_id).data["data"]
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["approval_status"], "approved")
        self.assertEqual(self.allocated(), [0, 0, 0, 0])
        booking = TokenBooking.objects.get(external_id=booking_id)
        self.assertIsNone(booking.updated_by)
        self.assertEqual(booking.note, "Testpatient note")

    def test_cancel_desk_booking(self):
        staff = APIClient()
        staff.force_authenticate(self.doctor)
        response = staff.post(
            f"/api/v1/facility/{self.facility.external_id}/slots/"
            f"{self.slot_ids[0]}/create_appointment/",
            {
                "patient": str(self.patient.external_id),
                "note": "Desk booking",
                "booking_channel": "phone",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        row = self.cancel(response.data["id"]).data["data"]
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNone(row["approval_status"])

    @override_settings(MOBILE_CANCEL_CUTOFF=timedelta(days=3))
    def test_cancel_closes_at_the_cutoff(self):
        booking_id = self.book(approve=True)
        row = self.client.get(f"{BASE}/{booking_id}").data["data"]
        self.assertFalse(row["can_cancel"])
        response = self.cancel(booking_id)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertIn("contact the hospital", response.data["message"])
        self.assertEqual(self.allocated()[0], 1)

    def test_cancel_after_arrival_is_for_the_desk(self):
        booking_id = self.book(approve=True)
        TokenBooking.objects.filter(external_id=booking_id).update(status="arrived")
        self.assertFalse(
            self.client.get(f"{BASE}/{booking_id}").data["data"]["can_cancel"]
        )
        self.assertEqual(self.cancel(booking_id).status_code, 409)

    def test_cancel_validation_and_isolation(self):
        booking_id = self.book()
        self.assertEqual(self.cancel(booking_id, reason="bored").status_code, 422)
        self.assertEqual(self.cancel(booking_id, refund=True).status_code, 422)
        self.assertEqual(
            self.client.post(
                f"{BASE}/{booking_id}/cancel", {}, format="json"
            ).status_code,
            422,
        )
        other = patient_client(
            self.create_patient(phone_number=self.patient.phone_number)
        )
        self.assertEqual(
            other.post(
                f"{BASE}/{booking_id}/cancel", {"reason": "other"}, format="json"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(f"{BASE}/{booking_id}/cancel").status_code, 405
        )
        self.assertEqual(
            TokenBooking.objects.get(external_id=booking_id).status, "pending"
        )

    # Reschedule

    def test_reschedule_confirmed_booking_stays_confirmed_with_same_number(self):
        booking_id = self.book(approve=True)
        old = TokenBooking.objects.get(external_id=booking_id)
        response = self.move(booking_id, 2, note="Meeting at work")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["message"], "Appointment rescheduled")
        row = response.data["data"]
        self.assertNotEqual(row["id"], booking_id)
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual(row["slot_id"], self.slot_ids[2])
        self.assertEqual(row["appointment_number"], old.appointment_number)
        self.assertEqual(row["booking_channel"], "online")
        # The app request's notes and ID follow the appointment.
        self.assertEqual(row["patient_notes"], "Testpatient note")
        self.assertIsNotNone(row["request_id"])
        self.assertEqual(row["reschedules_remaining"], 1)
        self.assertIsNone(row["patient_change"])
        self.assertEqual(self.allocated(), [0, 0, 1, 0])
        old_row = self.client.get(f"{BASE}/{booking_id}").data["data"]
        self.assertEqual(old_row["status"], "rescheduled")
        self.assertEqual(old_row["rescheduled_to"], row["id"])
        self.assertEqual(old_row["patient_change"]["action"], "reschedule")
        self.assertEqual(old_row["patient_change"]["reason"], "schedule_clash")
        self.assertFalse(old_row["can_reschedule"])
        # A retry returns the new booking without moving again.
        retry = self.move(booking_id, 2)
        self.assertEqual(retry.status_code, 200, retry.data)
        self.assertEqual(retry.data["data"]["id"], row["id"])
        self.assertEqual(self.allocated(), [0, 0, 1, 0])
        # Another time, or cancelling, goes through the new booking.
        self.assertEqual(self.move(booking_id, 3).status_code, 409)
        self.assertEqual(self.cancel(booking_id).status_code, 409)
        self.assertEqual(
            self.client.get(BASE, {"upcoming": True}).data["data"]["total"], 1
        )

    def test_reschedule_pending_request_stays_pending_for_staff(self):
        booking_id = self.book()
        row = self.move(booking_id, 1).data["data"]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["approval_status"], "pending")
        self.assertEqual(self.staff_queue(), [row["id"]])
        old = TokenBooking.objects.get(external_id=booking_id)
        self.assertEqual(old.status, "rescheduled")
        self.assertEqual(old.approval_status, "withdrawn")
        self.assertEqual(self.allocated(), [0, 1, 0, 0])
        review_booking(row["id"], "approved", user=self.doctor)
        self.assertEqual(
            self.client.get(f"{BASE}/{row['id']}").data["data"]["status"], "confirmed"
        )

    def test_reschedule_limit_counts_desk_moves_too(self):
        first = self.book(approve=True)
        second = self.move(first, 1).data["data"]
        self.assertEqual(second["reschedules_remaining"], 1)
        third = self.move(second["id"], 2).data["data"]
        self.assertEqual(third["reschedules_remaining"], 0)
        self.assertFalse(third["can_reschedule"])
        self.assertTrue(third["can_cancel"])
        response = self.move(third["id"], 3)
        self.assertEqual(response.status_code, 409)
        self.assertIn("most times", response.data["message"])
        self.assertEqual(self.allocated(), [0, 0, 1, 0])
        # Cancelling is still allowed.
        self.assertEqual(self.cancel(third["id"]).status_code, 200)

    @override_settings(MOBILE_RESCHEDULE_CUTOFF=timedelta(days=3))
    def test_reschedule_closes_at_the_cutoff(self):
        booking_id = self.book(approve=True)
        row = self.client.get(f"{BASE}/{booking_id}").data["data"]
        self.assertFalse(row["can_reschedule"])
        self.assertTrue(row["can_cancel"])
        self.assertEqual(row["reschedules_remaining"], 2)
        response = self.move(booking_id, 2)
        self.assertEqual(response.status_code, 409)
        self.assertIn("contact the hospital", response.data["message"])
        self.assertEqual(self.allocated(), [1, 0, 0, 0])

    def test_reschedule_to_a_full_or_invalid_slot_changes_nothing(self):
        booking_id = self.book(approve=True)
        other = self.create_patient(
            name="Testother Mobile", phone_number="+919800002223"
        )
        patient_client(other).post(
            BASE,
            {"request_id": str(uuid4()), "slot_id": self.slot_ids[2]},
            format="json",
        )
        self.assertEqual(self.allocated(), [1, 0, 1, 0])
        full = self.move(booking_id, 2)
        self.assertEqual(full.status_code, 409, full.data)
        same = self.move(booking_id, 0)
        self.assertEqual(same.status_code, 422)
        self.assertEqual(same.data["errors"][0]["field"], "slot_id")
        unknown = self.client.post(
            f"{BASE}/{booking_id}/reschedule",
            {"slot_id": str(uuid4()), "reason": "other"},
            format="json",
        )
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(self.allocated(), [1, 0, 1, 0])
        booking = TokenBooking.objects.get(external_id=booking_id)
        self.assertEqual(booking.status, "booked")
        self.assertFalse(MobileAppointmentChange.objects.exists())

    def test_reschedule_stays_with_the_same_doctor(self):
        booking_id = self.book(approve=True)
        other_doctor = self.create_super_user(first_name="Testother")
        other_slot = setup_slot(other_doctor, self.facility)
        response = self.client.post(
            f"{BASE}/{booking_id}/reschedule",
            {"slot_id": str(other_slot.external_id), "reason": "wrong_doctor"},
            format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_longer_appointment_keeps_its_length(self):
        booking_id = self.book(extra=[1], approve=True)
        row = self.move(booking_id, 2).data["data"]
        self.assertEqual(row["slot_id"], self.slot_ids[2])
        self.assertEqual([s["id"] for s in row["extra_slots"]], [self.slot_ids[3]])
        self.assertEqual(self.allocated(), [0, 0, 1, 1])
        # 10:45 has no slot after it in the session.
        response = self.move(row["id"], 3)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(self.allocated(), [0, 0, 1, 1])

    def test_reschedule_after_arrival_or_expiry_is_refused(self):
        booking_id = self.book()
        self.slot.start_datetime = timezone.now() - timedelta(seconds=1)
        self.slot.save(update_fields=["start_datetime"])
        response = self.move(booking_id, 2)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            TokenBooking.objects.get(external_id=booking_id).approval_status,
            "expired",
        )
