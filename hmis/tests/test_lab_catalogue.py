from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.urls import reverse
from rest_framework import status

from care.emr.models import ActivityDefinition
from care.emr.models.observation_definition import ObservationDefinition
from care.utils.tests.base import CareAPITestBase
from hmis.billing import setup_billing
from hmis.lab_catalogue import LAB_SEED, seed_lab_catalogue
from hmis.models import LabTestRule

VALIDATE = "care.emr.registries.care_valueset.care_valueset.validate_valueset"
SPECIMEN = {
    "system": "http://terminology.hl7.org/CodeSystem/v2-0487",
    "code": "SER",
    "display": "Serum",
}


def parameter(code, name):
    return {"system": "http://loinc.org", "code": code, "display": name, "name": name}


class LabCatalogueTests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        self.user = self.create_super_user()
        self.facility = self.create_facility(user=self.user)
        self.facility.unit_code = "LN"
        self.facility.save(update_fields=["unit_code"])
        setup_billing(self.facility)
        self.client.force_authenticate(user=self.user)
        self.url = reverse("lab-tests")

    def facility_id(self):
        return str(self.facility.external_id)

    def tariff_slug(self, code):
        tariff = self.client.get(
            reverse("lab-tariff"), {"facility": self.facility_id()}
        ).json()["results"]
        return next(row["slug"] for row in tariff if row["code"] == code)

    def catalogue(self, **params):
        response = self.client.get(self.url, {"facility": self.facility_id(), **params})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["code"]: row for row in response.json()["results"]}

    def body(self, **kwargs):
        return {
            "facility": self.facility_id(),
            "title": "Ferritin",
            "status": "active",
            "charge_item_definition": self.tariff_slug("CRP"),
            "specimen": SPECIMEN,
            "fasting_hours": None,
            "sensitive_report": False,
            "parameters": [parameter("2276-4", "Ferritin")],
            **kwargs,
        }

    def test_seed_adds_tests_once(self):
        out = StringIO()
        call_command("setup_lab_catalogue", facility=self.facility_id(), stdout=out)
        self.assertIn(f"{len(LAB_SEED)} lab tests added", out.getvalue())
        self.assertEqual(seed_lab_catalogue(self.facility), 0)
        rows = self.catalogue(status="active")
        self.assertEqual(len(rows), len(LAB_SEED))
        lipid = rows["LIP"]
        self.assertEqual(lipid["fasting_hours"], 10)
        self.assertEqual(lipid["specimen"]["code"], "SER")
        self.assertEqual(len(lipid["parameters"]), 7)
        self.assertGreater(float(lipid["charge_item_definition"]["price"]), 0)
        self.assertTrue(rows["HIV"]["sensitive_report"])
        self.assertIsNone(rows["CBC"]["fasting_hours"])
        self.assertTrue(rows["PPBS"]["after_meal"])
        self.assertIsNone(rows["PPBS"]["fasting_hours"])
        self.assertFalse(rows["FBS"]["after_meal"])

    def test_startup_seed_only_fills_an_empty_facility(self):
        out = StringIO()
        call_command("setup_lab_catalogue", if_empty=True, stdout=out)
        self.assertIn(f"{len(LAB_SEED)} lab tests added", out.getvalue())
        # An admin retires a test and edits another; startup leaves both alone.
        ActivityDefinition.objects.filter(slug__endswith="poh-lab-cbc").update(
            status="retired"
        )
        ActivityDefinition.objects.filter(slug__endswith="poh-lab-tsh").delete()
        call_command("setup_lab_catalogue", if_empty=True, stdout=StringIO())
        rows = self.catalogue()
        self.assertEqual(len(rows), len(LAB_SEED) - 1)
        self.assertEqual(rows["CBC"]["status"], "retired")
        self.assertNotIn("TSH", rows)

    @mock.patch(VALIDATE)
    def test_startup_seed_skips_a_facility_with_its_own_tests(self, validate):
        self.client.post(self.url, {**self.body(), "code": "FERR"}, format="json")
        self.assertEqual(seed_lab_catalogue(self.facility, only_if_empty=True), 0)
        self.assertEqual(list(self.catalogue()), ["FERR"])

    def test_startup_seed_adds_missing_tariff_rows_without_changing_prices(self):
        from care.emr.models.charge_item_definition import ChargeItemDefinition

        ChargeItemDefinition.objects.filter(slug__endswith="poh-ppbs").delete()
        cbc = ChargeItemDefinition.objects.get(
            facility=self.facility, slug__endswith="poh-cbc"
        )
        cbc.price_components = [{"monetary_component_type": "base", "amount": "999"}]
        cbc.save()
        seed_lab_catalogue(self.facility, only_if_empty=True)
        rows = self.catalogue()
        self.assertEqual(rows["PPBS"]["charge_item_definition"]["code"], "PPBS")
        self.assertEqual(rows["CBC"]["charge_item_definition"]["price"], "999")

    def test_startup_seed_never_fails_without_a_facility(self):
        self.facility.unit_code = None
        self.facility.save(update_fields=["unit_code"])
        out = StringIO()
        call_command("setup_lab_catalogue", if_empty=True, stdout=out)
        self.assertIn("No facility has a unit code yet", out.getvalue())

    @mock.patch(VALIDATE)
    def test_create_and_update(self, validate):
        response = self.client.post(self.url, {**self.body(), "code": "ferr"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.json())
        row = response.json()
        self.assertEqual(row["code"], "FERR")
        self.assertEqual(row["charge_item_definition"]["code"], "CRP")
        self.assertEqual(validate.call_count, 2)

        validate.reset_mock()
        response = self.client.put(
            reverse("lab-test", args=["FERR"]),
            self.body(
                title="Serum ferritin",
                fasting_hours=8,
                sensitive_report=True,
                status="retired",
                parameters=[
                    parameter("2276-4", "Ferritin (serum)"),
                    parameter("2498-4", "Iron"),
                ],
            ),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.json())
        row = response.json()
        self.assertEqual(row["title"], "Serum ferritin")
        self.assertEqual(row["fasting_hours"], 8)
        self.assertTrue(row["sensitive_report"])
        self.assertEqual([p["name"] for p in row["parameters"]], ["Ferritin (serum)", "Iron"])
        # Only the new parameter goes to the terminology server.
        self.assertEqual(validate.call_count, 1)
        self.assertEqual(
            ObservationDefinition.objects.filter(facility=self.facility).count(), 2
        )
        self.assertNotIn("FERR", self.catalogue(status="active"))
        self.assertIn("FERR", self.catalogue())
        activity = ActivityDefinition.objects.get(id=LabTestRule.objects.get().activity_definition_id)
        self.assertEqual(activity.status, "retired")

    @mock.patch(VALIDATE)
    def test_duplicate_code_is_rejected(self, validate):
        self.client.post(self.url, {**self.body(), "code": "FERR"}, format="json")
        response = self.client.post(self.url, {**self.body(), "code": "FERR"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["errors"][0]["loc"], ["code"])

    @mock.patch(VALIDATE)
    def test_invalid_input_is_rejected(self, validate):
        for body in [
            {**self.body(), "code": "bad code"},
            {**self.body(fasting_hours=30), "code": "FERR"},
            {**self.body(fasting_hours=8, after_meal=True), "code": "FERR"},
            {**self.body(parameters=[]), "code": "FERR"},
            {**self.body(charge_item_definition="missing"), "code": "FERR"},
        ]:
            response = self.client.post(self.url, body, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, body)
        validate.side_effect = ValueError("not in the valueset")
        response = self.client.post(self.url, {**self.body(), "code": "FERR"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["errors"][0]["loc"], ["specimen"])

    def test_only_admins_edit(self):
        body = {**self.body(), "code": "FERR"}
        self.client.force_authenticate(user=self.create_user())
        response = self.client.post(self.url, body, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.get(self.url, {"facility": self.facility_id()})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch(VALIDATE)
    def test_after_meal_is_saved(self, validate):
        response = self.client.post(
            self.url, {**self.body(after_meal=True), "code": "PPG"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.json())
        self.assertTrue(response.json()["after_meal"])
        response = self.client.put(
            reverse("lab-test", args=["PPG"]), self.body(after_meal=False), format="json"
        )
        self.assertFalse(response.json()["after_meal"])

    @mock.patch(VALIDATE)
    def test_icon_is_saved(self, validate):
        response = self.client.post(self.url, {**self.body(), "code": "FERR"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.json())
        self.assertEqual(response.json()["icon"], "")
        response = self.client.put(
            reverse("lab-test", args=["FERR"]), self.body(icon="blood"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.json())
        self.assertEqual(response.json()["icon"], "blood")
        response = self.client.put(
            reverse("lab-test", args=["FERR"]), self.body(icon="<svg>"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["errors"][0]["loc"], ["icon"])
