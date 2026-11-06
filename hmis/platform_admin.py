"""The IHL config portal's way in (hmis_fe/docs/api-keys.md, "IHL portal keys").

The portal (Adminconfigportal/portal-api) runs once for all clients and calls
each client's backend at /api/hmis/admin/ with an API key that acts as the
ihl-platform service account. That account has no roles, so its key can't read
patients even outside the admin paths, and the key's platform_admin scope keeps
it on those paths anyway.
"""

from django.conf import settings
from django.db import transaction
from rest_framework.permissions import BasePermission

from care.users.models import User
from hmis.constants import PLATFORM_USERNAME, ApiKeyScopes
from hmis.models import ApiKey, SettingChange

ACTOR_HEADER = "HTTP_X_PORTAL_ACTOR"
ACTOR_MAX_LENGTH = 254
# Masked in the settings history at any depth (matched on the key's name).
SECRET_WORDS = ("password", "secret", "token", "api_key", "apikey", "private")
MASK = "***"


def platform_user():
    return User.objects.filter(username=PLATFORM_USERNAME, deleted=False).first()


@transaction.atomic
def setup_platform_user():
    """Idempotent. Returns (user, created)."""
    user = platform_user()
    if user is not None:
        return user, False
    user = User(
        username=PLATFORM_USERNAME,
        first_name="IHL",
        last_name="config portal",
        user_type="staff",
        is_service_account=True,
    )
    # Signs in with an API key only, and gets no facility or organization role.
    user.set_unusable_password()
    user.save()
    return user, True


class IsPlatformAdmin(BasePermission):
    """The IHL portal's key (scope platform_admin), or a superuser signed in
    directly, e.g. to try an endpoint from the API docs."""

    message = "Only the IHL config portal or a superuser can use the admin endpoints."

    def has_permission(self, request, view):
        if isinstance(request.auth, ApiKey):
            return request.auth.scope == ApiKeyScopes.platform_admin
        return bool(request.user and request.user.is_superuser)


def portal_actor(request):
    """Who really made the call. The portal key acts as one shared account, so
    the portal names the IHL staff member in X-Portal-Actor."""
    label = (request.META.get(ACTOR_HEADER) or "").strip()[:ACTOR_MAX_LENGTH]
    return label or request.user.username


def mask_secrets(value):
    if isinstance(value, dict):
        return {
            k: MASK
            if any(w in str(k).lower() for w in SECRET_WORDS)
            else mask_secrets(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [mask_secrets(v) for v in value]
    return value


def record_setting_change(request, section, key, old_value, new_value, facility=None):
    """Called by the admin views of later phases (branding, gateways, ...)
    whenever they save a setting."""
    return SettingChange.objects.create(
        section=section,
        key=key,
        old_value=mask_secrets(old_value),
        new_value=mask_secrets(new_value),
        changed_by=request.user if request.user.is_authenticated else None,
        actor_label=portal_actor(request),
        facility=facility,
    )


def config_version():
    """Goes up whenever any setting changes through the admin endpoints, so
    the HMIS frontend can tell its cached config is stale."""
    last = SettingChange.objects.order_by("-id").values_list("id", flat=True).first()
    return last or 0


def gateway_status():
    """Where each gateway stands. SMS and email are still read from the
    server's settings file; the others arrive in later phases."""
    sms_backend = getattr(settings, "SMS_BACKEND", "") or ""
    sms_on = bool(getattr(settings, "USE_SMS", False))
    email_backend = getattr(settings, "EMAIL_BACKEND", "") or ""
    email_on = not any(x in email_backend for x in ("console", "locmem", "dummy"))
    return [
        {
            "type": "sms",
            "provider": sms_backend.rsplit(".", 1)[-1] or None,
            "status": "configured" if sms_on else "not_set_up",
            "source": "server_file",
        },
        {
            "type": "email",
            "provider": email_backend.rsplit(".", 1)[-1] or None,
            "status": "configured" if email_on else "not_set_up",
            "source": "server_file",
        },
        *(
            {"type": t, "provider": None, "status": "not_set_up", "source": None}
            for t in ("whatsapp", "payment", "abdm")
        ),
    ]
