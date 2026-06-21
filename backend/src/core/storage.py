"""
MinIO storage wrapper (ADR-002).

Структура ключей:
- text-items/{project_id}/{run_id}/{text_item_id}.txt
- results/{run_id}/result.csv
- uploads-tmp/{upload_id}/{original_filename}  (временный, чистится GC)

Используется как из API (FastAPI handlers), так и из workers (Celery/TaskIQ).
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import timedelta

import structlog
from minio import Minio
from minio.error import S3Error

from core.config import settings

log = structlog.get_logger(__name__)


class StorageError(Exception):
    pass


class ObjectStore:
    """Тонкая обёртка над minio SDK, унифицирующая бакеты и upload-патерны."""

    def __init__(self) -> None:
        self._client = Minio(
            endpoint=settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ROOT_USER,
            secret_key=settings.MINIO_ROOT_PASSWORD,
            secure=settings.MINIO_USE_HTTPS,
            region=settings.MINIO_REGION,
        )

    # ─── Bucket management ────────────────────────────────────────────

    def ensure_buckets(self) -> None:
        """Создать дефолтные бакеты если их ещё нет. Idempotent."""
        for bucket in (
            settings.MINIO_BUCKET_TEXT_ITEMS,
            settings.MINIO_BUCKET_RESULTS,
            settings.MINIO_BUCKET_UPLOADS,
        ):
            try:
                if not self._client.bucket_exists(bucket):
                    self._client.make_bucket(bucket, location=settings.MINIO_REGION)
                    log.info("storage.bucket.created", bucket=bucket)
            except S3Error as e:
                log.error("storage.bucket.error", bucket=bucket, error=str(e))
                raise StorageError(f"Cannot ensure bucket {bucket}: {e}") from e

    # ─── Object operations ────────────────────────────────────────────

    def put_bytes(
        self,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> None:
        try:
            self._client.put_object(
                bucket_name=bucket,
                object_name=key,
                data=io.BytesIO(data),
                length=len(data),
                content_type=content_type,
            )
        except S3Error as e:
            raise StorageError(f"put_bytes failed for {bucket}/{key}: {e}") from e

    def put_stream(
        self,
        bucket: str,
        key: str,
        stream: io.BufferedIOBase,
        length: int,
        content_type: str = "application/octet-stream",
    ) -> None:
        try:
            self._client.put_object(
                bucket_name=bucket,
                object_name=key,
                data=stream,
                length=length,
                content_type=content_type,
            )
        except S3Error as e:
            raise StorageError(f"put_stream failed for {bucket}/{key}: {e}") from e

    def get_bytes(self, bucket: str, key: str) -> bytes:
        try:
            resp = self._client.get_object(bucket, key)
            try:
                return resp.read()
            finally:
                resp.close()
                resp.release_conn()
        except S3Error as e:
            raise StorageError(f"get_bytes failed for {bucket}/{key}: {e}") from e

    def delete(self, bucket: str, key: str) -> None:
        try:
            self._client.remove_object(bucket, key)
        except S3Error as e:
            raise StorageError(f"delete failed for {bucket}/{key}: {e}") from e

    def list_prefix(self, bucket: str, prefix: str) -> Iterator[str]:
        try:
            for obj in self._client.list_objects(bucket, prefix=prefix, recursive=True):
                yield obj.object_name
        except S3Error as e:
            raise StorageError(f"list_prefix failed for {bucket}/{prefix}: {e}") from e

    def list_prefix_meta(self, bucket: str, prefix: str = ""):
        """Как list_prefix, но возвращает minio Object с метаданными (last_modified, size)."""
        try:
            for obj in self._client.list_objects(bucket, prefix=prefix, recursive=True):
                yield obj
        except S3Error as e:
            raise StorageError(f"list_prefix_meta failed for {bucket}/{prefix}: {e}") from e

    def presigned_url(self, bucket: str, key: str, ttl: timedelta = timedelta(minutes=5)) -> str:
        try:
            return self._client.presigned_get_object(bucket, key, expires=ttl)
        except S3Error as e:
            raise StorageError(f"presigned_url failed for {bucket}/{key}: {e}") from e

    # ─── Health ───────────────────────────────────────────────────────

    def ping(self) -> bool:
        try:
            # list_buckets — самый дешёвый round-trip
            self._client.list_buckets()
            return True
        except Exception:
            return False


# Singleton — переиспользуется во всех модулях
storage = ObjectStore()
