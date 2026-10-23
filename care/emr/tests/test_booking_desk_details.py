"""The desk details on TokenBooking (slot/appointment.py) and the booking
rules around them (viewsets/scheduling/desk_details.py)."""

import datetime

from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status

from care.emr.models import (
    Availability,
    SchedulableResource,
    Schedule,
    TokenBooking,
    TokenSlot,
)
from care.utils.tests.base import CareAPITestBase
from config.patient_otp_authentication import PatientOtpObject


def local(day, hour, minute=0):
    return timezone.make_aware(
        datetime.datetime.combine(day, datetime.time(hour, minute))
    )


class BookingDeskDetailsTests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        self.user = self.create_super_user()
        self.facility = self.create_facility(user=self.user)
        self.patient = self.create_patient(phone_number="+919800000101")
        self.client.force_authenticate(user=self.user)
        self.today = timezone.localdate()
        self.tomorrow = self.today + datetime.timedelta(days=1)
        self.resource = SchedulableResource.objects.create(
            facility=self.facility, resource_type="practitioner", user=self.user
        )
        self.schedule = Schedule.objects.create(
            resource=self.resource,
            name="OPD",
            valid_from=timezone.now() - datetime.timedelta(days=30),
            valid_to=timezone.now() + datetime.timedelta(days=30),
            is_public=True,
        )
        # Tomorrow 09:00-10:00 in 15-minute slots of one seat: 4 seats.
        self.availability = Availability.objects.create(
            schedule=self.schedule,
            name="Morning",
            slot_type="appointment",
            slot_size_in_minutes=15,
            tokens_per_slot=1,
            availability=[
                {
                    "day_of_week": self.tomorrow.weekday(),
                    "start_time": "09:00:00",
                    "end_time": "10:00:00",
                }
            ],
        )
        self.slots = [
            self.create_slot(local(self.tomorrow, 9, minute))
            for minute in (0, 15, 30, 45)
        ]

    def create_slot(self, start):
        return TokenSlot.objects.create(
            resource=self.resource,
            availability=self.availability,
            start_datetime=start,
            end_datetime=start + datetime.timedelta(minutes=15),
        )

    def book(self, slot=None, patient=None, **kwargs):
        url = reverse(
            "slot-create-appointment",
            kwargs={
                "facility_external_id": self.facility.external_id,
                "external_id": (slot or self.slots[0]).external_id,
            },
        )
        body = {"patient": str((patient or self.patient).external_id), **kwargs}
        return self.client.post(url, body, format="json")

    def booking_url(self, booking_id, action=None):
        name = f"appointments-{action}" if action else "appointments-detail"
        return reverse(
            name,
            kwargs={
                "facility_external_id": self.facility.external_id,
                "external_id": booking_id,
            },
        )

    def list_url(self):
        return reverse(
            "appointments-list",
            kwargs={"facility_external_id": self.facility.external_id},
        )

    def test_booking_records_the_desk_details(self):
        response = self.book(note="First visit")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        data = response.json()
        year = timezone.now().year % 100
        self.assertEqual(data["appointment_number"], f"APT-{year:02d}-000001")
        self.assertEqual(data["booking_channel"], "front_desk")
        self.assertEqual(data["consultation_type"], "in_person")
        self.assertEqual(data["encounter_class"], "amb")
        self.assertEqual(data["visit_purpose"], "new")
        self.assertIsNone(data["reminder_due_at"])
        self.assertEqual(
            data["token_slot"]["availability"]["id"],
            str(self.availability.external_id),
        )
        booking = TokenBooking.objects.get(external_id=data["id"])
        self.assertEqual(booking.note, "First visit")

        detail = self.client.get(self.booking_url(booking.external_id)).json()
        self.assertEqual(detail["appointment_number"], data["appointment_number"])

    def test_appointment_numbers_are_consecutive(self):
        other = self.create_patient(phone_number="+919800000102")
        first = self.book().json()["appointment_number"]
        second = self.book(slot=self.slots[1], patient=other).json()[
            "appointment_number"
        ]
        self.assertEqual(int(second[-6:]), int(first[-6:]) + 1)

    def test_guidance_is_kept_and_red_flags_go_to_emergency(self):
        response = self.book(
            guidance_area="Stomach / digestion",
            guidance_since="4-7 days",
            chief_complaint="  Acidity after meals  ",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        booking = TokenBooking.objects.get()
        self.assertEqual(booking.guidance_area, "Stomach / digestion")
        self.assertEqual(booking.guidance_since, "4-7 days")
        self.assertEqual(booking.chief_complaint, "Acidity after meals")

        other = self.create_patient(phone_number="+919800000102")
        response = self.book(
            slot=self.slots[1], patient=other, red_flags=["Chest pain or pressure"]
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("emergency desk", str(response.data))
        response = self.book(slot=self.slots[1], patient=other, guidance_area="Elbow")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(TokenBooking.objects.count(), 1)

    def test_visit_purpose_defaults_to_follow_up_inside_revisit_window(self):
        self.schedule.revisit_allowed_days = 7
        self.schedule.save()
        earlier = self.create_slot(local(self.today - datetime.timedelta(days=3), 9))
        TokenBooking.objects.create(
            token_slot=earlier, patient=self.patient, status="fulfilled"
        )
        self.assertEqual(self.book().json()["visit_purpose"], "follow_up")

        other = self.create_patient(phone_number="+919800000102")
        response = self.book(slot=self.slots[1], patient=other)
        self.assertEqual(response.json()["visit_purpose"], "new")
        response = self.book(
            slot=self.slots[2], patient=other, visit_purpose="report_review"
        )
        self.assertEqual(response.json()["visit_purpose"], "report_review")

    def test_phone_booking_gets_a_reminder_and_cancel_clears_it(self):
        data = self.book(booking_channel="phone").json()
        booking = TokenBooking.objects.get(external_id=data["id"])
        self.assertEqual(
            booking.reminder_due_at,
            local(self.tomorrow, 9) - datetime.timedelta(hours=2),
        )
        response = self.client.post(
            self.booking_url(booking.external_id, "cancel"),
            {"reason": "cancelled"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        booking.refresh_from_db()
        self.assertIsNone(booking.reminder_due_at)

    def test_walk_in_only_for_today(self):
        response = self.book(booking_channel="walk_in")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("only be made for today", str(response.data))

        # Midday, so "30 minutes from now" is still today whenever the suite runs.
        noon = timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)
        with freeze_time(noon):
            today_slot = self.create_slot(noon + datetime.timedelta(minutes=30))
            response = self.book(slot=today_slot, booking_channel="walk_in")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_walk_in_reserve_holds_seats_back(self):
        url = reverse(
            "schedule-availability-set-walk-in-reserve",
            kwargs={
                "facility_external_id": self.facility.external_id,
                "schedule_external_id": self.schedule.external_id,
                "external_id": self.availability.external_id,
            },
        )
        response = self.client.post(url, {"walk_in_every": 4}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.json()["walk_in_every"], 4)
        self.assertEqual(
            self.client.post(url, {"walk_in_every": 1}, format="json").status_code,
            status.HTTP_400_BAD_REQUEST,
        )

        patients = [
            self.create_patient(phone_number=f"+91980000020{i}") for i in range(4)
        ]
        for slot, patient in zip(self.slots[:3], patients, strict=False):
            response = self.book(slot=slot, patient=patient)
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        # 4 seats, 1 held for walk-ins: the 4th scheduled booking is refused.
        response = self.book(slot=self.slots[3], patient=patients[3])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("held for walk-ins", str(response.data))

        response = self.client.post(url, {"walk_in_every": None}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.availability.refresh_from_db()
        self.assertIsNone(self.availability.walk_in_every)
        response = self.book(slot=self.slots[3], patient=patients[3])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_reschedule_moves_the_number_and_details(self):
        data = self.book(
            booking_channel="phone", consultation_type="video", visit_purpose="new"
        ).json()
        response = self.client.post(
            self.booking_url(data["id"], "reschedule"),
            {"new_slot": str(self.slots[1].external_id), "new_booking_note": ""},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        moved = response.json()
        self.assertNotEqual(moved["id"], data["id"])
        self.assertEqual(moved["appointment_number"], data["appointment_number"])
        self.assertEqual(moved["consultation_type"], "video")
        self.assertEqual(moved["booking_channel"], "phone")
        old = TokenBooking.objects.get(external_id=data["id"])
        self.assertEqual(old.status, "rescheduled")
        self.assertIsNone(old.reminder_due_at)
        new = TokenBooking.objects.get(external_id=moved["id"])
        self.assertEqual(
            new.reminder_due_at,
            local(self.tomorrow, 9, 15) - datetime.timedelta(hours=2),
        )

        # Moving it again still works: only the live booking holds the number.
        response = self.client.post(
            self.booking_url(moved["id"], "reschedule"),
            {"new_slot": str(self.slots[2].external_id), "new_booking_note": ""},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            response.json()["appointment_number"], data["appointment_number"]
        )

    def test_cancelled_booking_cannot_be_rescheduled(self):
        data = self.book().json()
        self.client.post(
            self.booking_url(data["id"], "cancel"),
            {"reason": "cancelled"},
            format="json",
        )
        response = self.client.post(
            self.booking_url(data["id"], "reschedule"),
            {"new_slot": str(self.slots[1].external_id), "new_booking_note": ""},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_facility_wide_list_and_filters(self):
        other = self.create_patient(phone_number="+919800000102")
        first = self.book(booking_channel="phone").json()
        self.book(slot=self.slots[1], patient=other)

        response = self.client.get(
            self.list_url(),
            {"date_after": self.tomorrow, "date_before": self.tomorrow},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.json()["count"], 2)

        response = self.client.get(
            self.list_url(), {"appointment_number": first["appointment_number"].lower()}
        )
        self.assertEqual([x["id"] for x in response.json()["results"]], [first["id"]])
        response = self.client.get(self.list_url(), {"booking_channel": "phone"})
        self.assertEqual(response.json()["count"], 1)

        # Without facility-wide listing, the resource is still required.
        nurse = self.create_user(username="no-facility-list")
        self.client.force_authenticate(user=nurse)
        response = self.client.get(self.list_url())
        self.assertNotEqual(response.status_code, status.HTTP_200_OK)

    def test_patient_otp_booking_is_online(self):
        otp_user = PatientOtpObject()
        otp_user.phone_number = self.patient.phone_number
        self.client.force_authenticate(user=otp_user)
        response = self.client.post(
            reverse(
                "otp-slots-create-appointment",
                kwargs={"external_id": self.slots[0].external_id},
            ),
            {
                "patient": str(self.patient.external_id),
                "note": "Booked myself",
                "booking_channel": "walk_in",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        booking = TokenBooking.objects.get()
        self.assertEqual(booking.booking_channel, "online")
        self.assertTrue(booking.appointment_number.startswith("APT-"))
