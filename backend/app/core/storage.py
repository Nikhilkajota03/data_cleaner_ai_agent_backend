import io
import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import urlparse

from minio import Minio
from minio.error import S3Error

from backend.app.core.config import settings


class CustomJSONEncoder(json.JSONEncoder):
    """Encodes UUIDs, datetimes, and Decimals to JSON-serializable primitives."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, uuid.UUID):
            return str(obj)
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


class StorageManager:
    """Manages S3 object storage operations and payload offloading to MinIO."""

    def __init__(self) -> None:
        self.bucket_name = settings.MINIO_BUCKET_NAME
        self.threshold_bytes = settings.PAYLOAD_OFFLOAD_THRESHOLD_BYTES
        self.client = Minio(
            endpoint=settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            secure=settings.MINIO_SECURE,
        )

    def ensure_bucket_exists(self, bucket_name: Optional[str] = None) -> None:
        """Ensures the target MinIO bucket exists, creating it if necessary."""
        target_bucket = bucket_name or self.bucket_name
        try:
            if not self.client.bucket_exists(target_bucket):
                self.client.make_bucket(target_bucket)
        except S3Error as e:
            raise RuntimeError(
                f"Failed to verify or create bucket '{target_bucket}': {e}"
            ) from e

    def offload_if_exceeds_threshold(
        self,
        payload: Any,
        file_prefix: str = "payload",
    ) -> tuple[Any, Optional[str]]:
        """
        Inspects payload size. If smaller than threshold (5KB), returns the payload
        inline without uploading. If >= threshold, uploads to MinIO and returns a
        compact summary with the s3:// URI pointer.
        """
        raw_json_str = json.dumps(payload, cls=CustomJSONEncoder)
        raw_bytes = raw_json_str.encode("utf-8")
        payload_size = len(raw_bytes)

        # 1. Payload is within inline threshold (< 5KB)
        if payload_size < self.threshold_bytes:
            return payload, None

        # 2. Payload exceeds 5KB: Offload to MinIO cold storage
        self.ensure_bucket_exists(self.bucket_name)

        date_str = datetime.utcnow().strftime("%Y%m%d")
        object_name = f"{file_prefix}/{date_str}/{uuid.uuid4()}.json"

        self.client.put_object(
            bucket_name=self.bucket_name,
            object_name=object_name,
            data=io.BytesIO(raw_bytes),
            length=payload_size,
            content_type="application/json",
        )

        s3_uri = f"s3://{self.bucket_name}/{object_name}"

        # Generate a compact preview representation for inline state
        summary_sample: Any
        if isinstance(payload, list):
            summary_sample = payload[:3]
        elif isinstance(payload, dict):
            summary_sample = {k: payload[k] for k in list(payload.keys())[:3]}
        else:
            summary_sample = str(payload)[:200]

        offloaded_summary = {
            "_offloaded_to_s3": True,
            "s3_uri": s3_uri,
            "size_bytes": payload_size,
            "record_count": len(payload) if isinstance(payload, list) else 1,
            "preview_sample": summary_sample,
        }

        return offloaded_summary, s3_uri

    def retrieve_payload(self, s3_uri: str) -> Any:
        """Downloads and deserializes a previously offloaded JSON payload from S3."""
        parsed = urlparse(s3_uri)
        if parsed.scheme != "s3":
            raise ValueError(f"Invalid S3 URI scheme: {s3_uri}")

        bucket_name = parsed.netloc
        object_name = parsed.path.lstrip("/")

        response = None
        try:
            response = self.client.get_object(bucket_name, object_name)
            data_bytes = response.read()
            return json.loads(data_bytes.decode("utf-8"))
        finally:
            if response is not None:
                response.close()
                response.release_conn()


storage_manager = StorageManager()
