"""A payer or concession change applies from the current visit onwards
(billing.md §2).

Care copies a line's price components onto the charge item when it is raised,
so a line keeps the payer it was raised under. That is what we want for past
bills: a line that is on an invoice (billed or paid) is never touched, and
neither is a line from a visit that is already over. But a line raised for this
visit or a later one and not yet on a bill (an appointment fee charged at
booking, an order raised before the desk corrected the payer) would otherwise
be billed at the old payer. reprice_open_lines() re-prices those at the
patient's current tags. sync_billing_tags() calls it whenever it changes them."""

from django.db import transaction
from django.utils import timezone

from care.emr.models import Encounter, TokenBooking
from care.emr.models.charge_item import ChargeItem
from care.emr.resources.account.sync_items import rebalance_account_task
from care.emr.resources.charge_item.apply_charge_item_definition import (
    apply_charge_item_definition,
)
from care.emr.resources.charge_item.spec import (
    ChargeItemResourceOptions,
    ChargeItemStatusOptions,
)
from care.emr.resources.charge_item.sync_charge_item_costs import (
    sync_charge_item_costs,
)
from care.emr.resources.encounter.constants import COMPLETED_CHOICES, StatusChoices
from hmis.billing import CONCESSION_COMPONENTS, H1_STAFF_SELF, TARIFF_COMPONENTS
from hmis.billing_rules import catalogue_code, grant_entitlement

# The price components that follow the patient's payer and concession tags.
# Everything else on a line (coupons, discretionary discounts, entitlements)
# was chosen for that line and is kept.
TAG_CODES = (
    {code for code, *_ in TARIFF_COMPONENTS}
    | {code for code, *_ in CONCESSION_COMPONENTS}
    | {H1_STAFF_SELF[0]}
)
CLOSED_VISIT = {*COMPLETED_CHOICES, StatusChoices.discharged.value}


def component_code(component):
    return (component.get("code") or {}).get("code")


def belongs_to_open_visit(item, today):
    """The line is for the current visit or a later one. A line on an open
    encounter is; a line on a finished encounter isn't. An appointment line
    counts from the slot's day. Any other line counts only on the day it was
    raised."""
    if item.encounter_id:
        status = (
            Encounter.objects.filter(id=item.encounter_id)
            .values_list("status", flat=True)
            .first()
        )
        return status not in CLOSED_VISIT
    if item.service_resource == ChargeItemResourceOptions.appointment.value:
        start = (
            TokenBooking.objects.filter(external_id=item.service_resource_id)
            .values_list("token_slot__start_datetime", flat=True)
            .first()
        )
        if start is not None:
            return timezone.localdate(start) >= today
    return timezone.localdate(item.created_date) >= today


def open_lines(patient, facility):
    """The patient's POH catalogue lines at the facility that aren't on a bill
    yet and belong to this visit or a later one. A line whose price someone
    overrode by hand keeps it."""
    today = timezone.localdate()
    items = ChargeItem.objects.filter(
        patient=patient,
        facility=facility,
        status=ChargeItemStatusOptions.billable.value,
        paid_invoice__isnull=True,
        override_reason__isnull=True,
    ).select_related("charge_item_definition", "encounter")
    return [
        item
        for item in items
        if catalogue_code(item) is not None and belongs_to_open_visit(item, today)
    ]


def reprice(item):
    """Swap the line's payer and concession components for the ones the
    patient's current tags earn, keeping the rest of what's on it."""
    fresh = apply_charge_item_definition(
        item.charge_item_definition,
        item.patient,
        item.facility,
        encounter=item.encounter,
        account=item.account,
        quantity=item.quantity,
    ).unit_price_components
    fresh_codes = {component_code(c) for c in fresh}
    kept = [
        component
        for component in item.unit_price_components or []
        if component.get("monetary_component_type") != "base"
        and component_code(component) not in TAG_CODES
        and component_code(component) not in fresh_codes
    ]
    item.unit_price_components = fresh + kept
    entitlement = next(
        (
            component_code(c)
            for c in kept
            if (component_code(c) or "").startswith("entitlement_")
        ),
        None,
    )
    if entitlement:
        # An entitlement replaces any concession (billing.md §2).
        grant_entitlement(item, entitlement)
    else:
        sync_charge_item_costs(item)


def reprice_open_lines(patient, facility):
    """Re-price the open lines after a payer or concession change. Returns how
    many lines changed price."""
    changed = 0
    accounts = set()
    with transaction.atomic():
        for item in open_lines(patient, facility):
            before = item.total_price
            reprice(item)
            item.save()
            if item.total_price != before:
                changed += 1
            accounts.add(item.account_id)
    for account_id in accounts:
        rebalance_account_task(account_id)
    return changed
