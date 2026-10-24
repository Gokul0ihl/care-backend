"""API keys for the patient app's server."""

from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from care.users.models import User
from care.utils.shortcuts import get_object_or_404
from hmis.api_keys import key_state, new_key
from hmis.constants import (
    APP_SERVER_USERNAME,
)
from hmis.models import (
    ApiKey,
)
from hmis.specs import (
    ApiKeyCreateSpec,
)
from hmis.views.base import (
    EmergencyAPIView,
)


def serialize_api_key(api_key):
    return {
        "id": str(api_key.external_id),
        "name": api_key.name,
        "prefix": api_key.prefix,
        "username": api_key.user.username,
        "state": key_state(api_key),
        "created_by": api_key.created_by.username if api_key.created_by else None,
        "created_date": api_key.created_date,
        "expires_at": api_key.expires_at,
        "revoked_at": api_key.revoked_at,
        "last_used_at": api_key.last_used_at,
    }


def username_error(message):
    return ValidationError({"errors": [{"loc": ["username"], "msg": message}]})


def check_key_admin(request):
    # A key can't manage keys, even a superuser's, so a leaked key can't mint more.
    if isinstance(request.auth, ApiKey) or not request.user.is_superuser:
        raise PermissionDenied("Only administrators can manage API keys")


class ApiKeyView(EmergencyAPIView):
    """Settings → API keys: keys for clients such as the patient mobile app."""

    def get(self, request):
        check_key_admin(request)
        keys = ApiKey.objects.filter(deleted=False).select_related("user", "created_by")
        return Response(
            {"results": [serialize_api_key(key) for key in keys.order_by("-id")]}
        )

    def post(self, request):
        check_key_admin(request)
        spec = ApiKeyCreateSpec.model_validate(request.data)
        user = User.objects.filter(username=spec.username, deleted=False).first()
        if user is None and spec.username == APP_SERVER_USERNAME:
            raise username_error(
                "The app server account isn't set up yet; run setup_app_server"
            )
        if user is None or not user.is_active:
            raise username_error("No active user has this username")
        if user.is_superuser:
            # A leaked key would hand out the whole system.
            raise username_error("A key can't act as a superuser; use a staff user")
        key, prefix, key_hash = new_key()
        api_key = ApiKey.objects.create(
            name=spec.name,
            user=user,
            prefix=prefix,
            key_hash=key_hash,
            expires_at=spec.expires_at,
            created_by=request.user,
            updated_by=request.user,
        )
        # The only time the key is sent: it can't be recovered from its hash.
        return Response(
            {**serialize_api_key(api_key), "key": key},
            status=status.HTTP_201_CREATED,
        )


class ApiKeyRevokeView(EmergencyAPIView):
    def post(self, request, key_id):
        check_key_admin(request)
        api_key = get_object_or_404(ApiKey, external_id=key_id, deleted=False)
        if api_key.revoked_at is None:
            api_key.revoked_at = timezone.now()
            api_key.updated_by = request.user
            api_key.save(update_fields=["revoked_at", "updated_by", "modified_date"])
        return Response(serialize_api_key(api_key))
