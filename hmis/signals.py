from care.emr.models import Encounter, FacilityOrganization, TokenBooking
from care.emr.models.charge_item import ChargeItem
from care.emr.models.invoice import Invoice
from care.emr.models.payment_reconciliation import PaymentReconciliation
from care.emr.resources.scheduling.slot.appointment import CONSULTATION_ENCOUNTER_CLASS
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from hmis.constants import PatientNumberSeries
from hmis.app_server import grant_new_facility
from hmis.billing_rules import (
    check_charge_item,
    check_payment,
    number_invoice,
)
from hmis.services import issue_patient_number, patient_number


@receiver(pre_save, sender=Encounter, dispatch_uid="hmis_checkin_class")
def copy_consultation_type(sender, instance, **kwargs):
    """At check-in, an outpatient encounter created from a booking takes its
    class from the booking's consultation type (video → vr, home visit → hh).
    Any other class the desk chose, such as an admission, is kept."""
    if not instance._state.adding or not instance.appointment_id:
        return
    if instance.encounter_class != "amb":
        return
    consultation_type = (
        TokenBooking.objects.filter(id=instance.appointment_id)
        .values_list("consultation_type", flat=True)
        .first()
    )
    if consultation_type in CONSULTATION_ENCOUNTER_CLASS:
        instance.encounter_class = CONSULTATION_ENCOUNTER_CLASS[consultation_type]


@receiver(post_save, sender=Encounter, dispatch_uid="hmis_issue_uhid")
def issue_uhid_on_arrival(sender, instance, created, **kwargs):
    """A patient without a UHID gets one at their first encounter in a unit
    with a code: pre-registrations convert on arrival, and patients registered
    outside the desk are covered too. Unknown patients keep their UNK number
    until registration is completed (ConvertView)."""
    if not created:
        return
    if not instance.facility.unit_code:
        return
    patient = instance.patient
    if patient_number(patient, PatientNumberSeries.unknown):
        return
    issue_patient_number(patient, instance.facility)


@receiver(pre_save, sender=ChargeItem, dispatch_uid="hmis_charge_rules")
def apply_charge_rules(sender, instance, **kwargs):
    """Quantity, once-in-a-lifetime registration, free follow-up and SAC on
    POH catalogue lines (billing_rules)."""
    check_charge_item(instance)


@receiver(pre_save, sender=Invoice, dispatch_uid="hmis_invoice_number")
def apply_invoice_number(sender, instance, **kwargs):
    number_invoice(instance)


@receiver(
    pre_save, sender=PaymentReconciliation, dispatch_uid="hmis_payment_rules"
)
def apply_payment_rules(sender, instance, **kwargs):
    """Whole-rupee amounts, advance purpose, refund cause and approval, and the
    receipt number (billing_rules)."""
    check_payment(instance)


@receiver(
    post_save, sender=FacilityOrganization, dispatch_uid="hmis_app_server"
)
def grant_app_server(sender, instance, created, **kwargs):
    """The app server books at every facility, new ones included."""
    if created and instance.org_type == "root" and instance.system_generated:
        grant_new_facility(instance)
