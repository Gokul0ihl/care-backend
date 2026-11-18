"""VIP protocol (D29, R31): a VIP's name is never shown on a list, screen or
slip. Every Care response that carries the patient shows their VIP code
(VIP-K7M3QA) instead, through Care's patient serialization hook; phone,
date of birth and the UHID stay, so the desk can still find and check them.

Only a user whose role has the can_reveal_vip_name permission can see the
name, by revealing it on purpose (VipRevealView); each reveal is logged
(VipReveal). No system role has the permission and superusers don't bypass
it: it is granted through a custom role. The registration form's edit view
reveals too, since it needs the name parts, so editing a VIP's record needs
the permission as well.

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


VIP_REVEAL_PERMISSION = "can_reveal_vip_name"
VIP_REVEAL_DENIED = "Only a user with VIP access can see a VIP's name"


def can_reveal_vip(user):
    """Whether any role the user holds (in a facility organization or an
    organization) has can_reveal_vip_name. Deliberately no superuser bypass."""
    from care.emr.models.organization import (
        FacilityOrganizationUser,
        OrganizationUser,
    )
    from care.security.models import RolePermission

    if user is None or not getattr(user, "is_authenticated", False):
        return False
    roles = RolePermission.objects.filter(
        permission__slug=VIP_REVEAL_PERMISSION
    ).values("role_id")
    return (
        FacilityOrganizationUser.objects.filter(user=user, role_id__in=roles).exists()
        or OrganizationUser.objects.filter(user=user, role_id__in=roles).exists()
    )


def log_reveal(patient, user, source):
    from hmis.models import VipReveal

    VipReveal.objects.create(patient=patient, user=user, source=source)
