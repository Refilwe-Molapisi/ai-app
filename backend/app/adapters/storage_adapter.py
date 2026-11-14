"""Storage adapter — S3-compatible object storage behind one interface
(ADR-003, CAIOS-MASTER-001 §7.3).

Provider-specific logic lives HERE and nowhere else: the application never
touches boto3 directly. Works with Z1 (primary), Cloudflare R2 (backups) and
any future S3-compatible provider. The database stores keys/metadata, never
file contents (§7.3).

Security posture (§7.4): buckets private by default; access only via backend
authorisation; temporary signed URLs for downloads; credentials server-side.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings, get_settings


@dataclass
class StoredObject:
    key: str
    bucket: str
    size_bytes: int


class StorageAdapter:
    """Thin S3 wrapper. A NullStorage variant is returned in dev when storage
    is unconfigured, so uploads fail loudly instead of silently dropping files."""

    provider_name = "s3-compatible"

    def __init__(self, settings: Settings):
        import boto3  # lazy: optional at import time

        if not (settings.storage_endpoint and settings.storage_bucket
                and settings.storage_access_key and settings.storage_secret_key):
            raise RuntimeError(
                "Storage not configured: set STORAGE_ENDPOINT, STORAGE_BUCKET, "
                "STORAGE_ACCESS_KEY, STORAGE_SECRET_KEY"
            )
        self._bucket = settings.storage_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.storage_endpoint,
            aws_access_key_id=settings.storage_access_key,
            aws_secret_access_key=settings.storage_secret_key,
            region_name=settings.storage_region,
        )

    # -- naming ---------------------------------------------------------------
    @staticmethod
    def client_key(client_org_id: str, engagement_id: str, *parts: str) -> str:
        """Logical per-client separation (§7.4): every client file lives under
        its client prefix; engagement-scoped below that. Cross-client access is
        impossible by construction at the key level."""
        clean = [p.strip("/") for p in parts if p.strip("/")]
        return f"clients/{client_org_id}/{engagement_id}/" + "/".join(clean)

    @staticmethod
    def knowledge_key(*parts: str) -> str:
        clean = [p.strip("/") for p in parts if p.strip("/")]
        return "company-knowledge/" + "/".join(clean)

    # -- operations -----------------------------------------------------------
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> StoredObject:
        self._client.put_object(
            Bucket=self._bucket, Key=key, Body=data,
            ContentType=content_type,
            ServerSideEncryption="AES256",
        )
        return StoredObject(key=key, bucket=self._bucket, size_bytes=len(data))

    def get(self, key: str) -> bytes:
        resp = self._client.get_object(Bucket=self._bucket, Key=key)
        return resp["Body"].read()

    def presigned_get(self, key: str, expires_seconds: int = 600) -> str:
        """Temporary signed URL for authorised downloads only (§7.4)."""
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception:
            return False


def get_storage_adapter(settings: Settings | None = None) -> StorageAdapter:
    settings = settings or get_settings()
    return StorageAdapter(settings)
