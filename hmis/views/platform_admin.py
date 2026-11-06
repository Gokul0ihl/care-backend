"""/api/hmis/admin/: what the IHL config portal reads and changes on this
server (hmis/platform_admin.py explains the key it uses)."""

import datetime

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from care.facility.models import Facility
from hmis.models import ApiKey, SettingChange
from hmis.platform_admin import (
    IsPlatformAdmin,
    config_version,
    gateway_status,
    platform_user,
)
from hmis.views.base import EmergencyAPIView

DEFAULT_PAGE = 50
MAX_PAGE = 200


class PlatformAdminView(EmergencyAPIView):
    permission_classes = (IsPlatformAdmin,)


class AdminWhoAmIView(PlatformAdminView):
    """Lets the portal check a key before saving it (portal keys can't call
    /api/v1/users/getcurrentuser/)."""

    def get(self, request):
        key = request.auth if isinstance(request.auth, ApiKey) else None
        return Response(
            {
                "username": request.user.username,
                "is_superuser": request.user.is_superuser,
                "scope": key.scope if key else None,
                "key_prefix": key.prefix if key else None,
            }
        )


class AdminOverviewView(PlatformAdminView):
    """The portal's Overview page for this client."""

    def get(self, request):
        gateways = gateway_status()
        app_version = settings.APP_VERSION
        checklist = [
            {
                "item": "platform_user",
                "label": "IHL portal account set up",
                "done": platform_user() is not None,
            },
            {
                "item": "app_version",
                "label": "APP_VERSION set on the server",
                "done": app_version != "unknown",
            },
            {
                "item": "facility",
                "label": "At least one facility",
                "done": Facility.objects.filter(deleted=False).exists(),
            },
            *(
                {
                    "item": f"gateway_{g['type']}",
                    "label": f"{g['type'].upper()} gateway set up",
                    "done": g["status"] != "not_set_up",
                }
                for g in gateways
                if g["type"] in ("sms", "email")
            ),
        ]
        return Response(
            {
                "app_version": app_version,
                "config_version": config_version(),
                "facility_count": Facility.objects.filter(deleted=False).count(),
                "gateways": gateways,
                "checklist": checklist,
                "server_time": timezone.now(),
            }
        )


def serialize_setting_change(change):
    return {
        "id": change.id,
        "section": change.section,
        "key": change.key,
        "old_value": change.old_value,
        "new_value": change.new_value,
        "actor": change.actor_label,
        "changed_by": change.changed_by.username if change.changed_by else None,
        "facility": str(change.facility.external_id) if change.facility else None,
        "created_date": change.created_date,
    }


def date_param(request, name):
    raw = request.query_params.get(name)
    if not raw:
        return None
    value = parse_datetime(raw)
    if value is None:
        try:
            value = datetime.datetime.combine(
                datetime.date.fromisoformat(raw), datetime.time.min
            )
        except ValueError:
            raise ValidationError({name: "Use an ISO date or date-time"}) from None
    return value if timezone.is_aware(value) else timezone.make_aware(value)


def int_param(request, name, default=None, maximum=None):
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return default
    if not raw.isdigit() or int(raw) < 1:
        raise ValidationError({name: "Must be a positive whole number"})
    return min(int(raw), maximum) if maximum else int(raw)


class SettingsAuditView(PlatformAdminView):
    """Who changed which setting and when, newest first. Page with
    ?cursor=<next_cursor>."""

    def get(self, request):
        changes = SettingChange.objects.select_related("changed_by", "facility")
        if section := request.query_params.get("section"):
            changes = changes.filter(section=section)
        if start := date_param(request, "from"):
            changes = changes.filter(created_date__gte=start)
        if end := date_param(request, "to"):
            changes = changes.filter(created_date__lte=end)
        if cursor := int_param(request, "cursor"):
            changes = changes.filter(id__lt=cursor)
        limit = int_param(request, "limit", DEFAULT_PAGE, MAX_PAGE)
        rows = list(changes.order_by("-id")[: limit + 1])
        page = rows[:limit]
        return Response(
            {
                "results": [serialize_setting_change(c) for c in page],
                "next_cursor": page[-1].id if len(rows) > limit else None,
            }
        )
