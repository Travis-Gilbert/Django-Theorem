"""Celery lifecycle for Verifiers evaluation and prime-rl Pod training."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.orchestration.artifacts import (
    ArtifactConfigurationError,
    ArtifactStorageError,
    ArtifactStore,
    ArtifactValidationError,
)

from .ingest import ingest_trajectories, publish_supporting_artifacts
from .models import TrainingRun
from .runpod import (
    RunpodPodApiError,
    RunpodPodClient,
    RunpodPodConfigurationError,
    RunpodPodTimeoutError,
)


def _claim(run_id: str) -> TrainingRun | None:
    with transaction.atomic():
        run = TrainingRun.objects.select_for_update().get(id=run_id)
        if run.status == TrainingRun.Status.CANCELED:
            return None
        if run.status == TrainingRun.Status.SUCCEEDED:
            return None
        run.status = TrainingRun.Status.RUNNING
        run.started_at = run.started_at or timezone.now()
        run.error = ""
        run.save(update_fields=["status", "started_at", "error", "updated_at"])
        return run


def _finish(run: TrainingRun, status: str, *, error: str = "") -> dict[str, Any]:
    run.refresh_from_db()
    if run.status == TrainingRun.Status.CANCELED:
        status = TrainingRun.Status.CANCELED
    run.status = status
    run.error = error
    run.ended_at = timezone.now()
    run.save(update_fields=["status", "error", "ended_at", "updated_at"])
    return {"run_id": str(run.id), "status": run.status, "error": run.error}


def _eval_command(run: TrainingRun, output_dir: Path) -> list[str]:
    config = run.config_json
    taskset_id = config.get("taskset_id", run.taskset_ref)
    if not isinstance(taskset_id, str) or not taskset_id.strip():
        raise ValueError("eval config taskset_id must be a non-empty string")
    command = [
        "uv",
        "run",
        "eval",
        taskset_id,
        "-o",
        str(output_dir),
        "--no-push",
        "--no-rich",
    ]
    options = {
        "model": ("-m", str),
        "num_tasks": ("-n", int),
        "num_rollouts": ("-r", int),
        "max_concurrent": ("-c", int),
    }
    for name, (flag, expected_type) in options.items():
        value = config.get(name)
        if value is None:
            continue
        if expected_type is int and (not isinstance(value, int) or value <= 0):
            raise ValueError(f"eval config {name} must be a positive integer")
        if expected_type is str and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"eval config {name} must be a non-empty string")
        command.extend([flag, str(value)])
    return command


def _one_file(root: Path, name: str) -> Path:
    matches = list(root.glob(f"**/{name}"))
    if len(matches) != 1:
        raise RuntimeError(
            f"eval produced {len(matches)} {name} files; expected exactly one"
        )
    return matches[0]


def _resolved_eval_config(root: Path) -> Path:
    preferred = list(root.glob("**/configs/resolved/eval.json"))
    if len(preferred) == 1:
        return preferred[0]
    fallback = list(root.glob("**/configs/eval.json"))
    if len(fallback) == 1:
        return fallback[0]
    raise RuntimeError(
        "eval did not produce exactly one resolved configs/eval.json artifact"
    )


@shared_task(name="theorem_control.rl.tasks.run_eval")
def run_eval(run_id: str) -> dict[str, Any]:
    run = _claim(run_id)
    if run is None:
        current = TrainingRun.objects.get(id=run_id)
        return {"run_id": run_id, "status": current.status}
    try:
        store = ArtifactStore.from_settings()
        with TemporaryDirectory(prefix=f"theorem-rl-eval-{run.id}-") as directory:
            output_dir = Path(directory)
            result = subprocess.run(
                _eval_command(run, output_dir),
                cwd=settings.BASE_DIR,
                capture_output=True,
                text=True,
                timeout=settings.RL_EVAL_DEADLINE_SECONDS,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"uv run eval failed ({result.returncode}): {result.stderr[-2000:]}"
                )
            traces = _one_file(output_dir, "traces.jsonl").read_bytes()
            ingest_trajectories(run, traces, store=store)
            resolved = _resolved_eval_config(output_dir).read_bytes()
            publish_supporting_artifacts(
                run,
                [("eval-config.json", resolved, "application/json")],
                store=store,
            )
        return _finish(run, TrainingRun.Status.SUCCEEDED)
    except (
        ArtifactConfigurationError,
        ArtifactStorageError,
        ArtifactValidationError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        ValueError,
    ) as exc:
        return _finish(run, TrainingRun.Status.FAILED, error=str(exc))


def _incoming_key(run: TrainingRun, name: str) -> str:
    return f"tenants/{run.tenant_id}/rl/{run.id}/incoming/{name}"


def _training_payload(run: TrainingRun, store: ArtifactStore) -> dict[str, Any]:
    outputs = {
        "traces.jsonl": "application/jsonl",
        "reward_curve.csv": "text/csv",
        "eval_before.json": "application/json",
        "eval_after.json": "application/json",
        "prime_rl.toml": "text/plain",
    }
    upload = {
        name: {
            "artifact_key": _incoming_key(run, name),
            "content_type": media_type,
            "upload_url": store.presign_put_content(
                run.tenant_id,
                _incoming_key(run, name),
                content_type=media_type,
            ),
        }
        for name, media_type in outputs.items()
    }
    return {
        "name": f"theorem-rl-{str(run.id)[:12]}",
        "imageName": run.image_digest,
        "cloudType": settings.RL_RUNPOD_CLOUD_TYPE,
        "computeType": "GPU",
        "gpuTypeIds": [settings.RL_RUNPOD_GPU_TYPE],
        "gpuCount": 1,
        "containerDiskInGb": 50,
        "volumeInGb": 20,
        "volumeMountPath": "/workspace",
        "env": {
            "THEOREM_RL_RUN_ID": str(run.id),
            "THEOREM_TASKSET_REF": run.taskset_ref,
            "THEOREM_RL_CONFIG_JSON": json.dumps(
                run.config_json,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "THEOREM_RL_UPLOADS_JSON": json.dumps(
                upload,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    }


def _pod_client() -> RunpodPodClient:
    return RunpodPodClient(
        api_key=settings.RUNPOD_API_KEY,
        base_url=settings.RUNPOD_PODS_API_BASE,
        timeout_seconds=settings.RUNPOD_REQUEST_TIMEOUT_SECONDS,
    )


def _read_training_outputs(
    run: TrainingRun,
    store: ArtifactStore,
) -> tuple[bytes, list[tuple[str, bytes, str]]]:
    traces = store.get_bytes(run.tenant_id, _incoming_key(run, "traces.jsonl"))
    supporting = [
        (
            name,
            store.get_bytes(run.tenant_id, _incoming_key(run, source_name)),
            media_type,
        )
        for name, source_name, media_type in (
            ("reward-curve.csv", "reward_curve.csv", "text/csv"),
            ("eval-before.json", "eval_before.json", "application/json"),
            ("eval-after.json", "eval_after.json", "application/json"),
            ("prime-rl.toml", "prime_rl.toml", "application/toml"),
        )
    ]
    return traces, supporting


@shared_task(name="theorem_control.rl.tasks.run_prime_rl_training")
def run_prime_rl_training(run_id: str) -> dict[str, Any]:
    existing = TrainingRun.objects.get(id=run_id)
    if existing.status == TrainingRun.Status.CANCELED:
        if existing.pod_id:
            try:
                _pod_client().terminate(existing.pod_id)
            except (RunpodPodApiError, RunpodPodConfigurationError):
                pass
        return {"run_id": run_id, "status": existing.status}
    run = _claim(run_id)
    if run is None:
        current = TrainingRun.objects.get(id=run_id)
        return {"run_id": run_id, "status": current.status}
    client = None
    try:
        store = ArtifactStore.from_settings()
        client = _pod_client()
        pod = client.create(_training_payload(run, store))
        run.pod_id = pod.pod_id
        run.save(update_fields=["pod_id", "updated_at"])
        state = client.wait(
            pod.pod_id,
            timeout_seconds=settings.RL_RUNPOD_DEADLINE_SECONDS,
            poll_interval_seconds=settings.RL_RUNPOD_POLL_INTERVAL_SECONDS,
            canceled=lambda: TrainingRun.objects.filter(
                id=run.id,
                status=TrainingRun.Status.CANCELED,
            ).exists(),
        )
        if str(state.get("desiredStatus") or "").upper() == "CANCELED":
            return _finish(run, TrainingRun.Status.CANCELED)
        traces, supporting = _read_training_outputs(run, store)
        ingest_trajectories(run, traces, store=store)
        publish_supporting_artifacts(run, supporting, store=store)
        for incoming in (
            "traces.jsonl",
            "reward_curve.csv",
            "eval_before.json",
            "eval_after.json",
            "prime_rl.toml",
        ):
            store.delete_artifact(run.tenant_id, _incoming_key(run, incoming))
        return _finish(run, TrainingRun.Status.SUCCEEDED)
    except (
        ArtifactConfigurationError,
        ArtifactStorageError,
        ArtifactValidationError,
        RunpodPodApiError,
        RunpodPodConfigurationError,
        RunpodPodTimeoutError,
        RuntimeError,
        ValueError,
    ) as exc:
        if client is not None and run.pod_id:
            try:
                client.terminate(run.pod_id)
            except RunpodPodApiError:
                pass
        return _finish(run, TrainingRun.Status.FAILED, error=str(exc))
