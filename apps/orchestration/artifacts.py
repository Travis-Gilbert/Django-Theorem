"""Tenant-bound Arrow IPC artifacts backed by Neon S3-compatible storage."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import boto3
import pyarrow as pa
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings


ARROW_IPC_CONTENT_TYPE = "application/vnd.apache.arrow.stream"
SHA256_PREFIX = "sha256:"


class ArtifactConfigurationError(RuntimeError):
    """The trusted control-plane process lacks usable object-storage settings."""


class ArtifactValidationError(ValueError):
    """A key, digest, or Arrow payload violates the offload artifact contract."""


class ArtifactStorageError(RuntimeError):
    """The object store could not complete an otherwise valid artifact operation."""


@dataclass(frozen=True)
class StoredArrowArtifact:
    artifact_key: str
    payload_digest: str
    schema_json: str
    rows: int


@dataclass(frozen=True)
class StoredContentArtifact:
    artifact_key: str
    payload_digest: str
    media_type: str
    byte_length: int


def sha256_digest(payload: bytes) -> str:
    return f"{SHA256_PREFIX}{hashlib.sha256(payload).hexdigest()}"


def is_sha256_digest(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith(SHA256_PREFIX):
        return False
    digest = value.removeprefix(SHA256_PREFIX)
    return len(digest) == 64 and all(
        character in "0123456789abcdef" for character in digest
    )


def arrow_schema_json(schema: pa.Schema) -> str:
    """Return a deterministic, wire-friendly representation of an Arrow schema."""
    metadata = {
        key.decode("utf-8", errors="replace"): value.decode("utf-8", errors="replace")
        for key, value in sorted((schema.metadata or {}).items())
    }
    return json.dumps(
        {
            "fields": [
                {
                    "name": field.name,
                    "type": str(field.type),
                    "nullable": field.nullable,
                }
                for field in schema
            ],
            "metadata": metadata,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def encode_arrow_ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def decode_arrow_ipc(payload: bytes) -> pa.Table:
    try:
        return pa.ipc.open_stream(pa.py_buffer(payload)).read_all()
    except (pa.ArrowException, ValueError, OSError) as exc:
        raise ArtifactValidationError(
            "artifact is not a valid Arrow IPC stream"
        ) from exc


class ArtifactStore:
    """Owns trusted S3 access and emits one-object capability URLs for workers."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        region: str,
        bucket: str,
        presign_seconds: int,
        max_bytes: int,
        client: Any | None = None,
    ) -> None:
        values = {
            "ARTIFACT_S3_ENDPOINT_URL": endpoint_url,
            "ARTIFACT_S3_ACCESS_KEY_ID": access_key_id,
            "ARTIFACT_S3_SECRET_ACCESS_KEY": secret_access_key,
            "ARTIFACT_S3_REGION": region,
            "ARTIFACT_S3_BUCKET": bucket,
        }
        missing = [name for name, value in values.items() if not value.strip()]
        if missing:
            raise ArtifactConfigurationError(f"missing {', '.join(missing)}")
        if presign_seconds <= 0 or max_bytes <= 0:
            raise ArtifactConfigurationError(
                "artifact expiry and maximum bytes must be positive"
            )

        self.bucket = bucket
        self.presign_seconds = presign_seconds
        self.max_bytes = max_bytes
        self.client = client or boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            # Neon object storage presents TLS for its endpoint hostname. Keep
            # the bucket in the URL path rather than making it a subdomain.
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    @classmethod
    def from_settings(cls) -> "ArtifactStore":
        return cls(
            endpoint_url=settings.ARTIFACT_S3_ENDPOINT_URL,
            access_key_id=settings.ARTIFACT_S3_ACCESS_KEY_ID,
            secret_access_key=settings.ARTIFACT_S3_SECRET_ACCESS_KEY,
            region=settings.ARTIFACT_S3_REGION,
            bucket=settings.ARTIFACT_S3_BUCKET,
            presign_seconds=settings.ARTIFACT_PRESIGN_SECONDS,
            max_bytes=settings.ARTIFACT_MAX_BYTES,
        )

    @staticmethod
    def tenant_prefix(tenant_id: UUID) -> str:
        return f"tenants/{tenant_id}/"

    def validate_key(self, tenant_id: UUID, artifact_key: str) -> str:
        if not isinstance(artifact_key, str) or not artifact_key:
            raise ArtifactValidationError("artifact_key is required")
        if (
            len(artifact_key) > 512
            or "\\" in artifact_key
            or ".." in artifact_key.split("/")
        ):
            raise ArtifactValidationError("artifact_key is malformed")
        if not artifact_key.startswith(self.tenant_prefix(tenant_id)):
            raise ArtifactValidationError(
                "artifact_key is outside the admitted tenant prefix"
            )
        return artifact_key

    @staticmethod
    def _storage_call(action: str, call):
        """Hide provider details while preserving a controlled failure boundary."""
        try:
            return call()
        except (BotoCoreError, ClientError) as exc:
            raise ArtifactStorageError(f"artifact storage {action} failed") from exc

    def allocate_input_key(self, tenant_id: UUID) -> str:
        return f"{self.tenant_prefix(tenant_id)}inputs/{uuid4()}.arrow"

    def output_key(self, tenant_id: UUID, operation_id: str) -> str:
        operation_hash = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        return f"{self.tenant_prefix(tenant_id)}outputs/{operation_hash}.arrow"

    @staticmethod
    def _content_digest(value: str) -> str:
        if not is_sha256_digest(value):
            raise ArtifactValidationError("artifact identity must be a sha256 digest")
        return value.removeprefix(SHA256_PREFIX)

    def competence_artifact_key(
        self,
        tenant_id: UUID,
        project_id: UUID,
        candidate_id: str,
        artifact_kind: str,
        payload_digest: str,
    ) -> str:
        if not candidate_id.strip() or artifact_kind not in {
            "scorer_model",
            "prior_pack",
        }:
            raise ArtifactValidationError("competence artifact scope is malformed")
        candidate_hash = hashlib.sha256(candidate_id.encode("utf-8")).hexdigest()
        digest = self._content_digest(payload_digest)
        return (
            f"{self.tenant_prefix(tenant_id)}projects/{project_id}/competence/"
            f"{candidate_hash}/{artifact_kind}/{digest}"
        )

    def validate_competence_key(
        self,
        tenant_id: UUID,
        project_id: UUID,
        candidate_id: str,
        artifact_key: str,
    ) -> str:
        key = self.validate_key(tenant_id, artifact_key)
        candidate_hash = hashlib.sha256(candidate_id.encode("utf-8")).hexdigest()
        prefix = (
            f"{self.tenant_prefix(tenant_id)}projects/{project_id}/competence/"
            f"{candidate_hash}/"
        )
        if not key.startswith(prefix):
            raise ArtifactValidationError(
                "artifact_key is outside the admitted competence project/candidate scope"
            )
        return key

    def write_competence_artifact(
        self,
        tenant_id: UUID,
        project_id: UUID,
        candidate_id: str,
        artifact_kind: str,
        payload: bytes,
        media_type: str,
    ) -> StoredContentArtifact:
        expected_media_types = {
            "scorer_model": "application/vnd.theorem.competence-scorer+json",
            "prior_pack": "application/vnd.theorem.prior-pack+json",
        }
        if expected_media_types.get(artifact_kind) != media_type:
            raise ArtifactValidationError(
                "competence artifact kind and media type do not match"
            )
        if not payload or len(payload) > self.max_bytes:
            raise ArtifactValidationError(
                "competence artifact must be non-empty and within ARTIFACT_MAX_BYTES"
            )
        payload_digest = sha256_digest(payload)
        key = self.competence_artifact_key(
            tenant_id,
            project_id,
            candidate_id,
            artifact_kind,
            payload_digest,
        )
        self._storage_call(
            "write competence artifact",
            lambda: self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=payload,
                ContentType=media_type,
            ),
        )
        recovered = self.get_bytes(tenant_id, key)
        if recovered != payload:
            raise ArtifactValidationError(
                "competence artifact readback does not match published bytes"
            )
        return StoredContentArtifact(
            artifact_key=key,
            payload_digest=payload_digest,
            media_type=media_type,
            byte_length=len(payload),
        )

    def delete_competence_artifact(
        self,
        tenant_id: UUID,
        project_id: UUID,
        candidate_id: str,
        artifact_key: str,
    ) -> None:
        key = self.validate_competence_key(
            tenant_id,
            project_id,
            candidate_id,
            artifact_key,
        )
        self._storage_call(
            "delete",
            lambda: self.client.delete_object(Bucket=self.bucket, Key=key),
        )

    def presign_get(self, tenant_id: UUID, artifact_key: str) -> str:
        key = self.validate_key(tenant_id, artifact_key)
        return self._storage_call(
            "presign GET",
            lambda: self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=self.presign_seconds,
                HttpMethod="GET",
            ),
        )

    def presign_put(self, tenant_id: UUID, artifact_key: str) -> str:
        key = self.validate_key(tenant_id, artifact_key)
        return self._storage_call(
            "presign PUT",
            lambda: self.client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self.bucket,
                    "Key": key,
                    "ContentType": ARROW_IPC_CONTENT_TYPE,
                },
                ExpiresIn=self.presign_seconds,
                HttpMethod="PUT",
            ),
        )

    def get_bytes(self, tenant_id: UUID, artifact_key: str) -> bytes:
        key = self.validate_key(tenant_id, artifact_key)

        def read() -> bytes:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            content_length = int(response.get("ContentLength", 0))
            if content_length > self.max_bytes:
                raise ArtifactValidationError("artifact exceeds ARTIFACT_MAX_BYTES")
            payload = response["Body"].read(self.max_bytes + 1)
            if len(payload) > self.max_bytes:
                raise ArtifactValidationError("artifact exceeds ARTIFACT_MAX_BYTES")
            return payload

        return self._storage_call("read", read)

    def read_arrow(
        self,
        tenant_id: UUID,
        artifact_key: str,
        *,
        expected_digest: str = "",
        expected_schema_json: str = "",
        expected_rows: int | None = None,
    ) -> StoredArrowArtifact:
        payload = self.get_bytes(tenant_id, artifact_key)
        actual_digest = sha256_digest(payload)
        if expected_digest and actual_digest != expected_digest:
            raise ArtifactValidationError(
                "artifact payload digest does not match the descriptor"
            )
        table = decode_arrow_ipc(payload)
        schema_json = arrow_schema_json(table.schema)
        if expected_schema_json and schema_json != expected_schema_json:
            raise ArtifactValidationError(
                "artifact schema does not match the descriptor"
            )
        if expected_rows is not None and table.num_rows != expected_rows:
            raise ArtifactValidationError(
                "artifact row count does not match the descriptor"
            )
        return StoredArrowArtifact(
            artifact_key=artifact_key,
            payload_digest=actual_digest,
            schema_json=schema_json,
            rows=table.num_rows,
        )

    def read_table(
        self,
        tenant_id: UUID,
        artifact_key: str,
        *,
        expected_digest: str = "",
        expected_schema_json: str = "",
        expected_rows: int | None = None,
    ) -> pa.Table:
        payload = self.get_bytes(tenant_id, artifact_key)
        actual_digest = sha256_digest(payload)
        if expected_digest and actual_digest != expected_digest:
            raise ArtifactValidationError(
                "artifact payload digest does not match the descriptor"
            )
        table = decode_arrow_ipc(payload)
        schema_json = arrow_schema_json(table.schema)
        if expected_schema_json and schema_json != expected_schema_json:
            raise ArtifactValidationError(
                "artifact schema does not match the descriptor"
            )
        if expected_rows is not None and table.num_rows != expected_rows:
            raise ArtifactValidationError(
                "artifact row count does not match the descriptor"
            )
        return table

    def write_table(
        self, tenant_id: UUID, artifact_key: str, table: pa.Table
    ) -> StoredArrowArtifact:
        key = self.validate_key(tenant_id, artifact_key)
        payload = encode_arrow_ipc(table)
        if len(payload) > self.max_bytes:
            raise ArtifactValidationError("artifact exceeds ARTIFACT_MAX_BYTES")
        self._storage_call(
            "write",
            lambda: self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=payload,
                ContentType=ARROW_IPC_CONTENT_TYPE,
            ),
        )
        return StoredArrowArtifact(
            artifact_key=key,
            payload_digest=sha256_digest(payload),
            schema_json=arrow_schema_json(table.schema),
            rows=table.num_rows,
        )
