from django.contrib.postgres.fields import ArrayField
from django.db import models

from care.utils.models.base import BaseModel


class SecurityPolicy(BaseModel):
    """
    Organisation-wide access rules, set on the access portal's Security
    Policies page. There is one row; use SecurityPolicy.get().
    """

    # Two-factor sign-in
    mfa_required_for_superusers = models.BooleanField(default=False)
    # Role external ids whose holders must set up two-factor sign-in.
    mfa_required_role_ids = ArrayField(models.UUIDField(), default=list, blank=True)

    # Accounts and sessions
    # Deactivate accounts with no sign-in for this many days (nightly). None = off.
    auto_deactivate_after_days = models.PositiveIntegerField(null=True, blank=True)
    # Make people sign in again after this many hours, however active. None = off.
    max_session_hours = models.PositiveIntegerField(null=True, blank=True)

    # Access governance
    # Only super admins may give high-privilege roles directly; others must
    # send an access request.
    approval_required_for_high_privilege = models.BooleanField(default=False)
    # Roles that get an end date automatically when given without one.
    temporary_role_ids = ArrayField(models.UUIDField(), default=list, blank=True)
    temporary_role_days = models.PositiveIntegerField(default=30)

    updated_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    @classmethod
    def get(cls):
        policy = cls.objects.order_by("id").first()
        return policy or cls.objects.create()
