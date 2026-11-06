"""Branding and theme (hmis_fe/docs/branding.md): what the hospital group is
called, its logos and colours, and the header on printed receipts.

The IHL config portal edits a draft through /api/hmis/admin/branding/ and
publishes it. The HMIS frontend reads the published copy from the public
GET /api/hmis/branding/, before anyone signs in, so it never holds anything
secret. Logos live in the facility bucket, which is already public-read for
facility cover images.
"""

import io
import logging
import re
import secrets
from typing import Literal

import boto3
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, field_validator

from care.utils.csp.config import BucketType, get_client_config
from hmis.models import Branding
from hmis.platform_admin import record_setting_change

logger = logging.getLogger(__name__)

SECTION = "branding"
LOGO_KINDS = ("full", "small", "favicon", "dark")
LOGO_FOLDER = "branding"
LOGO_MAX_BYTES = 1024 * 1024
LOGO_MIN_PX = 16
LOGO_MAX_PX = 2048
# Pillow format -> (extension, content type). No SVG: it can carry scripts.
LOGO_FORMATS = {
    "PNG": ("png", "image/png"),
    "JPEG": ("jpg", "image/jpeg"),
    "WEBP": ("webp", "image/webp"),
    "ICO": ("ico", "image/x-icon"),
}
# Fonts the HMIS frontend knows how to load (Google Fonts).
FONTS = ("Lexend Deca", "Inter", "Roboto", "Open Sans", "Noto Sans", "Poppins")
HEX = r"^#[0-9a-fA-F]{6}$"

# The HMIS frontend's own palette (hmis_fe app/globals.css), so publishing
# without touching the colours changes nothing.
DEFAULT_COLORS = {
    "primary": "#EA720F",  # --brand-orange: buttons, links
    "secondary": "#3462AD",  # --brand-blue: focus rings, active menu item, charts
    "accent": "#E2EFFC",  # hover and selected surfaces
    "sidebar_background": "#FFFFFF",
    "sidebar_foreground": "#404040",
}


class Colors(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary: str = Field(default=DEFAULT_COLORS["primary"], pattern=HEX)
    secondary: str = Field(default=DEFAULT_COLORS["secondary"], pattern=HEX)
    accent: str = Field(default=DEFAULT_COLORS["accent"], pattern=HEX)
    sidebar_background: str = Field(
        default=DEFAULT_COLORS["sidebar_background"], pattern=HEX
    )
    sidebar_foreground: str = Field(
        default=DEFAULT_COLORS["sidebar_foreground"], pattern=HEX
    )

    @field_validator("*")
    @classmethod
    def upper(cls, value):
        return value.upper()


class PrintHeader(BaseModel):
    """Printed at the top of receipts and reports."""

    model_config = ConfigDict(extra="forbid")

    address_lines: list[str] = Field(default_factory=list, max_length=3)
    phone: str = Field(default="", max_length=30)
    email: str = Field(default="", max_length=254)
    website: str = Field(default="", max_length=200)
    registration_number: str = Field(default="", max_length=60)
    gstin: str = Field(default="", max_length=15)

    @field_validator("address_lines")
    @classmethod
    def lines(cls, value):
        value = [line.strip() for line in value if line.strip()]
        if any(len(line) > 120 for line in value):  # noqa: PLR2004
            raise ValueError("Each address line can be at most 120 characters")
        return value

    @field_validator("email")
    @classmethod
    def email_format(cls, value):
        value = value.strip()
        if value and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value):
            raise ValueError("Not a valid email address")
        return value

    @field_validator("website")
    @classmethod
    def website_url(cls, value):
        value = value.strip()
        if value and not re.match(r"^https?://[^\s]+$", value):
            raise ValueError("Website must start with http:// or https://")
        return value

    @field_validator("gstin")
    @classmethod
    def gstin_format(cls, value):
        value = value.strip().upper()
        if value and not re.match(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$", value):
            raise ValueError("Not a valid GSTIN")
        return value


class BrandingSpec(BaseModel):
    """What PUT /api/hmis/admin/branding/ accepts. Logos are set through
    their own upload endpoint and kept as they are."""

    model_config = ConfigDict(extra="forbid")

    hospital_name: str = Field(min_length=1, max_length=120)
    short_name: str = Field(default="", max_length=40)
    tagline: str = Field(default="", max_length=150)
    colors: Colors = Field(default_factory=Colors)
    font_family: Literal[FONTS] = "Lexend Deca"
    print_header: PrintHeader = Field(default_factory=PrintHeader)

    @field_validator("hospital_name", "short_name", "tagline", mode="before")
    @classmethod
    def strip(cls, value):
        return value.strip() if isinstance(value, str) else value


def default_branding():
    return {
        **BrandingSpec(hospital_name="Hospital").model_dump(mode="json"),
        "logos": dict.fromkeys(LOGO_KINDS),
    }


def with_defaults(data):
    """A stored copy with any field added since it was saved filled in."""
    base = default_branding()
    data = data or {}
    return {
        **base,
        **{k: v for k, v in data.items() if k in base},
        "logos": {**base["logos"], **(data.get("logos") or {})},
    }


def get_branding(for_update=False):
    qs = Branding.objects.select_for_update() if for_update else Branding.objects
    branding, _ = qs.get_or_create(pk=1, defaults={"draft": default_branding()})
    return branding


# Logos


def logo_url(key):
    if not key:
        return None
    if settings.FACILITY_CDN:
        return f"{settings.FACILITY_CDN}/{key}"
    return f"{settings.FACILITY_S3_BUCKET_EXTERNAL_ENDPOINT}/{settings.FACILITY_S3_BUCKET}/{key}"


def with_logo_urls(data):
    return {
        **data,
        "logos": {kind: logo_url(key) for kind, key in data["logos"].items()},
    }


class LogoError(ValueError):
    pass


def check_logo(upload):
    """Returns (bytes, extension, content type) or raises LogoError."""
    if upload.size > LOGO_MAX_BYTES:
        raise LogoError("Logo must be 1 MB or smaller")
    data = upload.read()
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            fmt, (width, height) = image.format, image.size
    except (UnidentifiedImageError, OSError, SyntaxError) as e:
        raise LogoError("Upload a PNG, JPEG, WebP or ICO image") from e
    if fmt not in LOGO_FORMATS:
        raise LogoError("Upload a PNG, JPEG, WebP or ICO image")
    if min(width, height) < LOGO_MIN_PX or max(width, height) > LOGO_MAX_PX:
        msg = (
            f"Logo must be between {LOGO_MIN_PX} and {LOGO_MAX_PX} pixels on each side"
        )
        raise LogoError(msg)
    ext, content_type = LOGO_FORMATS[fmt]
    return data, ext, content_type


def s3_client():
    config, bucket = get_client_config(BucketType.FACILITY)
    return boto3.client("s3", **config), bucket


def put_logo_object(kind, data, ext, content_type):
    s3, bucket = s3_client()
    key = f"{LOGO_FOLDER}/{kind}_{secrets.token_hex(8)}.{ext}"
    params = {
        "Bucket": bucket,
        "Key": key,
        "Body": data,
        "ContentType": content_type,
        "CacheControl": "public, max-age=31536000, immutable",
    }
    if settings.BUCKET_HAS_FINE_ACL:
        params["ACL"] = "public-read"
    s3.put_object(**params)
    return key


def delete_logo_objects(keys):
    keys = [k for k in keys if k]
    if not keys:
        return
    s3, bucket = s3_client()
    for key in keys:
        try:
            s3.delete_object(Bucket=bucket, Key=key)
        except Exception:
            # An orphaned logo costs a few KB; failing the request costs more.
            logger.warning("Failed to delete old logo %s", key)


def logo_keys(data):
    return set(filter(None, ((data or {}).get("logos") or {}).values()))


def delete_unused(keys, branding):
    """Logo files no longer in the draft or the published copy."""
    in_use = logo_keys(branding.draft) | logo_keys(branding.published)
    transaction.on_commit(lambda: delete_logo_objects(set(keys) - in_use))


# Changes (each one is recorded in hmis_settingchange)


def has_unpublished_changes(branding):
    if branding.published is None:
        return True
    return with_defaults(branding.draft) != with_defaults(branding.published)


@transaction.atomic
def save_draft(request, spec):
    branding = get_branding(for_update=True)
    old = with_defaults(branding.draft)
    new = {**spec.model_dump(mode="json"), "logos": old["logos"]}
    branding.draft = new
    branding.updated_by = request.user
    branding.save(update_fields=["draft", "updated_by", "updated_at"])
    record_setting_change(request, SECTION, "draft", old, new)
    return branding


@transaction.atomic
def set_logo(request, kind, key):
    branding = get_branding(for_update=True)
    draft = with_defaults(branding.draft)
    old_key = draft["logos"][kind]
    draft["logos"][kind] = key
    branding.draft = draft
    branding.updated_by = request.user
    branding.save(update_fields=["draft", "updated_by", "updated_at"])
    record_setting_change(request, SECTION, f"logo.{kind}", old_key, key)
    delete_unused([old_key], branding)
    return branding


@transaction.atomic
def publish(request):
    branding = get_branding(for_update=True)
    old = branding.published
    branding.published = with_defaults(branding.draft)
    branding.version += 1
    branding.published_at = timezone.now()
    branding.published_by = request.user
    branding.save()
    record_setting_change(
        request,
        SECTION,
        "published",
        old,
        {**branding.published, "version": branding.version},
    )
    delete_unused(logo_keys(old), branding)
    return branding


@transaction.atomic
def discard_draft(request):
    """Back to what is published (or the defaults, if nothing ever was)."""
    branding = get_branding(for_update=True)
    old = with_defaults(branding.draft)
    branding.draft = (
        with_defaults(branding.published) if branding.published else default_branding()
    )
    branding.updated_by = request.user
    branding.save(update_fields=["draft", "updated_by", "updated_at"])
    record_setting_change(request, SECTION, "draft_discarded", old, branding.draft)
    delete_unused(logo_keys(old), branding)
    return branding


# Contrast hints for the portal (WCAG 2.1 relative luminance)


def luminance(hex_color):
    def channel(c):
        c = int(c, 16) / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: PLR2004

    r, g, b = (channel(hex_color[i : i + 2]) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def contrast_warnings(data):
    colors = with_defaults(data)["colors"]
    checks = [
        (
            "primary",
            colors["primary"],
            "#FFFFFF",
            "white button text on the primary colour",
            3.0,  # WCAG AA for buttons and bold UI text
        ),
        (
            "sidebar_foreground",
            colors["sidebar_foreground"],
            colors["sidebar_background"],
            "sidebar text on the sidebar background",
            4.5,  # WCAG AA for normal text
        ),
    ]
    warnings = []
    for field, fg, bg, what, minimum in checks:
        ratio = contrast(fg, bg)
        if ratio < minimum:
            warnings.append(
                {
                    "field": f"colors.{field}",
                    "ratio": round(ratio, 2),
                    "message": f"Contrast of {what} is {ratio:.1f}:1; {minimum:g}:1 or more is easier to read",
                }
            )
    return warnings
