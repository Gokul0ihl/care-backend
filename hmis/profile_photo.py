"""Patient profile photos from the mobile app.

The app gets a presigned PUT for the private patient bucket, uploads the image
straight to S3, then saves the returned id with PATCH /users/me. The photo is a
Care FileUpload linked to the patient (file_type "patient", associating_id the
patient's external id, category "profile_photo"); patient.meta's mobile_profile
points at the current one. Responses carry a short-lived presigned GET.
"""

import logging
from datetime import timedelta

import boto3
from botocore.exceptions import ClientError
from django.utils import timezone
from rest_framework.exceptions import Throttled, ValidationError

from care.emr.models import FileUpload
from care.emr.resources.file_upload.spec import (
    FileCategoryChoices,
    FileTypeChoices,
)
from care.utils.csp.config import BucketType, get_client_config

# Logs carry ids only: never names, phone numbers or presigned URLs (they grant access).
logger = logging.getLogger(__name__)

PHOTO_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
MAX_PHOTO_BYTES = 5 * 1024 * 1024
UPLOAD_URL_LIFETIME = 10 * 60
READ_URL_LIFETIME = 60 * 60
# Unsaved uploads are removed after this; it also bounds open uploads per patient.
STALE_UPLOAD_AGE = timedelta(hours=24)
MAX_OPEN_UPLOADS = 10


def photo_uploads(patient):
    return FileUpload.objects.filter(
        file_type=FileTypeChoices.patient.value,
        associating_id=str(patient.external_id),
        file_category=FileCategoryChoices.profile_photo.value,
    )


def start_upload(patient, account, mime_type):
    open_uploads = photo_uploads(patient).filter(
        upload_completed=False,
        created_date__gt=timezone.now() - STALE_UPLOAD_AGE,
    )
    if open_uploads.count() >= MAX_OPEN_UPLOADS:
        logger.warning(
            "profile_photo.upload_throttled patient=%s open_uploads=%s",
            patient.external_id,
            MAX_OPEN_UPLOADS,
        )
        raise Throttled(detail="Too many photo uploads. Try again later.")
    upload = FileUpload(
        name="profile_photo",
        internal_name=f"profile_photo.{PHOTO_TYPES[mime_type]}",
        file_type=FileTypeChoices.patient.value,
        associating_id=str(patient.external_id),
        file_category=FileCategoryChoices.profile_photo.value,
        meta={
            "mime_type": mime_type,
            "source": "mobile_app",
            "account_id": str(account.id),
        },
    )
    upload.save()
    try:
        url = upload.files_manager.signed_url(upload, duration=UPLOAD_URL_LIFETIME)
    except Exception:
        logger.exception(
            "profile_photo.upload_sign_failed patient=%s upload=%s",
            patient.external_id,
            upload.external_id,
        )
        raise
    logger.info(
        "profile_photo.upload_started patient=%s upload=%s mime_type=%s",
        patient.external_id,
        upload.external_id,
        mime_type,
    )
    return {
        "upload_id": str(upload.external_id),
        "upload_url": url,
        "method": "PUT",
        "headers": {"Content-Type": mime_type},
        "expires_in": UPLOAD_URL_LIFETIME,
        "max_bytes": MAX_PHOTO_BYTES,
    }


def s3_client():
    config, bucket = get_client_config(BucketType.PATIENT)
    return boto3.client("s3", **config), bucket


def object_key(upload):
    return f"{upload.file_type}/{upload.internal_name}"


def stored_object(upload):
    """The uploaded object's metadata, or None while nothing is there."""
    s3, bucket = s3_client()
    try:
        return s3.head_object(Bucket=bucket, Key=object_key(upload))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
            return None
        raise


def delete_object(upload):
    s3, bucket = s3_client()
    s3.delete_object(Bucket=bucket, Key=object_key(upload))


def rejected(patient, file_id, reason, message, **details):
    logger.warning(
        "profile_photo.save_rejected patient=%s upload=%s reason=%s %s",
        patient.external_id,
        file_id,
        reason,
        " ".join(f"{key}={value}" for key, value in details.items()),
    )
    return ValidationError({"profile_photo_id": message})


def finished_upload(patient, file_id):
    """The patient's own photo upload, checked in S3 the first time it is used."""
    upload = (
        photo_uploads(patient)
        .filter(external_id=file_id, is_archived=False)
        .select_for_update()
        .first()
    )
    if upload is None:
        raise rejected(patient, file_id, "unknown", "Unknown photo upload.")
    if upload.upload_completed:
        return upload
    try:
        stored = stored_object(upload)
    except Exception:
        logger.exception(
            "profile_photo.s3_check_failed patient=%s upload=%s",
            patient.external_id,
            file_id,
        )
        raise
    if stored is None:
        raise rejected(
            patient,
            file_id,
            "not_uploaded",
            "Photo not uploaded yet. Upload it, then save.",
        )
    size = stored["ContentLength"]
    content_type = stored.get("ContentType")
    if not 0 < size <= MAX_PHOTO_BYTES:
        delete_object(upload)
        raise rejected(
            patient,
            file_id,
            "size",
            "Photo must be non-empty and 5 MB or smaller.",
            bytes=size,
        )
    if content_type != upload.meta["mime_type"]:
        delete_object(upload)
        raise rejected(
            patient,
            file_id,
            "content_type",
            "Upload the image with the type you requested.",
            expected=upload.meta["mime_type"],
            received=content_type,
        )
    upload.upload_completed = True
    upload.save(update_fields=["upload_completed", "modified_date"])
    logger.info(
        "profile_photo.upload_verified patient=%s upload=%s bytes=%s mime_type=%s",
        patient.external_id,
        file_id,
        size,
        content_type,
    )
    return upload


def current_photo_id(patient):
    return (patient.meta.get("mobile_profile") or {}).get("photo_file_id")


def set_photo(patient, file_id):
    """Points the (locked) patient at a new photo, or none, and archives the old one.
    The caller saves the patient."""
    current = current_photo_id(patient)
    if file_id is not None:
        file_id = str(file_id)
        if file_id == current:
            return
        finished_upload(patient, file_id)
    elif current is None:
        return
    if current:
        photo_uploads(patient).filter(external_id=current).update(
            is_archived=True,
            archive_reason="Replaced from the mobile app"
            if file_id
            else "Removed from the mobile app",
            archived_datetime=timezone.now(),
        )
    profile = {**patient.meta.get("mobile_profile", {})}
    if file_id:
        profile["photo_file_id"] = file_id
    else:
        profile.pop("photo_file_id", None)
    patient.meta = {**patient.meta, "mobile_profile": profile}
    logger.info(
        "profile_photo.%s patient=%s upload=%s previous=%s",
        "saved" if file_id else "removed",
        patient.external_id,
        file_id,
        current,
    )


def photo_url(patient):
    """A presigned GET for the current photo; None if there is none or staff archived it."""
    file_id = current_photo_id(patient)
    if not file_id:
        return None
    upload = (
        photo_uploads(patient)
        .filter(external_id=file_id, upload_completed=True, is_archived=False)
        .first()
    )
    if upload is None:
        return None
    try:
        return upload.files_manager.read_signed_url(upload, duration=READ_URL_LIFETIME)
    except Exception:
        logger.exception(
            "profile_photo.read_sign_failed patient=%s upload=%s",
            patient.external_id,
            file_id,
        )
        return None


def remove_stale_uploads():
    """Deletes photo uploads that were never saved to a profile."""
    stale = FileUpload.objects.filter(
        file_type=FileTypeChoices.patient.value,
        file_category=FileCategoryChoices.profile_photo.value,
        upload_completed=False,
        created_date__lt=timezone.now() - STALE_UPLOAD_AGE,
    )
    removed = failed = 0
    for upload in stale:
        try:
            delete_object(upload)
        except Exception:
            logger.warning(
                "profile_photo.stale_delete_failed upload=%s",
                upload.external_id,
                exc_info=True,
            )
            failed += 1
            continue
        upload.delete()
        removed += 1
    logger.info("profile_photo.stale_cleanup removed=%s failed=%s", removed, failed)
    return removed
