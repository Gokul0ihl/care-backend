from django.db import transaction
from django_filters import rest_framework as filters
from drf_spectacular.utils import extend_schema
from pydantic import BaseModel, Field
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from care.emr.api.viewsets.base import EMRModelViewSet
from care.emr.api.viewsets.user_access import user_role_assignments
from care.emr.models.organization import FacilityOrganizationUser, OrganizationUser
from care.emr.resources.role.spec import RoleCreateSpec, RoleReadSpec
from care.security.access_audit import record_access_event
from care.security.models import (
    AccessAuditAction,
    PermissionModel,
    RoleModel,
    RolePermission,
)
from care.utils.pagination.care_pagination import CareLimitOffsetPagination


class RoleContextFilter(filters.CharFilter):
    def filter(self, qs, value):
        queryset = qs
        if not value:
            return queryset
        return queryset.filter(contexts__overlap=[value])


class RoleFilter(filters.FilterSet):
    name = filters.CharFilter(lookup_expr="icontains")
    context = RoleContextFilter()
    is_system = filters.BooleanFilter(field_name="is_system")
    is_archived = filters.BooleanFilter(field_name="is_archived")


def role_user_counts(role_ids):
    """Distinct users holding each role, at organization or facility level."""
    users_by_role = {}
    for model in (OrganizationUser, FacilityOrganizationUser):
        rows = (
            model.objects.filter(role_id__in=role_ids)
            .values_list("role_id", "user_id")
            .distinct()
        )
        for role_id, user_id in rows:
            users_by_role.setdefault(role_id, set()).add(user_id)
    return {role_id: len(users) for role_id, users in users_by_role.items()}


def role_permission_slugs(role):
    return set(
        RolePermission.objects.filter(role=role).values_list(
            "permission__slug", flat=True
        )
    )


class CloneRoleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=1024)
    description: str | None = None


class RoleViewSet(EMRModelViewSet):
    database_model = RoleModel
    pydantic_model = RoleCreateSpec
    pydantic_read_model = RoleReadSpec
    filterset_class = RoleFilter
    filter_backends = [filters.DjangoFilterBackend]

    def permissions_controller(self, request):
        if self.action in ["list", "retrieve"]:
            return True
        return request.user.is_superuser

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request)
        roles = page if page is not None else list(queryset)
        counts = role_user_counts([role.id for role in roles])
        data = [
            self.get_read_pydantic_model().serialize(role, user_counts=counts).to_json()
            for role in roles
        ]
        if page is not None:
            return paginator.get_paginated_response(data)
        return Response(data)

    def get_serializer_retrieve_context(self):
        instance = self.get_object()
        return {"user_counts": role_user_counts([instance.id])}

    def validate_destroy(self, instance):
        if instance.is_system:
            raise ValidationError("Cannot delete system roles")
        if role_user_counts([instance.id]).get(instance.id):
            raise ValidationError(
                "This role is still assigned to users. Remove those assignments first."
            )
        return super().validate_destroy(instance)

    def _add_permissions(self, instance):
        permissions = PermissionModel.objects.filter(slug__in=instance.permissions)
        RolePermission.objects.filter(role=instance).delete()
        role_permissions = []
        for permission in permissions:
            role_permissions.append(
                RolePermission(role=instance, permission=permission)
            )
        RolePermission.objects.bulk_create(role_permissions)

    def perform_create(self, instance):
        with transaction.atomic():
            super().perform_create(instance)
            if instance.permissions is not None:
                self._add_permissions(instance)
            record_access_event(
                AccessAuditAction.role_created,
                self.request.user,
                role=instance,
                new_value={
                    "name": instance.name,
                    "permissions": sorted(instance.permissions or []),
                },
            )

    def perform_update(self, instance):
        with transaction.atomic():
            old_role = RoleModel.objects.get(id=instance.id)
            old_permissions = role_permission_slugs(instance)
            super().perform_update(instance)
            if instance.permissions is not None:
                RolePermission.objects.filter(role=instance).delete()
                self._add_permissions(instance)
            new_permissions = role_permission_slugs(instance)
            record_access_event(
                AccessAuditAction.role_updated,
                self.request.user,
                role=instance,
                old_value={
                    "name": old_role.name,
                    "description": old_role.description,
                    "permissions_removed": sorted(old_permissions - new_permissions),
                },
                new_value={
                    "name": instance.name,
                    "description": instance.description,
                    "permissions_added": sorted(new_permissions - old_permissions),
                },
            )

    def perform_destroy(self, instance):
        with transaction.atomic():
            record_access_event(
                AccessAuditAction.role_deleted,
                self.request.user,
                role=instance,
                old_value={
                    "name": instance.name,
                    "permissions": sorted(role_permission_slugs(instance)),
                },
            )
            super().perform_destroy(instance)
            RolePermission.objects.filter(role=instance).delete()

    def _require_superuser(self):
        if not self.request.user.is_superuser:
            raise PermissionDenied("Only super admins can do this")

    @extend_schema(request=CloneRoleRequest)
    @action(detail=True, methods=["POST"])
    def clone(self, request, *args, **kwargs):
        """Copies a role (system roles too) into a new custom role."""
        self._require_superuser()
        data = CloneRoleRequest(**request.data)
        source = self.get_object()
        name = data.name.strip()
        if RoleModel.objects.filter(name__iexact=name).exists():
            raise ValidationError("Role with this name already exists")
        with transaction.atomic():
            role = RoleModel.objects.create(
                name=name,
                description=source.description
                if data.description is None
                else data.description,
                contexts=[c for c in source.contexts if c != "ROLE_ORG"]
                or source.contexts,
                is_system=False,
            )
            source_permissions = list(
                RolePermission.objects.filter(role=source).values_list(
                    "permission_id", flat=True
                )
            )
            RolePermission.objects.bulk_create(
                [
                    RolePermission(role=role, permission_id=permission_id)
                    for permission_id in source_permissions
                ]
            )
            record_access_event(
                AccessAuditAction.role_created,
                request.user,
                role=role,
                new_value={
                    "name": role.name,
                    "cloned_from": {
                        "id": str(source.external_id),
                        "name": source.name,
                    },
                    "permissions": sorted(role_permission_slugs(role)),
                },
            )
        return Response(
            self.get_read_pydantic_model()
            .serialize(role, user_counts={role.id: 0})
            .to_json()
        )

    @action(detail=True, methods=["GET"])
    def users(self, request, *args, **kwargs):
        """Who holds this role, at organization and facility level. ?facility= narrows it."""
        self._require_superuser()
        role = self.get_object()
        facility = request.query_params.get("facility")
        user_ids = set(
            FacilityOrganizationUser.objects.filter(role=role).values_list(
                "user_id", flat=True
            )
        )
        if not facility:
            user_ids |= set(
                OrganizationUser.objects.filter(role=role).values_list(
                    "user_id", flat=True
                )
            )
        from care.users.models import User

        users = User.objects.filter(id__in=user_ids).order_by("first_name", "last_name")
        paginator = CareLimitOffsetPagination()
        page = paginator.paginate_queryset(users, request)
        results = []
        for user in page:
            assignments = [
                a
                for a in user_role_assignments(user, facility)
                if a["role"]["id"] == str(role.external_id)
            ]
            if not assignments:
                continue
            results.append(
                {
                    "user": {
                        "id": str(user.external_id),
                        "username": user.username,
                        "first_name": user.first_name,
                        "last_name": user.last_name,
                        "is_active": user.is_active,
                    },
                    "assignments": assignments,
                }
            )
        return paginator.get_paginated_response(results)
