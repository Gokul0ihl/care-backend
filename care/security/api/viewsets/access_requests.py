"""/api/v1/access_request/: ask for a role in a facility department, and let a
super admin approve (which creates the role assignment) or reject it."""

import datetime

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django_filters import rest_framework as filters
from drf_spectacular.utils import extend_schema
from pydantic import UUID4, BaseModel, Field, field_validator
from rest_framework import mixins
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from care.emr.models.organization import FacilityOrganization, FacilityOrganizationUser
from care.security.access_audit import assignment_value, record_access_event
from care.security.authorization import AuthorizationController
from care.security.models import (
    AccessAuditAction,
    AccessRequest,
    AccessRequestStatus,
    RoleModel,
)
from care.security.policy import default_expiry, high_privilege_role_ids
from care.users.models import User
from care.utils.pagination.care_pagination import CareLimitOffsetPagination
from care.utils.shortcuts import get_object_or_404


def _future(value):
    if value is not None and value <= timezone.now():
        raise ValueError("Expiry must be in the future")
    return value


class AccessRequestCreate(BaseModel):
    user: UUID4 | None = None  # defaults to the signed-in user
    role: UUID4
    facility_organization: UUID4
    expires_at: datetime.datetime | None = None
    reason: str = Field("", max_length=2000)

    @field_validator("expires_at")
    @classmethod
    def check_expiry(cls, value):
        return _future(value)


class AccessRequestApprove(BaseModel):
    comment: str = Field("", max_length=2000)
    # Leave out to keep what was requested; send null to make it permanent.
    expires_at: datetime.datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def check_expiry(cls, value):
        return _future(value)


class AccessRequestReview(BaseModel):
    comment: str = Field("", max_length=2000)


def _user_json(user):
    if user is None:
        return None
    return {
        "id": str(user.external_id),
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
    }


def serialize_access_request(request, high_privilege_roles, users_with_access):
    organization = request.facility_organization
    return {
        "id": str(request.external_id),
        "status": request.status,
        "user": _user_json(request.user),
        "requested_by": _user_json(request.requested_by),
        "role": {
            "id": str(request.role.external_id),
            "name": request.role.name,
            "is_system": request.role.is_system,
        },
        "facility": {
            "id": str(organization.facility.external_id),
            "name": organization.facility.name,
        },
        "facility_organization": {
            "id": str(organization.external_id),
            "name": organization.name,
            "org_type": organization.org_type,
        },
        "expires_at": request.expires_at,
        "reason": request.reason,
        "kind": "additional_access"
        if request.user_id in users_with_access
        else "first_access",
        "high_privilege": request.role_id in high_privilege_roles,
        "reviewed_by": _user_json(request.reviewed_by),
        "reviewed_at": request.reviewed_at,
        "review_comment": request.review_comment,
        "assignment": str(request.assignment.external_id)
        if request.assignment
        else None,
        "created_date": request.created_date,
    }


def serialize_many(requests):
    requests = list(requests)
    high = high_privilege_role_ids({r.role_id for r in requests})
    with_access = set(
        FacilityOrganizationUser.objects.filter(
            user_id__in={r.user_id for r in requests}
        ).values_list("user_id", flat=True)
    )
    return [serialize_access_request(r, high, with_access) for r in requests]


class AccessRequestFilter(filters.FilterSet):
    status = filters.CharFilter(field_name="status")
    user = filters.CharFilter(field_name="user__username", lookup_expr="iexact")
    role = filters.UUIDFilter(field_name="role__external_id")
    facility = filters.UUIDFilter(
        field_name="facility_organization__facility__external_id"
    )


def check_no_conflicting_assignment(user, organization):
    """
    Same rules as adding a user to a department directly: a user is linked to
    a department or to one of its parents or children, not several.
    Returns the existing assignment in this exact department, if any.
    """
    queryset = FacilityOrganizationUser.objects.filter(user=user)
    existing = queryset.filter(organization=organization).first()
    if existing:
        return existing
    if (
        organization.parent
        and queryset.filter(organization__in=organization.parent_cache).exists()
    ):
        raise ValidationError(
            "User already has a role in a parent of this department. Change that role instead."
        )
    if queryset.filter(organization__parent_cache__overlap=[organization.id]).exists():
        raise ValidationError(
            "User already has a role in a department under this one. Remove that first."
        )
    return None


class AccessRequestViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet
):
    lookup_field = "external_id"
    filterset_class = AccessRequestFilter
    filter_backends = [filters.DjangoFilterBackend]
    pagination_class = CareLimitOffsetPagination

    def get_queryset(self):
        queryset = AccessRequest.objects.select_related(
            "user",
            "requested_by",
            "reviewed_by",
            "role",
            "facility_organization",
            "facility_organization__facility",
            "assignment",
        ).order_by("-created_date")
        user = self.request.user
        if user.is_superuser:
            return queryset
        return queryset.filter(Q(user=user) | Q(requested_by=user))

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            return self.get_paginated_response(serialize_many(page))
        return Response(serialize_many(queryset))

    def retrieve(self, request, *args, **kwargs):
        return Response(serialize_many([self.get_object()])[0])

    def _require_superuser(self):
        if not self.request.user.is_superuser:
            raise PermissionDenied("Only super admins can review access requests")

    def _get_pending(self):
        access_request = self.get_object()
        if access_request.status != AccessRequestStatus.pending:
            message = f"This request is already {access_request.status}"
            raise ValidationError(message)
        return access_request

    def _audit(self, action_name, access_request, **extra):
        record_access_event(
            action_name,
            self.request.user,
            target_user=access_request.user,
            role=access_request.role,
            scope=access_request.facility_organization,
            **extra,
        )

    @extend_schema(request=AccessRequestCreate)
    def create(self, request, *args, **kwargs):
        data = AccessRequestCreate(**request.data)
        user = (
            get_object_or_404(User, external_id=data.user)
            if data.user
            else request.user
        )
        role = get_object_or_404(RoleModel, external_id=data.role)
        organization = get_object_or_404(
            FacilityOrganization.objects.select_related("facility"),
            external_id=data.facility_organization,
        )
        acting_for_someone_else = user != request.user and not request.user.is_superuser
        if acting_for_someone_else and not AuthorizationController.call(
            "can_manage_facility_organization_users_obj",
            request.user,
            organization,
            role,
        ):
            raise PermissionDenied(
                "You can only request access for yourself, or for colleagues in a department you manage"
            )
        if FacilityOrganizationUser.objects.filter(
            user=user, organization=organization, role=role
        ).exists():
            raise ValidationError("The user already has this role in this department")
        if AccessRequest.objects.filter(
            user=user,
            role=role,
            facility_organization=organization,
            status=AccessRequestStatus.pending,
        ).exists():
            raise ValidationError("There is already a pending request for this")

        with transaction.atomic():
            access_request = AccessRequest.objects.create(
                user=user,
                requested_by=request.user,
                role=role,
                facility_organization=organization,
                expires_at=data.expires_at,
                reason=data.reason,
            )
            self._audit(
                AccessAuditAction.request_created,
                access_request,
                new_value={
                    "request": str(access_request.external_id),
                    "expires_at": data.expires_at.isoformat()
                    if data.expires_at
                    else None,
                },
                reason=data.reason,
            )
        return Response(serialize_many([access_request])[0], status=201)

    @extend_schema(request=AccessRequestApprove)
    @action(detail=True, methods=["POST"])
    def approve(self, request, *args, **kwargs):
        self._require_superuser()
        data = AccessRequestApprove(**request.data)
        access_request = self._get_pending()
        expires_at = (
            data.expires_at
            if "expires_at" in data.model_fields_set
            else access_request.expires_at
        )
        if expires_at is None and "expires_at" not in data.model_fields_set:
            # Temporary roles get the policy's default end date.
            expires_at = default_expiry(access_request.role)
        if expires_at is not None and expires_at <= timezone.now():
            raise ValidationError("The requested expiry has already passed")
        organization = access_request.facility_organization

        with transaction.atomic():
            existing = check_no_conflicting_assignment(
                access_request.user, organization
            )
            if existing:
                old_value = assignment_value(existing)
                existing.role = access_request.role
                existing.expires_at = expires_at
                existing.updated_by = request.user
                existing.save()
                assignment = existing
                self._audit(
                    AccessAuditAction.role_changed,
                    access_request,
                    old_value=old_value,
                    new_value=assignment_value(assignment),
                )
            else:
                assignment = FacilityOrganizationUser.objects.create(
                    organization=organization,
                    user=access_request.user,
                    role=access_request.role,
                    expires_at=expires_at,
                    created_by=request.user,
                    updated_by=request.user,
                )
                self._audit(
                    AccessAuditAction.role_assigned,
                    access_request,
                    new_value=assignment_value(assignment),
                )
            access_request.status = AccessRequestStatus.approved
            access_request.reviewed_by = request.user
            access_request.reviewed_at = timezone.now()
            access_request.review_comment = data.comment
            access_request.assignment = assignment
            access_request.save()
            self._audit(
                AccessAuditAction.request_approved,
                access_request,
                new_value={"request": str(access_request.external_id)},
                reason=data.comment,
            )
        return Response(serialize_many([access_request])[0])

    @extend_schema(request=AccessRequestReview)
    @action(detail=True, methods=["POST"])
    def reject(self, request, *args, **kwargs):
        self._require_superuser()
        data = AccessRequestReview(**request.data)
        access_request = self._get_pending()
        with transaction.atomic():
            access_request.status = AccessRequestStatus.rejected
            access_request.reviewed_by = request.user
            access_request.reviewed_at = timezone.now()
            access_request.review_comment = data.comment
            access_request.save()
            self._audit(
                AccessAuditAction.request_rejected,
                access_request,
                new_value={"request": str(access_request.external_id)},
                reason=data.comment,
            )
        return Response(serialize_many([access_request])[0])

    @extend_schema(request=AccessRequestReview)
    @action(detail=True, methods=["POST"])
    def cancel(self, request, *args, **kwargs):
        access_request = self._get_pending()
        if request.user not in (access_request.user, access_request.requested_by):
            raise PermissionDenied("Only the person who asked can cancel a request")
        data = AccessRequestReview(**request.data)
        with transaction.atomic():
            access_request.status = AccessRequestStatus.cancelled
            access_request.save()
            self._audit(
                AccessAuditAction.request_cancelled,
                access_request,
                new_value={"request": str(access_request.external_id)},
                reason=data.comment,
            )
        return Response(serialize_many([access_request])[0])
