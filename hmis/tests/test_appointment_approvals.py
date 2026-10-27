"""App approvals reserve and release real Care seats, with facility permissions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
from rest_framework.exceptions import ValidationError

from care.emr.api.viewsets.scheduling.availability import SlotViewSet
from care.emr.models import (
    Availability,
    Patient,
    SchedulableResource,
    Schedule,
    TokenBooking,
    TokenSlot,
)
from care.users.models import User
from care.utils.tests.base import CareAPITestBase
from hmis.appointment_approvals import (
    ReviewConflict,
    expire_pending_bookings,
    review_booking,
)
from hmis.tasks import expire_app_appointment_requests


def make_slot(user, facility):
    start = timezone.now() + timedelta(days=1)
    resource, _ = SchedulableResource.objects.get_or_create(
        user=user, facility=facility, resource_type="practitioner"
    )
    schedule = baker.make(
        Schedule,
        resource=resource,
        is_public=True,
        valid_from=start,
        valid_to=start + timedelta(days=2),
        charge_item_definition=None,
    )
    availability = baker.make(
        Availability, schedule=schedule, tokens_per_slot=1, walk_in_every=None
    )
    return baker.make(
        TokenSlot,
        resource=resource,
        availability=availability,
        start_datetime=start,
        end_datetime=start + timedelta(minutes=15),
        allocated=0,
    )


def request_booking(slot, patient):
    return SlotViewSet.create_appointment_handler(
        slot,
        {
            "patient": str(patient.external_id),
            "note": "Test patient note",
            "booking_channel": "online",
        },
        None,
        pending_approval=True,
    )


class AppointmentApprovalTests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        self.user = self.create_super_user(first_name="Test", last_name="Reviewer")
        self.facility = self.create_facility(user=self.user)
        self.patient = self.create_patient(
            name="Testpatient Example",
            phone_number="+919800000001",
            restricted_alias=None,
        )
        self.slot = make_slot(self.user, self.facility)
        self.booking = request_booking(self.slot, self.patient)
        self.client.force_authenticate(self.user)
        self.base = (
            f"/api/hmis/facility/{self.facility.external_id}/appointment-requests/"
        )

    def review(self, action="approve", body=None):
        return self.client.post(
            f"{self.base}{self.booking.external_id}/{action}/",
            body or {},
            format="json",
        )

    def test_pending_reserves_capacity_without_a_charge(self):
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.allocated, 1)
        self.assertEqual(self.booking.status, "pending")
        self.assertEqual(self.booking.approval_status, "pending")
        self.assertEqual(self.booking.booking_channel, "online")
        self.assertIsNone(self.booking.charge_item_id)
        with self.assertRaises(ValidationError):
            request_booking(self.slot, self.create_patient())

    def test_queue_scopes_paginates_and_exposes_review_information(self):
        response = self.client.get(self.base)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        row = response.data["results"][0]
        self.assertTrue(row["can_review"])
        self.assertEqual(row["note"], "Test patient note")
        self.assertEqual(row["patient"]["name"], "Testpatient Example")
        self.assertEqual(self.client.get(self.base, {"offset": 1}).data["results"], [])
        self.assertEqual(self.client.get(self.base, {"limit": "bad"}).status_code, 400)
        self.assertEqual(self.client.get(self.base, {"status": "bad"}).status_code, 400)

    def test_approve_keeps_seat_number_and_original_reviewer_on_retry(self):
        number = self.booking.appointment_number
        response = self.review()
        self.assertEqual(response.status_code, 200, response.data)
        self.booking.refresh_from_db()
        reviewed_at = self.booking.reviewed_at
        self.assertEqual(self.booking.status, "booked")
        self.assertEqual(self.booking.approval_status, "approved")
        self.assertEqual(self.booking.reviewed_by, self.user)
        self.assertEqual(self.booking.appointment_number, number)
        self.assertIsNotNone(reviewed_at)
        other = self.create_super_user()
        self.client.force_authenticate(other)
        self.assertEqual(self.review().status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.reviewed_by, self.user)
        self.assertEqual(self.booking.reviewed_at, reviewed_at)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.allocated, 1)
        self.assertEqual(
            self.review("reject", {"reason": "Different decision"}).status_code, 409
        )
        self.assertEqual(self.client.get(self.base).data["count"], 0)
        self.assertEqual(
            self.client.get(self.base, {"status": "approved"}).data["count"], 1
        )

    def test_rejection_requires_reason_and_releases_exactly_once(self):
        for reason in ("", "  ", "x" * 501):
            self.assertEqual(self.review("reject", {"reason": reason}).status_code, 400)
        self.assertEqual(
            self.review("reject", {"reason": "  Doctor unavailable  "}).status_code, 200
        )
        self.assertEqual(self.review("reject", {"reason": "Retry"}).status_code, 200)
        self.booking.refresh_from_db()
        self.slot.refresh_from_db()
        self.assertEqual(self.booking.approval_reason, "Doctor unavailable")
        self.assertEqual(self.booking.note, "Test patient note")
        self.assertEqual(self.booking.status, "cancelled")
        self.assertEqual(self.slot.allocated, 0)
        self.assertEqual(self.review().status_code, 409)
        request_booking(self.slot, self.create_patient())
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.allocated, 1)

    def test_charge_is_deferred_until_approval_and_only_called_once(self):
        with patch("hmis.appointment_approvals.generate_booking_charge_item") as charge:
            self.assertEqual(self.review().status_code, 200)
            self.assertEqual(self.review().status_code, 200)
        charge.assert_called_once()

    def test_failed_charge_rolls_back_review_and_keeps_pending_hold(self):
        with patch(
            "hmis.appointment_approvals.generate_booking_charge_item",
            side_effect=ValidationError("Charge unavailable"),
        ):
            self.assertEqual(self.review().status_code, 400)
        self.booking.refresh_from_db()
        self.slot.refresh_from_db()
        self.assertEqual(self.booking.approval_status, "pending")
        self.assertEqual(self.booking.status, "pending")
        self.assertIsNone(self.booking.reviewed_by_id)
        self.assertEqual(self.slot.allocated, 1)

    def test_late_approval_expires_and_commits_release_even_with_409(self):
        TokenSlot.objects.filter(pk=self.slot.pk).update(
            start_datetime=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(self.review().status_code, 409)
        self.booking.refresh_from_db()
        self.slot.refresh_from_db()
        self.assertEqual(self.booking.approval_status, "expired")
        self.assertEqual(self.booking.status, "cancelled")
        self.assertIsNone(self.booking.reviewed_by_id)
        self.assertIsNotNone(self.booking.reviewed_at)
        self.assertEqual(self.slot.allocated, 0)

    def test_expiry_task_is_idempotent_and_ignores_approved_bookings(self):
        self.assertEqual(expire_pending_bookings(), 0)
        TokenSlot.objects.filter(pk=self.slot.pk).update(start_datetime=timezone.now())
        self.assertEqual(expire_app_appointment_requests(), 1)
        self.assertEqual(expire_app_appointment_requests(), 0)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.allocated, 0)
        other_slot = make_slot(self.user, self.facility)
        other = request_booking(other_slot, self.patient)
        review_booking(other.external_id, "approved", user=self.user)
        TokenSlot.objects.filter(pk=other_slot.pk).update(start_datetime=timezone.now())
        self.assertEqual(expire_app_appointment_requests(), 0)
        other.refresh_from_db()
        self.assertEqual(other.status, "booked")

    def test_cannot_bypass_review_with_staff_update_cancel_or_reschedule(self):
        kwargs = {
            "facility_external_id": self.facility.external_id,
            "external_id": self.booking.external_id,
        }
        response = self.client.patch(
            reverse("appointments-detail", kwargs=kwargs),
            {"status": "booked", "note": "Bypass"},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        response = self.client.post(
            reverse("appointments-cancel", kwargs=kwargs),
            {"reason": "cancelled"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        target = make_slot(self.user, self.facility)
        response = self.client.post(
            reverse("appointments-reschedule", kwargs=kwargs),
            {"new_slot": str(target.external_id), "new_booking_note": "Bypass"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "pending")

    def test_shared_cancel_handler_cannot_bypass_review(self):
        from care.emr.api.viewsets.scheduling.booking import TokenBookingViewSet

        with self.assertRaises(ValidationError):
            TokenBookingViewSet.cancel_appointment_handler(
                self.booking, {"reason": "cancelled"}, None
            )
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.allocated, 1)

    def test_mobile_patient_token_cannot_review_or_read_staff_queue(self):
        from hmis.mobile_auth import start_session

        _, tokens = start_session(self.patient, self.patient.phone_number)
        self.client.force_authenticate(None)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}")
        self.assertEqual(self.client.get(self.base).status_code, 403)
        self.assertEqual(self.review().status_code, 403)

    def test_no_patient_or_unprivileged_or_service_account_access(self):
        for user in (
            self.create_user(),
            self.create_super_user(is_service_account=True),
        ):
            self.client.force_authenticate(user)
            self.assertEqual(self.client.get(self.base).status_code, 403)
            self.assertEqual(self.review().status_code, 403)
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(self.base).status_code, (401, 403))
        self.assertIn(self.review().status_code, (401, 403))

    def test_facility_permissions_and_read_only_staff(self):
        from care.emr.models.organization import FacilityOrganizationUser

        reader = self.create_user()
        role = self.create_role_with_permissions(["can_list_booking"])
        FacilityOrganizationUser.objects.create(
            user=reader,
            organization=self.facility.default_internal_organization,
            role=role,
        )
        self.client.force_authenticate(reader)
        response = self.client.get(self.base)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["results"][0]["can_review"])
        self.assertEqual(self.review().status_code, 403)
        other_facility = self.create_facility(user=self.user)
        other_base = (
            f"/api/hmis/facility/{other_facility.external_id}/appointment-requests/"
        )
        self.assertEqual(self.client.get(other_base).status_code, 403)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get(other_base).data["count"], 0)
        self.assertEqual(
            self.client.post(
                f"{other_base}{self.booking.external_id}/approve/", {}, format="json"
            ).status_code,
            404,
        )

    def test_non_request_booking_not_reviewable_and_client_cannot_set_pending(self):
        other_slot = make_slot(self.user, self.facility)
        response = self.client.post(
            reverse(
                "slot-create-appointment",
                kwargs={
                    "facility_external_id": self.facility.external_id,
                    "external_id": other_slot.external_id,
                },
            ),
            {
                "patient": str(self.patient.external_id),
                "booking_channel": "online",
                "pending_approval": True,
                "status": "pending",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        booking = TokenBooking.objects.get(external_id=response.data["id"])
        self.assertEqual(booking.status, "booked")
        self.assertEqual(booking.approval_status, "")
        self.assertEqual(
            self.client.post(
                f"{self.base}{booking.external_id}/approve/", {}, format="json"
            ).status_code,
            404,
        )

    def test_restricted_patient_is_masked_in_queue(self):
        self.patient.restricted_alias = "PT-TEST22"
        self.patient.save()
        row = self.client.get(self.base).data["results"][0]
        self.assertEqual(row["patient"]["name"], "PT-TEST22")
        self.assertEqual(row["note"], "")
        self.assertIsNone(row["patient_number"])


class ApprovalConcurrencyTests(TransactionTestCase):
    def setUp(self):
        from care.facility.models import Facility

        self.user = baker.make(User, is_superuser=True)
        facility = baker.make(Facility, created_by=self.user)
        self.patient = baker.make(Patient, name="Testpatient Concurrent")
        self.slot = make_slot(self.user, facility)

    def run_concurrently(self, functions):
        barrier = Barrier(len(functions))

        def run(fn):
            try:
                barrier.wait(timeout=10)
                return fn()
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=len(functions)) as pool:
            return list(pool.map(run, functions))

    def test_two_rejections_release_only_one_seat(self):
        booking = request_booking(self.slot, self.patient)
        results = self.run_concurrently(
            [
                lambda: review_booking(
                    booking.external_id, "rejected", "Test rejection", self.user
                ).approval_status,
                lambda: review_booking(
                    booking.external_id, "rejected", "Test rejection", self.user
                ).approval_status,
            ]
        )
        self.assertEqual(results, ["rejected", "rejected"])
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.allocated, 0)

    def test_approval_and_rejection_have_one_winner(self):
        booking = request_booking(self.slot, self.patient)

        def decide(decision):
            try:
                return review_booking(
                    booking.external_id, decision, "Test decision", self.user
                ).approval_status
            except ReviewConflict:
                return "conflict"

        results = self.run_concurrently(
            [lambda: decide("approved"), lambda: decide("rejected")]
        )
        self.assertEqual(results.count("conflict"), 1)
        booking.refresh_from_db()
        self.slot.refresh_from_db()
        self.assertEqual(
            self.slot.allocated, int(booking.approval_status == "approved")
        )

    def test_desk_and_app_compete_for_last_seat(self):
        other = baker.make(Patient, name="Testother Concurrent")

        def book(patient, pending):
            try:
                return SlotViewSet.create_appointment_handler(
                    self.slot,
                    {
                        "patient": str(patient.external_id),
                        "note": "",
                        "booking_channel": "online" if pending else "front_desk",
                    },
                    None,
                    pending_approval=pending,
                ).status
            except ValidationError:
                return "full"

        results = self.run_concurrently(
            [
                lambda: book(self.patient, pending=True),
                lambda: book(other, pending=False),
            ]
        )
        self.assertEqual(results.count("full"), 1)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.allocated, 1)
        self.assertEqual(TokenBooking.objects.filter(token_slot=self.slot).count(), 1)
