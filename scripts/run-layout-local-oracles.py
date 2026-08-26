#!/usr/bin/env python3
"""Run exact board, real Valkey, and pinned disposable MinIO local oracles."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import secrets
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Mapping
from typing import TextIO
from uuid import uuid4

import boto3
from botocore.config import Config

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MINIO_RELEASE = "RELEASE.2025-09-07T16-13-09Z"
MINIO_URL = (
    f"https://dl.min.io/server/minio/release/darwin-arm64/archive/minio.{MINIO_RELEASE}"
)
MINIO_SHA256 = "7c3b3039b76e55a1b80935848ed83998d5e8d317374f87851f46a019ff5c0aa4"
DOWNLOAD_TIMEOUT_SECONDS = 30.0
VERSION_TIMEOUT_SECONDS = 5.0
READINESS_TIMEOUT_SECONDS = 15.0
PYTEST_TIMEOUT_SECONDS = 180.0
SHUTDOWN_TIMEOUT_SECONDS = 5.0
PORT_CLOSE_TIMEOUT_SECONDS = 3.0
LOOPBACK_NO_PROXY = "127.0.0.1,localhost,::1"
SYSTEM_ENVIRONMENT_ALLOWLIST = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
)
REQUIRED_TEST_NODE_IDS = (
    "tests/test_layout_local_oracles.py::test_plan_flow_oracle_rejects_a_collapsed_dependency_rank",
    "tests/test_layout_local_oracles.py::test_exact_agent_chat_board_uses_plan_dag_native_layout",
    "tests/test_layout_local_oracles.py::test_real_local_valkey_cold_warm_and_version_sensitive_keys",
    "tests/test_layout_local_oracles.py::test_production_artifact_store_against_actual_local_s3",
)
EXPECTED_TEST_IDENTITIES = tuple(
    ("tests.test_layout_local_oracles", node_id.rsplit("::", 1)[1])
    for node_id in REQUIRED_TEST_NODE_IDS
)
VALKEY_VERSION_PATTERN = re.compile(
    r"^Valkey server v=(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:[-+][^ ]+)?)\b"
)


class LocalOracleError(RuntimeError):
    """A required real-process oracle or its cleanup failed."""


def _redact(value: str, sensitive_values: tuple[str, ...]) -> str:
    redacted = value
    for sensitive in sensitive_values:
        if sensitive:
            redacted = redacted.replace(sensitive, "<redacted>")
    return redacted


def _minimal_system_environment(
    inherited: Mapping[str, str],
    *,
    temporary_root: Path | None = None,
) -> dict[str, str]:
    environment = {
        name: inherited[name]
        for name in SYSTEM_ENVIRONMENT_ALLOWLIST
        if inherited.get(name)
    }
    environment.setdefault("PATH", os.defpath)
    if temporary_root is not None:
        environment["TMPDIR"] = str(temporary_root)
    environment["NO_PROXY"] = LOOPBACK_NO_PROXY
    environment["no_proxy"] = LOOPBACK_NO_PROXY
    return environment


def _resolve_valkey_executable(
    inherited: Mapping[str, str],
) -> tuple[Path, str]:
    configured = inherited.get("THEOREM_TEST_VALKEY_SERVER", "").strip()
    candidate = configured or "valkey-server"
    resolved = shutil.which(candidate, path=inherited.get("PATH") or os.defpath)
    if resolved is None:
        raise LocalOracleError(
            "a real valkey-server executable is required before local replay"
        )
    try:
        executable = Path(resolved).resolve(strict=True)
    except OSError as error:
        raise LocalOracleError(
            f"cannot resolve Valkey executable {resolved!r}: {error}"
        ) from error
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise LocalOracleError(f"Valkey executable is not executable: {executable}")

    result = subprocess.run(
        [str(executable), "--version"],
        capture_output=True,
        text=True,
        timeout=VERSION_TIMEOUT_SECONDS,
        check=False,
        env=_minimal_system_environment(inherited),
    )
    first_line = (result.stdout or result.stderr).splitlines()
    identity = first_line[0] if first_line else ""
    match = VALKEY_VERSION_PATTERN.match(identity)
    if result.returncode != 0 or match is None:
        raise LocalOracleError(
            f"Valkey executable reported unexpected identity: {identity!r}"
        )
    return executable, match.group("version")


def _build_minio_environment(
    inherited: Mapping[str, str],
    *,
    temporary_root: Path,
    access_key: str,
    secret_key: str,
) -> dict[str, str]:
    environment = _minimal_system_environment(
        inherited,
        temporary_root=temporary_root,
    )
    environment.update(
        MINIO_ROOT_USER=access_key,
        MINIO_ROOT_PASSWORD=secret_key,
        MINIO_BROWSER="off",
        MINIO_UPDATE="off",
    )
    return environment


def _build_pytest_environment(
    inherited: Mapping[str, str],
    *,
    temporary_root: Path,
    endpoint_url: str,
    access_key: str,
    secret_key: str,
    valkey_executable: Path,
) -> dict[str, str]:
    environment = _minimal_system_environment(
        inherited,
        temporary_root=temporary_root,
    )
    environment.update(
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTHONDONTWRITEBYTECODE="1",
        DJANGO_SETTINGS_MODULE="theorem_control.settings",
        SECRET_KEY="local-layout-oracle-not-a-secret",
        DATABASE_URL="sqlite:///:memory:",
        VALKEY_URL="",
        REDIS_URL="",
        AWS_EC2_METADATA_DISABLED="true",
        THEOREM_TEST_VALKEY_SERVER=str(valkey_executable),
        THEOREM_LAYOUT_LOCAL_S3_ENDPOINT_URL=endpoint_url,
        THEOREM_LAYOUT_LOCAL_S3_ACCESS_KEY_ID=access_key,
        THEOREM_LAYOUT_LOCAL_S3_SECRET_ACCESS_KEY=secret_key,
        THEOREM_LAYOUT_LOCAL_S3_REGION="us-east-1",
        THEOREM_LAYOUT_LOCAL_S3_BUCKET_PREFIX="theorem-layout-oracle",
    )
    return environment


def _download_and_verify(destination: Path) -> None:
    request = urllib.request.Request(
        MINIO_URL,
        headers={"User-Agent": "theorem-layout-local-oracle/1.0"},
    )
    digest = hashlib.sha256()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with (
        opener.open(
            request,
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        ) as response,
        destination.open("xb") as output,
    ):
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
    actual = digest.hexdigest()
    if actual != MINIO_SHA256:
        raise LocalOracleError(
            f"pinned MinIO checksum mismatch: {actual} != {MINIO_SHA256}"
        )


def _verify_binary_identity(binary: Path, environment: Mapping[str, str]) -> None:
    result = subprocess.run(
        [str(binary), "--version"],
        capture_output=True,
        text=True,
        timeout=VERSION_TIMEOUT_SECONDS,
        check=False,
        env=environment,
    )
    expected = f"version {MINIO_RELEASE}"
    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    if result.returncode != 0 or expected not in first_line:
        raise LocalOracleError(
            f"pinned MinIO binary reported unexpected identity: {first_line!r}"
        )


def _reserve_loopback_ports() -> tuple[list[socket.socket], tuple[int, int]]:
    reservations: list[socket.socket] = []
    try:
        for _ in range(2):
            reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            reservation.bind(("127.0.0.1", 0))
            reservations.append(reservation)
        ports = tuple(int(reservation.getsockname()[1]) for reservation in reservations)
        return reservations, (ports[0], ports[1])
    except BaseException:
        for reservation in reservations:
            reservation.close()
        raise


def _safe_log_excerpt(
    log_path: Path,
    sensitive_values: tuple[str, ...],
) -> str:
    try:
        excerpt = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
    except OSError:
        return "<MinIO log unavailable>"
    return _redact(excerpt, sensitive_values)


def _wait_for_minio(
    process: subprocess.Popen[str],
    health_url: str,
    log_path: Path,
    log_handle: TextIO,
    sensitive_values: tuple[str, ...],
) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log_handle.flush()
            raise LocalOracleError(
                "pinned MinIO exited before readiness:\n"
                + _safe_log_excerpt(log_path, sensitive_values)
            )
        try:
            with opener.open(health_url, timeout=1.0) as response:
                if response.status == 200:
                    return
        except (TimeoutError, urllib.error.URLError):
            time.sleep(0.05)
    log_handle.flush()
    raise LocalOracleError(
        "pinned MinIO did not become ready before its deadline:\n"
        + _safe_log_excerpt(log_path, sensitive_values)
    )


def _s3_client(
    endpoint_url: str,
    access_key: str,
    secret_key: str,
):
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name="us-east-1",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(
            signature_version="s3v4",
            connect_timeout=2,
            read_timeout=2,
            retries={"max_attempts": 1, "mode": "standard"},
            s3={"addressing_style": "path"},
            proxies={},
        ),
    )


def _prove_minio_identity_and_bucket(
    endpoint_url: str,
    access_key: str,
    secret_key: str,
) -> None:
    client = _s3_client(endpoint_url, access_key, secret_key)
    bucket = f"theorem-layout-minio-identity-{uuid4().hex[:12]}"
    created = False
    try:
        response = client.list_buckets()
        headers = response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
        server_identity = str(headers.get("server", ""))
        if "minio" not in server_identity.lower():
            raise LocalOracleError(
                f"ready S3 endpoint did not identify as MinIO: {server_identity!r}"
            )
        client.create_bucket(Bucket=bucket)
        created = True
        client.head_bucket(Bucket=bucket)
    finally:
        if created:
            client.delete_bucket(Bucket=bucket)
        client.close()


def _terminate_process_group(
    process: subprocess.Popen,
    *,
    label: str,
) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        return
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise LocalOracleError(f"{label} survived SIGTERM and SIGKILL") from error


def _pytest_command(receipt_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-p",
        "pytest_django.plugin",
        "-o",
        "addopts=",
        f"--junitxml={receipt_path}",
        "--",
        *REQUIRED_TEST_NODE_IDS,
    ]


def _require_pytest_receipt(receipt_path: Path) -> str:
    if not receipt_path.is_file():
        raise LocalOracleError("local-oracle pytest receipt is missing")
    try:
        root = ET.parse(receipt_path).getroot()
    except (ET.ParseError, OSError) as error:
        raise LocalOracleError("local-oracle pytest receipt is malformed") from error

    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = list(root.findall("testsuite"))
    else:
        raise LocalOracleError("local-oracle pytest receipt has an invalid root")
    if len(suites) != 1:
        raise LocalOracleError(
            "local-oracle pytest receipt must contain exactly one test suite"
        )
    suite = suites[0]
    try:
        tests = int(suite.attrib["tests"])
        failures = int(suite.attrib["failures"])
        errors = int(suite.attrib["errors"])
        skipped = int(suite.attrib["skipped"])
    except (KeyError, TypeError, ValueError) as error:
        raise LocalOracleError(
            "local-oracle pytest receipt has invalid counters"
        ) from error

    if tests != len(REQUIRED_TEST_NODE_IDS):
        raise LocalOracleError(
            f"local-oracle pytest receipt must contain exactly 4 tests, got {tests}"
        )
    if skipped != 0:
        raise LocalOracleError(
            f"local-oracle pytest receipt must contain zero skipped tests, got {skipped}"
        )
    if failures != 0 or errors != 0:
        raise LocalOracleError(
            f"local-oracle pytest receipt contains failures={failures} errors={errors}"
        )

    cases = suite.findall("testcase")
    identities = tuple(
        (case.attrib.get("classname", ""), case.attrib.get("name", ""))
        for case in cases
    )
    if len(cases) != len(REQUIRED_TEST_NODE_IDS) or set(identities) != set(
        EXPECTED_TEST_IDENTITIES
    ):
        raise LocalOracleError(
            "local-oracle pytest receipt does not identify the four required tests"
        )
    if len(set(identities)) != len(identities):
        raise LocalOracleError("local-oracle pytest receipt contains duplicate tests")
    for case in cases:
        statuses = {
            child.tag for child in case if child.tag in {"skipped", "failure", "error"}
        }
        if statuses:
            raise LocalOracleError(
                "local-oracle pytest receipt contains non-pass test status"
            )
    return "4 passed, 0 skipped"


def _run_pytest(
    environment: dict[str, str],
    receipt_path: Path,
    sensitive_values: tuple[str, ...],
) -> str:
    if receipt_path.exists():
        raise LocalOracleError("local-oracle pytest receipt path already exists")
    process = subprocess.Popen(
        _pytest_command(receipt_path),
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=PYTEST_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        _terminate_process_group(process, label="local-oracle pytest")
        raise LocalOracleError("local-oracle pytest exceeded 180 seconds") from error
    safe_output = _redact(output, sensitive_values)
    if safe_output:
        print(safe_output, end="" if safe_output.endswith("\n") else "\n")
    return_code = process.returncode
    if return_code != 0:
        raise LocalOracleError(f"local-oracle pytest failed with exit {return_code}")
    return _require_pytest_receipt(receipt_path)


def _require_closed_port(port: int) -> None:
    deadline = time.monotonic() + PORT_CLOSE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                time.sleep(0.05)
        except OSError:
            return
    raise LocalOracleError(f"loopback port {port} remained reachable after cleanup")


def run_oracles() -> None:
    if platform.system() != "Darwin" or platform.machine().lower() not in {
        "arm64",
        "aarch64",
    }:
        raise LocalOracleError("the pinned local MinIO oracle requires Darwin on ARM64")

    inherited_environment = os.environ
    valkey_executable, valkey_version = _resolve_valkey_executable(
        inherited_environment
    )
    temporary = tempfile.TemporaryDirectory(prefix="theorem-layout-minio-")
    root = Path(temporary.name)
    binary = root / f"minio.{MINIO_RELEASE}"
    data_dir = root / "data"
    config_dir = root / "config"
    minio_tmp_dir = root / "minio-tmp"
    pytest_tmp_dir = root / "pytest-tmp"
    log_path = root / "minio.log"
    receipt_path = root / "pytest-receipt.xml"
    server_process: subprocess.Popen[str] | None = None
    log_handle: TextIO | None = None
    ports: tuple[int, int] = ()
    failure: BaseException | None = None
    cleanup_failures: list[str] = []
    pytest_receipt: str | None = None

    access_key = f"theorem{secrets.token_hex(8)}"
    secret_key = secrets.token_urlsafe(32)
    sensitive_values = (access_key, secret_key)
    try:
        _download_and_verify(binary)
        binary.chmod(0o700)
        data_dir.mkdir()
        config_dir.mkdir()
        minio_tmp_dir.mkdir()
        pytest_tmp_dir.mkdir()

        server_environment = _build_minio_environment(
            inherited_environment,
            temporary_root=minio_tmp_dir,
            access_key=access_key,
            secret_key=secret_key,
        )
        _verify_binary_identity(binary, server_environment)

        reservations, ports = _reserve_loopback_ports()
        api_port, console_port = ports
        for reservation in reservations:
            reservation.close()

        log_handle = log_path.open("w+", encoding="utf-8")
        server_process = subprocess.Popen(
            [
                str(binary),
                "server",
                str(data_dir),
                "--address",
                f"127.0.0.1:{api_port}",
                "--console-address",
                f"127.0.0.1:{console_port}",
                "--config-dir",
                str(config_dir),
            ],
            cwd=root,
            env=server_environment,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        endpoint_url = f"http://127.0.0.1:{api_port}"
        _wait_for_minio(
            server_process,
            f"{endpoint_url}/minio/health/ready",
            log_path,
            log_handle,
            sensitive_values,
        )
        _prove_minio_identity_and_bucket(endpoint_url, access_key, secret_key)

        test_environment = _build_pytest_environment(
            inherited_environment,
            temporary_root=pytest_tmp_dir,
            endpoint_url=endpoint_url,
            access_key=access_key,
            secret_key=secret_key,
            valkey_executable=valkey_executable,
        )
        pytest_receipt = _run_pytest(
            test_environment,
            receipt_path,
            sensitive_values,
        )
    except BaseException as error:
        failure = error
    finally:
        if server_process is not None:
            try:
                _terminate_process_group(server_process, label="pinned MinIO")
            except Exception as error:  # noqa: BLE001 - cleanup must be reported
                cleanup_failures.append(str(error))
        if log_handle is not None:
            try:
                log_handle.close()
            except OSError as error:
                cleanup_failures.append(f"cannot close MinIO log: {error}")
        for port in ports:
            try:
                _require_closed_port(port)
            except Exception as error:  # noqa: BLE001 - cleanup must be reported
                cleanup_failures.append(str(error))
        try:
            temporary.cleanup()
        except OSError as error:
            cleanup_failures.append(f"cannot remove temporary MinIO state: {error}")
        if root.exists():
            cleanup_failures.append(f"temporary MinIO state remains at {root}")

    if cleanup_failures:
        detail = "; ".join(_redact(item, sensitive_values) for item in cleanup_failures)
        if failure is not None:
            raise LocalOracleError(
                f"oracle failed and cleanup also failed: {detail}"
            ) from failure
        raise LocalOracleError(f"local MinIO cleanup failed: {detail}")
    if failure is not None:
        safe_failure = _redact(str(failure), sensitive_values)
        raise LocalOracleError(f"{type(failure).__name__}: {safe_failure}") from failure
    if pytest_receipt != "4 passed, 0 skipped":
        raise LocalOracleError("local-oracle pytest receipt was not verified")
    print(
        f"verified: pytest receipt={pytest_receipt}; "
        f"Valkey {valkey_version} executable={valkey_executable}; "
        f"MinIO {MINIO_RELEASE} sha256={MINIO_SHA256}; "
        "exact board, real Valkey, and production artifact adapter passed; "
        "processes and temporary state removed"
    )


def main() -> int:
    try:
        run_oracles()
    except KeyboardInterrupt:
        print("local layout oracle interrupted after cleanup", file=sys.stderr)
        return 130
    except Exception as error:  # noqa: BLE001 - CLI boundary reports all failures
        print(f"local layout oracle failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
