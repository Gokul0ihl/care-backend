import datetime
from unittest import mock

from care.emr.models import (
    Availability,
    Encounter,
    SchedulableResource,
    Schedule,
    TokenBooking,
    TokenSlot,
)
from care.utils.tests.base import CareAPITestBase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from care_emergency.tasks import send_appointment_reminders


def local(day, hour, minute=0):
    return timezone.make_aware(
        datetime.datetime.combine(day, datetime.time(hour, minute))
    )


class AppointmentTests(CareAPITestBase):
    """What the plug adds to Care's bookings: the phone reminder task and the
    check-in encounter class. The booking rules are tested in the fork
    (care/emr/tests/test_booking_desk_details.py)."""

    def setUp(self):
        super().setUp()
        self.user = self.create_super_user()
        self.facility = self.create_facility(user=self.user)
        self.organization = self.create_facility_organization(facility=self.facility)
        self.patient = self.create_patient(phone_number="+919812345678")
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
        )
        # Tomorrow 09:00–10:00 in 15-minute slots of one seat: 4 seats.
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

    def book(self, slot=None, **kwargs):
        """Books through Care, which holds the desk details on TokenBooking."""
        url = reverse(
            "slot-create-appointment",
            kwargs={
                "facility_external_id": self.facility.external_id,
                "external_id": (slot or self.slots[0]).external_id,
            },
        )
        body = {"patient": str(self.patient.external_id), **kwargs}
        return self.client.post(url, body, format="json")

    def test_phone_booking_gets_reminder(self):
        response = self.book(booking_channel="phone")
        self.assertEqual(response.status_code, 200, response.data)
        booking = TokenBooking.objects.get(external_id=response.data["id"])

        with mock.patch("care_emergency.tasks.send_text_message") as send:
            send_appointment_reminders()
            send.assert_not_called()
            TokenBooking.objects.filter(pk=booking.pk).update(
                reminder_due_at=timezone.now() - datetime.timedelta(minutes=1)
            )
            send_appointment_reminders()
            send_appointment_reminders()
        send.assert_called_once()
        self.assertIn(booking.appointment_number, send.call_args.kwargs["content"])
        self.assertEqual(send.call_args.kwargs["recipients"], "+919812345678")
        booking.refresh_from_db()
        self.assertIsNotNone(booking.reminder_sent_at)

    def test_check_in_takes_class_from_consultation_type(self):
        booking_id = self.book(consultation_type="video").data["id"]
        body = {
            "patient": str(self.patient.external_id),
            "facility": str(self.facility.external_id),
            "organizations": [str(self.organization.external_id)],
            "status": "in_progress",
            "encounter_class": "amb",
            "priority": "routine",
            "appointment": str(booking_id),
        }
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("encounter-list"), body, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["encounter_class"], "vr")
        self.assertEqual(
            Encounter.objects.get(external_id=response.data["id"]).encounter_class, "vr"
        )

    def test_shared_desk_number_does_not_receive_patient_reminders(self):
        self.facility.emergency_desk_phone = self.patient.phone_number
        self.facility.save(update_fields=["emergency_desk_phone"])
        response = self.book(booking_channel="phone")
        self.assertEqual(response.status_code, 200, response.data)
        TokenBooking.objects.filter(external_id=response.data["id"]).update(
            reminder_due_at=timezone.now() - datetime.timedelta(minutes=1)
        )
        with mock.patch("care_emergency.tasks.send_text_message") as send:
            send_appointment_reminders()
            send.assert_not_called()
