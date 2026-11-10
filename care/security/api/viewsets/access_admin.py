"""Read endpoints for the super admin access portal.

/api/v1/access_audit/            who changed whose access, and when
/api/v1/access_admin/overview/   the numbers on the portal's Overview page
"""

from django.db.models import Count, Q
from django_filters import rest_framework as filters
from rest_framework import mixins
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from care.emr.models.organization import FacilityOrganizationUser, OrganizationUser
from care.facility.models import Facility
from care.security.api.viewsets.access_admin_tools import AccessAdminToolsMixin
from care.security.models import (
    AccessAuditAction,
    AccessAuditEvent,
    AccessRequest,
    AccessRequestStatus,
    RoleModel,
)
from care.security.policy import high_privilege_role_ids
from care.users.models import User
from care.utils.pagination.care_pagination import CareLimitOffsetPagination

RECENT_CHANGES_LIMIT = 8


def mfa_enabled_ids():
    # Not .exclude(mfa_settings__totp__enabled=True): that also drops users with
    # no "totp" key at all, i.e. everyone who never set two-factor up.
    return User.objects.filter(mfa_settings__totp__enabled=True).values("id")


def missing_required_mfa(active_users):
    """Active users the security policy requires to use two-factor sign-in, without it."""
    from care.security.models import SecurityPolicy

    policy = SecurityPolicy.get()
    required = active_users.none()
    if policy.mfa_required_for_superusers:
        required = required | active_users.filter(is_superuser=True)
    if policy.mfa_required_role_ids:
        role_ids = policy.mfa_required_role_ids
        required = required | active_users.filter(
            Q(
                id__in=FacilityOrganizationUser.objects.filter(
                    role__external_id__in=role_ids
                ).values("user_id")
            )
            | Q(
                id__in=OrganizationUser.objects.filter(
                    role__external_id__in=role_ids
                ).values("user_id")
            )
        )
    return (
        required.exclude(id__in=mfa_enabled_ids())
        .exclude(is_service_account=True)
        .distinct()
        .count()
    )


class SuperuserOnlyMixin:
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not request.user.is_superuser:
            raise PermissionDenied("Only super admins can view access administration")


def serialize_access_event(event):
    return {
        "id": str(event.external_id),
        "action": event.action,
        "actor": {
            "id": str(event.actor.external_id) if event.actor else None,
            "username": event.actor_username,
        },
        "target_user": (
            {
                "id": str(event.target_user.external_id) if event.target_user else None,
                "username": event.target_username,
            }
            if event.target_username
            else None
        ),
        "role": (
            {
                "id": str(event.role.external_id) if event.role else None,
                "name": event.role_name,
            }
            if event.role_name
            else None
        ),
        "scope_type": event.scope_type or None,
        "scope": (
            {"id": str(event.scope_external_id), "name": event.scope_name}
            if event.scope_external_id
            else None
        ),
        "facility": (
            {"id": str(event.facility.external_id), "name": event.facility.name}
            if event.facility
            else None
        ),
        "old_value": event.old_value,
        "new_value": event.new_value,
        "reason": event.reason,
        "created_date": event.created_date,
    }


class AccessAuditFilter(filters.FilterSet):
    action = filters.BaseInFilter(field_name="action")
    actor = filters.CharFilter(field_name="actor_username", lookup_expr="iexact")
    target_user = filters.CharFilter(field_name="target_username", lookup_expr="iexact")
    role = filters.UUIDFilter(field_name="role__external_id")
    facility = filters.UUIDFilter(field_name="facility__external_id")
    scope = filters.UUIDFilter(field_name="scope_external_id")
    date_from = filters.DateFilter(field_name="created_date", lookup_expr="date__gte")
    date_to = filters.DateFilter(field_name="created_date", lookup_expr="date__lte")
    search = filters.CharFilter(method="filter_search")

    def filter_search(self, queryset, name, value):
        return queryset.filter(
            Q(actor_username__icontains=value)
            | Q(target_username__icontains=value)
            | Q(role_name__icontains=value)
            | Q(scope_name__icontains=value)
            | Q(reason__icontains=value)
        )


class AccessAuditViewSet(SuperuserOnlyMixin, mixins.ListModelMixin, GenericViewSet):
    queryset = AccessAuditEvent.objects.select_related(
        "actor", "target_user", "role", "facility"
    ).order_by("-created_date")
    filterset_class = AccessAuditFilter
    filter_backends = [filters.DjangoFilterBackend]
    pagination_class = CareLimitOffsetPagination

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            return self.get_paginated_response(
                [serialize_access_event(event) for event in page]
            )
        return Response([serialize_access_event(event) for event in queryset])


class AccessAdminViewSet(SuperuserOnlyMixin, AccessAdminToolsMixin, GenericViewSet):
    @action(detail=False, methods=["GET"])
    def overview(self, request, *args, **kwargs):
        people = User.objects.filter(deleted=False, is_service_account=False)
        active = people.filter(is_active=True)

        roles = RoleModel.objects.filter(is_archived=False)
        users_by_role = {}
        for model in (OrganizationUser, FacilityOrganizationUser):
            for role_id, user_id in (
                model.objects.filter(user__deleted=False, user__is_active=True)
                .values_list("role_id", "user_id")
                .distinct()
            ):
                users_by_role.setdefault(role_id, set()).add(user_id)
        users_per_role = sorted(
            (
                {
                    "role": {"id": str(role.external_id), "name": role.name},
                    "is_system": role.is_system,
                    "user_count": len(users_by_role.get(role.id, ())),
                }
                for role in roles
            ),
            key=lambda row: (-row["user_count"], row["role"]["name"]),
        )

        assigned_user_ids = (
            set().union(*users_by_role.values()) if users_by_role else set()
        )
        recent = AccessAuditEvent.objects.select_related(
            "actor", "target_user", "role", "facility"
        ).order_by("-created_date")[:RECENT_CHANGES_LIMIT]

        role_counts = roles.aggregate(
            total=Count("id"),
            system=Count("id", filter=Q(is_system=True)),
        )
        pending_roles = list(
            AccessRequest.objects.filter(
                status=AccessRequestStatus.pending
            ).values_list("role_id", flat=True)
        )
        high_roles = high_privilege_role_ids(set(pending_roles))
        return Response(
            {
                "requests": {
                    "pending": len(pending_roles),
                    "high_privilege": sum(1 for r in pending_roles if r in high_roles),
                },
                "users": {
                    "active": active.count(),
                    "inactive": people.filter(is_active=False).count(),
                    "superusers": active.filter(is_superuser=True).count(),
                    "without_mfa": active.exclude(id__in=mfa_enabled_ids()).count(),
                    "never_signed_in": active.filter(last_login__isnull=True).count(),
                    "missing_required_mfa": missing_required_mfa(active),
                    "without_role": active.exclude(id__in=assigned_user_ids)
                    .exclude(is_superuser=True)
                    .count(),
                    "service_accounts": User.objects.filter(
                        deleted=False, is_service_account=True
                    ).count(),
                },
                "roles": {
                    "total": role_counts["total"],
                    "system": role_counts["system"],
                    "custom": role_counts["total"] - role_counts["system"],
                },
                "facilities": Facility.objects.filter(deleted=False).count(),
                "users_per_role": users_per_role,
                "recent_changes": [serialize_access_event(event) for event in recent],
                "audit_actions": [choice.value for choice in AccessAuditAction],
            }
        )
