#!/usr/bin/env python3
"""Run exact board, real Valkey, and pinned disposable MinIO local oracles."""

from __future__ import annotations

import hashlib
import os
import platform
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
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


class LocalOracleError(RuntimeError):
    """A required real-process oracle or its cleanup failed."""


def _redact(value: str, sensitive_values: tuple[str, ...]) -> str:
    redacted = value
    for sensitive in sensitive_values:
        if sensitive:
            redacted = redacted.replace(sensitive, "<redacted>")
    return redacted


def _download_and_verify(destination: Path) -> None:
    request = urllib.request.Request(
        MINIO_URL,
        headers={"User-Agent": "theorem-layout-local-oracle/1.0"},
    )
    digest = hashlib.sha256()
    with (
        urllib.request.urlopen(
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


def _verify_binary_identity(binary: Path) -> None:
    result = subprocess.run(
        [str(binary), "--version"],
        capture_output=True,
        text=True,
        timeout=VERSION_TIMEOUT_SECONDS,
        check=False,
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
    deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log_handle.flush()
            raise LocalOracleError(
                "pinned MinIO exited before readiness:\n"
                + _safe_log_excerpt(log_path, sensitive_values)
            )
        try:
            with urllib.request.urlopen(health_url, timeout=1.0) as response:
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


def _run_pytest(environment: dict[str, str]) -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_layout_local_oracles.py",
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        start_new_session=True,
    )
    try:
        return_code = process.wait(timeout=PYTEST_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        _terminate_process_group(process, label="local-oracle pytest")
        raise LocalOracleError("local-oracle pytest exceeded 180 seconds") from error
    if return_code != 0:
        raise LocalOracleError(f"local-oracle pytest failed with exit {return_code}")


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

    temporary = tempfile.TemporaryDirectory(prefix="theorem-layout-minio-")
    root = Path(temporary.name)
    binary = root / f"minio.{MINIO_RELEASE}"
    data_dir = root / "data"
    config_dir = root / "config"
    log_path = root / "minio.log"
    server_process: subprocess.Popen[str] | None = None
    log_handle: TextIO | None = None
    ports: tuple[int, int] = ()
    failure: BaseException | None = None
    cleanup_failures: list[str] = []

    access_key = f"theorem{secrets.token_hex(8)}"
    secret_key = secrets.token_urlsafe(32)
    sensitive_values = (access_key, secret_key)
    try:
        _download_and_verify(binary)
        binary.chmod(0o700)
        _verify_binary_identity(binary)
        data_dir.mkdir()
        config_dir.mkdir()

        reservations, ports = _reserve_loopback_ports()
        api_port, console_port = ports
        for reservation in reservations:
            reservation.close()

        server_environment = os.environ.copy()
        server_environment.update(
            MINIO_ROOT_USER=access_key,
            MINIO_ROOT_PASSWORD=secret_key,
            MINIO_BROWSER="off",
            MINIO_UPDATE="off",
        )
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

        test_environment = os.environ.copy()
        test_environment.update(
            THEOREM_LAYOUT_LOCAL_S3_ENDPOINT_URL=endpoint_url,
            THEOREM_LAYOUT_LOCAL_S3_ACCESS_KEY_ID=access_key,
            THEOREM_LAYOUT_LOCAL_S3_SECRET_ACCESS_KEY=secret_key,
            THEOREM_LAYOUT_LOCAL_S3_REGION="us-east-1",
            THEOREM_LAYOUT_LOCAL_S3_BUCKET_PREFIX="theorem-layout-oracle",
        )
        _run_pytest(test_environment)
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
    print(
        f"verified: MinIO {MINIO_RELEASE} sha256={MINIO_SHA256}; "
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
