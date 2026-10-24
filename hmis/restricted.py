"""Statutory confidentiality (v12.1 D30, Zones sheet "Alias").

Once a clinician raises a statutory flag, the patient is shown as their alias
(PT-K7M3QA, Patient.restricted_alias) in every Care response that carries the
patient, through Care's patient serialization hook. Only the clinicians who raised a flag and users
with an unexpired break-glass access see the identity.
"""

import secrets

from django.utils import timezone

from care.emr.models import Patient
from hmis.constants import ALIAS_ALPHABET
from hmis.models import BreakGlassAccess, StatutoryFlag

MASKED = "Restricted"

# Identity and contact fields of Care's patient that are hidden.
MASKED_PATIENT_FIELDS = {
    "phone_number": MASKED,
    "emergency_phone_number": None,
    "address": MASKED,
    "permanent_address": None,
    "pincode": None,
    "date_of_birth": None,
    "year_of_birth": None,
    "registration": None,
}


def new_alias():
    while True:
        alias = "PT-" + "".join(secrets.choice(ALIAS_ALPHABET) for _ in range(6))
        # Soft-deleted patients included: the column is unique across all rows.
        if not Patient._base_manager.filter(  # noqa: SLF001
            restricted_alias=alias
        ).exists():
            return alias


def is_restricted(patient):
    return patient.restricted_alias is not None


def can_see_identity(user, patient):
    """The flagging clinicians and anyone with live break-glass access."""
    if user is None:
        return False
    if StatutoryFlag.objects.filter(patient=patient, created_by=user).exists():
        return True
    return BreakGlassAccess.objects.filter(
        patient=patient, user=user, expires_at__gt=timezone.now()
    ).exists()


def _request_cache():
    """Per-request memo of can_see_identity, so a patient list checks each
    restricted patient once."""
    from care.emr.resources.patient.hooks import current_request

    request = current_request()
    if request is None:
        return {}
    if not hasattr(request, "_hmis_restricted"):
        request._hmis_restricted = {}  # noqa: SLF001
    return request._hmis_restricted  # noqa: SLF001


def mask_patient(mapping, patient, user):
    """Care patient serialization hook. Outside a request (tasks, commands)
    nothing is masked, so reminders and exports keep working."""
    if user is None:
        return
    alias = patient.restricted_alias
    if alias is None:
        return
    cache = _request_cache()
    cache.setdefault("visible", {})
    if patient.id not in cache["visible"]:
        cache["visible"][patient.id] = can_see_identity(user, patient)
    if cache["visible"][patient.id]:
        return
    mapping["name"] = alias
    for field, value in MASKED_PATIENT_FIELDS.items():
        if field in mapping:
            mapping[field] = value
