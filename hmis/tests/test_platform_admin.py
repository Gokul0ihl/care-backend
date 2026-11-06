from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIRequestFactory

from care.utils.tests.base import CareAPITestBase
from hmis.constants import PLATFORM_USERNAME
from hmis.models import ApiKey, SettingChange
from hmis.platform_admin import (
    platform_user,
    record_setting_change,
    setup_platform_user,
)

ME = "/api/v1/users/getcurrentuser/"


class PlatformAdminTests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        self.admin = self.create_super_user(username="key-admin")
        self.platform, _ = setup_platform_user()
        self.staff = self.create_user(username="desk-user", is_active=True)

    def create_key(self, **body):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            reverse("api-keys"), {"name": "IHL portal", **body}, format="json"
        )
        self.client.force_authenticate(user=None)
        return response

    def portal_key(self):
        response = self.create_key(scope="platform_admin")
        self.assertEqual(
            response.status_code, status.HTTP_201_CREATED, response.content
        )
        return response.json()["key"]

    # setup_platform_user

    def test_setup_is_idempotent_and_gives_no_password(self):
        user, created = setup_platform_user()
        self.assertFalse(created)
        self.assertEqual(user, self.platform)
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.is_superuser)
        call_command("setup_platform_user")
        self.assertEqual(
            type(user).objects.filter(username=PLATFORM_USERNAME).count(), 1
        )

    # Creating portal keys

    def test_portal_key_defaults_to_platform_user_and_shows_scope(self):
        body = self.create_key(scope="platform_admin").json()
        self.assertEqual(body["username"], PLATFORM_USERNAME)
        self.assertEqual(body["scope"], "platform_admin")
        self.assertEqual(
            ApiKey.objects.get(external_id=body["id"]).scope, "platform_admin"
        )

    def test_portal_key_must_act_as_platform_user(self):
        response = self.create_key(scope="platform_admin", username="desk-user")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(PLATFORM_USERNAME, str(response.json()))

    def test_portal_key_needs_the_platform_user_set_up(self):
        self.platform.deleted = True
        self.platform.save(update_fields=["deleted"])
        response = self.create_key(scope="platform_admin")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("setup_platform_user", str(response.json()))

    def test_existing_keys_default_to_full_scope(self):
        body = self.create_key(username="desk-user").json()
        self.assertEqual(body["scope"], "full")

    # What a portal key can reach

    def test_portal_key_only_reaches_admin_paths(self):
        key = self.portal_key()
        refused = self.client.get(ME, HTTP_X_API_KEY=key)
        self.assertEqual(refused.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("admin endpoints", refused.json()["detail"])

        who = self.client.get(reverse("admin-whoami"), HTTP_X_API_KEY=key)
        self.assertEqual(who.status_code, status.HTTP_200_OK, who.content)
        self.assertEqual(who.json()["username"], PLATFORM_USERNAME)
        self.assertEqual(who.json()["scope"], "platform_admin")
        self.assertTrue(key.startswith(who.json()["key_prefix"]))

    def test_full_key_and_plain_staff_cannot_use_admin_paths(self):
        full = self.create_key(username="desk-user").json()["key"]
        self.assertEqual(
            self.client.get(reverse("admin-overview"), HTTP_X_API_KEY=full).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.client.force_authenticate(user=self.staff)
        self.assertEqual(
            self.client.get(reverse("admin-overview")).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.client.force_authenticate(user=None)
        self.assertIn(
            self.client.get(reverse("admin-overview")).status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_superuser_can_use_admin_paths(self):
        self.client.force_authenticate(user=self.admin)
        self.assertEqual(
            self.client.get(reverse("admin-overview")).status_code, status.HTTP_200_OK
        )

    # Overview

    @override_settings(APP_VERSION="2026.11.1", USE_SMS=True)
    def test_overview(self):
        body = self.client.get(
            reverse("admin-overview"), HTTP_X_API_KEY=self.portal_key()
        ).json()
        self.assertEqual(body["app_version"], "2026.11.1")
        self.assertEqual(body["config_version"], 0)
        types = [g["type"] for g in body["gateways"]]
        self.assertEqual(types, ["sms", "email", "whatsapp", "payment", "abdm"])
        self.assertEqual(body["gateways"][0]["status"], "configured")
        checks = {c["item"]: c["done"] for c in body["checklist"]}
        self.assertTrue(checks["platform_user"])
        self.assertTrue(checks["app_version"])

    # Settings history

    def test_settings_audit_masks_secrets_records_actor_and_pages(self):
        request = APIRequestFactory().put(
            "/", HTTP_X_PORTAL_ACTOR="ops@indiahealthlink.com"
        )
        request.user = self.platform
        for i in range(3):
            record_setting_change(
                request,
                "gateway.sms",
                "settings",
                {"sender_id": "OLD", "api_key": "secret-old"},
                {
                    "sender_id": f"NEW{i}",
                    "api_key": "secret-new",
                    "nested": {"password": "p"},
                },
            )
        record_setting_change(request, "branding", "name", "A", "B")
        key = self.portal_key()
        url = reverse("admin-settings-audit")

        first = self.client.get(
            url, {"section": "gateway.sms", "limit": 2}, HTTP_X_API_KEY=key
        ).json()
        self.assertEqual(
            [r["new_value"]["sender_id"] for r in first["results"]], ["NEW2", "NEW1"]
        )
        row = first["results"][0]
        self.assertEqual(row["new_value"]["api_key"], "***")
        self.assertEqual(row["new_value"]["nested"]["password"], "***")
        self.assertEqual(row["old_value"]["api_key"], "***")
        self.assertEqual(row["actor"], "ops@indiahealthlink.com")
        self.assertEqual(row["changed_by"], PLATFORM_USERNAME)
        self.assertIsNotNone(first["next_cursor"])

        second = self.client.get(
            url,
            {"section": "gateway.sms", "limit": 2, "cursor": first["next_cursor"]},
            HTTP_X_API_KEY=key,
        ).json()
        self.assertEqual(
            [r["new_value"]["sender_id"] for r in second["results"]], ["NEW0"]
        )
        self.assertIsNone(second["next_cursor"])

        overview = self.client.get(reverse("admin-overview"), HTTP_X_API_KEY=key).json()
        self.assertEqual(
            overview["config_version"], SettingChange.objects.order_by("-id").first().id
        )

    def test_settings_audit_rejects_bad_params(self):
        key = self.portal_key()
        url = reverse("admin-settings-audit")
        self.assertEqual(
            self.client.get(url, {"from": "yesterday"}, HTTP_X_API_KEY=key).status_code,
            400,
        )
        self.assertEqual(
            self.client.get(url, {"limit": "-1"}, HTTP_X_API_KEY=key).status_code, 400
        )

    def test_actor_falls_back_to_username(self):
        request = APIRequestFactory().put("/")
        request.user = platform_user()
        self.assertEqual(
            record_setting_change(request, "branding", "name", None, "X").actor_label,
            PLATFORM_USERNAME,
        )
