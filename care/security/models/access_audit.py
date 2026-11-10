from django.db import models

from care.utils.models.base import BaseModel


class AccessAuditAction(models.TextChoices):
    user_created = "user_created"
    user_deleted = "user_deleted"
    user_activated = "user_activated"
    user_deactivated = "user_deactivated"
    superuser_granted = "superuser_granted"
    superuser_revoked = "superuser_revoked"
    mfa_reset = "mfa_reset"
    password_set = "password_set"
    sessions_revoked = "sessions_revoked"
    role_created = "role_created"
    role_updated = "role_updated"
    role_deleted = "role_deleted"
    role_assigned = "role_assigned"
    role_changed = "role_changed"
    role_revoked = "role_revoked"
    role_expired = "role_expired"
    request_created = "request_created"
    request_approved = "request_approved"
    request_rejected = "request_rejected"
    request_cancelled = "request_cancelled"
    policy_updated = "policy_updated"
    roles_bulk_assigned = "roles_bulk_assigned"
    users_imported = "users_imported"


class AccessAuditScope(models.TextChoices):
    user = "user"
    role = "role"
    organization = "organization"
    facility_organization = "facility_organization"


class AccessAuditEvent(BaseModel):
    """
    One change to who can do what: a user's account, a role's permissions, or
    a role given to or taken from a user. The access portal's Audit Log reads
    these rows. Names are copied in so the row still reads correctly after the
    user, role or department is deleted.
    """

    action = models.CharField(
        max_length=32, choices=AccessAuditAction.choices, db_index=True
    )
    actor = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    actor_username = models.CharField(max_length=255, blank=True, default="")
    target_user = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    target_username = models.CharField(max_length=255, blank=True, default="")
    role = models.ForeignKey(
        "security.RoleModel",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    role_name = models.CharField(max_length=1024, blank=True, default="")
    scope_type = models.CharField(
        max_length=32, choices=AccessAuditScope.choices, blank=True, default=""
    )
    scope_external_id = models.UUIDField(null=True, blank=True)
    scope_name = models.CharField(max_length=1024, blank=True, default="")
    facility = models.ForeignKey(
        "facility.Facility",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    old_value = models.JSONField(default=dict, blank=True)
    new_value = models.JSONField(default=dict, blank=True)
    reason = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_date"]
