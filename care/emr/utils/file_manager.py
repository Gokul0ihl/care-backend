import logging
import re
import unicodedata
from urllib.parse import quote

import boto3
from botocore.exceptions import ClientError

from care.utils.csp.config import get_client_config

logger = logging.getLogger(__name__)


SAFE_INLINE_FORMATS = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/tiff",
    "image/bmp",
    "image/x-icon",
    "application/pdf",
}


PLAIN_FILENAME = re.compile(r"[ -~]+")
UNSAFE_FILENAME_CHARS = re.compile(r'["\\/;,]')
# Em and en dashes have no ASCII decomposition, so NFKD alone would drop them.
DASHES = {0x2014: "-", 0x2013: "-"}  # em dash, en dash


def content_disposition(disposition, filename):
    """S3 signs the header into the URL and refuses anything outside
    ISO-8859-1, so names like "ID — Passport" (em dash) or in other scripts
    made every read URL fail. Those get an ASCII fallback plus the real name
    as RFC 5987 filename*; plain names keep the old header."""
    if PLAIN_FILENAME.fullmatch(filename) and not UNSAFE_FILENAME_CHARS.search(
        filename
    ):
        return f"{disposition}; filename={filename}"
    ascii_name = (
        unicodedata.normalize("NFKD", filename.translate(DASHES))
        .encode("ascii", "ignore")
        .decode()
    )
    ascii_name = UNSAFE_FILENAME_CHARS.sub("-", ascii_name).strip() or "file"
    return (
        f'{disposition}; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


class FileManager:
    """
    A utility class to manage all file management related operations
    """


class S3FilesManager(FileManager):
    bucket_type = None

    def __init__(self, bucket_type):
        self.bucket_type = bucket_type

    def signed_url(self, file_obj, duration=60 * 60, mime_type=None):
        config, bucket_name = get_client_config(self.bucket_type, external=True)
        s3 = boto3.client("s3", **config)
        params = {
            "Bucket": bucket_name,
            "Key": f"{file_obj.file_type}/{file_obj.internal_name}",
        }

        _mime_type = file_obj.meta.get("mime_type") or mime_type
        if _mime_type:
            params["ContentType"] = _mime_type
        return s3.generate_presigned_url(
            "put_object",
            Params=params,
            ExpiresIn=duration,  # seconds
        )

    def read_signed_url(self, file_obj, duration=60 * 60):
        config, bucket_name = get_client_config(self.bucket_type, external=True)
        s3 = boto3.client("s3", **config)

        mime_type = file_obj.meta.get("mime_type")
        disposition = "inline" if mime_type in SAFE_INLINE_FORMATS else "attachment"

        return s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket_name,
                "Key": f"{file_obj.file_type}/{file_obj.internal_name}",
                "ResponseContentDisposition": content_disposition(
                    disposition, f"{file_obj.name}{file_obj.get_extension()}"
                ),
            },
            ExpiresIn=duration,  # seconds
        )

    def put_object(self, file_obj, file, **kwargs):
        config, bucket_name = get_client_config(self.bucket_type)
        s3 = boto3.client("s3", **config)
        return s3.put_object(
            Body=file,
            Bucket=bucket_name,
            Key=f"{file_obj.file_type}/{file_obj.internal_name}",
            **kwargs,
        )

    def get_object(self, file_obj, **kwargs):
        config, bucket_name = get_client_config(self.bucket_type)
        s3 = boto3.client("s3", **config)
        return s3.get_object(
            Bucket=bucket_name,
            Key=f"{file_obj.file_type}/{file_obj.internal_name}",
            **kwargs,
        )

    def file_contents(self, file_obj):
        response = self.get_object(file_obj)
        content_type = response["ContentType"]
        content = response["Body"].read()
        return content_type, content

    def delete_object(self, file_obj, quiet=False, **kwargs):
        config, bucket_name = get_client_config(self.bucket_type)
        s3 = boto3.client("s3", **config)

        try:
            return s3.delete_object(
                Bucket=bucket_name,
                Key=f"{file_obj.file_type}/{file_obj.internal_name}",
                **kwargs,
            )
        except s3.exceptions.NoSuchKey as e:
            if not quiet:
                raise e
            msg = f"Object not found: {file_obj.file_type}/{file_obj.internal_name}"
            logger.debug(msg)

    def delete_objects(self, file_obj_list, quiet=False, **kwargs):
        config, bucket_name = get_client_config(self.bucket_type)
        s3 = boto3.client("s3", **config)

        keys = [
            f"{file_obj.file_type}/{file_obj.internal_name}"
            for file_obj in file_obj_list
        ]
        objects = [{"Key": key} for key in keys]

        try:
            return s3.delete_objects(
                Bucket=bucket_name,
                Delete={"Objects": objects, "Quiet": quiet},
                **kwargs,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "NotImplemented":
                # bulk delete is not supported by some providers: GCP
                msg = f"Batch delete objects not implemented for {self.bucket_type.value} bucket"
                raise NotImplementedError(msg) from e
            raise
