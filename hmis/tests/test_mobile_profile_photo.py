"""Profile photos from the mobile app: presigned upload, save with PATCH, signed reads."""

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.utils import timezone
from model_bakery import baker
from rest_framework.test import APITestCase

from care.emr.models import FileUpload, Patient
from hmis.mobile_auth import start_session
from hmis.profile_photo import MAX_OPEN_UPLOADS, MAX_PHOTO_BYTES
from hmis.tasks import remove_stale_profile_photo_uploads

BASE = "/api/hmis/mobile/api/v1"
PHONE = "+919800004444"
STORED = {"ContentLength": 2048, "ContentType": "image/jpeg"}


@patch(
    "care.emr.utils.file_manager.S3FilesManager.read_signed_url",
    lambda self, obj, duration=0: f"https://bucket.test/get/{obj.internal_name}",
)
@patch(
    "care.emr.utils.file_manager.S3FilesManager.signed_url",
    lambda self,
    obj,
    duration=0,
    mime_type=None: f"https://bucket.test/put/{obj.internal_name}",
)
class MobileProfilePhotoTests(APITestCase):
    def setUp(self):
        self.patient = baker.make(
            Patient, name="Test Patient", first_name="Test", phone_number=PHONE
        )
        _, tokens = start_session(self.patient, PHONE)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + tokens["access_token"])

    def start(self, mime_type="image/jpeg"):
        response = self.client.post(
            f"{BASE}/users/me/profile-photo", {"mime_type": mime_type}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["data"]

    def save(self, photo_id, stored=STORED, **fields):
        with (
            patch("hmis.profile_photo.stored_object", return_value=stored),
            patch("hmis.profile_photo.delete_object") as delete,
        ):
            response = self.client.patch(
                f"{BASE}/users/me",
                {**fields, "profile_photo_id": photo_id},
                format="json",
            )
        return response, delete

    def test_upload_ticket_creates_unfinished_patient_file(self):
        ticket = self.start("image/png")
        self.assertEqual(ticket["method"], "PUT")
        self.assertEqual(ticket["headers"], {"Content-Type": "image/png"})
        self.assertEqual(ticket["max_bytes"], MAX_PHOTO_BYTES)
        upload = FileUpload.objects.get(external_id=ticket["upload_id"])
        self.assertEqual(upload.file_type, "patient")
        self.assertEqual(upload.associating_id, str(self.patient.external_id))
        self.assertEqual(upload.file_category, "profile_photo")
        self.assertFalse(upload.upload_completed)
        self.assertTrue(upload.internal_name.endswith(".png"))
        self.assertEqual(
            ticket["upload_url"], f"https://bucket.test/put/{upload.internal_name}"
        )
        # Nothing shows until the upload is saved.
        self.assertIsNone(
            self.client.get(f"{BASE}/users/me").data["data"]["profile_photo"]
        )

    def test_upload_validation_and_auth(self):
        for body in (
            {},
            {"mime_type": "image/gif"},
            {"mime_type": "image/jpeg", "x": 1},
        ):
            with self.subTest(body=body):
                response = self.client.post(
                    f"{BASE}/users/me/profile-photo", body, format="json"
                )
                self.assertEqual(response.status_code, 422, response.data)
        self.client.credentials()
        response = self.client.post(
            f"{BASE}/users/me/profile-photo", {"mime_type": "image/jpeg"}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_save_photo_with_text_fields_and_read_it_back(self):
        ticket = self.start()
        response, _ = self.save(ticket["upload_id"], city="Test City")
        self.assertEqual(response.status_code, 200, response.data)
        upload = FileUpload.objects.get(external_id=ticket["upload_id"])
        self.assertTrue(upload.upload_completed)
        url = f"https://bucket.test/get/{upload.internal_name}"
        self.assertEqual(response.data["data"]["profile_photo"], url)
        self.assertEqual(response.data["data"]["city"], "Test City")
        self.assertEqual(
            self.client.get(f"{BASE}/users/me").data["data"]["profile_photo"], url
        )
        self.patient.refresh_from_db()
        self.assertEqual(
            self.patient.meta["mobile_profile"]["photo_file_id"], ticket["upload_id"]
        )

    def test_replace_archives_old_photo_and_null_removes(self):
        first = self.start()
        self.assertEqual(self.save(first["upload_id"])[0].status_code, 200)
        second = self.start()
        response, _ = self.save(second["upload_id"])
        self.assertEqual(response.status_code, 200, response.data)
        old = FileUpload.objects.get(external_id=first["upload_id"])
        self.assertTrue(old.is_archived)
        self.assertEqual(old.archive_reason, "Replaced from the mobile app")
        # An archived photo can't be brought back.
        self.assertEqual(self.save(first["upload_id"])[0].status_code, 422)

        response, _ = self.save(None)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["data"]["profile_photo"])
        self.assertTrue(
            FileUpload.objects.get(external_id=second["upload_id"]).is_archived
        )
        self.patient.refresh_from_db()
        self.assertNotIn("photo_file_id", self.patient.meta["mobile_profile"])

    def test_rejects_missing_oversized_wrong_type_and_foreign_uploads(self):
        ticket = self.start()
        response, _ = self.save(ticket["upload_id"], stored=None)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["errors"][0]["field"], "profile_photo_id")

        for stored in (
            {"ContentLength": MAX_PHOTO_BYTES + 1, "ContentType": "image/jpeg"},
            {"ContentLength": 0, "ContentType": "image/jpeg"},
            {"ContentLength": 10, "ContentType": "text/html"},
        ):
            with self.subTest(stored=stored):
                response, delete = self.save(
                    ticket["upload_id"], stored=stored, city="Not Saved"
                )
                self.assertEqual(response.status_code, 422, response.data)
                delete.assert_called_once()

        other = baker.make(Patient, name="Other Patient", phone_number="+919800004445")
        foreign = baker.make(
            FileUpload,
            file_type="patient",
            associating_id=str(other.external_id),
            file_category="profile_photo",
            meta={"mime_type": "image/jpeg"},
        )
        for photo_id in (str(foreign.external_id), str(uuid4())):
            with self.subTest(photo_id=photo_id):
                self.assertEqual(self.save(photo_id)[0].status_code, 422)

        self.patient.refresh_from_db()
        self.assertNotEqual(self.patient.city, "Not Saved")
        self.assertNotIn("photo_file_id", self.patient.meta.get("mobile_profile", {}))

    def test_logs_ids_and_reasons_but_never_urls(self):
        with self.assertLogs("hmis.profile_photo", "INFO") as logs:
            ticket = self.start()
            self.save(ticket["upload_id"], stored=None)
            self.save(ticket["upload_id"])
            self.client.get(f"{BASE}/users/me")
        output = "\n".join(logs.output)
        self.assertIn(
            f"profile_photo.upload_started patient={self.patient.external_id}", output
        )
        self.assertIn("reason=not_uploaded", output)
        self.assertIn("profile_photo.upload_verified", output)
        self.assertIn(f"profile_photo.saved patient={self.patient.external_id}", output)
        self.assertNotIn("https://", output)
        self.assertNotIn(PHONE.removeprefix("+91"), output)

    def test_staff_archived_photo_is_hidden(self):
        ticket = self.start()
        self.save(ticket["upload_id"])
        FileUpload.objects.filter(external_id=ticket["upload_id"]).update(
            is_archived=True
        )
        self.assertIsNone(
            self.client.get(f"{BASE}/users/me").data["data"]["profile_photo"]
        )

    def test_open_uploads_are_limited(self):
        for _ in range(MAX_OPEN_UPLOADS):
            self.start()
        response = self.client.post(
            f"{BASE}/users/me/profile-photo", {"mime_type": "image/jpeg"}, format="json"
        )
        self.assertEqual(response.status_code, 429)

    def test_stale_unsaved_uploads_are_removed(self):
        stale, fresh, saved = self.start(), self.start(), self.start()
        self.save(saved["upload_id"])
        FileUpload.objects.filter(
            external_id__in=[stale["upload_id"], saved["upload_id"]]
        ).update(created_date=timezone.now() - timedelta(days=2))
        with patch("hmis.profile_photo.delete_object") as delete:
            self.assertEqual(remove_stale_profile_photo_uploads(), 1)
        delete.assert_called_once()
        self.assertFalse(
            FileUpload.objects.filter(external_id=stale["upload_id"]).exists()
        )
        self.assertTrue(
            FileUpload.objects.filter(external_id=fresh["upload_id"]).exists()
        )
        self.assertTrue(
            FileUpload.objects.filter(external_id=saved["upload_id"]).exists()
        )
