#!/usr/bin/env python3
"""Build and verify the exact declared rendering image when a runtime exists."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from apps.rendering.oracle_process import BoundedProcessError  # noqa: E402
from apps.rendering.oracle_process import run_bounded_process  # noqa: E402

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
BUILD_CLIENTS = {"docker"}
DOCKERIGNORE_REQUIRED_PATTERNS = (
    ".git",
    ".git/**",
    ".github/",
    ".agents/",
    ".claude/",
    ".codex/",
    ".superpowers/",
    ".env",
    ".env*",
    ".env.*",
    "!.env.example",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_ed25519",
    ".venv/",
    "venv/",
    "**/__pycache__/",
    "*.py[cod]",
    "*.egg-info/",
    ".eggs/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".coverage",
    ".coverage.*",
    "htmlcov/",
    "build/",
    "dist/",
    "target/",
    "node_modules/",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
    "media/",
    "staticfiles/",
    "logs/",
    "tmp/",
    "temp/",
    "*.log",
    "celerybeat-schedule",
    ".DS_Store",
)


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
    endpoint: str | None = None


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
    try:
        result = run_bounded_process(
            command,
            environment=_minimal_environment(),
            timeout_seconds=timeout_seconds,
            output_max_bytes=PROCESS_OUTPUT_MAX_BYTES,
            cwd=cwd,
            check=check,
        )
    except BoundedProcessError as exc:
        raise ContainerOracleError(str(exc)) from exc
    return result.returncode, result.output


def require_local_runtime_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "unix"
        or parsed.netloc
        or not parsed.path.startswith("/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ContainerOraclePrerequisiteError(
            f"runtime endpoint must be a local Unix socket, not {endpoint!r}"
        )
    return parsed.path


def _docker_endpoint(executable: str) -> str:
    configured = os.environ.get("DOCKER_HOST", "").strip()
    if configured:
        return configured
    status, output = _run_bounded(
        [
            executable,
            "context",
            "inspect",
            "--format",
            "{{.Endpoints.docker.Host}}",
        ],
        timeout_seconds=5.0,
        check=False,
    )
    if status != 0 or len(output.splitlines()) != 1:
        raise ContainerOraclePrerequisiteError(
            "Docker current-context endpoint could not be resolved"
        )
    return output.strip()


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
        if name not in BUILD_CLIENTS:
            inventory.append(
                RuntimeCandidate(
                    name,
                    executable,
                    "prerequisite-only",
                    f"{version}; locality-safe execution is not implemented",
                )
            )
            continue
        try:
            endpoint = _docker_endpoint(executable)
            require_local_runtime_endpoint(endpoint)
        except ContainerOracleError as exc:
            inventory.append(
                RuntimeCandidate(name, executable, "remote-refused", str(exc))
            )
            continue
        status, detail = _run_bounded(
            [executable, "--host", endpoint, "info"],
            timeout_seconds=PROCESS_TIMEOUT_SECONDS,
            check=False,
        )
        inventory.append(
            RuntimeCandidate(
                name,
                executable,
                "usable" if status == 0 else "daemon-unavailable",
                f"{version}; {detail}".strip("; "),
                endpoint,
            )
        )
    return tuple(inventory)


def select_runtime(inventory: tuple[RuntimeCandidate, ...]) -> RuntimeCandidate:
    for candidate in inventory:
        if candidate.name in BUILD_CLIENTS and candidate.status == "usable":
            if candidate.endpoint is None:
                raise ContainerOraclePrerequisiteError(
                    "usable runtime did not record a local endpoint"
                )
            require_local_runtime_endpoint(candidate.endpoint)
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


def verify_dockerignore(path: Path) -> None:
    try:
        entries = {
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
    except OSError as exc:
        raise ContainerOracleError(f"cannot read .dockerignore: {exc}") from exc
    missing = [value for value in DOCKERIGNORE_REQUIRED_PATTERNS if value not in entries]
    if missing:
        raise ContainerOracleError(
            ".dockerignore is missing required secret/state exclusions: "
            + ", ".join(missing)
        )


def parse_graphviz_version(output: str) -> str:
    line = output.strip()
    match = re.fullmatch(
        r"(?:dot - )?graphviz version (?P<version>[0-9]+(?:\.[0-9]+)+)(?: \([^)]*\))?",
        line,
    )
    if match is None:
        raise ContainerOracleError(f"malformed Graphviz identity: {line!r}")
    return match.group("version")


def parse_java_version(output: str) -> str:
    line = output.splitlines()[0].strip() if output.strip() else ""
    match = re.fullmatch(
        r'(?:openjdk|java) version "(?P<version>[^"]+)"(?: .*)?',
        line,
    )
    if match is None:
        raise ContainerOracleError(f"malformed Java identity: {line!r}")
    return match.group("version")


def container_probe_script() -> str:
    return f"""set -eu
# Graphviz 2.42.2
# pygraphviz==2.0.1 and diagrams==0.25.1
python - <<'PY'
import importlib.metadata
import os
import re
import shutil
import subprocess
from pygraphviz import _graphviz

dot = shutil.which("dot")
assert dot is not None and os.access(dot, os.X_OK)
dot_identity = subprocess.check_output(
    [dot, "-V"], stderr=subprocess.STDOUT, text=True
).strip()
match = re.fullmatch(
    r"(?:dot - )?graphviz version (?P<version>[0-9]+(?:\\.[0-9]+)+)(?: \\([^)]*\\))?",
    dot_identity,
)
assert match is not None
version = match.group("version")
assert version == '2.42.2'
linked_version = ".".join(
    str(value)
    for value in (
        _graphviz.GRAPHVIZ_MAJOR_VERSION,
        _graphviz.GRAPHVIZ_MINOR_VERSION,
        _graphviz.GRAPHVIZ_PATCH_VERSION,
    )
)
assert linked_version == version
assert importlib.metadata.version("pygraphviz") == "2.0.1"
assert importlib.metadata.version("diagrams") == "0.25.1"

java_identity = subprocess.check_output(
    ["java", "-version"], stderr=subprocess.STDOUT, text=True
).splitlines()[0]
match = re.fullmatch(
    r'(?:openjdk|java) version "(?P<version>[^"]+)"(?: .*)?',
    java_identity,
)
assert match is not None
version = match.group("version")
assert version == '17.0.20'
print("pygraphviz==2.0.1 diagrams==0.25.1")
PY

# OpenJDK 17.0.20
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


def _runtime_command(runtime: RuntimeCandidate, *arguments: str) -> list[str]:
    assert runtime.executable is not None
    assert runtime.endpoint is not None
    return [runtime.executable, "--host", runtime.endpoint, *arguments]


def _build_command(
    runtime: RuntimeCandidate, image_tag: str, image_id_path: Path
) -> list[str]:
    return _runtime_command(
        runtime,
        "build",
        "-f",
        "Dockerfile",
        "--iidfile",
        str(image_id_path),
        "-t",
        image_tag,
        ".",
    )


def _run_command(runtime: RuntimeCandidate, image_tag: str) -> list[str]:
    return _runtime_command(
        runtime,
        "run",
        "--rm",
        image_tag,
        "/bin/sh",
        "-lc",
        container_probe_script(),
    )


def _remove_command(runtime: RuntimeCandidate, image_reference: str) -> list[str]:
    return _runtime_command(runtime, "image", "rm", image_reference)


def new_image_tag(context_digest: str) -> str:
    return (
        f"theorem-rendering-oracle:{context_digest[:16]}-"
        f"{secrets.token_hex(16)}"
    )


def inspect_image_id(runtime: RuntimeCandidate, image_tag: str) -> str | None:
    status, output = _run_bounded(
        _runtime_command(
            runtime, "image", "inspect", "--format", "{{.Id}}", image_tag
        ),
        timeout_seconds=PROCESS_TIMEOUT_SECONDS,
        cwd=REPOSITORY_ROOT,
        check=False,
    )
    if status != 0:
        lowered = output.casefold()
        if any(
            marker in lowered
            for marker in ("no such image", "no such object", "not found")
        ):
            return None
        raise ContainerOracleError(
            f"could not inspect helper image tag {image_tag}: {output[-4000:]}"
        )
    identity = output.strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", identity) is None:
        raise ContainerOracleError(f"runtime returned malformed image ID: {identity!r}")
    return identity


def reserve_image_tag(runtime: RuntimeCandidate, image_tag: str) -> None:
    if inspect_image_id(runtime, image_tag) is not None:
        raise ContainerOracleError(f"refusing pre-existing image tag: {image_tag}")


def _read_created_image_id(path: Path) -> str:
    try:
        identity = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ContainerOracleError(f"build did not record its image ID: {exc}") from exc
    if re.fullmatch(r"sha256:[0-9a-f]{64}", identity) is None:
        raise ContainerOracleError(f"build recorded malformed image ID: {identity!r}")
    return identity


def cleanup_created_image(
    runtime: RuntimeCandidate, image_tag: str, created_image_id: str
) -> None:
    current_image_id = inspect_image_id(runtime, image_tag)
    if current_image_id != created_image_id:
        raise ContainerOracleError(
            f"image tag ownership changed; refusing cleanup for {image_tag}"
        )
    status, output = _run_bounded(
        _remove_command(runtime, created_image_id),
        timeout_seconds=PROCESS_TIMEOUT_SECONDS,
        cwd=REPOSITORY_ROOT,
        check=False,
    )
    remaining_image_id = inspect_image_id(runtime, image_tag)
    if remaining_image_id not in (None, created_image_id):
        refusal = f"; immutable-ID removal result: {output[-4000:]}" if output else ""
        raise ContainerOracleError(
            f"image tag ownership changed during cleanup for {image_tag}{refusal}"
        )
    if status != 0:
        raise ContainerOracleError(
            "helper-owned immutable image cleanup failed for "
            f"{created_image_id}: {output[-4000:]}"
        )
    if remaining_image_id == created_image_id:
        raise ContainerOracleError(f"helper image survived cleanup: {image_tag}")


def raise_after_cleanup(
    primary_error: Exception | None, cleanup_error: Exception | None
) -> None:
    if primary_error is not None and cleanup_error is not None:
        raise ContainerOracleError(
            f"primary failure: {primary_error}; cleanup also failed: {cleanup_error}"
        ) from primary_error
    if primary_error is not None:
        raise primary_error
    if cleanup_error is not None:
        raise cleanup_error


def run_container_oracle(runtime: RuntimeCandidate) -> None:
    if runtime.endpoint is None:
        raise ContainerOraclePrerequisiteError(
            "declared-image runtime has no recorded endpoint"
        )
    require_local_runtime_endpoint(runtime.endpoint)
    dockerfile = REPOSITORY_ROOT / "Dockerfile"
    verify_declared_dockerfile(dockerfile)
    verify_declared_requirements(REPOSITORY_ROOT / "requirements.txt")
    dockerignore = REPOSITORY_ROOT / ".dockerignore"
    verify_dockerignore(dockerignore)
    free_bytes = shutil.disk_usage(REPOSITORY_ROOT).free
    if free_bytes < MINIMUM_FREE_BYTES:
        raise ContainerOraclePrerequisiteError(
            "declared-image build requires at least 20 GiB free on the repository volume; "
            f"found {free_bytes / (1024 ** 3):.1f} GiB"
        )
    context_digest = hashlib.sha256(
        dockerfile.read_bytes()
        + (REPOSITORY_ROOT / "requirements.txt").read_bytes()
        + dockerignore.read_bytes()
    ).hexdigest()
    image_tag = new_image_tag(context_digest)
    reserve_image_tag(runtime, image_tag)
    primary_error: Exception | None = None
    cleanup_error: Exception | None = None
    created_image_id: str | None = None
    with tempfile.TemporaryDirectory(prefix="theorem-container-oracle-") as temporary:
        image_id_path = Path(temporary) / "image-id"
        try:
            _run_bounded(
                _build_command(runtime, image_tag, image_id_path),
                timeout_seconds=BUILD_TIMEOUT_SECONDS,
                cwd=REPOSITORY_ROOT,
            )
            created_image_id = _read_created_image_id(image_id_path)
            tagged_image_id = inspect_image_id(runtime, image_tag)
            if tagged_image_id != created_image_id:
                raise ContainerOracleError(
                    "built image ID did not match the helper-owned tag"
                )
            _run_bounded(
                _run_command(runtime, image_tag),
                timeout_seconds=PROBE_TIMEOUT_SECONDS,
                cwd=REPOSITORY_ROOT,
            )
        except Exception as exc:
            primary_error = exc
        finally:
            if created_image_id is not None:
                try:
                    cleanup_created_image(runtime, image_tag, created_image_id)
                except Exception as exc:
                    cleanup_error = exc
    raise_after_cleanup(primary_error, cleanup_error)


def main() -> int:
    inventory = inspect_runtime_candidates()
    for candidate in inventory:
        location = candidate.executable or "missing"
        print(f"{candidate.name}: {candidate.status} ({location})")
    try:
        verify_declared_dockerfile(REPOSITORY_ROOT / "Dockerfile")
        verify_declared_requirements(REPOSITORY_ROOT / "requirements.txt")
        verify_dockerignore(REPOSITORY_ROOT / ".dockerignore")
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
