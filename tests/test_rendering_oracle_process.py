"""Bounded process capture for native and declared-image oracle helpers."""

from __future__ import annotations

import sys
import time

import pytest

from apps.rendering.oracle_process import COMMAND_CONTEXT_BYTES
from apps.rendering.oracle_process import DIAGNOSTIC_TAIL_BYTES
from apps.rendering.oracle_process import ProcessOutputLimitExceeded
from apps.rendering.oracle_process import ProcessExitedNonzero
from apps.rendering.oracle_process import ProcessTimedOut
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


@pytest.mark.parametrize(
    "secret", ("TOPSECRET-ABCDEFGHIJ", "秘密令牌-ABCDEFGHIJ")
)
@pytest.mark.parametrize("termination", ("overflow", "timeout", "nonzero"))
def test_multicharacter_secret_is_redacted_across_raw_tail_boundary(
    tmp_path, termination, secret
):
    suffix = secret[-10:]
    environment = {
        "PATH": "/usr/bin:/bin",
        "ORACLE_BOUNDARY_SECRET": secret,
        "ORACLE_TERMINATION": termination,
    }
    command = [
        sys.executable,
        "-c",
        (
            "import os,sys,time; "
            "secret=os.environ['ORACLE_BOUNDARY_SECRET']; "
            "encoded=secret.encode('utf-8'); "
            "prefix=b'P' * (8192 - len(encoded) - 3990); "
            "sys.stderr.buffer.write(prefix + encoded + b'S' * 3990); "
            "sys.stderr.flush(); "
            "mode=os.environ['ORACLE_TERMINATION']; "
            "time.sleep(5) if mode == 'timeout' else None; "
            "raise SystemExit(9 if mode == 'nonzero' else 0)"
        ),
    ]
    expected_error = {
        "overflow": ProcessOutputLimitExceeded,
        "timeout": ProcessTimedOut,
        "nonzero": ProcessExitedNonzero,
    }[termination]

    with pytest.raises(expected_error) as raised:
        run_bounded_process(
            command,
            environment=environment,
            timeout_seconds=0.25 if termination == "timeout" else 10.0,
            output_max_bytes=128 if termination == "overflow" else 16_384,
            cwd=tmp_path,
            sensitive_values=(secret,),
        )

    message = str(raised.value)
    diagnostic_tail = message.partition("diagnostic:\n")[2]
    assert secret not in diagnostic_tail
    assert suffix not in diagnostic_tail
    assert "<redacted>" in diagnostic_tail
    assert len(diagnostic_tail.encode()) <= DIAGNOSTIC_TAIL_BYTES


def test_oversize_sensitive_value_is_refused_before_process_start(tmp_path):
    with pytest.raises(ValueError, match="4096-byte redaction cap"):
        run_bounded_process(
            [sys.executable, "-c", "raise SystemExit(0)"],
            environment={"PATH": "/usr/bin:/bin"},
            timeout_seconds=10.0,
            output_max_bytes=16_384,
            cwd=tmp_path,
            sensitive_values=("s" * 4_097,),
        )
