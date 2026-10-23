from io import StringIO
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
from django.test import TestCase, override_settings

from care.utils.sms import send_text_message


@override_settings(
    SMS_BACKEND="care.utils.sms.backend.sns.SnsBackend",
    SNS_REGION="us-east-1",
    SNS_ACCESS_KEY="fake_access_key",
    SNS_SECRET_KEY="fake_secret_key",
    SNS_ROLE_BASED_MODE=False,
)
class TestSendTextMessage(TestCase):
    @patch("care.utils.sms.backend.sns.SnsBackend._get_client")
    def test_send_to_single_recipient(self, mock_get_client):
        """Sending an SMS to one recipient should call publish once and return 1."""
        mock_sns_client = MagicMock()
        mock_get_client.return_value = mock_sns_client

        sent_count = send_text_message(
            content="Hello, world!",
            recipients="+10000000000",
        )

        self.assertEqual(sent_count, 1)
        mock_sns_client.publish.assert_called_once_with(
            PhoneNumber="+10000000000",
            Message="Hello, world!",
        )

    @patch("care.utils.sms.backend.sns.SnsBackend._get_client")
    def test_send_to_multiple_recipients(self, mock_get_client):
        """Sending an SMS to multiple recipients should call publish per recipient."""
        mock_sns_client = MagicMock()
        mock_get_client.return_value = mock_sns_client

        recipients = ["+10000000000", "+20000000000"]
        sent_count = send_text_message(
            content="Group message",
            recipients=recipients,
        )

        self.assertEqual(sent_count, 2)
        self.assertEqual(mock_sns_client.publish.call_count, 2)

    @patch("care.utils.sms.backend.sns.SnsBackend._get_client")
    def test_fail_silently_false_raises_error(self, mock_get_client):
        """If publish fails and fail_silently=False, a ClientError should be raised."""
        mock_sns_client = MagicMock()
        mock_sns_client.publish.side_effect = ClientError(
            {"Error": {"Code": "MockError"}}, "Publish"
        )
        mock_get_client.return_value = mock_sns_client

        with self.assertRaises(ClientError):
            send_text_message(
                content="Failing message",
                recipients=["+30000000000"],
                fail_silently=False,
            )

    @patch("care.utils.sms.backend.sns.SnsBackend._get_client")
    def test_fail_silently_true_swallows_error(self, mock_get_client):
        """If publish fails but fail_silently=True, no error should be raised."""
        mock_sns_client = MagicMock()
        mock_sns_client.publish.side_effect = ClientError(
            {"Error": {"Code": "MockError"}}, "Publish"
        )
        mock_get_client.return_value = mock_sns_client

        sent_count = send_text_message(
            content="Silently failing message",
            recipients=["+40000000000"],
            fail_silently=True,
        )

        self.assertEqual(sent_count, 0, "Should report 0 messages sent on failure")
        self.assertEqual(mock_sns_client.publish.call_count, 1)


@override_settings(
    SMS_BACKEND="care.utils.sms.backend.console.ConsoleBackend",
)
class TestTextMessageWithConsoleBackend(TestCase):
    """
    Tests sending SMS via the ConsoleBackend, which writes messages to stdout.
    """

    @patch("sys.stdout", new_callable=StringIO)
    def test_send_single_recipient(self, mock_stdout):
        """
        Verifies a single message to one recipient is printed correctly
        and returns a successful send count of 1.
        """
        sent_count = send_text_message(
            content="Hello via Console!",
            recipients="+10000000000",
        )
        self.assertEqual(sent_count, 1, "Should report 1 for one successful send")

        output = mock_stdout.getvalue()
        self.assertIn("Hello via Console!", output)
        self.assertIn("+10000000000", output)

    @patch("sys.stdout", new_callable=StringIO)
    def test_send_multiple_recipients(self, mock_stdout):
        """
        Verifies multiple messages are printed for multiple recipients
        and that send_text_message reports the correct count.
        """
        recipients = ["+20000000000", "+30000000000"]
        sent_count = send_text_message(
            content="Group console message",
            recipients=recipients,
        )
        self.assertEqual(sent_count, 2, "Should report 2 for two successful sends")

        output = mock_stdout.getvalue()
        self.assertIn("Group console message", output)
        for r in recipients:
            self.assertIn(r, output)


@override_settings(
    SMS_BACKEND="care.utils.sms.backend.foxsms.FoxSmsBackend",
    FOX_SMS_BASE_URL="http://fox.example/sms/submitsms.jsp",
    FOX_SMS_USER="TESTUSER",
    FOX_SMS_API_KEY="fake_fox_key",
    FOX_SMS_SENDER_ID="TESTID",
)
class TestFoxSmsBackend(TestCase):
    def _response(self, text, status=200):
        response = MagicMock()
        response.status_code = status
        response.text = text
        return response

    @patch("care.utils.sms.backend.foxsms.requests.get")
    def test_sends_with_dlt_params(self, mock_get):
        mock_get.return_value = self._response("sent,success,1,2,+919999999999 \r\n")

        sent_count = send_text_message(
            content="Code 12345.\nPacific OneHealth",
            recipients="+919999999999",
        )

        self.assertEqual(sent_count, 1)
        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], "http://fox.example/sms/submitsms.jsp")
        self.assertEqual(
            kwargs["params"],
            {
                "user": "TESTUSER",
                "key": "fake_fox_key",
                "mobile": "919999999999",
                "message": "Code 12345.\nPacific OneHealth",
                "senderid": "TESTID",
                "accusage": "1",
            },
        )
        # FoxxSMS returns 403 to the default python-requests User-Agent.
        self.assertNotIn("python", kwargs["headers"]["User-Agent"].lower())

    @patch("care.utils.sms.backend.foxsms.requests.get")
    def test_ten_digit_number_gets_country_code(self, mock_get):
        mock_get.return_value = self._response("sent,success,1,2,+919999999999")
        send_text_message(content="hi", recipients="9999999999")
        self.assertEqual(mock_get.call_args.kwargs["params"]["mobile"], "919999999999")

    @patch("care.utils.sms.backend.foxsms.requests.get")
    def test_rejection_raises_without_leaking_key(self, mock_get):
        from care.utils.sms.backend.foxsms import FoxSmsError

        mock_get.return_value = self._response("invalid template")
        with self.assertRaises(FoxSmsError) as ctx:
            send_text_message(content="hi", recipients="+919999999999")
        self.assertNotIn("fake_fox_key", str(ctx.exception))

    @patch("care.utils.sms.backend.foxsms.requests.get")
    def test_http_error_raises(self, mock_get):
        from care.utils.sms.backend.foxsms import FoxSmsError

        mock_get.return_value = self._response("Forbidden", status=403)
        with self.assertRaises(FoxSmsError):
            send_text_message(content="hi", recipients="+919999999999")

    @patch("care.utils.sms.backend.foxsms.requests.get")
    def test_fail_silently_returns_zero(self, mock_get):
        mock_get.return_value = self._response("error")
        sent_count = send_text_message(
            content="hi", recipients="+919999999999", fail_silently=True
        )
        self.assertEqual(sent_count, 0)

    @override_settings(FOX_SMS_API_KEY="")
    def test_missing_config_is_improperly_configured(self):
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaises(ImproperlyConfigured):
            send_text_message(content="hi", recipients="+919999999999")
