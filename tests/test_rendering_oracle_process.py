"""Bounded process capture for native and declared-image oracle helpers."""

from __future__ import annotations

import sys
import time

import pytest

from apps.rendering.oracle_process import COMMAND_CONTEXT_BYTES
from apps.rendering.oracle_process import DIAGNOSTIC_TAIL_BYTES
from apps.rendering.oracle_process import ProcessOutputLimitExceeded
from apps.rendering.oracle_process import ProcessExitedNonzero
from apps.rendering.oracle_process import run_bounded_process


def test_noisy_child_is_killed_when_streaming_output_crosses_the_cap(tmp_path):
    started = time.monotonic()
    with pytest.raises(ProcessOutputLimitExceeded, match="output cap"):
        run_bounded_process(
            [
                sys.executable,
                "-c",
                (
                    "import sys,time; "
                    "sys.stdout.write('x' * 65536); sys.stdout.flush(); "
                    "time.sleep(5)"
                ),
            ],
            environment={"PATH": "/usr/bin:/bin"},
            timeout_seconds=10.0,
            output_max_bytes=1024,
            cwd=tmp_path,
        )
    assert time.monotonic() - started < 3.0


def test_overflow_diagnostic_tail_is_bounded_and_redacted(tmp_path):
    secret = "!"
    with pytest.raises(ProcessOutputLimitExceeded) as raised:
        run_bounded_process(
            [
                sys.executable,
                "-c",
                (
                    "import sys,time; "
                    "sys.stderr.write(chr(33) * 8192); sys.stderr.flush(); "
                    "time.sleep(5)"
                ),
            ],
            environment={"PATH": "/usr/bin:/bin"},
            timeout_seconds=10.0,
            output_max_bytes=128,
            cwd=tmp_path,
            sensitive_values=(secret,),
        )

    diagnostic = str(raised.value)
    assert secret not in diagnostic
    assert "<redacted>" in diagnostic
    assert "diagnostic:\n" in diagnostic
    diagnostic_tail = diagnostic.partition("diagnostic:\n")[2]
    assert len(diagnostic_tail.encode()) <= DIAGNOSTIC_TAIL_BYTES


def test_nonzero_diagnostic_tail_is_byte_bounded_after_redaction(tmp_path):
    secret = "!"
    with pytest.raises(ProcessExitedNonzero) as raised:
        run_bounded_process(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write(chr(33) * 8192); raise SystemExit(9)",
            ],
            environment={"PATH": "/usr/bin:/bin"},
            timeout_seconds=10.0,
            output_max_bytes=16_384,
            cwd=tmp_path,
            sensitive_values=(secret,),
        )

    message = str(raised.value)
    assert secret not in message
    assert "<redacted>" in message
    assert "diagnostic:\n" in message
    diagnostic_tail = message.partition("diagnostic:\n")[2]
    assert len(diagnostic_tail.encode()) <= DIAGNOSTIC_TAIL_BYTES


def test_nonzero_command_context_is_redacted_and_byte_bounded(tmp_path):
    secret = "!"
    with pytest.raises(ProcessExitedNonzero) as raised:
        run_bounded_process(
            [
                sys.executable,
                "-c",
                "raise SystemExit(7)",
                secret * 8_192,
            ],
            environment={"PATH": "/usr/bin:/bin"},
            timeout_seconds=10.0,
            output_max_bytes=16_384,
            cwd=tmp_path,
            sensitive_values=(secret,),
        )

    message = str(raised.value)
    command_context = message.partition("command: ")[2].partition("\ndiagnostic:")[0]
    assert secret not in message
    assert "<redacted>" in command_context
    assert len(command_context.encode()) <= COMMAND_CONTEXT_BYTES
