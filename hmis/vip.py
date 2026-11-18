"""VIP protocol (D29, R31): a VIP's name is never shown on a list, screen or
slip. Every Care response that carries the patient shows their VIP code
(VIP-K7M3QA) instead, through Care's patient serialization hook; phone,
date of birth and the UHID stay, so the desk can still find and check them.

Any staff member can see the name by revealing it on purpose
(VipRevealView), and each reveal is logged (VipReveal). The registration
form's edit view reveals too, since it needs the name parts.

A restricted patient (hmis.restricted) is already shown as their alias, which
wins: the VIP code would add nothing.
"""

import hashlib

from hmis.constants import ALIAS_ALPHABET

VIP_CODE_LENGTH = 6


def vip_code(patient):
    """A stable code from the patient's id, so it needs no column and stays the
    same on every screen and slip. It can't be turned back into the name."""
    digest = hashlib.sha256(f"vip:{patient.external_id}".encode()).digest()
    return "VIP-" + "".join(
        ALIAS_ALPHABET[byte % len(ALIAS_ALPHABET)] for byte in digest[:VIP_CODE_LENGTH]
    )


def is_masked_vip(patient):
    return bool(patient.is_vip) and patient.restricted_alias is None


def _is_staff(user):
    """Masking is for hospital staff screens. The mobile app's patient session
    (not a Care user) reads its own name through its own serializers."""
    from care.users.models import User

    return isinstance(user, User)


def mask_vip(mapping, patient, user):
    """Care patient serialization hook. Outside a request (tasks, commands,
    ABDM exports) nothing is masked."""
    if user is None or not _is_staff(user) or not is_masked_vip(patient):
        return
    mapping["name"] = vip_code(patient)
    mapping["vip"] = True


def vip_mapping(mapping, patient, user):
    """mask_vip for the hmis serializers that build their own patient
    mapping (OPD queue, approvals, duplicates, emergency)."""
    mask_vip(mapping, patient, user)
    return mapping


def log_reveal(patient, user, source):
    from hmis.models import VipReveal

    VipReveal.objects.create(patient=patient, user=user, source=source)
