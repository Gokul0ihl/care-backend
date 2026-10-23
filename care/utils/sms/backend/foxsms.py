import logging

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from care.utils.sms.backend.base import SmsBackendBase
from care.utils.sms.message import TextMessage

logger = logging.getLogger(__name__)

# FoxxSMS answers 403 to python-requests' default User-Agent.
USER_AGENT = "care-sms/1.0"


class FoxSmsError(Exception):
    pass


def normalize_mobile(number: str) -> str:
    """
    FoxxSMS wants 91XXXXXXXXXX: country code, no "+" or spaces.
    Care stores E.164 (+91XXXXXXXXXX); a bare 10-digit number is taken as Indian.
    """
    digits = "".join(ch for ch in number if ch.isdigit())
    if len(digits) == 10:  # noqa: PLR2004
        digits = f"91{digits}"
    return digits


class FoxSmsBackend(SmsBackendBase):
    """
    Sends SMS through the FoxxSMS HTTP API (submitsms.jsp).

    FoxxSMS matches the DLT template from the message text, so the content
    must be the approved template with only the variables filled in.
    """

    def __init__(self, fail_silently: bool = False, **kwargs) -> None:
        super().__init__(fail_silently=fail_silently, **kwargs)
        self.base_url = getattr(settings, "FOX_SMS_BASE_URL", "")
        self.user = getattr(settings, "FOX_SMS_USER", "")
        self.api_key = getattr(settings, "FOX_SMS_API_KEY", "")
        self.sender_id = getattr(settings, "FOX_SMS_SENDER_ID", "")
        self.timeout = getattr(settings, "FOX_SMS_TIMEOUT", 15)
        if not all([self.base_url, self.user, self.api_key, self.sender_id]):
            raise ImproperlyConfigured(
                "FoxxSMS is not configured. Check 'FOX_SMS_BASE_URL', "
                "'FOX_SMS_USER', 'FOX_SMS_API_KEY' and 'FOX_SMS_SENDER_ID'."
            )

    def _send_one(self, recipient: str, content: str) -> None:
        mobile = normalize_mobile(recipient)
        response = requests.get(
            self.base_url,
            params={
                "user": self.user,
                "key": self.api_key,
                "mobile": mobile,
                "message": content,
                "senderid": self.sender_id,
                "accusage": "1",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=self.timeout,
        )
        body = response.text.strip()
        # Success looks like: sent,success,<message id>,<job id>,+91XXXXXXXXXX
        parts = [part.strip() for part in body.split(",")]
        if response.status_code != 200 or parts[:2] != ["sent", "success"]:  # noqa: PLR2004
            # Never log the request URL: it carries the API key.
            msg = f"FoxxSMS rejected message to ...{mobile[-4:]}: HTTP {response.status_code} {body[:200]}"
            raise FoxSmsError(msg)
        logger.info(
            "FoxxSMS accepted message to ...%s (ids %s)",
            mobile[-4:],
            ",".join(parts[2:4]),
        )

    def send_message(self, message: TextMessage) -> int:
        successful_sends = 0
        for recipient in message.recipients:
            try:
                self._send_one(recipient, message.content)
                successful_sends += 1
            except (requests.RequestException, FoxSmsError) as error:
                # requests exceptions embed the URL, which carries the API key.
                detail = str(error).replace(self.api_key, "***")
                logger.error("FoxxSMS send failed: %s", detail)
                if not self.fail_silently:
                    raise FoxSmsError(detail) from None
        return successful_sends
