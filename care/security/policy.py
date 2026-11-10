"""Helpers that apply SecurityPolicy rules where access is given or checked."""

from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from care.security.models import RolePermission, SecurityPolicy

POLICY_CACHE_KEY = "security_policy:v1"
POLICY_CACHE_SECONDS = 60


def current_policy():
    """The policy, cached for a minute because sign-in checks read it on every request."""
    policy = cache.get(POLICY_CACHE_KEY)
    if policy is None:
        policy = SecurityPolicy.get()
        cache.set(POLICY_CACHE_KEY, policy, POLICY_CACHE_SECONDS)
    return policy


def forget_cached_policy():
    cache.delete(POLICY_CACHE_KEY)


# A role carrying any of these lets its holder manage other people's access.
HIGH_PRIVILEGE_PERMISSIONS = {
    "can_manage_facility_organization_users",
    "can_manage_organization_users",
    "can_create_user",
    "can_manage_facility_organization",
    "can_create_facility_organization_root",
    "can_delete_facility_organization",
    "can_manage_organization",
}


def high_privilege_role_ids(role_ids):
    return set(
        RolePermission.objects.filter(
            role_id__in=role_ids, permission__slug__in=HIGH_PRIVILEGE_PERMISSIONS
        ).values_list("role_id", flat=True)
    )


def is_high_privilege(role):
    return bool(high_privilege_role_ids([role.id]))


def default_expiry(role, policy=None):
    """End date a temporary role gets when it is given without one, else None."""
    policy = policy or current_policy()
    if role.external_id in set(policy.temporary_role_ids):
        return timezone.now() + timedelta(days=policy.temporary_role_days)
    return None


def apply_default_expiry(assignment, policy=None):
    if assignment.expires_at is None:
        assignment.expires_at = default_expiry(assignment.role, policy)


def check_direct_grant_allowed(user, role, policy=None):
    """Raises PermissionDenied when policy says this role needs an access request."""
    from rest_framework.exceptions import PermissionDenied

    if user.is_superuser:
        return
    policy = policy or current_policy()
    if policy.approval_required_for_high_privilege and is_high_privilege(role):
        message = f"{role.name} can only be given by a super admin. Send an access request instead."
        raise PermissionDenied(message)


def mfa_setup_required(user, policy=None):
    """Whether policy says this user must have two-factor sign-in and they don't yet."""
    if user.is_mfa_enabled() or user.is_service_account:
        return False
    policy = policy or current_policy()
    if user.is_superuser and policy.mfa_required_for_superusers:
        return True
    if not policy.mfa_required_role_ids:
        return False
    from care.emr.models.organization import FacilityOrganizationUser, OrganizationUser

    role_ids = set(policy.mfa_required_role_ids)
    return (
        FacilityOrganizationUser.objects.filter(
            user=user, role__external_id__in=role_ids
        ).exists()
        or OrganizationUser.objects.filter(
            user=user, role__external_id__in=role_ids
        ).exists()
    )


def session_too_old(token, policy=None):
    """True when the sign-in behind this token is older than max_session_hours."""
    policy = policy or current_policy()
    if not policy.max_session_hours:
        return False
    issued_at = token.get("iat")
    if issued_at is None:
        return False
    age = timezone.now().timestamp() - issued_at
    return age > policy.max_session_hours * 3600
