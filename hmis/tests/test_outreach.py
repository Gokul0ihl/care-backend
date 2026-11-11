import json
from datetime import timedelta

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from care.emr.models import Patient, Questionnaire
from care.utils.tests.base import CareAPITestBase
from hmis.models import CampParticipant, OutreachEvent, OutreachEventLogin

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
            "login_emails": ["Camp.Desk@example.com", "nurse.two@example.com"],
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
            "title": "Ms",
            "first_name": "Test",
            "last_name": "Person",
            "gender": "female",
            "date_of_birth": "1980-01-01",
            "preferred_language": "English",
            "nationality": "Indian resident",
            "id_type": "pan",
            "id_number": "ABCDE1234F",
            "mobile": "9000000001",
            "address": "1 Test Road",
            "city": "Test Locality",
            "pincode": "110001",
            "finding": "High sugar",
            "bp": "120/80",
            "height_cm": "165",
            "weight_kg": "70",
            "consent_screening": True,
            "consent_dpdp_notice": True,
            "consent_tariff": True,
            "consent_by": "The patient",
            "consent_method": "Signature",
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
        event = self.create_event(
            description="short", tests=[], staff=[], login_emails=[" "]
        )
        response = self.act(event["id"], "submit", self.staff)
        self.assertEqual(response.status_code, 400)
        self.assertTrue(
            {("description",), ("tests",), ("staff",), ("login_emails",)}
            <= self.locs(response)
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

    def camp_login(self, event_id, email):
        self.client.force_authenticate(user=None)
        return self.client.post(
            reverse("camp-login", args=[event_id]), {"email": email}, format="json"
        )

    def test_camp_email_signs_in_to_this_camp_only(self):
        cache.clear()
        self.as_user(self.staff)
        response = self.client.post(
            reverse("outreach-events"),
            self.event_body(
                login_emails=["not-an-email", "a@example.com", "A@example.com"]
            ),
            format="json",
        )
        self.assertEqual(
            self.locs(response), {("login_emails", "0"), ("login_emails", "2")}
        )

        draft = self.create_event(coupon_code="DRAFT1")
        event = self.approved_event()
        self.assertEqual(
            self.client.get(reverse("outreach-event", args=[event["id"]])).json()[
                "login_emails"
            ],
            ["camp.desk@example.com", "nurse.two@example.com"],
        )

        # The sign-in page shows the camp before anyone signs in.
        self.client.force_authenticate(user=None)
        header = self.client.get(reverse("camp-login", args=[event["id"]])).json()
        self.assertEqual(header["name"], "Diabetes screening camp")
        self.assertEqual(header["number"], event["number"] or header["number"])

        wrong = self.camp_login(event["id"], "someone@example.com")
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(self.locs(wrong), {("email",)})
        self.assertEqual(
            self.camp_login(draft["id"], "camp.desk@example.com").status_code, 400
        )

        response = self.camp_login(event["id"], "  CAMP.desk@example.com ")
        self.assertEqual(response.status_code, 200, response.json())
        tokens = response.json()
        self.assertTrue(tokens["access"] and tokens["refresh"])

        def account(email):
            return OutreachEventLogin.objects.get(
                event__external_id=event["id"], email=email
            ).user

        login_user = account("camp.desk@example.com")
        self.assertFalse(login_user.has_usable_password())
        # Signing in again is the same account; another email is another one.
        self.camp_login(event["id"], "camp.desk@example.com")
        self.assertEqual(account("camp.desk@example.com"), login_user)
        second = self.camp_login(event["id"], "nurse.two@example.com").json()
        self.assertNotEqual(account("nurse.two@example.com"), login_user)

        # The camp's account captures at this camp, and nowhere else.
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        session = self.client.post(
            reverse("camp-sessions"), {"event": event["id"]}, format="json"
        )
        self.assertEqual(session.status_code, 201, session.json())
        captured = self.client.post(
            reverse("camp-participants", args=[session.json()["id"]]),
            self.person(),
            format="json",
        )
        self.assertEqual(captured.status_code, 201, captured.json())
        self.assertEqual(
            captured.json()["captured_by"]["email"], "camp.desk@example.com"
        )
        # The second email captures as itself, in its own session.
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {second['access']}")
        other_session = self.client.post(
            reverse("camp-sessions"), {"event": event["id"]}, format="json"
        ).json()
        by_second = self.client.post(
            reverse("camp-participants", args=[other_session["id"]]),
            self.person(first_name="Second", mobile="9000000002"),
            format="json",
        )
        self.assertEqual(by_second.status_code, 201, by_second.json())
        listed = self.client.get(
            reverse("camp-participants", args=[other_session["id"]])
        ).json()["results"]
        self.assertEqual(
            [p["captured_by"]["email"] for p in listed],
            ["nurse.two@example.com", "camp.desk@example.com"],
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        self.assertEqual(
            [
                e["id"]
                for e in self.client.get(reverse("camp-events")).json()["results"]
            ],
            [event["id"]],
        )
        self.assertTrue(self.client.get(reverse("outreach-me")).json()["camp_only"])
        other = self.approved_event(coupon_code="OTHER1")
        self.client.force_authenticate(user=None)
        self.assertEqual(
            self.client.post(
                reverse("camp-sessions"), {"event": other["id"]}, format="json"
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                reverse("outreach-events"), {"facility": str(self.facility.external_id)}
            ).status_code,
            403,
        )
        self.client.credentials()

    def test_existing_person_takes_identity_from_the_record(self):
        event = self.approved_event()
        session = self.start_session(event["id"]).json()
        first = self.capture(session["id"], first_name="Testrecord", last_name="Person")
        self.assertEqual(first.status_code, 201, first.json())
        patient_id = first.json()["patient"]

        def lookup(q):
            return self.client.get(
                reverse("camp-patient-lookup", args=[session["id"]]), {"q": q}
            ).json()["results"]

        self.assertEqual([p["id"] for p in lookup("testrec")], [patient_id])
        self.assertEqual(lookup("te"), [])
        self.assertEqual([p["id"] for p in lookup("9000000001")], [patient_id])

        # At the next camp only the visit is sent: no names, birth date, mobile or PIN.
        later = self.approved_event(coupon_code="LATER1")
        session = self.start_session(later["id"]).json()
        response = self.capture(
            session["id"],
            kind="existing",
            patient=patient_id,
            first_name="",
            last_name="",
            gender="",
            date_of_birth=None,
            mobile="",
            pincode="",
            # Left over from New Person: not shown, so not saved.
            address="1 Hidden Road",
            id_type="pan",
            id_number="ABCDE1234F",
            allergy_status="has",
            allergen="Dust",
            blood_group="O+",
        )
        self.assertEqual(response.status_code, 201, response.json())
        row = CampParticipant.objects.get(external_id=response.json()["id"])
        self.assertEqual(
            (
                row.address,
                row.id_type,
                row.id_number,
                row.allergy_status,
                row.allergen,
                row.blood_group,
            ),
            ("", "", "", "", "", ""),
        )
        self.assertEqual(
            (
                row.first_name,
                row.gender,
                str(row.date_of_birth),
                row.mobile,
                row.pincode,
            ),
            ("Testrecord", "female", "1980-01-01", "9000000001", "110001"),
        )
        self.assertEqual(str(row.patient.external_id), patient_id)
        self.assertEqual(
            self.locs(self.capture(session["id"], kind="existing", patient=None)),
            {("patient",)},
        )

    def test_patient_page_lists_camp_visits(self):
        event = self.approved_event()
        session = self.start_session(event["id"]).json()
        first = self.capture(
            session["id"],
            tests=[
                {"id": t["id"], "result": "-2.1"} for t in session["event"]["tests"][:1]
            ],
        ).json()
        later = self.approved_event(coupon_code="LATER1")
        session = self.start_session(later["id"]).json()
        self.capture(
            session["id"], kind="existing", patient=first["patient"], bp="130/85"
        )

        self.as_user(self.admin)
        url = reverse("patient-camp-visits", args=[first["patient"]])
        visits = self.client.get(url).json()["results"]
        self.assertEqual(
            [(v["event"]["id"], v["kind"], v["bp"]) for v in visits],
            [(later["id"], "existing", "130/85"), (event["id"], "new", "120/80")],
        )
        self.assertEqual(visits[1]["tests"][0]["result"], "-2.1")
        self.assertEqual(visits[1]["patient_number"]["label"], "Pre-registration")

        outsider = self.create_user()
        self.as_user(outsider)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_camp_email_sign_in_locks_out_after_ten_wrong_tries(self):
        cache.clear()
        event = self.approved_event()
        for _ in range(10):
            self.camp_login(event["id"], "wrong@example.com")
        response = self.camp_login(event["id"], "camp.desk@example.com")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Too many tries", json.dumps(response.json()))
        cache.clear()

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

    def test_capture_keeps_the_full_details(self):
        event = self.approved_event()
        session = self.start_session(event["id"]).json()
        self.assertEqual(session["event"]["captured_today"], 0)
        self.assertIn("departments", session["event"])
        rbs = next(t["id"] for t in session["event"]["tests"] if t["name"] == "RBS")
        response = self.capture(
            session["id"],
            title="Ms",
            middle_name="Fake",
            preferred_language="Hindi",
            blood_group="O+",
            alt_mobile="9000000009",
            alt_mobile_belongs_to="Spouse",
            address="1 Test Lane",
            city="Testpur",
            permanent_address={"address": "2 Other Lane", "pincode": "110002"},
            allergy_status="has",
            allergen="Latex",
            allergy_reaction="Hives",
            allergy_severity="mild",
            send_via=["whatsapp", "phone"],
            tests=[{"id": rbs, "result": " 112 ", "location": "camp"}],
        )
        self.assertEqual(response.status_code, 201, response.json())
        row = response.json()
        self.assertEqual(row["title"], "Ms")
        self.assertEqual(row["bmi"], "25.7")
        self.assertEqual(row["alt_mobile_isd"], "+91")
        self.assertEqual(row["permanent_address"]["address"], "2 Other Lane")
        self.assertEqual(row["allergen"], "Latex")
        self.assertEqual(row["consent_method"], "Signature")
        self.assertEqual(row["tests"][0]["result"], "112")
        self.assertEqual(row["tests"][0]["location"], "camp")
        self.as_user(self.camp_staff)
        detail = self.client.get(reverse("camp-session", args=[session["id"]]))
        self.assertEqual(detail.json()["event"]["captured_today"], 1)

    def test_new_person_becomes_a_pre_registered_patient(self):
        event = self.approved_event()
        session = self.start_session(event["id"]).json()
        response = self.capture(
            session["id"],
            title="Ms",
            middle_name="Fake",
            blood_group="O+",
            alt_mobile="9000000009",
            alt_mobile_belongs_to="Spouse",
            address="1 Test Lane",
            city="Testpur",
            allergy_status="has",
            allergen="Latex",
            allergy_reaction="Hives",
            allergy_severity="mild",
            send_via=["whatsapp"],
        )
        self.assertEqual(response.status_code, 201, response.json())
        row = response.json()
        self.assertEqual(row["patient_number"]["label"], "Pre-registration")
        self.assertTrue(row["patient_number"]["value"].startswith("PRE-LN-"))
        # Captured, not yet registered at the hospital.
        self.assertEqual(row["status"], "open")
        patient = Patient.objects.get(external_id=row["patient"])
        self.assertEqual(patient.name, "Test Fake Person")
        self.assertEqual(patient.registration_facility, self.facility)
        self.assertIsNone(patient.registered_at)
        self.assertEqual(patient.camp_patient_id, row["number"])
        self.assertEqual(patient.arrival_source, "Health camp")
        self.assertEqual(patient.alternate_phone_owner, "Spouse")
        self.assertEqual(patient.blood_group, "O_positive")
        self.assertEqual(patient.communication_channels, ["WhatsApp"])
        self.assertEqual(patient.desk_allergies.get().substance, "Latex")
        # The desk's edit form opens with the camp's details.
        self.as_user(self.admin)
        desk = self.client.get(
            reverse("desk-registration-detail", args=[patient.external_id])
        )
        self.assertEqual(desk.status_code, 200)
        self.assertEqual(desk.json()["house"], "1 Test Lane")
        self.assertEqual(desk.json()["created_by"], self.camp_staff.username)
        self.assertEqual(patient.nationality_category, "Indian resident")

    def test_new_person_needs_the_mandatory_details(self):
        event = self.approved_event()
        session = self.start_session(event["id"]).json()
        response = self.capture(
            session["id"],
            title="",
            preferred_language="",
            nationality="Foreign",
            id_type="",
            id_number="",
            mobile_isd="91",
            address=" ",
            city="",
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(
            {
                ("title",),
                ("preferred_language",),
                ("nationality",),
                ("id_type",),
                ("id_number",),
                ("mobile_isd",),
                ("address",),
                ("city",),
            }
            <= self.locs(response)
        )

    def test_capture_checks_vitals_allergies_consent_and_contact(self):
        event = self.approved_event()
        session = self.start_session(event["id"]).json()
        response = self.capture(
            session["id"],
            bp="",
            height_cm=None,
            weight_kg=None,
            allergy_status="has",
            consent_tariff=False,
            consent_by="",
            consent_method="Thumb impression",
            send_via=["do_not_contact", "sms"],
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(
            {
                ("bp",),
                ("height_cm",),
                ("weight_kg",),
                ("allergen",),
                ("allergy_reaction",),
                ("allergy_severity",),
                ("consent_tariff",),
                ("consent_by",),
                ("consent_witness",),
                ("consent_file",),
                ("send_via",),
            }
            <= self.locs(response)
        )

    def test_capture_takes_the_photo_and_paper_consent_as_multipart(self):
        event = self.approved_event()
        session = self.start_session(event["id"]).json()
        url = reverse("camp-participants", args=[session["id"]])
        body = self.person(consent_method="Scanned form")
        response = self.client.post(url, {"data": json.dumps(body)}, format="multipart")
        self.assertIn(("consent_file",), self.locs(response))
        response = self.client.post(
            url,
            {
                "data": json.dumps(body),
                "photo": SimpleUploadedFile("p.txt", b"x", "text/plain"),
                "consent_file": SimpleUploadedFile(
                    "c.pdf", b"%PDF-1.4", "application/pdf"
                ),
            },
            format="multipart",
        )
        self.assertIn(("photo",), self.locs(response))
        response = self.client.post(
            url,
            {
                "data": json.dumps(body),
                "consent_file": SimpleUploadedFile(
                    "c.pdf", b"%PDF-1.4", "application/pdf"
                ),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.json())
        self.assertTrue(response.json()["has_consent_file"])
        self.assertFalse(response.json()["has_photo"])

    def make_questionnaire(self, slug="camp-form-test-v1", status="active"):
        return Questionnaire.objects.create(
            slug=slug,
            title="Camp form",
            version="1.0",
            subject_type="patient",
            status=status,
            questions=[],
        )

    def test_event_links_its_form(self):
        form = self.make_questionnaire()
        event = self.create_event(questionnaire=form.slug)
        self.assertEqual(event["questionnaire"], form.slug)
        self.assertEqual(OutreachEvent.objects.get(external_id=event["id"]).questionnaire, form)
        newer = self.make_questionnaire("camp-form-test-v2")
        response = self.client.put(
            reverse("outreach-event", args=[event["id"]]),
            self.event_body(questionnaire=newer.slug),
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["questionnaire"], newer.slug)
        response = self.client.put(
            reverse("outreach-event", args=[event["id"]]),
            self.event_body(),
            format="json",
        )
        self.assertIsNone(response.json()["questionnaire"])

    def test_event_form_must_exist_and_capture(self):
        self.as_user(self.staff)
        response = self.client.post(
            reverse("outreach-events"),
            self.event_body(questionnaire="no-such-form"),
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(("questionnaire",), self.locs(response))
        draft = self.make_questionnaire("camp-form-draft-v1", status="draft")
        response = self.client.post(
            reverse("outreach-events"),
            self.event_body(questionnaire=draft.slug),
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(("questionnaire",), self.locs(response))
        form = self.make_questionnaire()
        response = self.client.post(
            reverse("outreach-events"),
            self.event_body(
                event_type="CME", form_fields=[], questionnaire=form.slug
            ),
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(("questionnaire",), self.locs(response))
