from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils.decorators import method_decorator
from django_filters import rest_framework as filters
from drf_spectacular.utils import extend_schema
from jsonschema import validate
from pydantic import BaseModel, model_validator
from rest_framework import filters as drf_filters
from rest_framework import serializers
from rest_framework.authtoken.models import Token
from rest_framework.decorators import action, parser_classes
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from care.emr.api.viewsets.base import EMRModelViewSet
from care.emr.api.viewsets.user_access import UserAccessAdminMixin
from care.emr.models import Organization
from care.emr.models.organization import FacilityOrganizationUser, OrganizationUser
from care.emr.resources.common.mail_type import MailTypeChoices
from care.emr.resources.organization.spec import OrganizationTypeChoices
from care.emr.resources.user.spec import (
    CurrentUserRetrieveSpec,
    UserCreateSpec,
    UserDetailSpec,
    UserListSpec,
    UserUpdateSpec,
)
from care.emr.utils.reset_password import send_password_reset_email
from care.security.access_audit import record_access_event, role_summary
from care.security.authorization import AuthorizationController
from care.security.models import AccessAuditAction, RoleModel
from care.users.models import User
from care.utils.file_uploads.cover_image import delete_cover_image, upload_cover_image
from care.utils.filters.default_filter import DefaultBooleanFilter
from care.utils.models.validators import (
    cover_image_validator,
    custom_image_extension_validator,
)
from care.utils.shortcuts import get_object_or_404


class UserImageUploadSerializer(serializers.ModelSerializer):
    profile_picture = serializers.ImageField(
        required=True,
        write_only=True,
        validators=[custom_image_extension_validator, cover_image_validator],
    )
    read_profile_picture_url = serializers.URLField(read_only=True)

    class Meta:
        model = User
        fields = ("profile_picture", "read_profile_picture_url")

    def save(self, **kwargs):
        user: User = self.instance
        image = self.validated_data["profile_picture"]
        user.profile_picture_url = upload_cover_image(
            image,
            str(user.external_id),
            "avatars",
            user.profile_picture_url,
        )
        user.save(update_fields=["profile_picture_url"])
        return user


class UserPreferenceRequest(BaseModel):
    preference: str
    version: str
    value: dict

    @model_validator(mode="after")
    def validate_preference(self):
        preference_schema = settings.PREFERENCE_SCHEMA
        if self.preference in preference_schema:
            try:
                validate(self.value, preference_schema[self.preference])
            except Exception as e:
                raise ValueError("Invalid JSON") from e
        return self


class UserFilter(filters.FilterSet):
    email = filters.CharFilter(field_name="email", lookup_expr="icontains")
    phone_number = filters.CharFilter(
        field_name="phone_number", lookup_expr="icontains"
    )
    username = filters.CharFilter(field_name="username", lookup_expr="icontains")
    is_service_account = DefaultBooleanFilter(
        field_name="is_service_account", default=False
    )
    is_active = filters.BooleanFilter(field_name="is_active")
    is_superuser = filters.BooleanFilter(field_name="is_superuser")
    mfa_enabled = filters.BooleanFilter(method="filter_mfa_enabled")
    role = filters.UUIDFilter(method="filter_role")
    facility = filters.UUIDFilter(method="filter_facility")
    facility_organization = filters.UUIDFilter(method="filter_facility_organization")
    organization = filters.UUIDFilter(method="filter_organization")

    def filter_mfa_enabled(self, queryset, name, value):
        if value:
            return queryset.filter(mfa_settings__totp__enabled=True)
        # exclude() on a JSON key drops rows where the key is missing, which is
        # exactly the users who never set up two-factor; exclude by id instead.
        return queryset.exclude(
            id__in=User.objects.filter(mfa_settings__totp__enabled=True).values("id")
        )

    def filter_role(self, queryset, name, value):
        return queryset.filter(
            id__in=OrganizationUser.objects.filter(role__external_id=value).values(
                "user_id"
            )
        ) | queryset.filter(
            id__in=FacilityOrganizationUser.objects.filter(
                role__external_id=value
            ).values("user_id")
        )

    def filter_facility(self, queryset, name, value):
        return queryset.filter(
            id__in=FacilityOrganizationUser.objects.filter(
                organization__facility__external_id=value,
                organization__facility__deleted=False,
            ).values("user_id")
        )

    def filter_facility_organization(self, queryset, name, value):
        return queryset.filter(
            id__in=FacilityOrganizationUser.objects.filter(
                organization__external_id=value
            ).values("user_id")
        )

    def filter_organization(self, queryset, name, value):
        return queryset.filter(
            id__in=OrganizationUser.objects.filter(
                organization__external_id=value
            ).values("user_id")
        )


class UserViewSet(UserAccessAdminMixin, EMRModelViewSet):
    database_model = User
    pydantic_model = UserCreateSpec
    pydantic_update_model = UserUpdateSpec
    pydantic_read_model = UserListSpec
    pydantic_retrieve_model = UserDetailSpec
    lookup_field = "username"
    filterset_class = UserFilter
    filter_backends = [
        filters.DjangoFilterBackend,
        drf_filters.SearchFilter,
        drf_filters.OrderingFilter,
    ]
    search_fields = ["first_name", "last_name", "username"]
    ordering_fields = [
        "first_name",
        "last_name",
        "username",
        "last_login",
        "date_joined",
    ]

    def get_queryset(self):
        return super().get_queryset().filter(deleted=False)

    def perform_create(self, instance):
        with transaction.atomic():
            super().perform_create(instance)
            record_access_event(
                AccessAuditAction.user_created,
                self.request.user,
                target_user=instance,
                new_value={
                    "username": instance.username,
                    "is_service_account": instance.is_service_account,
                },
            )

            # Authorize and add Roles
            for role_org in instance._role_orgs:  # noqa SLF001
                role_org_obj = get_object_or_404(
                    Organization, external_id=role_org.organization
                )
                requested_role = get_object_or_404(RoleModel, external_id=role_org.role)

                if role_org_obj.org_type != OrganizationTypeChoices.role.value:
                    raise ValidationError(
                        "Role organization is not a role organization"
                    )

                if not AuthorizationController.call(
                    "can_manage_organization_users_obj",
                    self.request.user,
                    role_org_obj,
                    requested_role,
                ):
                    raise PermissionDenied("Access denied for requested roles")

                OrganizationUser.objects.create(
                    organization=role_org_obj,
                    user=instance,
                    role=requested_role,
                )
                record_access_event(
                    AccessAuditAction.role_assigned,
                    self.request.user,
                    target_user=instance,
                    role=requested_role,
                    scope=role_org_obj,
                    new_value={"role": role_summary(requested_role)},
                )
            if not instance.is_service_account and not instance.has_usable_password():
                try:
                    mail_type = MailTypeChoices.create.value
                    send_password_reset_email(instance, mail_type)
                except Exception as e:
                    raise IntegrityError(
                        "User creation failed due to email error."
                    ) from e  # to fail the transaction

    def authorize_update(self, request_obj, model_instance):
        if self.request.user.is_superuser:
            return
        if not self.request.user.id == model_instance.id:
            raise PermissionDenied("You do not have permission to update this user")

    def authorize_create(self, instance):
        if instance.is_service_account:
            if not AuthorizationController.call(
                "can_create_service_account", self.request.user
            ):
                raise PermissionDenied(
                    "You do not have permission to create service accounts"
                )
        elif not AuthorizationController.call("can_create_user", self.request.user):
            raise PermissionDenied("You do not have permission to create Users")

    def perform_destroy(self, instance):
        with transaction.atomic():
            record_access_event(
                AccessAuditAction.user_deleted,
                self.request.user,
                target_user=instance,
                old_value={"username": instance.username},
            )
            if instance.last_login:
                instance.deleted = True
                instance.save(update_fields=["deleted"])
            else:
                instance.delete()

    def authorize_destroy(self, instance):
        if not self.request.user.is_superuser:
            raise PermissionDenied("You do not have permission to delete this user")

    @extend_schema(responses={200: CurrentUserRetrieveSpec})
    @action(detail=False, methods=["GET"])
    def getcurrentuser(self, request):
        return Response(CurrentUserRetrieveSpec.serialize(request.user).to_json())

    @action(methods=["GET"], detail=True)
    def check_availability(self, request, username):
        """
        Checks availability of username by getting as query, returns 200 if available, and 409 otherwise.
        """
        if User.check_username_exists(username):
            return Response(status=409)
        return Response(status=200)

    @method_decorator(parser_classes([MultiPartParser]))
    @action(
        detail=True, methods=["POST", "DELETE"], permission_classes=[IsAuthenticated]
    )
    def profile_picture(self, request, *args, **kwargs):
        user = self.get_object()
        self.authorize_update({}, user)

        if request.method == "POST":
            serializer = UserImageUploadSerializer(user, data=request.data)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(status=200)
        if request.method == "DELETE":
            if not user.profile_picture_url:
                return Response({"detail": "No cover image to delete"}, status=404)
            delete_cover_image(user.profile_picture_url, "avatars")
            user.profile_picture_url = None
            user.save(update_fields=["profile_picture_url"])
            return Response(status=204)
        return Response({"detail": "Method not allowed"}, status=405)

    @action(
        detail=True,
        methods=["PATCH", "GET"],
        permission_classes=[IsAuthenticated],
    )
    def pnconfig(self, request, *args, **kwargs):
        user = request.user
        if request.method == "GET":
            return Response(
                {
                    "pf_endpoint": user.pf_endpoint,
                    "pf_p256dh": user.pf_p256dh,
                    "pf_auth": user.pf_auth,
                }
            )
        acceptable_fields = ["pf_endpoint", "pf_p256dh", "pf_auth"]
        for field in acceptable_fields:
            if field in request.data:
                setattr(user, field, request.data[field])
        user.save(update_fields=["pf_endpoint", "pf_p256dh", "pf_auth"])
        return Response({})

    @action(detail=True, methods=["POST"])
    def generate_service_account_token(self, request, *args, **kwargs):
        user = get_object_or_404(
            User.objects.filter(deleted=False),
            **{self.lookup_field: self.kwargs[self.lookup_field]},
        )

        if not user.is_service_account:
            return Response(
                {"error": "Only service accounts can generate token"}, status=400
            )

        has_permission = self.request.user.is_superuser or (
            self.request.user == user.created_by
        )

        if not has_permission:
            raise PermissionDenied(
                "You do not have permission to update token for service account"
            )

        Token.objects.filter(user=user).delete()
        token = Token.objects.create(user=user)

        return Response(
            {
                "token": token.key,
                "user": user.username,
                "created": token.created.isoformat(),
            }
        )

    @action(detail=True, methods=["DELETE"])
    def revoke_service_account_token(self, request, *args, **kwargs):
        user = get_object_or_404(
            User.objects.filter(deleted=False),
            **{self.lookup_field: self.kwargs[self.lookup_field]},
        )

        if not user.is_service_account:
            return Response({"error": "Not a service account"}, status=400)

        has_permission = self.request.user.is_superuser or (
            self.request.user == user.created_by
        )

        if not has_permission:
            raise PermissionDenied(
                "You do not have permission to update token for service account"
            )

        Token.objects.filter(user=user).delete()

        return Response({"message": "Token revoked successfully"})

    @action(detail=False, methods=["POST"])
    def set_preferences(self, request, *args, **kwargs):
        user = self.request.user
        preferences = UserPreferenceRequest(**request.data)
        user.preferences[preferences.preference] = preferences.value
        user.save(update_fields=["preferences"])
        return Response(status=201)
