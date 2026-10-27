from datetime import date
from decimal import Decimal
from io import StringIO

from django.core.management import call_command

from care.emr.models.charge_item_definition import ChargeItemDefinition
from care.emr.models.facility_config import FacilityMonetoryConfig
from care.emr.models.tag_config import TagConfig
from care.emr.resources.charge_item.apply_charge_item_definition import (
    apply_charge_item_definition,
)
from care.emr.resources.invoice.default_expression_evaluator import (
    evaluate_invoice_identifier_default_expression,
)
from care.utils.tests.base import CareAPITestBase
from care.utils.time_util import care_now
from hmis.billing import (
    CATALOGUE,
    PAYER_TAGS,
    setup_billing,
    slug_for,
    sync_billing_tags,
)


class BillingSetupTests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        self.user = self.create_super_user()
        self.facility = self.create_facility(user=self.user)
        self.facility.unit_code = "LN"
        self.facility.save(update_fields=["unit_code"])
        setup_billing(self.facility)

    def definition(self, code):
        slug = ChargeItemDefinition.calculate_slug_from_facility(
            self.facility.external_id, slug_for(code)
        )
        return ChargeItemDefinition.objects.get(slug=slug)

    def register(self, payer_type="Self", payer_name="Self-pay", **kwargs):
        patient = self.create_patient(
            date_of_birth=kwargs.pop("date_of_birth", date(1990, 1, 1)),
            facility_tags={},
        )
        # A desk registration, as the desk views write it.
        patient.registration_facility = self.facility
        patient.registered_at = care_now()
        patient.registration_type = kwargs.pop("registration_type", "DOM")
        patient.payer_type = payer_type
        patient.payer_name = payer_name
        patient.concession = kwargs.pop("concession", "")
        patient.concessions = kwargs.pop("concessions", [])
        patient.save()
        sync_billing_tags(patient)
        patient.refresh_from_db()
        return patient

    def price(self, code, patient):
        item = apply_charge_item_definition(
            self.definition(code), patient, self.facility
        )
        return item.total_price

    def test_rerun_updates_in_place(self):
        out = StringIO()
        call_command(
            "setup_billing", facility=str(self.facility.external_id), stdout=out
        )
        self.assertIn(f"{len(CATALOGUE)} charge item definitions", out.getvalue())
        self.assertEqual(
            ChargeItemDefinition.objects.filter(facility=self.facility).count(),
            len(CATALOGUE),
        )
        self.assertEqual(
            TagConfig.objects.filter(
                facility=self.facility, resource="patient"
            ).count(),
            12,
        )
        config = FacilityMonetoryConfig.get_monetory_config(self.facility.id)
        codes = [code["code"] for code in config.discount_codes]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertIn("coupon_firstvisit", codes)

    def test_invoice_number(self):
        yy = care_now().year % 100
        self.assertEqual(
            evaluate_invoice_identifier_default_expression(self.facility),
            f"INV/LN/{yy}/00001",
        )

    def test_standard_and_payer_tariffs(self):
        self.assertEqual(self.price("CON2", self.register()), Decimal(900))
        self.assertEqual(
            self.price("CON2", self.register("Insurance/TPA", "Star Health")),
            Decimal(900),
        )
        self.assertEqual(
            self.price("CON2", self.register("Corporate", "Infosys")), Decimal(765)
        )
        self.assertEqual(
            self.price("CON2", self.register("Govt scheme", "CGHS")), Decimal(540)
        )
        self.assertEqual(
            self.price("CON2", self.register("International", "Bupa Global")),
            Decimal(1710),
        )
        # An international journey on a domestic payer is still ×1.9.
        self.assertEqual(
            self.price(
                "CON2",
                self.register("Insurance/TPA", "Star Health", registration_type="NRI"),
            ),
            Decimal(1710),
        )

    def test_staff_concessions(self):
        staff = self.register("Staff", "POH staff — self")
        self.assertEqual(self.price("CON2", staff), Decimal(0))
        self.assertEqual(self.price("LIP", staff), Decimal("237.5"))
        dependant = self.register("Staff", "POH staff — dependant")
        self.assertEqual(self.price("CON2", dependant), Decimal(225))

    def test_direct_concession_follows_the_desk_choice(self):
        senior = self.register(
            date_of_birth=date(1950, 1, 1), concession="Senior citizen — 15%"
        )
        self.assertEqual(self.price("CON2", senior), Decimal(765))
        not_chosen = self.register(date_of_birth=date(1950, 1, 1))
        self.assertEqual(self.price("CON2", not_chosen), Decimal(900))
        ews = self.register(concession="EWS / charity")
        self.assertEqual(self.price("CON2", ews), Decimal(900))

    def test_multiple_concessions_apply_only_the_highest_eligible_discount(self):
        patient = self.register(
            date_of_birth=date(1950, 1, 1),
            concessions=[
                "Defence veteran — 10%",
                "Senior citizen — 15%",
                "Freedom fighter — 25%",
            ],
        )
        self.assertEqual(self.price("CON2", patient), Decimal(675))
        patient.concessions = ["Senior citizen — 15%", "EWS / charity"]
        patient.save()
        sync_billing_tags(patient)
        self.assertEqual(self.price("CON2", patient), Decimal(765))
        # Even an invalid persisted age-based selection cannot discount a younger patient.
        patient.date_of_birth = date(2000, 1, 1)
        patient.save()
        sync_billing_tags(patient)
        self.assertEqual(self.price("CON2", patient), Decimal(900))
        patient.concessions = []
        patient.concession = ""
        patient.save()
        sync_billing_tags(patient)
        self.assertEqual(self.price("CON2", patient), Decimal(900))

    def test_patient_not_registered_at_the_desk_is_not_tagged(self):
        patient = self.create_patient(facility_tags={})
        patient.registration_facility = self.facility
        patient.payer_type = "Corporate"
        patient.save()
        sync_billing_tags(patient)
        patient.refresh_from_db()
        self.assertEqual(patient.facility_tags, {})

    def test_payer_change_replaces_the_tag_and_keeps_others(self):
        patient = self.register("Corporate", "Infosys")
        other = TagConfig.objects.create(
            facility=self.facility,
            resource="patient",
            display="VIP",
            status="active",
            category="admin",
        )
        key = str(self.facility.id)
        patient.facility_tags = {key: [*patient.facility_tags[key], other.id]}
        patient.save(update_fields=["facility_tags"])
        patient.payer_type = "Govt scheme"
        patient.payer_name = "CGHS"
        patient.save(update_fields=["payer_type", "payer_name"])
        sync_billing_tags(patient)
        patient.refresh_from_db()
        displays = set(
            TagConfig.objects.filter(id__in=patient.facility_tags[key]).values_list(
                "display", flat=True
            )
        )
        self.assertEqual(displays, {"VIP", PAYER_TAGS["scheme"]})
        self.assertEqual(self.price("CON2", patient), Decimal(540))
