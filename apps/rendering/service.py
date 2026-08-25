"""Bounded PlantUML and Diagrams execution."""

from __future__ import annotations

import importlib.metadata
import json
import os
import resource
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from django.conf import settings

from apps.rendering.validation import validate_diagrams_source

DIAGRAMS_WORKER_PATH = Path(__file__).with_name("diagrams_worker.py")
SUPPORTS_RLIMIT_AS = sys.platform.startswith("linux")


class RenderExecutionError(RuntimeError):
    pass


class RenderExecutionTimeout(RenderExecutionError):
    pass


def _resource_limits() -> None:
    cpu_seconds = int(getattr(settings, "RENDER_CPU_SECONDS", 10))
    memory_bytes = int(getattr(settings, "RENDER_MEMORY_BYTES", 1024 * 1024 * 1024))
    output_bytes = int(
        getattr(settings, "RENDER_OUTPUT_MAX_BYTES", 16 * 1024 * 1024)
    )
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_FSIZE, (output_bytes, output_bytes))
    if SUPPORTS_RLIMIT_AS:
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))


def _subprocess_environment() -> dict[str, str]:
    """Return a credential-free environment with only renderer necessities."""

    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONHASHSEED": "0",
    }


def _read_bounded(stream, limit: int) -> bytes:
    stream.seek(0)
    return stream.read(limit + 1)


def _run(command: list[str], *, stdin: bytes, cwd: str | None = None) -> bytes:
    output_limit = int(
        getattr(settings, "RENDER_OUTPUT_MAX_BYTES", 16 * 1024 * 1024)
    )
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=_subprocess_environment(),
            stdin=subprocess.PIPE,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
            preexec_fn=_resource_limits,
        )
        try:
            process.communicate(
                stdin,
                timeout=float(
                    getattr(settings, "RENDER_SUBPROCESS_TIMEOUT_SECONDS", 12.0)
                ),
            )
        except subprocess.TimeoutExpired as exc:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise RenderExecutionTimeout("render exceeded its deadline") from exc

        stdout_size = os.fstat(stdout_file.fileno()).st_size
        stderr_size = os.fstat(stderr_file.fileno()).st_size
        stderr = _read_bounded(stderr_file, 1_000)
        size_limited = (
            process.returncode == -signal.SIGXFSZ
            or stdout_size > output_limit
            or (
                process.returncode != 0
                and (stdout_size >= output_limit or stderr_size >= output_limit)
            )
        )
        if size_limited:
            raise RenderExecutionError("render output exceeded its size cap")
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace")[:1_000]
            raise RenderExecutionError(f"renderer failed: {detail}")
        stdout = _read_bounded(stdout_file, output_limit)
        if not stdout:
            raise RenderExecutionError("renderer returned no output")
        if len(stdout) > output_limit:
            raise RenderExecutionError("render output exceeded its size cap")
        return stdout


def _validate_svg(payload: bytes) -> None:
    lowered = payload.lower()
    if b"<svg" not in lowered[:1_000] or any(
        forbidden in lowered
        for forbidden in (b"<script", b"<foreignobject", b"javascript:")
    ):
        raise RenderExecutionError("renderer returned unsafe SVG")


def render_plantuml(source: str) -> tuple[bytes, str, str]:
    encoded = source.encode("utf-8")
    max_source = int(getattr(settings, "RENDER_MAX_SOURCE_BYTES", 256 * 1024))
    if not encoded or len(encoded) > max_source:
        raise ValueError("source must be non-empty and within its size cap")
    jar_path = Path(settings.PLANTUML_JAR_PATH)
    if not jar_path.is_file():
        raise RenderExecutionError("pinned PlantUML jar is unavailable")
    security_profile = str(settings.PLANTUML_SECURITY_PROFILE)
    if security_profile != "SANDBOX":
        raise RenderExecutionError("PlantUML security profile must be SANDBOX")
    payload = _run(
        [
            "java",
            f"-DPLANTUML_SECURITY_PROFILE={security_profile}",
            "-jar",
            str(jar_path),
            "-tsvg",
            "-pipe",
        ],
        stdin=encoded,
    )
    _validate_svg(payload)
    return payload, "image/svg+xml", str(settings.PLANTUML_VERSION)


def render_diagrams(source: str, output_format: str) -> tuple[bytes, str, str]:
    validate_diagrams_source(
        source,
        max_bytes=int(getattr(settings, "RENDER_MAX_SOURCE_BYTES", 256 * 1024)),
    )
    with tempfile.TemporaryDirectory(prefix="theorem-diagrams-") as workdir:
        request = json.dumps(
            {"source": source, "format": output_format, "workdir": workdir},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        raw_receipt = _run(
            [sys.executable, "-I", str(DIAGRAMS_WORKER_PATH)],
            stdin=request,
            cwd=workdir,
        )
        try:
            receipt = json.loads(raw_receipt)
            output_path = Path(receipt["output_path"]).resolve(strict=True)
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            raise RenderExecutionError("diagrams worker returned an invalid receipt") from exc
        expected_path = Path(workdir).resolve() / f"render.{output_format}"
        if output_path != expected_path:
            raise RenderExecutionError("diagrams worker escaped its output directory")
        output_limit = int(
            getattr(settings, "RENDER_OUTPUT_MAX_BYTES", 16 * 1024 * 1024)
        )
        if output_path.stat().st_size > output_limit:
            raise RenderExecutionError("render output violated its size cap")
        with output_path.open("rb") as output_file:
            payload = output_file.read(output_limit + 1)
        if not payload or len(payload) > output_limit:
            raise RenderExecutionError("render output violated its size cap")
    if output_format == "svg":
        _validate_svg(payload)
        media_type = "image/svg+xml"
    else:
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RenderExecutionError("diagrams returned invalid PNG")
        media_type = "image/png"
    return payload, media_type, importlib.metadata.version("diagrams")
