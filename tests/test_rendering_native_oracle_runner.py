"""Integrity tests for the real native rendering replay helper."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

RUNNER_PATH = (
    Path(__file__).resolve().parents[1] / "scripts/run-rendering-native-oracles.py"
)
RUNNER_SPEC = importlib.util.spec_from_file_location(
    "rendering_native_oracle_runner", RUNNER_PATH
)
assert RUNNER_SPEC is not None and RUNNER_SPEC.loader is not None
runner = importlib.util.module_from_spec(RUNNER_SPEC)
sys.modules[RUNNER_SPEC.name] = runner
RUNNER_SPEC.loader.exec_module(runner)


def test_native_runner_pins_reviewed_renderer_sources():
    assert runner.PLANTUML_VERSION == "1.2026.6"
    assert runner.PLANTUML_SHA256 == (
        "89948f14c93756c7a3fb7b69078ff37e8489fd79dd430c582b931e2f65358690"
    )
    assert runner.GRAPHVIZ_NATIVE_VERSION == "14.1.5"
    assert runner.GRAPHVIZ_NATIVE_SHA256 == (
        "b017378835f7ca12f1a3f1db5c338d7e7af16b284b7007ad73ccec960c1b45b3"
    )


def test_verified_download_refuses_oversize_or_wrong_digest(monkeypatch, tmp_path):
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload
            self.offset = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, amount):
            chunk = self.payload[self.offset : self.offset + amount]
            self.offset += len(chunk)
            return chunk

    class FakeOpener:
        def __init__(self, payload):
            self.payload = payload

        def open(self, *_args, **_kwargs):
            return FakeResponse(self.payload)

    destination = tmp_path / "payload"
    monkeypatch.setattr(
        runner.urllib.request,
        "build_opener",
        lambda *_args: FakeOpener(b"oversize"),
    )
    with pytest.raises(runner.NativeOracleError, match="size cap"):
        runner._download_verified(
            "https://official.invalid/artifact",
            destination,
            sha256_hex="0" * 64,
            max_bytes=4,
        )
    assert not destination.exists()

    monkeypatch.setattr(
        runner.urllib.request,
        "build_opener",
        lambda *_args: FakeOpener(b"wrong"),
    )
    with pytest.raises(runner.NativeOracleError, match="checksum"):
        runner._download_verified(
            "https://official.invalid/artifact",
            destination,
            sha256_hex="0" * 64,
            max_bytes=64,
        )
    assert not destination.exists()


def test_native_receipt_requires_content_addressed_svg_and_png():
    receipt = runner.NativeReceipt(
        java_identity="OpenJDK fixture",
        plantuml_version="1.2026.6",
        plantuml_sha256=runner.PLANTUML_SHA256,
        plantuml_svg_digest="sha256:" + "a" * 64,
        plantuml_include_refusal="cannot include /etc/passwd",
        diagrams_version="0.25.1",
        graphviz_version="14.1.5",
        diagrams_svg_digest="sha256:" + "b" * 64,
        diagrams_png_digest="sha256:" + "c" * 64,
        diagrams_svg_key="tenants/native-oracle/renders/" + "b" * 64 + ".svg",
        diagrams_png_key="tenants/native-oracle/renders/" + "c" * 64 + ".png",
        diagrams_import_refusal="import 'os' is outside the diagrams package",
    )

    runner._validate_receipt(receipt)

    with pytest.raises(runner.NativeOracleError, match="Diagrams version"):
        runner._validate_receipt(
            runner.dataclasses.replace(receipt, diagrams_version="0.25.0")
        )
