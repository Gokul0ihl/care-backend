"""VIP protocol (D29, R31).

VIP access is the role permission can_reveal_vip_name. No system role has it
and superusers don't bypass it: it is granted through a custom role.

Staff without VIP access can't see VIP patients at all. A VIP is left out of
every list and search, and opening one is refused (hides_vips,
exclude_hidden_vips, is_hidden_vip). The layers that do it:
- Care's EMR viewsets drop hidden VIPs from every list and lookup, for the
  Patient model and any model with a patient (care/emr/api/viewsets/base.py);
- Care's patient permission checks (can_view_patient_obj and the rest,
  care/security/authorization/patient.py) say no for a hidden VIP;
- the hmis views and Care actions that build their own patient querysets
  call exclude_hidden_vips.
The duplicate check at registration doesn't show a VIP match either.

Staff with VIP access see a VIP shown as their VIP code (VIP-K7M3QA) instead
of the name, through Care's patient serialization hook, and can reveal the
name on purpose (VipRevealView); each reveal is logged (VipReveal). The
registration form's edit view reveals too, since it needs the name parts.

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


def _vip_access(user):
    """can_reveal_vip, remembered for the request, so a list checks it once."""
    from care.emr.resources.patient.hooks import current_request

    request = current_request()
    if request is None:
        return can_reveal_vip(user)
    cache = request.__dict__.setdefault("_hmis_vip_access", {})
    if user.pk not in cache:
        cache[user.pk] = can_reveal_vip(user)
    return cache[user.pk]


def hides_vips(user):
    """Whether VIP patients are hidden from this user: a staff user without
    VIP access. Outside a request (tasks, commands, exports) and for the
    mobile app's patient session nothing is hidden."""
    return user is not None and _is_staff(user) and not _vip_access(user)


def is_hidden_vip(user, patient):
    return bool(patient is not None and patient.is_vip) and hides_vips(user)


def exclude_hidden_vips(queryset, user, path=""):
    """The queryset without the VIPs this user may not see. path leads from
    the queryset's model to the patient, e.g. "patient__" or
    "encounter__patient__"; empty for a Patient queryset."""
    if not hides_vips(user):
        return queryset
    return queryset.exclude(**{f"{path}is_vip": True})


def _patient_of(obj):
    """The patient a model instance is, or belongs to (obj.patient)."""
    from django.db import models

    from care.emr.models import Patient

    if isinstance(obj, Patient):
        return obj
    if isinstance(obj, models.Model) and vip_path(type(obj)) == "patient__":
        return obj.patient
    return None


def concerns_hidden_vip(user, objects):
    """Whether a permission check by this user is about a VIP they may not
    see: one of the objects is that patient or belongs to them. Care's
    AuthorizationController.call refuses such checks."""
    if not hides_vips(user):
        return False
    return any(
        (patient := _patient_of(obj)) is not None and patient.is_vip for obj in objects
    )


def vip_path(model):
    """How to reach the patient from a model, for exclude_hidden_vips: "" for
    Patient, "patient__" for a model with a patient, None otherwise."""
    from care.emr.models import Patient

    if model is Patient or (isinstance(model, type) and issubclass(model, Patient)):
        return ""
    try:
        field = model._meta.get_field("patient")  # noqa: SLF001
    except Exception:
        return None
    if getattr(field, "related_model", None) is Patient:
        return "patient__"
    return None


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


def _is_staff(user):
    """Masking is for hospital staff screens. The mobile app's patient session
    (not a Care user) reads its own name through its own serializers."""
    from care.users.models import User

    return isinstance(user, User)


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
    from django.db.models import Q

    held = Q(
        role_id__in=FacilityOrganizationUser.objects.filter(user=user).values("role_id")
    ) | Q(role_id__in=OrganizationUser.objects.filter(user=user).values("role_id"))
    # One query, as it runs on every staff request that lists patients.
    return RolePermission.objects.filter(
        held, permission__slug=VIP_REVEAL_PERMISSION
    ).exists()


def log_reveal(patient, user, source):
    from hmis.models import VipReveal

    VipReveal.objects.create(patient=patient, user=user, source=source)
