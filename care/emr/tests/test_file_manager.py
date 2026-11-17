from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote

from django.test import SimpleTestCase

from care.emr.utils.file_manager import S3FilesManager, content_disposition


class ContentDispositionTest(SimpleTestCase):
    def test_plain_name_keeps_the_old_header(self):
        self.assertEqual(
            content_disposition("inline", "file.jpg"), "inline; filename=file.jpg"
        )

    def test_non_latin1_name_is_header_safe_and_keeps_the_real_name(self):
        name = "ID — Passport — 12/11/2026.jpg"
        header = content_disposition("inline", name)
        # S3 rejects signed response headers it can't represent in ISO-8859-1.
        header.encode("ascii")
        self.assertIn('filename="ID - Passport - 12-11-2026.jpg"', header)
        self.assertEqual(unquote(header.split("filename*=UTF-8''")[1]), name)

    def test_other_scripts_fall_back_to_a_placeholder(self):
        header = content_disposition("attachment", "रिपोर्ट")
        header.encode("ascii")
        self.assertTrue(header.startswith('attachment; filename="file"; '))
        self.assertEqual(unquote(header.split("filename*=UTF-8''")[1]), "रिपोर्ट")

    def test_quotes_cannot_break_out_of_the_fallback(self):
        header = content_disposition("inline", 'a"b;c — d.pdf')
        self.assertIn('filename="a-b-c - d.pdf"', header)


class ReadSignedUrlTest(SimpleTestCase):
    def signed_params(self, mime_type):
        file_obj = SimpleNamespace(
            meta={"mime_type": mime_type},
            file_type="patient",
            internal_name="abc",
            name="Patient photo",
            get_extension=lambda: ".jpg",
        )
        with (
            patch(
                "care.emr.utils.file_manager.get_client_config",
                return_value=({}, "bucket"),
            ),
            patch("care.emr.utils.file_manager.boto3") as boto3,
        ):
            S3FilesManager("patient").read_signed_url(file_obj)
        return boto3.client.return_value.generate_presigned_url.call_args.kwargs[
            "Params"
        ]

    def test_viewable_file_is_sent_with_its_type(self):
        # Objects stored without a ContentType would otherwise download.
        params = self.signed_params("image/jpeg")
        self.assertEqual(params["ResponseContentType"], "image/jpeg")
        self.assertTrue(params["ResponseContentDisposition"].startswith("inline"))

    def test_other_files_download_without_a_type_override(self):
        params = self.signed_params("application/zip")
        self.assertNotIn("ResponseContentType", params)
        self.assertTrue(params["ResponseContentDisposition"].startswith("attachment"))
