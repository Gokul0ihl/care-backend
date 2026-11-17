"""Nightly job: remove role assignments whose expires_at has passed."""

import logging

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from care.emr.models.organization import FacilityOrganizationUser, OrganizationUser
from care.security.access_audit import assignment_value, record_access_event
from care.security.models import AccessAuditAction

logger = logging.getLogger(__name__)


def is_last_root_member(assignment):
    organization = assignment.organization
    if getattr(organization, "org_type", None) != "root":
        return False
    return (
        not type(assignment)
        .objects.filter(organization=organization)
        .exclude(id=assignment.id)
        .exists()
    )


def expire_role_assignments_now():
    """Removes expired assignments and records each one. Returns how many were removed."""
    now = timezone.now()
    removed = 0
    for model in (FacilityOrganizationUser, OrganizationUser):
        expired = model.objects.filter(
            expires_at__isnull=False, expires_at__lte=now
        ).select_related("organization", "role", "user")
        for assignment in expired:
            if model is FacilityOrganizationUser and is_last_root_member(assignment):
                # Removing it would leave the facility with nobody in charge.
                logger.warning(
                    "Not expiring %s: last member of the facility root", assignment.id
                )
                continue
            with transaction.atomic():
                assignment.deleted = True
                assignment.save(update_fields=["deleted", "modified_date"])
                record_access_event(
                    AccessAuditAction.role_expired,
                    None,
                    target_user=assignment.user,
                    role=assignment.role,
                    scope=assignment.organization,
                    old_value=assignment_value(assignment),
                )
            removed += 1
    return removed


@shared_task
def expire_role_assignments():
    removed = expire_role_assignments_now()
    logger.info("Expired %s role assignments", removed)
    return removed


def deactivate_inactive_users_now():
    """
    Deactivates accounts with no sign-in for the policy's number of days.
    Accounts that never signed in count from when they were created.
    Super admins and service accounts are left alone.
    """
    from datetime import timedelta

    from django.db.models import Q

    from care.security.models import SecurityPolicy
    from care.users.models import User

    days = SecurityPolicy.get().auto_deactivate_after_days
    if not days:
        return 0
    cutoff = timezone.now() - timedelta(days=days)
    stale = User.objects.filter(
        is_active=True, is_superuser=False, is_service_account=False
    ).filter(
        Q(last_login__lt=cutoff) | Q(last_login__isnull=True, date_joined__lt=cutoff)
    )
    count = 0
    for user in stale:
        with transaction.atomic():
            user.is_active = False
            user.tokens_valid_after = timezone.now()
            user.save(update_fields=["is_active", "tokens_valid_after"])
            record_access_event(
                AccessAuditAction.user_deactivated,
                None,
                target_user=user,
                old_value={"is_active": True},
                new_value={"is_active": False},
                reason=f"No sign-in for {days} days (security policy)",
            )
        count += 1
    return count


@shared_task
def deactivate_inactive_users():
    count = deactivate_inactive_users_now()
    logger.info("Deactivated %s inactive users", count)
    return count
