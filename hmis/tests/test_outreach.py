from datetime import timedelta

from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from care.utils.tests.base import CareAPITestBase
from hmis.models import CampParticipant, OutreachEvent

MANAGE = "can_manage_outreach_event"
APPROVE = "can_approve_outreach_event"
CAPTURE = "can_capture_camp_participant"


class OutreachTests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        self.admin = self.create_super_user()
        self.facility = self.create_facility(user=self.admin)
        self.facility.unit_code = "LN"
        self.facility.save(update_fields=["unit_code"])
        self.org = self.create_facility_organization(
            facility=self.facility, org_type="root"
        )
        self.staff = self.user_with([MANAGE, CAPTURE])
        self.camp_staff = self.user_with([CAPTURE], role_name="Camp Staff")
        self.today = timezone.localdate()

    def user_with(self, permissions, role_name=None):
        user = self.create_user()
        role = self.create_role_with_permissions(permissions, role_name)
        self.attach_role_facility_organization_user(self.org, user, role)
        return user

    def as_user(self, user):
        self.client.force_authenticate(user=user)

    def event_body(self, **kwargs):
        return {
            "facility": str(self.facility.external_id),
            "event_type": "CAMP",
            "name": "Diabetes screening camp",
            "theme": "World Diabetes Day",
            "description": "Free sugar and BP screening for residents of the society.",
            "venue_name": "Test Society Clubhouse",
            "address": "1 Example Road",
            "pincode": "110001",
            "event_date": str(self.today),
            "start_time": "09:00",
            "end_time": "13:00",
            "expected_attendees": 100,
            "has_coupon": True,
            "coupon_code": "test26",
            "coupon_valid_from": str(self.today),
            "coupon_valid_to": str(self.today + timedelta(days=30)),
            "discount_registration_pct": 100,
            "discount_consultation_pct": 50,
            "discount_tests_pct": 20,
            "tests": [
                {
                    "name": "RBS",
                    "location": "camp",
                    "fee_type": "free",
                    "cost_to_poh": "20",
                    "quantity_offered": 1,
                },
                {
                    "name": "HbA1c",
                    "location": "hospital",
                    "fee_type": "paid",
                    "rate": "400",
                },
            ],
            "staff": [
                {"role": "lead", "name": "Dr Test Doctor", "department": "Medicine"}
            ],
            "cost_lines": [
                {
                    "category": "consumable",
                    "item_name": "Strips",
                    "unit": "box",
                    "quantity": "2",
                    "unit_cost": "500",
                }
            ],
            "form_fields": [
                {
                    "key": "diabetic_family",
                    "label": "Diabetes in the family?",
                    "field_type": "yes_no",
                    "required": True,
                },
                {
                    "key": "visit_reason",
                    "label": "How did you hear?",
                    "field_type": "dropdown",
                    "options": ["Poster", "Friend"],
                },
            ],
            **kwargs,
        }

    def create_event(self, **kwargs):
        self.as_user(self.staff)
        response = self.client.post(
            reverse("outreach-events"), self.event_body(**kwargs), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.json())
        return response.json()

    def act(self, event_id, action, user, **body):
        self.as_user(user)
        return self.client.post(
            reverse("outreach-event", args=[event_id]),
            {"action": action, **body},
            format="json",
        )

    def approved_event(self, **kwargs):
        event = self.create_event(**kwargs)
        self.assertEqual(self.act(event["id"], "submit", self.staff).status_code, 200)
        self.assertEqual(self.act(event["id"], "approve", self.admin).status_code, 200)
        return event

    def start_session(self, event_id, user=None):
        self.as_user(user or self.camp_staff)
        return self.client.post(
            reverse("camp-sessions"), {"event": event_id}, format="json"
        )

    def person(self, **kwargs):
        return {
            "first_name": "Test",
            "last_name": "Person",
            "gender": "female",
            "date_of_birth": "1980-01-01",
            "mobile": "9000000001",
            "pincode": "110001",
            "finding": "High sugar",
            "consent_screening": True,
            "consent_dpdp_notice": True,
            "extra_answers": {"diabetic_family": True},
            **kwargs,
        }

    def capture(self, session_id, **kwargs):
        return self.client.post(
            reverse("camp-participants", args=[session_id]),
            self.person(**kwargs),
            format="json",
        )

    def locs(self, response):
        return {tuple(e["loc"]) for e in response.json()["errors"] if "loc" in e}

    def test_draft_needs_only_name_and_date(self):
        event = self.create_event(description="", tests=[], staff=[], venue_name="")
        self.assertEqual(event["status"], "draft")
        self.assertIsNone(event["number"])
        self.assertEqual(event["coupon_code"], "TEST26")

    def test_submit_checks_everything_and_numbers_the_event(self):
        event = self.create_event(description="short", tests=[], staff=[])
        response = self.act(event["id"], "submit", self.staff)
        self.assertEqual(response.status_code, 400)
        self.assertTrue(
            {("description",), ("tests",), ("staff",)} <= self.locs(response)
        )

        self.as_user(self.staff)
        self.client.put(
            reverse("outreach-event", args=[event["id"]]),
            self.event_body(),
            format="json",
        )
        response = self.act(event["id"], "submit", self.staff)
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["status"], "pending_approval")
        self.assertEqual(response.json()["number"], f"CAMP-LN-{self.today:%y%m}-01")

    def test_staff_cannot_approve_admin_can_approve_own(self):
        self.as_user(self.admin)
        response = self.client.post(
            reverse("outreach-events"), self.event_body(), format="json"
        )
        event_id = response.json()["id"]
        self.act(event_id, "submit", self.admin)
        self.assertEqual(self.act(event_id, "approve", self.staff).status_code, 403)
        response = self.act(event_id, "approve", self.admin)
        self.assertEqual(response.json()["status"], "approved")
        self.assertEqual(response.json()["decisions"][0]["decision"], "approved")

    def test_reject_needs_remarks_and_sends_back_for_editing(self):
        event = self.create_event()
        self.act(event["id"], "submit", self.staff)
        response = self.act(event["id"], "reject", self.admin)
        self.assertIn(("remarks",), self.locs(response))
        response = self.act(
            event["id"], "reject", self.admin, remarks="Change the venue"
        )
        self.assertEqual(response.json()["status"], "rejected")
        self.as_user(self.staff)
        response = self.client.put(
            reverse("outreach-event", args=[event["id"]]),
            self.event_body(),
            format="json",
        )
        self.assertEqual(response.json()["status"], "draft")

    def test_coupon_code_is_unique(self):
        self.create_event()
        self.as_user(self.staff)
        response = self.client.post(
            reverse("outreach-events"), self.event_body(), format="json"
        )
        self.assertIn(("coupon_code",), self.locs(response))

    def test_capture_needs_an_approved_event(self):
        event = self.create_event()
        self.assertEqual(self.start_session(event["id"]).status_code, 400)

    def test_camp_list_and_capabilities(self):
        event = self.approved_event()
        self.create_event(name="Unapproved camp", coupon_code="OTHER1")
        self.as_user(self.camp_staff)
        me = self.client.get(reverse("outreach-me")).json()
        self.assertTrue(me["camp_only"])
        self.assertFalse(me["facilities"][0]["can_manage"])
        names = [
            e["name"] for e in self.client.get(reverse("camp-events")).json()["results"]
        ]
        self.assertEqual(names, [event["name"]])
        # Camp staff can't see the main event list.
        response = self.client.get(
            reverse("outreach-events"), {"facility": str(self.facility.external_id)}
        )
        self.assertEqual(response.status_code, 403)

    def test_capture_validates_fixed_and_custom_fields(self):
        event = self.approved_event()
        session = self.start_session(event["id"]).json()
        response = self.capture(
            session["id"],
            date_of_birth=None,
            mobile="5000000000",
            consent_dpdp_notice=False,
            extra_answers={"visit_reason": "Radio", "unknown": 1},
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(
            {
                ("date_of_birth",),
                ("mobile",),
                ("consent_dpdp_notice",),
                ("extra_answers", "diabetic_family"),
                ("extra_answers", "visit_reason"),
                ("extra_answers", "unknown"),
            }
            <= self.locs(response)
        )

    def test_capture_numbers_dedups_and_uses_free_quota(self):
        event = self.approved_event()
        session = self.start_session(event["id"]).json()
        rbs = next(t["id"] for t in session["event"]["tests"] if t["name"] == "RBS")
        response = self.capture(
            session["id"], tests=[rbs], id_type="aadhaar", id_number="1234 5678 9012"
        )
        self.assertEqual(response.status_code, 201, response.json())
        row = response.json()
        self.assertEqual(row["number"], f"CMP-LN-{self.today:%y%m}-0001")
        self.assertEqual(row["id_number"], "XXXX XXXX 9012")
        self.assertEqual(row["tests"][0]["fee_type"], "free")
        self.assertEqual(row["extra_answers"]["diabetic_family"]["value"], True)

        self.assertEqual(
            self.capture(session["id"], first_name="test").status_code, 400
        )

        # The one free RBS is used, so the next person's is charged.
        row = self.capture(session["id"], mobile="9000000002", tests=[rbs]).json()
        self.assertEqual(row["tests"][0]["source"], "beyond_free_quota")
        self.assertEqual(row["tests"][0]["payment_status"], "due")

    def test_new_version_pauses_capture(self):
        event = self.approved_event()
        session = self.start_session(event["id"]).json()
        self.assertEqual(
            self.act(event["id"], "new-version", self.staff).json()["version"], 2
        )
        self.as_user(self.camp_staff)
        self.assertEqual(self.capture(session["id"]).status_code, 400)

    def test_session_belongs_to_its_user(self):
        event = self.approved_event()
        session = self.start_session(event["id"]).json()
        self.as_user(self.staff)
        self.assertEqual(self.capture(session["id"]).status_code, 403)

    def test_cancel_only_without_participants(self):
        event = self.approved_event()
        session = self.start_session(event["id"]).json()
        self.capture(session["id"])
        self.assertEqual(self.act(event["id"], "cancel", self.staff).status_code, 400)
        self.assertEqual(
            self.act(event["id"], "close", self.staff).json()["status"], "closed"
        )
        self.assertEqual(
            CampParticipant.objects.filter(event__external_id=event["id"]).count(), 1
        )
        self.assertEqual(
            OutreachEvent.objects.get(external_id=event["id"]).status, "closed"
        )
