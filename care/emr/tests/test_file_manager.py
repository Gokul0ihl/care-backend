from urllib.parse import unquote

from django.test import SimpleTestCase

from care.emr.utils.file_manager import content_disposition


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
