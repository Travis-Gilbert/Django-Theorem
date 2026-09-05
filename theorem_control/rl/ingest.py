"""Digest-verified ingestion for Verifiers traces and grader verdicts."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

import pyarrow as pa
from django.db import transaction

from apps.orchestration.artifacts import ArtifactStore, sha256_digest

from .models import GraderVerdict, TrainingRun, Trajectory

TRACE_SCHEMA = pa.schema(
    [
        pa.field("task_key", pa.string(), nullable=False),
        pa.field("trace_digest", pa.string(), nullable=False),
        pa.field("reward", pa.float64(), nullable=False),
        pa.field("metrics_json", pa.string(), nullable=False),
        pa.field("resolved", pa.float64(), nullable=False),
        pa.field("tripwire_flags_json", pa.string(), nullable=False),
    ]
)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def config_digest(config: Mapping[str, Any]) -> str:
    return sha256_digest(canonical_json(config))


def _reward_score(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Mapping):
        for key in ("score", "value"):
            if key in value:
                return float(value[key])
    raise ValueError("reward value must be numeric or contain a numeric score")


def parse_traces(payload: bytes) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(payload.splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            item = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid traces.jsonl line {line_number}") from exc
        if not isinstance(item, Mapping):
            raise TypeError(f"traces.jsonl line {line_number} must be an object")
        task = item.get("task", {})
        task_data = task.get("data", {}) if isinstance(task, Mapping) else {}
        task_key = (
            item.get("task_key")
            or item.get("task_id")
            or item.get("name")
            or (task.get("key") if isinstance(task, Mapping) else None)
            or (task_data.get("task_id") if isinstance(task_data, Mapping) else None)
        )
        metrics = item.get("metrics", {})
        trace_value = item.get("trace", item)
        if not isinstance(task_key, str) or not task_key.strip():
            raise ValueError(f"traces.jsonl line {line_number} lacks task_key")
        if not isinstance(metrics, Mapping):
            raise TypeError(
                f"traces.jsonl line {line_number} metrics must be an object"
            )
        rewards = item.get("rewards", {})
        if not isinstance(rewards, Mapping):
            raise TypeError(
                f"traces.jsonl line {line_number} rewards must be an object"
            )
        resolved_value = rewards.get(
            "resolved",
            metrics.get("resolved", item.get("reward", 0.0)),
        )
        resolved = _reward_score(resolved_value)
        explicit_reward = item.get("reward")
        if explicit_reward is not None:
            reward = _reward_score(explicit_reward)
        elif rewards:
            reward = sum(
                _reward_score(value)
                * float(value.get("weight", 1.0) if isinstance(value, Mapping) else 1.0)
                for value in rewards.values()
                if value is not None
            )
        else:
            reward = resolved
        if not 0.0 <= resolved <= 1.0:
            raise ValueError(
                f"traces.jsonl line {line_number} resolved is out of range"
            )
        info = item.get("info", {})
        if not isinstance(info, Mapping):
            raise TypeError(f"traces.jsonl line {line_number} info must be an object")
        grader = info.get("django_v1", {})
        grader_flags = (
            grader.get("tripwires", {}) if isinstance(grader, Mapping) else {}
        )
        flags = item.get(
            "tripwire_flags",
            metrics.get("tripwire_flags", grader_flags),
        )
        if isinstance(flags, Mapping):
            flags = [name for name, fired in flags.items() if fired]
        if not isinstance(flags, list) or any(
            not isinstance(flag, str) for flag in flags
        ):
            raise ValueError(
                f"traces.jsonl line {line_number} tripwire flags are invalid"
            )
        if flags and resolved != 0.0:
            raise ValueError(
                f"traces.jsonl line {line_number} resolves despite a tripwire"
            )
        computed_digest = sha256_digest(canonical_json(trace_value))
        supplied_digest = item.get("trace_digest")
        if supplied_digest is not None and supplied_digest != computed_digest:
            raise ValueError(f"traces.jsonl line {line_number} digest mismatch")
        rows.append(
            {
                "task_key": task_key,
                "trace_digest": computed_digest,
                "reward": reward,
                "metrics": dict(metrics),
                "metrics_json": canonical_json(metrics).decode("utf-8"),
                "resolved": resolved,
                "tripwire_flags": flags,
                "tripwire_flags_json": canonical_json(flags).decode("utf-8"),
            }
        )
    if not rows:
        raise ValueError("traces.jsonl did not contain any trajectories")
    return rows


def ingest_trajectories(
    run: TrainingRun,
    traces_jsonl: bytes,
    *,
    store: ArtifactStore,
) -> dict[str, Any]:
    """Publish one Arrow shard, verify it, then mint relational ledger rows."""
    rows = parse_traces(traces_jsonl)
    table = pa.Table.from_pylist(
        [
            {
                key: row[key]
                for key in (
                    "task_key",
                    "trace_digest",
                    "reward",
                    "metrics_json",
                    "resolved",
                    "tripwire_flags_json",
                )
            }
            for row in rows
        ],
        schema=TRACE_SCHEMA,
    )
    source_digest = sha256_digest(traces_jsonl)
    artifact_key = store.rl_artifact_key(
        run.tenant_id,
        run.id,
        "trajectories.arrow",
        source_digest,
    )
    artifact = store.write_table(run.tenant_id, artifact_key, table)
    store.read_arrow(
        run.tenant_id,
        artifact.artifact_key,
        expected_digest=artifact.payload_digest,
        expected_schema_json=artifact.schema_json,
        expected_rows=artifact.rows,
    )
    with transaction.atomic():
        for row in rows:
            trajectory, _created = Trajectory.objects.update_or_create(
                run=run,
                trace_digest=row["trace_digest"],
                defaults={
                    "task_key": row["task_key"],
                    "reward": row["reward"],
                    "metrics": row["metrics"],
                    "arrow_shard_key": artifact.artifact_key,
                },
            )
            GraderVerdict.objects.update_or_create(
                trajectory=trajectory,
                defaults={
                    "resolved": row["resolved"],
                    "metric_values": row["metrics"],
                    "tripwire_flags": row["tripwire_flags"],
                },
            )
        descriptor = {
            "kind": "trajectories",
            "artifact_key": artifact.artifact_key,
            "payload_digest": artifact.payload_digest,
            "schema_json": artifact.schema_json,
            "rows": artifact.rows,
            "content_type": "application/vnd.apache.arrow.stream",
        }
        descriptors = [
            item
            for item in run.artifact_descriptors
            if item.get("kind") != "trajectories"
        ]
        descriptors.append(descriptor)
        run.artifact_descriptors = descriptors
        run.artifact_keys = [item["artifact_key"] for item in descriptors]
        run.save(update_fields=["artifact_descriptors", "artifact_keys", "updated_at"])
    return descriptor


def publish_supporting_artifacts(
    run: TrainingRun,
    artifacts: Iterable[tuple[str, bytes, str]],
    *,
    store: ArtifactStore,
) -> list[dict[str, Any]]:
    descriptors = list(run.artifact_descriptors)
    for name, payload, media_type in artifacts:
        artifact = store.write_rl_content(
            run.tenant_id,
            run.id,
            name,
            payload,
            media_type=media_type,
        )
        descriptors = [item for item in descriptors if item.get("kind") != name]
        descriptors.append(
            {
                "kind": name,
                "artifact_key": artifact.artifact_key,
                "payload_digest": artifact.payload_digest,
                "content_type": artifact.media_type,
                "byte_length": artifact.byte_length,
            }
        )
    run.artifact_descriptors = descriptors
    run.artifact_keys = [item["artifact_key"] for item in descriptors]
    run.save(update_fields=["artifact_descriptors", "artifact_keys", "updated_at"])
    return descriptors
