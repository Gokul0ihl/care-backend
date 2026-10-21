import threading
from datetime import UTC, datetime, timedelta

from django.db import connection
from django.test import TransactionTestCase
from model_bakery import baker

from care.emr.api.viewsets.scheduling.availability import SlotViewSet
from care.emr.models import Availability, SchedulableResource, Schedule, TokenSlot
from care.emr.resources.scheduling.schedule.spec import (
    SchedulableResourceTypeOptions,
    SlotTypeOptions,
)
from care.facility.models.facility import Facility
from care.users.models import User


class TestSlotsForDayConcurrency(TransactionTestCase):
    """Slots are created lazily on the first read of a day, so two reads that
    arrive together must not both create the day's slots."""

    def setUp(self):
        self.user = baker.make(User)
        self.facility = baker.make(Facility, created_by=self.user)
        resource = SchedulableResource.objects.create(
            resource_type=SchedulableResourceTypeOptions.practitioner.value,
            user=self.user,
            facility=self.facility,
        )
        schedule = Schedule.objects.create(
            resource=resource,
            name="Test Schedule",
            valid_from=datetime.now(UTC) - timedelta(days=30),
            valid_to=datetime.now(UTC) + timedelta(days=30),
        )
        Availability.objects.create(
            schedule=schedule,
            name="Test Availability",
            slot_type=SlotTypeOptions.appointment.value,
            slot_size_in_minutes=30,
            tokens_per_slot=1,
            create_tokens=False,
            reason="Regular schedule",
            availability=[
                {"day_of_week": day, "start_time": "09:00:00", "end_time": "13:00:00"}
                for day in range(7)
            ],
        )

    def test_concurrent_reads_create_each_slot_once(self):
        request = {
            "resource_type": SchedulableResourceTypeOptions.practitioner.value,
            "resource_id": str(self.user.external_id),
            "day": (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d"),
        }
        start = threading.Barrier(4)
        errors = []

        def read():
            try:
                start.wait()
                SlotViewSet.get_slots_for_day_handler(self.facility.external_id, request)
            except Exception as error:  # noqa: BLE001
                errors.append(error)
            finally:
                connection.close()

        threads = [threading.Thread(target=read) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(TokenSlot.objects.count(), 8)
