import io
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from PIL import Image
from rest_framework import status

from care.utils.tests.base import CareAPITestBase
from hmis.models import SettingChange
from hmis.platform_admin import setup_platform_user

BUCKET = override_settings(
    FACILITY_CDN="",
    FACILITY_S3_BUCKET_EXTERNAL_ENDPOINT="http://files.test",
    FACILITY_S3_BUCKET="facility",
)


def image(fmt="PNG", size=(64, 64), name="logo.png"):
    buf = io.BytesIO()
    Image.new("RGB", size, "#2B7BBD").save(buf, fmt)
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


VALID = {
    "hospital_name": "Pacific OneHealth",
    "short_name": "POH",
    "tagline": "Care, close to home",
    "colors": {"primary": "#0b5394", "accent": "#16A34A"},
    "font_family": "Inter",
    "print_header": {
        "address_lines": ["12 MG Road", "Bengaluru 560001"],
        "phone": "+91 80 1234 5678",
        "email": "billing@poh.example",
        "website": "https://poh.example",
        "gstin": "29ABCDE1234F1Z5",
    },
}


@BUCKET
class BrandingTests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        self.admin = self.create_super_user(username="key-admin")
        self.platform, _ = setup_platform_user()
        self.client.force_authenticate(user=self.admin)
        key = self.client.post(
            reverse("api-keys"),
            {"name": "IHL portal", "scope": "platform_admin"},
            format="json",
        ).json()["key"]
        self.client.force_authenticate(user=None)
        self.auth = {
            "HTTP_X_API_KEY": key,
            "HTTP_X_PORTAL_ACTOR": "ops@indiahealthlink.com",
        }
        self.put_object = mock.patch(
            "hmis.branding.put_logo_object", side_effect=self.fake_put
        ).start()
        self.delete_objects = mock.patch("hmis.branding.delete_logo_objects").start()
        self.addCleanup(mock.patch.stopall)
        self.uploaded = 0

    def fake_put(self, kind, data, ext, content_type):
        self.uploaded += 1
        return f"branding/{kind}_{self.uploaded}.{ext}"

    def admin_get(self):
        return self.client.get(reverse("admin-branding"), **self.auth)

    def save(self, body=None):
        return self.client.put(
            reverse("admin-branding"), body or VALID, format="json", **self.auth
        )

    def publish(self):
        return self.client.post(reverse("admin-branding-publish"), **self.auth)

    def upload(self, kind="full", file=None):
        return self.client.post(
            reverse("admin-branding-logo", args=[kind]),
            {"file": file or image()},
            format="multipart",
            **self.auth,
        )

    # Public endpoint

    def test_public_defaults_before_anything_is_published(self):
        response = self.client.get(reverse("branding"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body["version"], 0)
        self.assertEqual(body["hospital_name"], "Hospital")
        self.assertEqual(body["font_family"], "Lexend Deca")
        self.assertEqual(
            body["logos"], {"full": None, "small": None, "favicon": None, "dark": None}
        )

    def test_public_ignores_bad_tokens(self):
        response = self.client.get(
            reverse("branding"), HTTP_AUTHORIZATION="Bearer expired.or.junk"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_draft_is_not_public_until_published(self):
        self.assertEqual(self.save().status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.client.get(reverse("branding")).json()["hospital_name"], "Hospital"
        )

        published = self.publish().json()
        self.assertEqual(published["version"], 1)
        self.assertFalse(published["has_unpublished_changes"])

        public = self.client.get(reverse("branding"))
        body = public.json()
        self.assertEqual(body["hospital_name"], "Pacific OneHealth")
        self.assertEqual(body["colors"]["primary"], "#0B5394")
        self.assertEqual(body["print_header"]["gstin"], "29ABCDE1234F1Z5")
        self.assertEqual(public["ETag"], '"branding-v1"')

        cached = self.client.get(
            reverse("branding"), HTTP_IF_NONE_MATCH='"branding-v1"'
        )
        self.assertEqual(cached.status_code, status.HTTP_304_NOT_MODIFIED)

    # Draft, publish, discard

    def test_save_validates(self):
        bad = {
            **VALID,
            "colors": {"primary": "blue"},
            "font_family": "Comic Sans",
            "print_header": {"gstin": "123", "website": "poh.example"},
            "extra": 1,
        }
        response = self.save(bad)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        locs = {tuple(e["loc"]) for e in response.json()["errors"]}
        self.assertIn(("colors", "primary"), locs)
        self.assertIn(("font_family",), locs)
        self.assertIn(("print_header", "gstin"), locs)
        self.assertIn(("print_header", "website"), locs)
        self.assertIn(("extra",), locs)

    def test_discard_goes_back_to_published(self):
        self.save()
        self.publish()
        self.save({**VALID, "hospital_name": "Renamed"})
        self.assertTrue(self.admin_get().json()["has_unpublished_changes"])
        body = self.client.post(reverse("admin-branding-discard"), **self.auth).json()
        self.assertEqual(body["draft"]["hospital_name"], "Pacific OneHealth")
        self.assertFalse(body["has_unpublished_changes"])

    def test_contrast_warnings(self):
        body = self.save(
            {
                **VALID,
                "colors": {
                    "primary": "#FFEE99",
                    "sidebar_background": "#FFFFFF",
                    "sidebar_foreground": "#EEEEEE",
                },
            }
        ).json()
        fields = {w["field"] for w in body["warnings"]}
        self.assertEqual(fields, {"colors.primary", "colors.sidebar_foreground"})

    def test_every_change_is_in_the_settings_history_with_the_actor(self):
        self.save()
        self.upload()
        self.publish()
        changes = list(SettingChange.objects.filter(section="branding").order_by("id"))
        self.assertEqual([c.key for c in changes], ["draft", "logo.full", "published"])
        self.assertTrue(
            all(c.actor_label == "ops@indiahealthlink.com" for c in changes)
        )
        overview = self.client.get(reverse("admin-overview"), **self.auth).json()
        self.assertEqual(overview["config_version"], changes[-1].id)

    # Logos

    def test_logo_upload_goes_to_draft_then_public_on_publish(self):
        response = self.upload("full")
        self.assertEqual(
            response.status_code, status.HTTP_201_CREATED, response.content
        )
        self.assertEqual(
            response.json()["draft"]["logos"]["full"],
            "http://files.test/facility/branding/full_1.png",
        )
        self.assertIsNone(self.client.get(reverse("branding")).json()["logos"]["full"])
        self.publish()
        self.assertEqual(
            self.client.get(reverse("branding")).json()["logos"]["full"],
            "http://files.test/facility/branding/full_1.png",
        )

    def test_logo_rejects_bad_files(self):
        not_image = SimpleUploadedFile(
            "x.png", b"<svg onload=alert(1)>", content_type="image/png"
        )
        self.assertEqual(
            self.upload(file=not_image).status_code, status.HTTP_400_BAD_REQUEST
        )
        self.assertEqual(
            self.upload(file=image(size=(8, 8))).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.upload(file=image(size=(3000, 100))).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        gif = image(fmt="GIF", name="logo.gif")
        self.assertEqual(self.upload(file=gif).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            self.upload(kind="banner").status_code, status.HTTP_400_BAD_REQUEST
        )
        self.put_object.assert_not_called()

    def test_replaced_logo_kept_while_published_then_deleted(self):
        self.upload("full")  # full_1
        self.publish()
        with self.captureOnCommitCallbacks(execute=True):
            self.upload("full")  # full_2 replaces it in the draft
        # full_1 is still the published logo, so it must not be deleted yet.
        self.assertNotIn("branding/full_1.png", self.deleted_keys())
        with self.captureOnCommitCallbacks(execute=True):
            self.publish()
        self.assertIn("branding/full_1.png", self.deleted_keys())

    def test_delete_logo_from_draft(self):
        self.upload("favicon", image(fmt="ICO", size=(32, 32), name="favicon.ico"))
        body = self.client.delete(
            reverse("admin-branding-logo", args=["favicon"]), **self.auth
        ).json()
        self.assertIsNone(body["draft"]["logos"]["favicon"])

    # Access

    def test_admin_endpoints_need_the_portal_key(self):
        staff = self.create_user(username="desk", is_active=True)
        self.client.force_authenticate(user=staff)
        self.assertEqual(
            self.client.get(reverse("admin-branding")).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.put(
                reverse("admin-branding"), VALID, format="json"
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def deleted_keys(self):
        return {
            key for call in self.delete_objects.call_args_list for key in call.args[0]
        }
