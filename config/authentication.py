import logging

from django.core.cache import cache
from django.utils.translation import gettext_lazy as _
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.plumbing import build_bearer_security_scheme_object
from rest_framework.authentication import BasicAuthentication
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken

logger = logging.getLogger(__name__)


def token_issued_before_revocation(user, token) -> bool:
    """True when an admin signed the user out after this token was issued."""
    valid_after = getattr(user, "tokens_valid_after", None)
    if not valid_after:
        return False
    issued_at = token.get("iat")
    if issued_at is None:
        return True
    return issued_at < valid_after.timestamp()


class CustomJWTAuthentication(JWTAuthentication):
    def authenticate_header(self, request):
        return ""

    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        if token_issued_before_revocation(user, validated_token):
            raise InvalidToken(
                {"detail": "Session has been signed out, please relogin to continue"}
            )
        return user

    def get_validated_token(self, raw_token):
        from config.auth_views import ACCESS_TOKEN_INVALIDATION_PREFIX

        if cache.get(ACCESS_TOKEN_INVALIDATION_PREFIX + raw_token.decode()):
            raise InvalidToken("Invalid Token")
        try:
            return super().get_validated_token(raw_token)
        except InvalidToken as e:
            raise InvalidToken(
                {
                    "detail": "Invalid Token, please relogin to continue",
                    "messages": e.detail.get("messages", []),
                }
            ) from e


class CustomBasicAuthentication(BasicAuthentication):
    def authenticate_header(self, request):
        return ""


class CustomJWTAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "config.authentication.CustomJWTAuthentication"
    name = "jwtAuth"

    def get_security_definition(self, auto_schema):
        return build_bearer_security_scheme_object(
            header_name="Authorization",
            token_prefix="Bearer",
            bearer_format="JWT",
        )


class CustomBasicAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "config.authentication.CustomBasicAuthentication"
    name = "basicAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "basic",
            "description": _("Do not use this scheme for production."),
        }


class SessionAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "rest_framework.authentication.SessionAuthentication"
    name = "cookieAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "cookie",
            "name": "sessionid",
            "scheme": "http",
            "description": _("Do not use this scheme for production."),
        }
