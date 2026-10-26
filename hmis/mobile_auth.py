"""Sign-in for the patient mobile app: its own token types and sessions.

Patients are never Django users. Their tokens carry their own token_type, so
the staff authenticator (config/authentication.py) rejects them everywhere
else. sub is the PatientAccount id and sid the PatientSession, which is
checked on every request, so revoking a session ends its tokens at once.

Rejections are 401 with a WWW-Authenticate header: the app only refreshes its
token on a 401, and Care's staff authenticator turns its own into 403.
"""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import Token

from hmis.models import PatientAccount, PatientSession

# A refresh whose response never reached the app is retried with the token
# before. Within this window that's a retry; after it, reuse of an old token
# means a copy is in someone else's hands, and the session is revoked.
REFRESH_GRACE = timedelta(minutes=2)


class PatientAccessToken(Token):
    token_type = "patient_access"
    lifetime = settings.PATIENT_ACCESS_TOKEN_LIFETIME


class PatientRefreshToken(Token):
    token_type = "patient_refresh"
    lifetime = settings.PATIENT_REFRESH_TOKEN_LIFETIME


class MobilePatient:
    """request.user for an app request: the signed-in profile and its session."""

    is_authenticated = True
    is_anonymous = False
    # Like Care's own patient OTP user (config/patient_otp_authentication.py),
    # so the audit log writes "patient|<last 4 digits>".
    is_alternative_login = True

    def __init__(self, session):
        self.session = session
        self.account = session.account
        self.patient = session.account.patient
        self.phone_number = session.phone_number


def _token_pair(session):
    refresh = PatientRefreshToken()
    refresh["sub"] = str(session.account_id)
    refresh["sid"] = session.id
    session.refresh_jti = refresh["jti"]
    access = PatientAccessToken()
    access["sub"] = str(session.account_id)
    access["sid"] = session.id
    return {
        "access_token": str(access),
        "refresh_token": str(refresh),
        "token_type": "Bearer",
        "expires_in": int(PatientAccessToken.lifetime.total_seconds()),
    }


def start_session(patient, phone_number):
    """A new session for the patient, signed in on this number, and its first
    token pair."""
    account, _ = PatientAccount.objects.get_or_create(patient=patient)
    session = PatientSession.objects.create(account=account, phone_number=phone_number)
    tokens = _token_pair(session)
    session.save(update_fields=["refresh_jti"])
    return session, tokens


def session_usable(session):
    """Not revoked, and the patient still exists and still has the number the
    session was signed in on."""
    patient = session.account.patient
    return (
        session.revoked_at is None
        and not patient.deleted
        and patient.phone_number == session.phone_number
    )


def rotate(raw_refresh_token):
    """A new token pair for a refresh token, or None if the session is over."""
    try:
        token = PatientRefreshToken(raw_refresh_token)
    except TokenError:
        return None
    now = timezone.now()
    with transaction.atomic():
        session = (
            PatientSession.objects.select_for_update()
            .select_related("account__patient")
            .filter(id=token.get("sid"), account_id=token.get("sub"))
            .first()
        )
        if session is None or not session_usable(session):
            return None
        jti = token["jti"]
        retry = (
            jti == session.previous_refresh_jti
            and session.rotated_at
            and now - session.rotated_at < REFRESH_GRACE
        )
        if jti != session.refresh_jti and not retry:
            session.revoked_at = now
            session.save(update_fields=["revoked_at"])
            return None
        session.previous_refresh_jti = session.refresh_jti
        tokens = _token_pair(session)
        session.rotated_at = now
        session.last_used_at = now
        session.save(
            update_fields=[
                "refresh_jti",
                "previous_refresh_jti",
                "rotated_at",
                "last_used_at",
            ]
        )
        return tokens


def end_session(raw_refresh_token):
    """Revokes the refresh token's session. Invalid tokens are ignored."""
    try:
        token = PatientRefreshToken(raw_refresh_token)
    except TokenError:
        return
    PatientSession.objects.filter(
        id=token.get("sid"), account_id=token.get("sub"), revoked_at__isnull=True
    ).update(revoked_at=timezone.now())


class PatientAccessAuthentication(BaseAuthentication):
    www_authenticate_realm = "api"

    def authenticate(self, request):
        header = get_authorization_header(request).split()
        if not header or header[0].lower() != b"bearer":
            return None
        if len(header) != 2:  # noqa: PLR2004
            raise AuthenticationFailed("Invalid token header.")
        try:
            token = PatientAccessToken(header[1].decode())
        except (TokenError, UnicodeError) as e:
            raise AuthenticationFailed("Session expired. Please sign in again.") from e
        session = (
            PatientSession.objects.select_related("account__patient")
            .filter(id=token.get("sid"), account_id=token.get("sub"))
            .first()
        )
        if session is None or not session_usable(session):
            raise AuthenticationFailed("Session expired. Please sign in again.")
        return MobilePatient(session), token

    def authenticate_header(self, request):
        return f'Bearer realm="{self.www_authenticate_realm}"'
