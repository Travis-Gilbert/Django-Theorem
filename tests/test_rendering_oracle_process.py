"""Bounded process capture for native and declared-image oracle helpers."""

from __future__ import annotations

import sys
import time

import pytest

from apps.rendering.oracle_process import ProcessOutputLimitExceeded
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
    secret = "renderer-credential"
    with pytest.raises(ProcessOutputLimitExceeded) as raised:
        run_bounded_process(
            [
                sys.executable,
                "-c",
                (
                    "import sys,time; "
                    f"sys.stderr.write({secret!r} * 1024); sys.stderr.flush(); "
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
    assert len(diagnostic.encode()) < 8_000
