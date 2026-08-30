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
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import BinaryIO
from typing import Mapping
from uuid import uuid4

import boto3
from botocore.config import Config

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from apps.rendering.oracle_process import BoundedProcessError  # noqa: E402
from apps.rendering.oracle_process import run_bounded_process  # noqa: E402

MINIO_RELEASE = "RELEASE.2025-09-07T16-13-09Z"
MINIO_URL = (
    f"https://dl.min.io/server/minio/release/darwin-arm64/archive/minio.{MINIO_RELEASE}"
)
MINIO_SHA256 = "7c3b3039b76e55a1b80935848ed83998d5e8d317374f87851f46a019ff5c0aa4"
# The reviewed immutable release is comfortably below this hard ceiling. Keep
# the ceiling independent of server metadata so a missing or lying length
# cannot turn the replay helper into an unbounded disk sink.
MINIO_DOWNLOAD_MAX_BYTES = 256 * 1024 * 1024
DOWNLOAD_READ_BYTES = 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 30.0
VERSION_TIMEOUT_SECONDS = 5.0
VERSION_OUTPUT_MAX_BYTES = 16 * 1024
READINESS_TIMEOUT_SECONDS = 15.0
MINIO_LOG_MAX_BYTES = 1024 * 1024
MINIO_LOG_TAIL_BYTES = 4_000
MINIO_LOG_READ_BYTES = 64 * 1024
PYTEST_TIMEOUT_SECONDS = 180.0
PYTEST_OUTPUT_MAX_BYTES = 256 * 1024
PYTEST_DIAGNOSTIC_MAX_BYTES = 8 * 1024
PYTEST_RECEIPT_MAX_BYTES = 256 * 1024
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


class _BoundedProcessLog:
    """Drain a long-running child while retaining only a bounded rolling tail."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        stream: BinaryIO,
        *,
        max_bytes: int = MINIO_LOG_MAX_BYTES,
        tail_bytes: int = MINIO_LOG_TAIL_BYTES,
    ) -> None:
        if max_bytes <= 0 or tail_bytes <= 0:
            raise ValueError("process log limits must be positive")
        self.process = process
        self.stream = stream
        self.max_bytes = max_bytes
        self.tail_bytes = tail_bytes
        self.total_bytes = 0
        self.output_limit_exceeded = False
        self.read_error: OSError | None = None
        self._tail = bytearray()
        self._thread = threading.Thread(
            target=self._drain,
            name="theorem-minio-log-drain",
            daemon=True,
        )
        self._thread.start()

    def _drain(self) -> None:
        try:
            while chunk := os.read(self.stream.fileno(), MINIO_LOG_READ_BYTES):
                self.total_bytes += len(chunk)
                self._tail.extend(chunk)
                retained_bytes = self.tail_bytes + 4_096
                if len(self._tail) > retained_bytes:
                    del self._tail[:-retained_bytes]
                if self.total_bytes > self.max_bytes and not self.output_limit_exceeded:
                    self.output_limit_exceeded = True
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        except OSError as error:
            self.read_error = error

    def close(self) -> None:
        self._thread.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        if self._thread.is_alive():
            raise LocalOracleError("MinIO log drainer did not stop after process exit")
        self.stream.close()
        if self.read_error is not None:
            raise LocalOracleError(f"MinIO log drain failed: {self.read_error}")

    def tail(self, sensitive_values: tuple[str, ...]) -> str:
        value = bytes(self._tail).decode("utf-8", errors="replace")
        redacted = _redact(value, sensitive_values)
        encoded = redacted.encode("utf-8")
        if len(encoded) > self.tail_bytes:
            redacted = encoded[-self.tail_bytes :].decode("utf-8", errors="ignore")
        return redacted


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

    try:
        result = run_bounded_process(
            [str(executable), "--version"],
            environment=_minimal_system_environment(inherited),
            timeout_seconds=VERSION_TIMEOUT_SECONDS,
            output_max_bytes=VERSION_OUTPUT_MAX_BYTES,
            check=False,
        )
    except BoundedProcessError as error:
        raise LocalOracleError(
            f"Valkey bounded version probe failed: {error}"
        ) from error
    first_line = result.output.splitlines()
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
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT_SECONDS
    created_destination = False
    try:
        with opener.open(
            request,
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        ) as response:
            declared_length_value = response.headers.get("Content-Length")
            declared_length: int | None = None
            if declared_length_value is not None:
                try:
                    declared_length = int(declared_length_value)
                except (TypeError, ValueError) as error:
                    raise LocalOracleError(
                        "pinned MinIO response has an invalid declared length"
                    ) from error
                if declared_length < 0:
                    raise LocalOracleError(
                        "pinned MinIO response has an invalid declared length"
                    )
                if declared_length > MINIO_DOWNLOAD_MAX_BYTES:
                    raise LocalOracleError(
                        "pinned MinIO declared length exceeds the download size cap"
                    )

            total_bytes = 0
            with destination.open("xb") as output:
                created_destination = True
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LocalOracleError(
                            "pinned MinIO download exceeded its whole-operation deadline"
                        )
                    response_socket = getattr(
                        getattr(getattr(response, "fp", None), "raw", None),
                        "_sock",
                        None,
                    )
                    if response_socket is not None:
                        response_socket.settimeout(remaining)
                    chunk = response.read(DOWNLOAD_READ_BYTES)
                    if time.monotonic() >= deadline:
                        raise LocalOracleError(
                            "pinned MinIO download exceeded its whole-operation deadline"
                        )
                    if not chunk:
                        break
                    next_total = total_bytes + len(chunk)
                    if next_total > MINIO_DOWNLOAD_MAX_BYTES:
                        raise LocalOracleError(
                            "pinned MinIO download exceeded its size cap"
                        )
                    digest.update(chunk)
                    output.write(chunk)
                    total_bytes = next_total

                if declared_length is not None and total_bytes != declared_length:
                    raise LocalOracleError(
                        "pinned MinIO response body does not match its declared length"
                    )
                actual = digest.hexdigest()
                if actual != MINIO_SHA256:
                    raise LocalOracleError(
                        f"pinned MinIO checksum mismatch: {actual} != {MINIO_SHA256}"
                    )
    except BaseException as error:
        if created_destination:
            try:
                destination.unlink(missing_ok=True)
            except OSError as cleanup_error:
                raise LocalOracleError(
                    "pinned MinIO download failed and its partial file could not be "
                    f"removed: {cleanup_error}"
                ) from error
        raise


def _verify_binary_identity(binary: Path, environment: Mapping[str, str]) -> None:
    try:
        result = run_bounded_process(
            [str(binary), "--version"],
            environment=environment,
            timeout_seconds=VERSION_TIMEOUT_SECONDS,
            output_max_bytes=VERSION_OUTPUT_MAX_BYTES,
            check=False,
        )
    except BoundedProcessError as error:
        raise LocalOracleError(
            f"MinIO bounded version probe failed: {error}"
        ) from error
    expected = f"version {MINIO_RELEASE}"
    first_line = result.output.splitlines()[0] if result.output else ""
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


def _wait_for_minio(
    process: subprocess.Popen[bytes],
    health_url: str,
    process_log: _BoundedProcessLog,
    sensitive_values: tuple[str, ...],
) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process_log.output_limit_exceeded:
            raise LocalOracleError(
                "pinned MinIO exceeded its startup output cap:\n"
                + process_log.tail(sensitive_values)
            )
        if process.poll() is not None:
            raise LocalOracleError(
                "pinned MinIO exited before readiness:\n"
                + process_log.tail(sensitive_values)
            )
        try:
            with opener.open(health_url, timeout=1.0) as response:
                if response.status == 200:
                    return
        except (TimeoutError, urllib.error.URLError):
            time.sleep(0.05)
    raise LocalOracleError(
        "pinned MinIO did not become ready before its deadline:\n"
        + process_log.tail(sensitive_values)
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
        with receipt_path.open("rb") as receipt:
            document = receipt.read(PYTEST_RECEIPT_MAX_BYTES + 1)
        if len(document) > PYTEST_RECEIPT_MAX_BYTES:
            raise LocalOracleError("local-oracle pytest receipt size cap exceeded")
        root = ET.fromstring(document)
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
    try:
        result = run_bounded_process(
            _pytest_command(receipt_path),
            environment=environment,
            timeout_seconds=PYTEST_TIMEOUT_SECONDS,
            output_max_bytes=PYTEST_OUTPUT_MAX_BYTES,
            cwd=REPOSITORY_ROOT,
            sensitive_values=sensitive_values,
            check=False,
        )
    except BoundedProcessError as error:
        detail = _redact(str(error), sensitive_values)
        encoded_detail = detail.encode("utf-8")
        if len(encoded_detail) > PYTEST_DIAGNOSTIC_MAX_BYTES:
            detail = encoded_detail[-PYTEST_DIAGNOSTIC_MAX_BYTES:].decode(
                "utf-8", errors="ignore"
            )
        raise LocalOracleError(
            f"local-oracle pytest violated process bounds:\n{detail}"
        ) from error
    if result.output:
        print(result.output)
    if result.returncode != 0:
        raise LocalOracleError(
            f"local-oracle pytest failed with exit {result.returncode}"
        )
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
    receipt_path = root / "pytest-receipt.xml"
    server_process: subprocess.Popen[bytes] | None = None
    process_log: _BoundedProcessLog | None = None
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
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        assert server_process.stdout is not None
        process_log = _BoundedProcessLog(server_process, server_process.stdout)
        endpoint_url = f"http://127.0.0.1:{api_port}"
        _wait_for_minio(
            server_process,
            f"{endpoint_url}/minio/health/ready",
            process_log,
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
        if process_log is not None:
            try:
                process_log.close()
            except Exception as error:  # noqa: BLE001 - cleanup must be reported
                cleanup_failures.append(str(error))
            if process_log.output_limit_exceeded and failure is None:
                failure = LocalOracleError(
                    "pinned MinIO exceeded its output cap:\n"
                    + process_log.tail(sensitive_values)
                )
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
