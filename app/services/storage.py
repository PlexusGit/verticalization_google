"""Google Cloud Storage operations used by the app pipeline."""
from __future__ import annotations

from datetime import timedelta
import os

from google.cloud import storage

from app.config import settings

_client: storage.Client | None = None


def get_client() -> storage.Client:
    """Build one storage client using local credentials or workload identity."""

    global _client
    if _client is not None:
        return _client
    kwargs: dict = {"project": settings.PROJECT_ID}
    path = settings.GOOGLE_APPLICATION_CREDENTIALS
    if path and os.path.isfile(path):
        from google.oauth2 import service_account

        kwargs["credentials"] = service_account.Credentials.from_service_account_file(path)
    _client = storage.Client(**kwargs)
    return _client


def bucket(name: str | None = None) -> storage.Bucket:
    return get_client().bucket(name or settings.BUCKET_NAME)


def upload_file(local_path: str, object_path: str, content_type: str = "application/octet-stream") -> str:
    bucket().blob(object_path).upload_from_filename(local_path, content_type=content_type)
    return object_path


def upload_bytes(data: bytes, object_path: str, content_type: str = "application/octet-stream") -> str:
    bucket().blob(object_path).upload_from_string(data, content_type=content_type)
    return object_path


def download_to_file(object_path: str, local_path: str, bucket_name: str | None = None) -> str:
    bucket(bucket_name).blob(object_path).download_to_filename(local_path)
    return local_path


def object_exists(object_path: str, bucket_name: str | None = None) -> bool:
    return bucket(bucket_name).blob(object_path).exists()


def copy_object(source_bucket: str, source_path: str, target_path: str) -> str:
    """Copy an importer object into the managed bucket without proxying bytes."""

    source = bucket(source_bucket)
    source_blob = source.blob(source_path)
    if not source_blob.exists():
        raise FileNotFoundError(f"gs://{source_bucket}/{source_path} does not exist")
    source.copy_blob(source_blob, bucket(), target_path)
    return target_path


def create_resumable_upload_session(
    object_path: str,
    content_type: str,
    origin: str,
    size: int | None = None,
) -> str:
    """Create a direct browser-to-GCS upload URL."""

    return bucket().blob(object_path).create_resumable_upload_session(
        content_type=content_type,
        origin=origin,
        size=size if size and size > 0 else None,
    )


def signed_url(object_path: str, minutes: int = 120) -> str:
    """Generate a V4 URL with either a key or Cloud Run IAM signBlob."""

    blob = bucket().blob(object_path)
    path = settings.GOOGLE_APPLICATION_CREDENTIALS
    if path and os.path.isfile(path):
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(path)
        return blob.generate_signed_url(version="v4", expiration=timedelta(minutes=minutes), credentials=credentials)

    from google.auth import default
    from google.auth.transport.requests import Request

    credentials, _ = default()
    credentials.refresh(Request())
    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=minutes),
        service_account_email=getattr(credentials, "service_account_email", None),
        access_token=credentials.token,
    )


def delete_prefix(prefix: str) -> None:
    """Delete all managed artifacts belonging to one video."""

    for blob in get_client().list_blobs(settings.BUCKET_NAME, prefix=prefix):
        blob.delete()
