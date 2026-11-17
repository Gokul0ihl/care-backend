"""Branding and theme: the public copy the HMIS frontend reads, and the admin
endpoints the IHL config portal edits it with (hmis/branding.py)."""

from pydantic import ValidationError as PydanticValidationError
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from hmis import branding as b
from hmis.views.base import EmergencyAPIView
from hmis.views.platform_admin import PlatformAdminView

PUBLIC_MAX_AGE = 60


def admin_body(branding):
    draft = b.with_defaults(branding.draft)
    published = b.with_defaults(branding.published) if branding.published else None
    return {
        "draft": b.with_logo_urls(draft),
        "published": b.with_logo_urls(published) if published else None,
        "version": branding.version,
        "published_at": branding.published_at,
        "published_by": branding.published_by.username
        if branding.published_by
        else None,
        "updated_at": branding.updated_at,
        "has_unpublished_changes": b.has_unpublished_changes(branding),
        "warnings": b.contrast_warnings(draft),
        "options": {"fonts": list(b.FONTS), "logo_kinds": list(b.LOGO_KINDS)},
    }


class PublicBrandingView(EmergencyAPIView):
    """No login: the sign-in page needs the logo and colours too. A stale or
    bad token must not break it, so no authentication runs at all."""

    authentication_classes = ()
    permission_classes = (AllowAny,)

    def get(self, request):
        branding = b.get_branding()
        data = (
            b.with_defaults(branding.published)
            if branding.published
            else b.default_branding()
        )
        etag = f'"branding-v{branding.version}"'
        headers = {"ETag": etag, "Cache-Control": f"public, max-age={PUBLIC_MAX_AGE}"}
        if request.headers.get("If-None-Match") == etag:
            return Response(status=status.HTTP_304_NOT_MODIFIED, headers=headers)
        body = {
            **b.with_logo_urls(data),
            "version": branding.version,
            "published_at": branding.published_at,
        }
        return Response(body, headers=headers)


class AdminBrandingView(PlatformAdminView):
    def get(self, request):
        return Response(admin_body(b.get_branding()))

    def put(self, request):
        """Saves the draft. Nothing changes for HMIS users until it is published."""
        try:
            spec = b.BrandingSpec.model_validate(request.data)
        except PydanticValidationError as e:
            raise ValidationError(
                {
                    "errors": [
                        {"loc": list(err["loc"]), "msg": err["msg"]}
                        for err in e.errors()
                    ]
                }
            ) from e
        return Response(admin_body(b.save_draft(request, spec)))


class AdminBrandingPublishView(PlatformAdminView):
    def post(self, request):
        return Response(admin_body(b.publish(request)))


class AdminBrandingDiscardView(PlatformAdminView):
    def post(self, request):
        return Response(admin_body(b.discard_draft(request)))


class AdminBrandingLogoView(PlatformAdminView):
    parser_classes = (MultiPartParser, FormParser, JSONParser)

    def post(self, request, kind):
        """Multipart field "file". Goes into the draft."""
        kind = self.check_kind(kind)
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "Attach the logo as the 'file' field"})
        try:
            data, ext, content_type = b.check_logo(upload)
        except b.LogoError as e:
            raise ValidationError({"file": str(e)}) from e
        key = b.put_logo_object(kind, data, ext, content_type)
        return Response(
            admin_body(b.set_logo(request, kind, key)), status=status.HTTP_201_CREATED
        )

    def delete(self, request, kind):
        """Removes it from the draft; the frontend falls back to its default."""
        kind = self.check_kind(kind)
        return Response(admin_body(b.set_logo(request, kind, None)))

    @staticmethod
    def check_kind(kind):
        if kind not in b.LOGO_KINDS:
            raise ValidationError({"kind": f"One of {', '.join(b.LOGO_KINDS)}"})
        return kind
