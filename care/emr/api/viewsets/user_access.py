"""Account and access actions on /api/v1/users/{username}/ for super admins.

These back the access portal's user panel: turning an account off and on,
making someone a super admin, clearing two-factor sign-in, signing a user out
everywhere, and showing what a user can do and where.
"""

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from pydantic import BaseModel, Field
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from care.emr.models.organization import FacilityOrganizationUser, OrganizationUser
from care.emr.tasks.totp import send_totp_disabled_email
from care.security.access_audit import record_access_event
from care.security.models import AccessAuditAction, PermissionModel, RolePermission
from care.users.models import User


class SetActiveRequest(BaseModel):
    is_active: bool
    reason: str = Field("", max_length=1000)


class SetSuperuserRequest(BaseModel):
    is_superuser: bool
    reason: str = Field("", max_length=1000)


class ReasonRequest(BaseModel):
    reason: str = Field("", max_length=1000)


class SetPasswordRequest(BaseModel):
    password: str = Field(max_length=128)
    reason: str = Field("", max_length=1000)


def _role_json(role):
    return {"id": str(role.external_id), "name": role.name}


def _facility_json(facility):
    if facility is None:
        return None
    return {"id": str(facility.external_id), "name": facility.name}


def user_role_assignments(user, facility_external_id=None):
    """Every role the user holds: organization level and facility department."""
    assignments = []
    if facility_external_id is None:
        org_users = OrganizationUser.objects.filter(user=user).select_related(
            "organization", "role", "created_by"
        )
        for org_user in org_users:
            organization = org_user.organization
            assignments.append(
                {
                    "id": str(org_user.external_id),
                    "scope_type": "organization",
                    "organization": {
                        "id": str(organization.external_id),
                        "name": organization.name,
                        "org_type": organization.org_type,
                    },
                    "facility": None,
                    "role": _role_json(org_user.role),
                    "assigned_by": getattr(org_user.created_by, "username", None),
                    "expires_at": org_user.expires_at,
                    "created_date": org_user.created_date,
                }
            )
    facility_users = FacilityOrganizationUser.objects.filter(
        user=user, organization__facility__deleted=False
    ).select_related("organization", "organization__facility", "role", "created_by")
    if facility_external_id is not None:
        facility_users = facility_users.filter(
            organization__facility__external_id=facility_external_id
        )
    for facility_user in facility_users:
        organization = facility_user.organization
        assignments.append(
            {
                "id": str(facility_user.external_id),
                "scope_type": "facility_organization",
                "organization": {
                    "id": str(organization.external_id),
                    "name": organization.name,
                    "org_type": organization.org_type,
                },
                "facility": _facility_json(organization.facility),
                "role": _role_json(facility_user.role),
                "assigned_by": getattr(facility_user.created_by, "username", None),
                "expires_at": facility_user.expires_at,
                "created_date": facility_user.created_date,
            }
        )
    return assignments


class UserAccessAdminMixin:
    """Mixed into UserViewSet. Every action here is for super admins only."""

    def _require_superuser(self):
        if not self.request.user.is_superuser:
            raise PermissionDenied("Only super admins can manage user access")

    def _get_target_user(self):
        return self.get_object()

    def _other_active_superusers(self, user):
        return (
            User.objects.filter(is_superuser=True, is_active=True)
            .exclude(id=user.id)
            .exists()
        )

    @extend_schema(request=SetActiveRequest)
    @action(detail=True, methods=["POST"])
    def set_active(self, request, *args, **kwargs):
        self._require_superuser()
        data = SetActiveRequest(**request.data)
        user = self._get_target_user()
        if user == request.user and not data.is_active:
            raise ValidationError("You cannot deactivate your own account")
        if (
            not data.is_active
            and user.is_superuser
            and not self._other_active_superusers(user)
        ):
            raise ValidationError("Cannot deactivate the last active super admin")
        if user.is_active == data.is_active:
            return Response(
                self.get_retrieve_pydantic_model().serialize(user).to_json()
            )
        with transaction.atomic():
            user.is_active = data.is_active
            update_fields = ["is_active"]
            if not data.is_active:
                user.tokens_valid_after = timezone.now()
                update_fields.append("tokens_valid_after")
            user.save(update_fields=update_fields)
            record_access_event(
                AccessAuditAction.user_activated
                if data.is_active
                else AccessAuditAction.user_deactivated,
                request.user,
                target_user=user,
                old_value={"is_active": not data.is_active},
                new_value={"is_active": data.is_active},
                reason=data.reason,
            )
        return Response(self.get_retrieve_pydantic_model().serialize(user).to_json())

    @extend_schema(request=SetSuperuserRequest)
    @action(detail=True, methods=["POST"])
    def set_superuser(self, request, *args, **kwargs):
        self._require_superuser()
        data = SetSuperuserRequest(**request.data)
        user = self._get_target_user()
        if user == request.user and not data.is_superuser:
            raise ValidationError("You cannot remove your own super admin access")
        if user.is_service_account and data.is_superuser:
            raise ValidationError("Service accounts cannot be super admins")
        if not data.is_superuser and not self._other_active_superusers(user):
            raise ValidationError("Cannot remove the last active super admin")
        if user.is_superuser == data.is_superuser:
            return Response(
                self.get_retrieve_pydantic_model().serialize(user).to_json()
            )
        with transaction.atomic():
            user.is_superuser = data.is_superuser
            user.save(update_fields=["is_superuser"])
            record_access_event(
                AccessAuditAction.superuser_granted
                if data.is_superuser
                else AccessAuditAction.superuser_revoked,
                request.user,
                target_user=user,
                old_value={"is_superuser": not data.is_superuser},
                new_value={"is_superuser": data.is_superuser},
                reason=data.reason,
            )
        return Response(self.get_retrieve_pydantic_model().serialize(user).to_json())

    @extend_schema(request=ReasonRequest)
    @action(detail=True, methods=["POST"])
    def reset_mfa(self, request, *args, **kwargs):
        self._require_superuser()
        data = ReasonRequest(**request.data)
        user = self._get_target_user()
        if not user.is_mfa_enabled():
            raise ValidationError("Two-factor sign-in is not turned on for this user")
        with transaction.atomic():
            mfa_settings = user.mfa_settings or {}
            mfa_settings["totp"] = {
                "enabled": False,
                "totp_disabled_at": timezone.now().isoformat(),
                "backup_codes": [],
            }
            user.mfa_settings = mfa_settings
            user.totp_secret = None
            user.save(update_fields=["mfa_settings", "totp_secret"])
            record_access_event(
                AccessAuditAction.mfa_reset,
                request.user,
                target_user=user,
                old_value={"mfa_enabled": True},
                new_value={"mfa_enabled": False},
                reason=data.reason,
            )
        send_totp_disabled_email.delay(user.email, user.username)
        return Response({"detail": "Two-factor sign-in has been reset"})

    @extend_schema(request=SetPasswordRequest)
    @action(detail=True, methods=["POST"])
    def set_password(self, request, *args, **kwargs):
        """
        Set a new password for the user, in place of the emailed reset link.
        The user is signed out of every device and signs in with the new one.
        """
        self._require_superuser()
        data = SetPasswordRequest(**request.data)
        user = self._get_target_user()
        if user == request.user:
            raise ValidationError("Use change password to set your own password")
        if user.is_service_account:
            raise ValidationError("Service accounts sign in with tokens, not passwords")
        try:
            validate_password(data.password, user=user)
        except DjangoValidationError as e:
            raise ValidationError(" ".join(e.messages)) from e
        with transaction.atomic():
            user.set_password(data.password)
            user.tokens_valid_after = timezone.now()
            user.save(update_fields=["password", "tokens_valid_after"])
            record_access_event(
                AccessAuditAction.password_set,
                request.user,
                target_user=user,
                reason=data.reason,
            )
        return Response({"detail": "Password updated. The user has been signed out."})

    @extend_schema(request=ReasonRequest)
    @action(detail=True, methods=["POST"])
    def revoke_sessions(self, request, *args, **kwargs):
        self._require_superuser()
        data = ReasonRequest(**request.data)
        user = self._get_target_user()
        if user == request.user:
            raise ValidationError("Use sign out to end your own session")
        with transaction.atomic():
            user.tokens_valid_after = timezone.now()
            user.save(update_fields=["tokens_valid_after"])
            record_access_event(
                AccessAuditAction.sessions_revoked,
                request.user,
                target_user=user,
                reason=data.reason,
            )
        return Response({"detail": "User has been signed out of all devices"})

    @action(detail=True, methods=["GET"])
    def role_assignments(self, request, *args, **kwargs):
        self._require_superuser()
        user = self._get_target_user()
        results = user_role_assignments(user, request.query_params.get("facility"))
        return Response({"count": len(results), "results": results})

    @action(detail=True, methods=["GET"])
    def effective_permissions(self, request, *args, **kwargs):
        """
        Everything the user can do, merged across their roles. Pass ?facility=
        to see only what they get in that facility (organization-level roles
        are left out then).
        """
        self._require_superuser()
        user = self._get_target_user()
        facility = request.query_params.get("facility")

        if user.is_superuser:
            permissions = [
                {
                    "slug": permission.slug,
                    "name": permission.name,
                    "context": permission.context,
                    "granted_by": [{"source": "superuser"}],
                }
                for permission in PermissionModel.objects.order_by("context", "slug")
            ]
            return Response(
                {
                    "is_superuser": True,
                    "count": len(permissions),
                    "results": permissions,
                }
            )

        assignments = user_role_assignments(user, facility)
        roles_by_id = {}
        for assignment in assignments:
            roles_by_id.setdefault(assignment["role"]["id"], []).append(assignment)

        role_permissions = (
            RolePermission.objects.filter(role__external_id__in=list(roles_by_id))
            .select_related("permission", "role")
            .order_by("permission__context", "permission__slug")
        )
        merged = {}
        for role_permission in role_permissions:
            permission = role_permission.permission
            entry = merged.setdefault(
                permission.slug,
                {
                    "slug": permission.slug,
                    "name": permission.name,
                    "context": permission.context,
                    "granted_by": [],
                },
            )
            for assignment in roles_by_id[str(role_permission.role.external_id)]:
                entry["granted_by"].append(
                    {
                        "source": "role",
                        "role": assignment["role"],
                        "scope_type": assignment["scope_type"],
                        "organization": assignment["organization"],
                        "facility": assignment["facility"],
                    }
                )
        results = list(merged.values())
        return Response(
            {"is_superuser": False, "count": len(results), "results": results}
        )
