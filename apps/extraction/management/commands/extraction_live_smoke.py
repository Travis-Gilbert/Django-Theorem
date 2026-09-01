"""Verify the deployed extraction boundary with real RunPod and artifact I/O."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
from django.core.management.base import BaseCommand, CommandError

from apps.orchestration.artifacts import arrow_schema_json, decode_arrow_ipc


ROOT = Path(__file__).resolve().parents[4]
FIXTURE_PATH = ROOT / "contracts/theorem.extraction.v1.fixture.json"
TERMINAL_STATES = {"succeeded", "partial", "failed", "canceled"}


def _json_object(response: httpx.Response, *, expected_status: int) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise CommandError(
            f"extraction endpoint returned non-JSON HTTP {response.status_code}"
        ) from exc
    if not isinstance(payload, dict):
        raise CommandError("extraction endpoint returned a non-object payload")
    if response.status_code != expected_status:
        raise CommandError(
            f"extraction request failed with HTTP {response.status_code}: "
            f"{payload.get('detail', payload)}"
        )
    return payload


class Command(BaseCommand):
    help = "Submit and verify theorem.extraction.v1 through the deployed boundary."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--base-url", required=True)
        parser.add_argument("--timeout-seconds", type=float, default=900.0)
        parser.add_argument("--poll-seconds", type=float, default=2.0)

    def handle(self, *args, **options) -> None:
        base_url = options["base_url"].rstrip("/")
        timeout_seconds = options["timeout_seconds"]
        poll_seconds = options["poll_seconds"]
        if not base_url.startswith("https://"):
            raise CommandError("--base-url must use https://")
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise CommandError("poll and timeout durations must be positive")
        machine_key = os.environ.get("THEOREM_EXTRACTION_LIVE_MACHINE_KEY", "")
        if not machine_key:
            raise CommandError("THEOREM_EXTRACTION_LIVE_MACHINE_KEY is required")

        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        input_descriptor = fixture["input"]
        input_bytes = base64.b64decode(input_descriptor["payload_base64"])
        headers = {"Authorization": f"Bearer {machine_key}"}

        with httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=httpx.Timeout(30.0),
            follow_redirects=False,
        ) as api_client:
            upload = _json_object(
                api_client.post("/internal/offload/artifact-upload"),
                expected_status=200,
            )
            upload_url = upload.get("upload_url")
            artifact_key = upload.get("artifact_key")
            if not isinstance(upload_url, str) or not isinstance(artifact_key, str):
                raise CommandError("artifact upload response omitted its capability")
            required_headers = upload.get("required_headers") or {}
            with httpx.Client(
                timeout=httpx.Timeout(60.0),
                follow_redirects=False,
            ) as storage_client:
                uploaded = storage_client.put(
                    upload_url,
                    content=input_bytes,
                    headers=required_headers,
                )
                if uploaded.status_code not in {200, 201, 204}:
                    raise CommandError(
                        f"artifact upload failed with HTTP {uploaded.status_code}"
                    )

                submission = _json_object(
                    api_client.post(
                        "/internal/extraction/submit",
                        json={
                            "operation": "atlas",
                            "source_kind": "artifact",
                            "source_ref": {
                                "artifact_key": artifact_key,
                                "payload_digest": input_descriptor["payload_digest"],
                                "schema_json": input_descriptor["schema_json"],
                                "rows": input_descriptor["rows"],
                            },
                            "params": {},
                        },
                    ),
                    expected_status=200,
                )
                job_id = submission.get("job_id")
                if not isinstance(job_id, str):
                    raise CommandError("extraction submission omitted job_id")

                deadline = time.monotonic() + timeout_seconds
                status: dict[str, Any] | None = None
                while time.monotonic() < deadline:
                    status = _json_object(
                        api_client.get(f"/internal/extraction/{job_id}"),
                        expected_status=200,
                    )
                    if status.get("status") in TERMINAL_STATES:
                        break
                    time.sleep(poll_seconds)
                if status is None or status.get("status") not in TERMINAL_STATES:
                    raise CommandError(
                        f"extraction job {job_id} did not finish within {timeout_seconds}s"
                    )
                if status.get("status") != "succeeded":
                    raise CommandError(
                        f"extraction job {job_id} ended as {status.get('status')}"
                    )

                shard_counts = []
                for shard in status.get("shards", []):
                    output = shard.get("output") if isinstance(shard, dict) else None
                    if not isinstance(output, dict):
                        raise CommandError("successful shard omitted its Arrow descriptor")
                    download_url = output.get("download_url")
                    if not isinstance(download_url, str):
                        raise CommandError("successful shard omitted download_url")
                    downloaded = storage_client.get(download_url)
                    if downloaded.status_code != 200:
                        raise CommandError(
                            f"artifact download failed with HTTP {downloaded.status_code}"
                        )
                    payload = downloaded.content
                    actual_digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
                    if actual_digest != output.get("payload_digest"):
                        raise CommandError("downloaded shard digest does not match descriptor")
                    table = decode_arrow_ipc(payload)
                    if arrow_schema_json(table.schema) != output.get("schema_json"):
                        raise CommandError("downloaded shard schema does not match descriptor")
                    if table.num_rows != output.get("rows"):
                        raise CommandError("downloaded shard row count does not match descriptor")
                    shard_counts.append(table.num_rows)

        if not shard_counts:
            raise CommandError("succeeded extraction returned no shard outputs")
        receipt = {
            "schema": "theorem.extraction-live-smoke.v1",
            "boundary_evidence_class": "live",
            "contract": "theorem.extraction.v1",
            "operation": "data_science.extraction.atlas",
            "job_id": job_id,
            "shard_rows": shard_counts,
            "rows_total": sum(shard_counts),
            "artifact_digest_schema_rows_verified": True,
        }
        self.stdout.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
