"""Streaming, fail-closed process capture for renderer oracle helpers."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

DIAGNOSTIC_TAIL_BYTES = 4_000
COMMAND_CONTEXT_BYTES = 1_000


class BoundedProcessError(RuntimeError):
    """A subprocess violated its exit, time, or output contract."""


class ProcessTimedOut(BoundedProcessError):
    """The process exceeded its wall-clock deadline."""


class ProcessOutputLimitExceeded(BoundedProcessError):
    """The process crossed its combined stdout/stderr byte cap."""


class ProcessExitedNonzero(BoundedProcessError):
    """The process returned a nonzero status when success was required."""


@dataclass(frozen=True)
class BoundedProcessResult:
    returncode: int
    output: str


def _redact(value: str, sensitive_values: Sequence[str]) -> str:
    redacted = value
    for sensitive in sensitive_values:
        if sensitive:
            redacted = redacted.replace(sensitive, "<redacted>")
    return redacted


def _utf8_safe_tail(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[-max_bytes:].decode("utf-8", errors="ignore")


def _utf8_safe_head(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _diagnostic_tail(
    raw: bytes | bytearray | str, sensitive_values: Sequence[str]
) -> str:
    value = (
        bytes(raw).decode("utf-8", errors="replace")
        if isinstance(raw, (bytes, bytearray))
        else raw
    )
    return _utf8_safe_tail(
        _redact(value, sensitive_values), DIAGNOSTIC_TAIL_BYTES
    )


def _command_context(
    command: Sequence[str], sensitive_values: Sequence[str]
) -> str:
    return _utf8_safe_head(
        _redact(" ".join(command), sensitive_values), COMMAND_CONTEXT_BYTES
    )


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def run_bounded_process(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    timeout_seconds: float,
    output_max_bytes: int,
    cwd: Path | None = None,
    sensitive_values: Sequence[str] = (),
    check: bool = True,
) -> BoundedProcessResult:
    """Capture output while running and kill the process group on either limit."""

    if timeout_seconds <= 0 or output_max_bytes <= 0:
        raise ValueError("process limits must be positive")
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    tail = bytearray()
    total_bytes = 0
    deadline = time.monotonic() + timeout_seconds
    try:
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)

        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process_group(process)
                diagnostic = _diagnostic_tail(tail, sensitive_values)
                raise ProcessTimedOut(
                    f"process exceeded {timeout_seconds:g}s\n"
                    f"command: {_command_context(command, sensitive_values)}\n"
                    f"diagnostic:\n{diagnostic}"
                )
            events = selector.select(timeout=min(remaining, 0.1))
            for key, _mask in events:
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), 64 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    continue
                streams[stream].extend(chunk)
                tail.extend(chunk)
                if len(tail) > DIAGNOSTIC_TAIL_BYTES:
                    del tail[:-DIAGNOSTIC_TAIL_BYTES]
                total_bytes += len(chunk)
                if total_bytes > output_max_bytes:
                    _terminate_process_group(process)
                    diagnostic = _diagnostic_tail(tail, sensitive_values)
                    raise ProcessOutputLimitExceeded(
                        f"process output cap exceeded: {output_max_bytes} bytes\n"
                        f"command: {_command_context(command, sensitive_values)}\n"
                        f"diagnostic:\n{diagnostic}"
                    )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_process_group(process)
            raise ProcessTimedOut(
                f"process exceeded {timeout_seconds:g}s\n"
                f"command: {_command_context(command, sensitive_values)}\n"
                f"diagnostic:\n{_diagnostic_tail(tail, sensitive_values)}"
            )
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_group(process)
            raise ProcessTimedOut(
                f"process exceeded {timeout_seconds:g}s\n"
                f"command: {_command_context(command, sensitive_values)}\n"
                f"diagnostic:\n{_diagnostic_tail(tail, sensitive_values)}"
            ) from exc
    except BaseException:
        if process.poll() is None:
            _terminate_process_group(process)
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()

    stdout = streams[process.stdout].decode("utf-8", errors="replace").strip()
    stderr = streams[process.stderr].decode("utf-8", errors="replace").strip()
    output = _utf8_safe_tail(
        _redact(
            "\n".join(part for part in (stdout, stderr) if part), sensitive_values
        ),
        output_max_bytes,
    )
    if check and returncode != 0:
        diagnostic = _diagnostic_tail(output, sensitive_values)
        raise ProcessExitedNonzero(
            f"process failed ({returncode})\n"
            f"command: {_command_context(command, sensitive_values)}\n"
            f"diagnostic:\n{diagnostic}"
        )
    return BoundedProcessResult(returncode=returncode, output=output)
