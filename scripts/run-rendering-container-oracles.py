#!/usr/bin/env python3
"""Build and verify the exact declared rendering image when a runtime exists."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLANTUML_SHA256 = (
    "89948f14c93756c7a3fb7b69078ff37e8489fd79dd430c582b931e2f65358690"
)
MINIMUM_FREE_BYTES = 20 * 1024 * 1024 * 1024
PROCESS_TIMEOUT_SECONDS = 30.0
BUILD_TIMEOUT_SECONDS = 1_200.0
PROBE_TIMEOUT_SECONDS = 300.0
PROCESS_OUTPUT_MAX_BYTES = 8 * 1024 * 1024
RUNTIME_NAMES = (
    "docker",
    "podman",
    "colima",
    "limactl",
    "lima",
    "finch",
    "nerdctl",
    "container",
    "orbctl",
)
BUILD_CLIENTS = {"docker", "podman", "finch", "nerdctl", "container"}
HELPER_ONLY = {"colima", "limactl", "lima", "orbctl"}


class ContainerOracleError(RuntimeError):
    """The declared image or a real probe violated its contract."""


class ContainerOraclePrerequisiteError(ContainerOracleError):
    """No already-usable Docker-compatible runtime is available."""


@dataclasses.dataclass(frozen=True)
class RuntimeCandidate:
    name: str
    executable: str | None
    status: str
    detail: str


def _minimal_environment() -> dict[str, str]:
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ.get("PATH") or os.defpath,
    }


def _run_bounded(
    command: list[str],
    *,
    timeout_seconds: float,
    cwd: Path | None = None,
    check: bool = True,
) -> tuple[int, str]:
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=_minimal_environment(),
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
        )
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise ContainerOracleError(
                f"runtime command exceeded {timeout_seconds:g}s: {command[0]}"
            ) from exc
        stdout_size = os.fstat(stdout_file.fileno()).st_size
        stderr_size = os.fstat(stderr_file.fileno()).st_size
        if max(stdout_size, stderr_size) > PROCESS_OUTPUT_MAX_BYTES:
            raise ContainerOracleError("runtime command output exceeded its cap")
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read().decode("utf-8", errors="replace")
        stderr = stderr_file.read().decode("utf-8", errors="replace")
    output = "\n".join(part for part in (stdout.strip(), stderr.strip()) if part)
    if check and process.returncode != 0:
        raise ContainerOracleError(
            f"runtime command failed ({process.returncode}): {' '.join(command)}\n"
            f"{output[-4000:]}"
        )
    return process.returncode, output


def _status_command(name: str, executable: str) -> list[str]:
    if name == "container":
        return [executable, "system", "status"]
    return [executable, "info"]


def inspect_runtime_candidates() -> tuple[RuntimeCandidate, ...]:
    inventory: list[RuntimeCandidate] = []
    for name in RUNTIME_NAMES:
        executable = shutil.which(name)
        if executable is None:
            inventory.append(RuntimeCandidate(name, None, "missing", "not on PATH"))
            continue
        try:
            version_status, version = _run_bounded(
                [executable, "--version"],
                timeout_seconds=5.0,
                check=False,
            )
        except ContainerOracleError as exc:
            inventory.append(RuntimeCandidate(name, executable, "unusable", str(exc)))
            continue
        if version_status != 0:
            inventory.append(
                RuntimeCandidate(name, executable, "unusable", version or "version failed")
            )
            continue
        if name in HELPER_ONLY:
            inventory.append(
                RuntimeCandidate(
                    name,
                    executable,
                    "helper-only",
                    f"{version}; no image build client was found",
                )
            )
            continue
        status, detail = _run_bounded(
            _status_command(name, executable),
            timeout_seconds=PROCESS_TIMEOUT_SECONDS,
            check=False,
        )
        inventory.append(
            RuntimeCandidate(
                name,
                executable,
                "usable" if status == 0 else "daemon-unavailable",
                f"{version}; {detail}".strip("; "),
            )
        )
    return tuple(inventory)


def select_runtime(inventory: tuple[RuntimeCandidate, ...]) -> RuntimeCandidate:
    for candidate in inventory:
        if candidate.name in BUILD_CLIENTS and candidate.status == "usable":
            return candidate
    detail = "; ".join(
        f"{candidate.name}={candidate.status}" for candidate in inventory
    )
    raise ContainerOraclePrerequisiteError(
        "an already-running Docker-compatible image runtime is required; " + detail
    )


def verify_declared_dockerfile(path: Path) -> None:
    try:
        contents = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContainerOracleError(f"cannot read declared Dockerfile: {exc}") from exc
    required = (
        "FROM python:3.12.12-slim-bookworm",
        "ARG GRAPHVIZ_DEBIAN_VERSION=2.42.2-7+deb12u1",
        "ARG DEFAULT_JRE_DEBIAN_VERSION=2:1.17-74",
        "ARG OPENJDK_DEBIAN_VERSION=17.0.20+8-1~deb12u1",
        "COPY requirements.txt .",
        "https://github.com/plantuml/plantuml/releases/download/v1.2026.6/plantuml-1.2026.6.jar",
        "PLANTUML_VERSION=1.2026.6",
        f"PLANTUML_SHA256={PLANTUML_SHA256}",
        "PLANTUML_SECURITY_PROFILE=SANDBOX",
    )
    missing = [value for value in required if value not in contents]
    if missing:
        raise ContainerOracleError(
            "declared Dockerfile is missing exact runtime pins: " + ", ".join(missing)
        )


def verify_declared_requirements(path: Path) -> None:
    try:
        requirements = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ContainerOracleError(f"cannot read declared requirements: {exc}") from exc
    declared = {line.strip() for line in requirements}
    required = {"pygraphviz==2.0.1", "diagrams==0.25.1"}
    missing = sorted(required - declared)
    if missing:
        raise ContainerOracleError(
            "declared requirements are missing exact renderer pins: "
            + ", ".join(missing)
        )


def container_probe_script() -> str:
    return f"""set -eu
# Graphviz 2.42.2
dot_identity=$(dot -V 2>&1)
case "$dot_identity" in *"graphviz version 2.42.2"*) ;; *) echo "$dot_identity" >&2; exit 31;; esac

# pygraphviz==2.0.1 and diagrams==0.25.1
python - <<'PY'
import importlib.metadata
from pygraphviz import _graphviz
assert importlib.metadata.version("pygraphviz") == "2.0.1"
assert importlib.metadata.version("diagrams") == "0.25.1"
assert (_graphviz.GRAPHVIZ_MAJOR_VERSION, _graphviz.GRAPHVIZ_MINOR_VERSION, _graphviz.GRAPHVIZ_PATCH_VERSION) == (2, 42, 2)
print("pygraphviz==2.0.1 diagrams==0.25.1")
PY

# OpenJDK 17.0.20
java_identity=$(java -version 2>&1)
case "$java_identity" in *'17.0.20'*) ;; *) echo "$java_identity" >&2; exit 32;; esac

echo '{PLANTUML_SHA256}  /opt/plantuml/plantuml.jar' | sha256sum -c -
plantuml_identity=$(java -jar /opt/plantuml/plantuml.jar -version)
# PlantUML version 1.2026.6
case "$plantuml_identity" in 'PlantUML version 1.2026.6 '*|'PlantUML version 1.2026.6') ;; *) echo "$plantuml_identity" >&2; exit 33;; esac

DJANGO_SETTINGS_MODULE=theorem_control.settings PLANTUML_SECURITY_PROFILE=SANDBOX python - <<'PY'
import django
django.setup()
from apps.rendering.service import render_diagrams, render_plantuml
plantuml, media, version = render_plantuml("@startuml\\nAlice -> Bob: image oracle\\n@enduml\\n")
assert media == "image/svg+xml" and version == "1.2026.6" and b"<svg" in plantuml[:1000].lower()
source = "from diagrams import Diagram\\nfrom diagrams.generic.blank import Blank\\nwith Diagram('Image Oracle'):\\n    Blank('node')\\n"
svg, svg_media, diagrams_version = render_diagrams(source, "svg")
png, png_media, _ = render_diagrams(source, "png")
assert diagrams_version == "0.25.1"
assert svg_media == "image/svg+xml" and b"<svg" in svg[:1000].lower()
assert png_media == "image/png" and png.startswith(b"\\x89PNG\\r\\n\\x1a\\n")
PY

# manage.py check
python manage.py check
pytest -q --junitxml=/tmp/pinned-cold.xml tests/test_layout_contract.py::test_pinned_graphviz_cold_recompute_matches_exact_fixture_bytes
python - <<'PY'
import xml.etree.ElementTree as ET
root = ET.parse("/tmp/pinned-cold.xml").getroot()
suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
assert len(suites) == 1
suite = suites[0]
assert tuple(int(suite.attrib.get(name, "-1")) for name in ("tests", "failures", "errors", "skipped")) == (1, 0, 0, 0)
PY
"""


def _build_command(runtime: RuntimeCandidate, image_tag: str) -> list[str]:
    assert runtime.executable is not None
    return [runtime.executable, "build", "-f", "Dockerfile", "-t", image_tag, "."]


def _run_command(runtime: RuntimeCandidate, image_tag: str) -> list[str]:
    assert runtime.executable is not None
    return [
        runtime.executable,
        "run",
        "--rm",
        image_tag,
        "/bin/sh",
        "-lc",
        container_probe_script(),
    ]


def _remove_command(runtime: RuntimeCandidate, image_tag: str) -> list[str]:
    assert runtime.executable is not None
    return [runtime.executable, "image", "rm", image_tag]


def run_container_oracle(runtime: RuntimeCandidate) -> None:
    dockerfile = REPOSITORY_ROOT / "Dockerfile"
    verify_declared_dockerfile(dockerfile)
    verify_declared_requirements(REPOSITORY_ROOT / "requirements.txt")
    free_bytes = shutil.disk_usage(REPOSITORY_ROOT).free
    if free_bytes < MINIMUM_FREE_BYTES:
        raise ContainerOraclePrerequisiteError(
            "declared-image build requires at least 20 GiB free on the repository volume; "
            f"found {free_bytes / (1024 ** 3):.1f} GiB"
        )
    digest = hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:16]
    image_tag = f"theorem-rendering-oracle:{digest}"
    built = False
    try:
        _run_bounded(
            _build_command(runtime, image_tag),
            timeout_seconds=BUILD_TIMEOUT_SECONDS,
            cwd=REPOSITORY_ROOT,
        )
        built = True
        _run_bounded(
            _run_command(runtime, image_tag),
            timeout_seconds=PROBE_TIMEOUT_SECONDS,
            cwd=REPOSITORY_ROOT,
        )
    finally:
        if built:
            status, output = _run_bounded(
                _remove_command(runtime, image_tag),
                timeout_seconds=PROCESS_TIMEOUT_SECONDS,
                cwd=REPOSITORY_ROOT,
                check=False,
            )
            if status != 0:
                raise ContainerOracleError(
                    f"helper-owned image cleanup failed for {image_tag}: {output}"
                )


def main() -> int:
    inventory = inspect_runtime_candidates()
    for candidate in inventory:
        location = candidate.executable or "missing"
        print(f"{candidate.name}: {candidate.status} ({location})")
    try:
        verify_declared_dockerfile(REPOSITORY_ROOT / "Dockerfile")
        verify_declared_requirements(REPOSITORY_ROOT / "requirements.txt")
        runtime = select_runtime(inventory)
        run_container_oracle(runtime)
    except ContainerOraclePrerequisiteError as exc:
        print(f"declared-image prerequisite missing: {exc}", file=sys.stderr)
        return 2
    except (ContainerOracleError, OSError, subprocess.SubprocessError) as exc:
        print(f"declared-image oracle failed: {exc}", file=sys.stderr)
        return 1
    print(f"declared-image oracle passed with {runtime.name}; helper image removed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
