"""OPD vitals station (OPD01-08): seed, save, flags, corrections, the station
queue and walk-ins. All patients here are fake."""

import uuid
from datetime import date

from django.urls import reverse

from care.emr.locks.billing import PatientCreateLock
from care.emr.models import Observation, ObservationDefinition, Questionnaire
from care.emr.resources.encounter.constants import ClassChoices
from care.utils.tests.base import CareAPITestBase
from hmis import opd_vitals
from hmis.constants import OpdStage
from hmis.models import OpdStation, OpdVisit, OpdVitalsRecord
from hmis.opd import priority_tier, sort_queue
from hmis.opd_vitals import calculate, question_id


def reading(systolic, diastolic):
    return [
        {"question_id": question_id("systolic"), "values": [{"value": str(systolic)}]},
        {
            "question_id": question_id("diastolic"),
            "values": [{"value": str(diastolic)}],
        },
    ]


def answer(link_id, value):
    return {"question_id": question_id(link_id), "values": [{"value": str(value)}]}


def full_results(bp=((120, 80),), **overrides):
    values = {
        "pulse": 72,
        "spo2": 98,
        "temperature": 98.6,
        "respiratory_rate": 16,
        "height": 170,
        "weight": 70,
        "pain": 0,
        **overrides,
    }
    results = [answer(k, v) for k, v in values.items() if v is not None]
    if bp:
        results.append(
            {"question_id": question_id("bp"), "sub_results": [reading(*r) for r in bp]}
        )
    return results


class CalculationTests(CareAPITestBase):
    def test_map_bmi_bsa(self):
        out = calculate([(120, 80), (130, 90)], 170, 70)
        self.assertEqual(out["bp_average"], {"systolic": 125, "diastolic": 85})
        self.assertEqual(out["map"], 98)
        self.assertEqual(out["bmi"], 24.2)
        self.assertEqual(out["bsa"], 1.82)

    def test_no_height_no_bmi(self):
        out = calculate([], None, 70)
        self.assertNotIn("bmi", out)
        self.assertNotIn("map", out)

    def test_priority_tiers(self):
        def tier(age, *, pregnant=False, booked=False):
            return priority_tier(age, pregnant=pregnant, has_booking=booked)

        self.assertEqual(tier(65), 0)
        self.assertEqual(tier(30, pregnant=True), 0)
        self.assertEqual(tier(30, booked=True), 1)
        self.assertEqual(tier(None), 2)


class OpdVitalsTests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        PatientCreateLock().release()
        self.user = self.create_super_user()
        self.facility = self.create_facility(user=self.user)
        self.organization = self.create_facility_organization(facility=self.facility)
        self.client.force_authenticate(user=self.user)
        opd_vitals.seed(apply=True, user=self.user)
        self.doctor = self.create_user()
        self.station = OpdStation.objects.create(
            facility=self.facility, code="V1", name="Station 1", is_central=True
        )

    def patient(self, **kwargs):
        defaults = {
            "name": "Test Patient",
            "gender": "male",
            "date_of_birth": date(1990, 1, 1),
        }
        return self.create_patient(**{**defaults, **kwargs})

    def visit(self, patient=None, **kwargs):
        encounter = self.create_encounter(
            patient or self.patient(),
            self.facility,
            self.organization,
            encounter_class=ClassChoices.amb.value,
            **kwargs,
        )
        return OpdVisit.objects.get(encounter=encounter)

    def save(self, visit, results=None, **kwargs):
        body = {
            "visit": str(visit.external_id),
            "submission_key": str(uuid.uuid4()),
            "results": full_results() if results is None else results,
            **kwargs,
        }
        return self.client.post(reverse("opd-vitals"), body, format="json")

    def error_locs(self, response):
        return [tuple(str(x) for x in e["loc"]) for e in response.data["errors"]]

    # --- Seed ---------------------------------------------------------------

    def test_seed_is_idempotent_and_dry_run_by_default(self):
        before = ObservationDefinition.objects.count()
        lines = opd_vitals.seed()
        self.assertTrue(all(line.startswith("update") for line in lines))
        opd_vitals.seed(apply=True)
        self.assertEqual(ObservationDefinition.objects.count(), before)
        self.assertEqual(Questionnaire.objects.filter(slug="opd-vitals").count(), 1)

    def test_form(self):
        response = self.client.get(reverse("opd-vitals-form"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("bp", response.data["required"])
        self.assertEqual(response.data["questions"][0]["link_id"], "bp")

    # --- Visits -------------------------------------------------------------

    def test_amb_encounter_opens_a_visit_others_do_not(self):
        visit = self.visit()
        self.assertEqual(visit.stage, OpdStage.waiting_vitals)
        self.assertEqual(visit.queue_number, 1)
        self.assertEqual(visit.station, self.station)
        self.assertEqual(self.visit().queue_number, 2)
        encounter = self.create_encounter(
            self.patient(), self.facility, self.organization, encounter_class="imp"
        )
        self.assertFalse(OpdVisit.objects.filter(encounter=encounter).exists())

    def test_doctor_station_beats_central(self):
        own = OpdStation.objects.create(facility=self.facility, code="V2", name="Dr")
        own.doctors.add(self.doctor)
        from hmis.opd import station_for

        self.assertEqual(station_for(self.facility, self.doctor), own)
        self.assertEqual(station_for(self.facility, None), self.station)

    def test_walk_in(self):
        patient = self.patient()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("opd-visits"),
                {
                    "facility": str(self.facility.external_id),
                    "patient": str(patient.external_id),
                    "doctor": str(self.doctor.external_id),
                    "organizations": [str(self.organization.external_id)],
                },
                format="json",
            )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["is_walk_in"])
        self.assertEqual(response.data["doctor"]["id"], str(self.doctor.external_id))
        self.assertEqual(OpdVisit.objects.filter(encounter__patient=patient).count(), 1)

    # --- Queue --------------------------------------------------------------

    def test_queue_order_and_call_next(self):
        young = self.visit(self.patient(name="Young Walkin"))
        senior = self.visit(
            self.patient(name="Senior Walkin", date_of_birth=date(1940, 1, 1))
        )
        pregnant = self.visit(
            self.patient(
                name="Pregnant Walkin", gender="female", pregnancy_status="pregnant"
            )
        )
        order = [v.id for v in sort_queue([young, senior, pregnant])]
        self.assertEqual(order, [senior.id, pregnant.id, young.id])

        url = reverse("opd-station-call-next", args=[self.station.external_id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["id"], str(senior.external_id))
        self.assertIn("senior", response.data["priority"])
        # One at a time.
        self.assertEqual(self.client.post(url).status_code, 400)

        queue = self.client.get(
            reverse("opd-station-queue", args=[self.station.external_id])
        )
        self.assertEqual(queue.data["current"]["id"], str(senior.external_id))
        self.assertEqual(
            [v["id"] for v in queue.data["waiting"]],
            [str(pregnant.external_id), str(young.external_id)],
        )

    def test_station_admin(self):
        response = self.client.post(
            reverse("opd-stations"),
            {"facility": str(self.facility.external_id), "code": "v1", "name": "Dup"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            reverse("opd-stations"),
            {
                "facility": str(self.facility.external_id),
                "code": " v3 ",
                "name": "Station 3",
                "doctors": [str(self.doctor.external_id)],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["code"], "V3")
        nurse = self.client.put(
            reverse("opd-station-nurse", args=[response.data["id"]]),
            {"nurse": str(self.user.external_id), "nurse_status": "lunch"},
            format="json",
        )
        self.assertEqual(nurse.data["nurse_status"], "lunch")

    # --- Saving -------------------------------------------------------------

    def test_save_creates_care_observations_with_flags(self):
        visit = self.visit()
        response = self.save(visit, full_results(bp=((150, 95), (190, 100)), spo2=91))
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            response.data["bp_average"], {"systolic": 170, "diastolic": 98}
        )
        flagged = {(f["vital"], f["code"]) for f in response.data["flags"]}
        self.assertIn(("systolic", "HH"), flagged)
        self.assertIn(("diastolic", "H"), flagged)
        self.assertEqual(response.data["visit"]["stage"], OpdStage.waiting_doctor)

        observations = Observation.objects.filter(encounter=visit.encounter)
        codes = [o.main_code["code"] for o in observations]
        self.assertEqual(codes.count("85354-9"), 2)  # one panel per reading
        for code in ("8867-4", "59408-5", "39156-5", "8478-0", "8277-6"):
            self.assertIn(code, codes)
        bmi = observations.get(main_code__code="39156-5")
        self.assertEqual(bmi.value["value"], "24.2")
        self.assertEqual(bmi.interpretation["code"]["code"], "N")
        self.assertEqual(bmi.category["code"], "vital-signs")

    def test_boundary_value_gets_higher_band(self):
        response = self.save(self.visit(), full_results(bp=((140, 90),)))
        self.assertEqual(response.status_code, 201, response.data)
        flagged = {(f["vital"], f["code"]) for f in response.data["flags"]}
        self.assertEqual(flagged, {("systolic", "H"), ("diastolic", "H"), ("map", "H")})

    def test_limits_and_required(self):
        visit = self.visit()
        results = full_results(bp=((80, 90),), pulse=400, spo2=None)
        response = self.save(visit, results)
        self.assertEqual(response.status_code, 400)
        locs = self.error_locs(response)
        self.assertIn(("bp", "0", "diastolic"), locs)
        self.assertNotIn(("bp",), locs)
        self.assertIn(("pulse",), locs)
        self.assertIn(("spo2",), locs)
        self.assertFalse(Observation.objects.filter(encounter=visit.encounter).exists())

    def test_not_measured_needs_reason(self):
        visit = self.visit()
        response = self.save(
            visit, full_results(height=None), not_measured={"height": " "}
        )
        self.assertEqual(self.error_locs(response), [("not_measured", "height")])
        response = self.save(
            visit, full_results(height=None), not_measured={"height": "Wheelchair"}
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["not_measured"], {"height": "Wheelchair"})
        self.assertNotIn("bmi", response.data["readings"])

    def test_resend_is_idempotent(self):
        visit = self.visit()
        body = {
            "visit": str(visit.external_id),
            "submission_key": str(uuid.uuid4()),
            "results": full_results(),
        }
        first = self.client.post(reverse("opd-vitals"), body, format="json")
        again = self.client.post(reverse("opd-vitals"), body, format="json")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(first.data["id"], again.data["id"])
        self.assertEqual(OpdVitalsRecord.objects.count(), 1)

    def test_second_save_must_be_a_correction(self):
        visit = self.visit()
        first = self.save(visit)
        self.assertEqual(self.save(visit).status_code, 400)
        corrected = self.save(visit, full_results(pulse=80), replaces=first.data["id"])
        self.assertEqual(corrected.status_code, 201, corrected.data)
        old = OpdVitalsRecord.objects.get(external_id=first.data["id"])
        self.assertEqual(old.status, "corrected")
        old_obs = Observation.objects.filter(
            questionnaire_response=old.questionnaire_response
        )
        self.assertTrue(all(o.status == "entered_in_error" for o in old_obs))
        # A correction doesn't move the visit or call anyone.
        self.assertEqual(corrected.data["visit"]["stage"], OpdStage.waiting_doctor)
        self.assertIsNone(corrected.data["next"])

    def test_save_calls_next_patient(self):
        first, second = self.visit(), self.visit()
        self.client.post(
            reverse("opd-station-call-next", args=[self.station.external_id])
        )
        response = self.save(first)
        self.assertEqual(response.data["next"]["id"], str(second.external_id))
        second.refresh_from_db()
        self.assertEqual(second.stage, OpdStage.at_vitals)

    def test_needs_encounter_permission(self):
        visit = self.visit()
        self.client.force_authenticate(user=self.create_user())
        self.assertEqual(self.save(visit).status_code, 403)
        self.assertEqual(
            self.client.get(
                reverse("opd-station-queue", args=[self.station.external_id])
            ).status_code,
            403,
        )
