"""Deterministic layout orchestration with bounded Graphviz execution."""

from __future__ import annotations

import hashlib
import json
import os
import resource
import signal
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

from django.conf import settings
import pygraphviz

from apps.layout.cache import get_cached_response, set_cached_response
from apps.layout.canonical import canonical_dot, validate_graph
from apps.layout.contracts import LayoutPosition, LayoutRequest, LayoutResponse
from apps.layout.policy import resolve_policy

WORKER_PATH = Path(__file__).with_name("worker.py")


class LayoutExecutionError(RuntimeError):
    pass


class LayoutExecutionTimeout(LayoutExecutionError):
    pass


@lru_cache(maxsize=1)
def graphviz_version() -> str:
    # This is the runtime linked into pygraphviz, which is more authoritative
    # than an unrelated `dot` executable that may appear first on PATH.
    return str(pygraphviz.__graphviz_version__)


def _limit_worker_output() -> None:
    output_bytes = int(
        getattr(settings, "LAYOUT_MAX_OUTPUT_BYTES", 4 * 1024 * 1024)
    )
    resource.setrlimit(resource.RLIMIT_FSIZE, (output_bytes, output_bytes))


def _execute_worker(dot: str, engine: str, node_ids: list[str]) -> dict[str, dict[str, float]]:
    payload = json.dumps(
        {"dot": dot, "engine": engine, "node_ids": node_ids},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    output_limit = int(
        getattr(settings, "LAYOUT_MAX_OUTPUT_BYTES", 4 * 1024 * 1024)
    )
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            [sys.executable, "-I", str(WORKER_PATH)],
            env={
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "PYTHONHASHSEED": "0",
            },
            stdin=subprocess.PIPE,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
            preexec_fn=_limit_worker_output,
        )
        try:
            process.communicate(
                payload,
                timeout=float(
                    getattr(settings, "LAYOUT_SUBPROCESS_TIMEOUT_SECONDS", 8.0)
                ),
            )
        except subprocess.TimeoutExpired as exc:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise LayoutExecutionTimeout("Graphviz layout exceeded its deadline") from exc

        stdout_size = os.fstat(stdout_file.fileno()).st_size
        stderr_size = os.fstat(stderr_file.fileno()).st_size
        if (
            process.returncode == -signal.SIGXFSZ
            or stdout_size > output_limit
            or (
                process.returncode != 0
                and (stdout_size >= output_limit or stderr_size >= output_limit)
            )
        ):
            raise LayoutExecutionError("Graphviz layout output exceeded its size cap")
        stderr_file.seek(0)
        stderr = stderr_file.read(1_001)
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace")[:1_000]
            raise LayoutExecutionError(f"Graphviz layout failed: {detail}")
        stdout_file.seek(0)
        stdout = stdout_file.read(output_limit + 1)
        if len(stdout) > output_limit:
            raise LayoutExecutionError("Graphviz layout output exceeded its size cap")
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise LayoutExecutionError("Graphviz worker returned invalid JSON") from exc
    if sorted(result) != sorted(node_ids):
        raise LayoutExecutionError("Graphviz worker returned an incomplete position set")
    return result


def _digest_input(dot: str, engine: str, policy_params: dict[str, object], version: str) -> str:
    canonical_params = json.dumps(
        policy_params, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    payload = "\0".join((dot, engine, canonical_params, version)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compute_layout(body: LayoutRequest, *, tenant_slug: str) -> bytes:
    validate_graph(body.nodes, body.edges)
    graph_class, policy, focus_id = resolve_policy(
        body.graph_class, body.nodes, body.edges, body.params
    )
    engine = policy.engine_for(len(body.nodes))
    dot = canonical_dot(body.nodes, body.edges, policy, focus_id=focus_id)
    version = graphviz_version()
    effective_attrs = dict(policy.graph_attrs)
    digest_hex = _digest_input(
        dot,
        engine,
        {"graph_class": graph_class, "graph_attrs": effective_attrs, "focus_id": focus_id},
        version,
    )
    cached = get_cached_response(tenant_slug, digest_hex)
    if cached is not None:
        return cached

    raw_positions = _execute_worker(
        dot, engine, sorted(node.id for node in body.nodes)
    )
    response = LayoutResponse(
        positions=[
            LayoutPosition(
                id=node_id,
                x_px=raw_positions[node_id]["x"],
                y_px=raw_positions[node_id]["y"],
            )
            for node_id in sorted(raw_positions)
        ],
        engine=engine,
        graphviz_version=version,
        policy_id=graph_class,
        input_digest=f"sha256:{digest_hex}",
    )
    encoded = json.dumps(
        response.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    set_cached_response(tenant_slug, digest_hex, encoded)
    return encoded
