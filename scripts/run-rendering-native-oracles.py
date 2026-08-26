#!/usr/bin/env python3
"""Replay PlantUML and Diagrams through real, checksum-bound native runtimes."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from uuid import UUID

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

PLANTUML_VERSION = "1.2026.6"
PLANTUML_SHA256 = (
    "89948f14c93756c7a3fb7b69078ff37e8489fd79dd430c582b931e2f65358690"
)
PLANTUML_URL = (
    "https://github.com/plantuml/plantuml/releases/download/"
    f"v{PLANTUML_VERSION}/plantuml-{PLANTUML_VERSION}.jar"
)
GRAPHVIZ_NATIVE_VERSION = "14.1.5"
GRAPHVIZ_NATIVE_SHA256 = (
    "b017378835f7ca12f1a3f1db5c338d7e7af16b284b7007ad73ccec960c1b45b3"
)
GRAPHVIZ_NATIVE_URL = (
    "https://gitlab.com/api/v4/projects/4207231/packages/generic/"
    f"graphviz-releases/{GRAPHVIZ_NATIVE_VERSION}/"
    f"graphviz-{GRAPHVIZ_NATIVE_VERSION}.tar.xz"
)
DOWNLOAD_TIMEOUT_SECONDS = 120.0
PROCESS_OUTPUT_MAX_BYTES = 4 * 1024 * 1024
GRAPHVIZ_ARCHIVE_MAX_BYTES = 128 * 1024 * 1024
PLANTUML_ARCHIVE_MAX_BYTES = 32 * 1024 * 1024
NATIVE_TENANT_ID = UUID("00000000-0000-0000-0000-000000000005")
SAFE_DIAGRAM = """\
from diagrams import Diagram
from diagrams.generic.blank import Blank

with Diagram("Native Oracle", direction="LR"):
    Blank("producer") >> Blank("consumer")
"""


class NativeOracleError(RuntimeError):
    """A real native renderer, its provenance, or cleanup failed."""


@dataclasses.dataclass(frozen=True)
class NativeReceipt:
    java_identity: str
    plantuml_version: str
    plantuml_sha256: str
    plantuml_svg_digest: str
    plantuml_include_refusal: str
    diagrams_version: str
    graphviz_version: str
    diagrams_svg_digest: str
    diagrams_png_digest: str
    diagrams_svg_key: str
    diagrams_png_key: str
    diagrams_import_refusal: str


def _download_verified(
    url: str,
    destination: Path,
    *,
    sha256_hex: str,
    max_bytes: int,
) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "theorem-rendering-native-oracle/1.0"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    digest = hashlib.sha256()
    size = 0
    try:
        with (
            opener.open(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response,
            destination.open("xb") as output,
        ):
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise NativeOracleError("download exceeded its size cap")
                digest.update(chunk)
                output.write(chunk)
        actual = digest.hexdigest()
        if actual != sha256_hex:
            raise NativeOracleError(
                f"download checksum mismatch: {actual} != {sha256_hex}"
            )
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def _process_environment(*, temporary_root: Path) -> dict[str, str]:
    path = os.environ.get("PATH") or os.defpath
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": path,
        "TMPDIR": str(temporary_root),
    }


def _run_checked(
    command: list[str],
    *,
    environment: dict[str, str],
    timeout_seconds: float,
    cwd: Path | None = None,
) -> str:
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
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
            raise NativeOracleError(
                f"process exceeded {timeout_seconds:g}s: {command[0]}"
            ) from exc
        stdout_size = os.fstat(stdout_file.fileno()).st_size
        stderr_size = os.fstat(stderr_file.fileno()).st_size
        if max(stdout_size, stderr_size) > PROCESS_OUTPUT_MAX_BYTES:
            raise NativeOracleError(f"process output exceeded cap: {command[0]}")
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read().decode("utf-8", errors="replace")
        stderr = stderr_file.read().decode("utf-8", errors="replace")
    combined = "\n".join(part for part in (stdout.strip(), stderr.strip()) if part)
    if process.returncode != 0:
        raise NativeOracleError(
            f"process failed ({process.returncode}): {' '.join(command)}\n"
            f"{combined[-4000:]}"
        )
    return combined


def _extract_graphviz(archive: Path, destination: Path) -> Path:
    with tarfile.open(archive, mode="r:xz") as bundle:
        roots = {Path(member.name).parts[0] for member in bundle.getmembers()}
        expected_root = f"graphviz-{GRAPHVIZ_NATIVE_VERSION}"
        if roots != {expected_root}:
            raise NativeOracleError("Graphviz archive has an unexpected root")
        bundle.extractall(destination, filter="data")
    source = destination / expected_root
    if not (source / "configure").is_file():
        raise NativeOracleError("Graphviz release archive did not contain configure")
    return source


def _build_graphviz(temporary_root: Path) -> tuple[Path, str]:
    make = shutil.which("make")
    compiler = shutil.which("cc")
    if make is None or compiler is None:
        raise NativeOracleError(
            "a real native Graphviz build requires make and a C compiler"
        )
    archive = temporary_root / "graphviz.tar.xz"
    _download_verified(
        GRAPHVIZ_NATIVE_URL,
        archive,
        sha256_hex=GRAPHVIZ_NATIVE_SHA256,
        max_bytes=GRAPHVIZ_ARCHIVE_MAX_BYTES,
    )
    source = _extract_graphviz(archive, temporary_root / "source")
    build = temporary_root / "graphviz-build"
    prefix = temporary_root / "graphviz-prefix"
    build.mkdir()
    environment = _process_environment(temporary_root=temporary_root)
    environment["CC"] = compiler
    configure = [
        str(source / "configure"),
        f"--prefix={prefix}",
        "--enable-static",
        "--enable-shared",
        "--enable-ltdl",
        "--with-included-ltdl",
        "--disable-swig",
        "--disable-sharp",
        "--disable-go",
        "--disable-guile",
        "--disable-java",
        "--disable-lua",
        "--disable-perl",
        "--disable-python",
        "--disable-python3",
        "--disable-ruby",
        "--disable-tcl",
        "--without-x",
        "--without-devil",
        "--without-webp",
        "--without-poppler",
        "--without-rsvg",
        "--without-ghostscript",
        "--without-lasi",
        "--without-gdk",
        "--without-gdk-pixbuf",
        "--without-gtk",
        "--without-gtkgl",
        "--without-gtkglext",
        "--without-gts",
        "--without-glade",
        "--without-qt",
        "--with-quartz=yes",
        "--without-smyrna",
    ]
    _run_checked(
        configure,
        environment=environment,
        timeout_seconds=180.0,
        cwd=build,
    )
    _run_checked(
        [make, "-j2"],
        environment=environment,
        timeout_seconds=600.0,
        cwd=build,
    )
    _run_checked(
        [make, "install"],
        environment=environment,
        timeout_seconds=180.0,
        cwd=build,
    )
    dot = prefix / "bin" / "dot"
    if not dot.is_file() or not os.access(dot, os.X_OK):
        raise NativeOracleError("Graphviz build did not produce an executable dot")
    identity_environment = dict(environment)
    identity_environment["PATH"] = f"{prefix / 'bin'}:{environment['PATH']}"
    identity = _run_checked(
        [str(dot), "-V"],
        environment=identity_environment,
        timeout_seconds=5.0,
    )
    match = re.search(r"graphviz version (?P<version>[0-9.]+)", identity)
    if match is None or match.group("version") != GRAPHVIZ_NATIVE_VERSION:
        raise NativeOracleError(f"Graphviz reported unexpected identity: {identity!r}")
    return prefix / "bin", match.group("version")


def _content_key(payload: bytes, extension: str) -> tuple[str, str]:
    from apps.orchestration.artifacts import ArtifactStore, sha256_digest

    digest = sha256_digest(payload)
    store = object.__new__(ArtifactStore)
    key = store.render_artifact_key(NATIVE_TENANT_ID, digest, extension)
    return digest, key


def _validate_receipt(receipt: NativeReceipt) -> None:
    if "openjdk" not in receipt.java_identity.lower():
        raise NativeOracleError("Java identity was not OpenJDK")
    if receipt.plantuml_version != PLANTUML_VERSION:
        raise NativeOracleError("PlantUML version did not match the declared release")
    if receipt.plantuml_sha256 != PLANTUML_SHA256:
        raise NativeOracleError("PlantUML checksum did not match the declared release")
    if receipt.diagrams_version != "0.25.1":
        raise NativeOracleError("Diagrams version must be exactly 0.25.1")
    if receipt.graphviz_version != GRAPHVIZ_NATIVE_VERSION:
        raise NativeOracleError("native Graphviz source identity did not match")
    for label, digest in (
        ("PlantUML SVG", receipt.plantuml_svg_digest),
        ("Diagrams SVG", receipt.diagrams_svg_digest),
        ("Diagrams PNG", receipt.diagrams_png_digest),
    ):
        if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            raise NativeOracleError(f"{label} digest is malformed")
    for digest, key, extension in (
        (receipt.diagrams_svg_digest, receipt.diagrams_svg_key, "svg"),
        (receipt.diagrams_png_digest, receipt.diagrams_png_key, "png"),
    ):
        if not key.endswith(f"/{digest.removeprefix('sha256:')}.{extension}"):
            raise NativeOracleError("Diagrams content key does not match its bytes")
    if "cannot include /etc/passwd" not in receipt.plantuml_include_refusal.lower():
        raise NativeOracleError("PlantUML local-include refusal was not proven")
    if "'os'" not in receipt.diagrams_import_refusal:
        raise NativeOracleError("Diagrams refusal did not name the offending import")


def _execute_native_oracles(temporary_root: Path) -> NativeReceipt:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "theorem_control.settings")
    import django
    from django.test import override_settings

    django.setup()

    from apps.orchestration.artifacts import sha256_digest
    from apps.rendering.service import (
        RenderExecutionError,
        render_diagrams,
        render_plantuml,
    )
    from apps.rendering.validation import DiagramSourceError

    java = shutil.which("java")
    if java is None:
        raise NativeOracleError("a real Java executable is required")
    process_environment = _process_environment(temporary_root=temporary_root)
    java_identity = _run_checked(
        [java, "-version"],
        environment=process_environment,
        timeout_seconds=5.0,
    ).splitlines()[0]

    jar = temporary_root / "plantuml.jar"
    _download_verified(
        PLANTUML_URL,
        jar,
        sha256_hex=PLANTUML_SHA256,
        max_bytes=PLANTUML_ARCHIVE_MAX_BYTES,
    )
    graphviz_bin, graphviz_version = _build_graphviz(temporary_root)

    with override_settings(
        PLANTUML_JAR_PATH=str(jar),
        PLANTUML_VERSION=PLANTUML_VERSION,
        PLANTUML_SHA256=PLANTUML_SHA256,
        PLANTUML_SECURITY_PROFILE="SANDBOX",
        RENDER_GRAPHVIZ_BIN_DIR=str(graphviz_bin),
        RENDER_SUBPROCESS_TIMEOUT_SECONDS=12.0,
        RENDER_OUTPUT_MAX_BYTES=16 * 1024 * 1024,
    ):
        plantuml_source = "@startuml\nAlice -> Bob: native oracle\n@enduml\n"
        first_plantuml, plantuml_type, plantuml_version = render_plantuml(
            plantuml_source
        )
        second_plantuml, _, _ = render_plantuml(plantuml_source)
        if first_plantuml != second_plantuml or plantuml_type != "image/svg+xml":
            raise NativeOracleError("PlantUML SVG replay was not byte deterministic")
        try:
            render_plantuml(
                "@startuml\n!include /etc/passwd\nAlice -> Bob\n@enduml\n"
            )
        except RenderExecutionError as exc:
            plantuml_include_refusal = str(exc)
        else:
            raise NativeOracleError("PlantUML admitted a local include under SANDBOX")

        svg_first, svg_type, diagrams_version = render_diagrams(SAFE_DIAGRAM, "svg")
        png_first, png_type, png_version = render_diagrams(SAFE_DIAGRAM, "png")
        if svg_type != "image/svg+xml" or png_type != "image/png":
            raise NativeOracleError("Diagrams returned an unexpected media type")
        if diagrams_version != "0.25.1" or png_version != diagrams_version:
            raise NativeOracleError("Diagrams package identity drifted")
        try:
            render_diagrams("import os\n", "svg")
        except DiagramSourceError as exc:
            diagrams_import_refusal = str(exc)
        else:
            raise NativeOracleError("Diagrams admitted a forbidden import")

    svg_digest, svg_key = _content_key(svg_first, "svg")
    png_digest, png_key = _content_key(png_first, "png")
    receipt = NativeReceipt(
        java_identity=java_identity,
        plantuml_version=plantuml_version,
        plantuml_sha256=PLANTUML_SHA256,
        plantuml_svg_digest=sha256_digest(first_plantuml),
        plantuml_include_refusal=plantuml_include_refusal,
        diagrams_version=diagrams_version,
        graphviz_version=graphviz_version,
        diagrams_svg_digest=svg_digest,
        diagrams_png_digest=png_digest,
        diagrams_svg_key=svg_key,
        diagrams_png_key=png_key,
        diagrams_import_refusal=diagrams_import_refusal,
    )
    _validate_receipt(receipt)
    return receipt


def run_oracles() -> NativeReceipt:
    temporary = tempfile.TemporaryDirectory(prefix="theorem-rendering-native-")
    temporary_root = Path(temporary.name)
    try:
        receipt = _execute_native_oracles(temporary_root)
    finally:
        temporary.cleanup()
    if temporary_root.exists():
        raise NativeOracleError("native renderer temporary state survived cleanup")
    return receipt


def main() -> int:
    try:
        receipt = run_oracles()
    except (NativeOracleError, OSError, subprocess.SubprocessError) as exc:
        print(f"native rendering oracle failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(dataclasses.asdict(receipt), sort_keys=True, separators=(",", ":")))
    print("native rendering oracle passed; temporary binaries were removed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
