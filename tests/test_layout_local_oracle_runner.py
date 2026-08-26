"""Cheap integrity tests for the real-process local-oracle runner."""

from __future__ import annotations

import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

RUNNER_PATH = (
    Path(__file__).resolve().parents[1] / "scripts/run-layout-local-oracles.py"
)
RUNNER_SPEC = importlib.util.spec_from_file_location(
    "layout_local_oracle_runner",
    RUNNER_PATH,
)
assert RUNNER_SPEC is not None and RUNNER_SPEC.loader is not None
runner = importlib.util.module_from_spec(RUNNER_SPEC)
RUNNER_SPEC.loader.exec_module(runner)


def _write_receipt(
    path: Path,
    *,
    tests: int,
    skipped: int,
    names: tuple[str, ...],
    failures: int = 0,
    errors: int = 0,
    case_statuses: tuple[str | None, ...] = (),
) -> None:
    root = ET.Element("testsuites")
    suite = ET.SubElement(
        root,
        "testsuite",
        tests=str(tests),
        failures=str(failures),
        errors=str(errors),
        skipped=str(skipped),
    )
    for index, name in enumerate(names):
        case = ET.SubElement(
            suite,
            "testcase",
            classname="tests.test_layout_local_oracles",
            name=name,
        )
        if index < skipped:
            ET.SubElement(case, "skipped")
        if index < len(case_statuses) and case_statuses[index] is not None:
            ET.SubElement(case, case_statuses[index])
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def test_pytest_child_discards_inherited_controls_credentials_and_proxies(tmp_path):
    inherited = {
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "PYTEST_ADDOPTS": "--collect-only",
        "PYTEST_PLUGINS": "caller_plugin",
        "PYTEST_CURRENT_TEST": "caller::test",
        "AWS_SECRET_ACCESS_KEY": "caller-secret",
        "DATABASE_URL": "postgresql://hosted.invalid/database",
        "THEOREM_LAYOUT_LIVE_VALKEY_URL": "redis://hosted.invalid/0",
        "MINIO_BROWSER_REDIRECT_URL": "https://hosted.invalid",
        "HTTP_PROXY": "http://proxy.invalid",
        "HTTPS_PROXY": "http://proxy.invalid",
    }

    environment = runner._build_pytest_environment(
        inherited,
        temporary_root=tmp_path,
        endpoint_url="http://127.0.0.1:49152",
        access_key="ephemeral-access",
        secret_key="ephemeral-secret",
        valkey_executable=Path("/opt/homebrew/bin/valkey-server"),
    )
    command = runner._pytest_command(tmp_path / "receipt.xml")

    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert environment["NO_PROXY"] == "127.0.0.1,localhost,::1"
    assert environment["no_proxy"] == "127.0.0.1,localhost,::1"
    assert environment["THEOREM_TEST_VALKEY_SERVER"] == (
        "/opt/homebrew/bin/valkey-server"
    )
    for excluded in (
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "PYTEST_CURRENT_TEST",
        "AWS_SECRET_ACCESS_KEY",
        "THEOREM_LAYOUT_LIVE_VALKEY_URL",
        "MINIO_BROWSER_REDIRECT_URL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
    ):
        assert excluded not in environment
    assert "--collect-only" not in command
    assert command.count("addopts=") == 1
    assert tuple(command[-4:]) == runner.REQUIRED_TEST_NODE_IDS


def test_minio_child_uses_a_separate_environment_allowlist(tmp_path):
    inherited = {
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "AWS_ACCESS_KEY_ID": "caller-access",
        "GITHUB_TOKEN": "caller-token",
        "MINIO_BROWSER_REDIRECT_URL": "https://hosted.invalid",
        "MINIO_CONFIG_ENV_FILE": "/caller/config.env",
        "PYTEST_ADDOPTS": "--collect-only",
        "ALL_PROXY": "http://proxy.invalid",
    }

    environment = runner._build_minio_environment(
        inherited,
        temporary_root=tmp_path,
        access_key="ephemeral-access",
        secret_key="ephemeral-secret",
    )

    assert environment["MINIO_ROOT_USER"] == "ephemeral-access"
    assert environment["MINIO_ROOT_PASSWORD"] == "ephemeral-secret"
    assert environment["TMPDIR"] == str(tmp_path)
    assert environment["NO_PROXY"] == "127.0.0.1,localhost,::1"
    for excluded in (
        "AWS_ACCESS_KEY_ID",
        "GITHUB_TOKEN",
        "MINIO_BROWSER_REDIRECT_URL",
        "MINIO_CONFIG_ENV_FILE",
        "PYTEST_ADDOPTS",
        "ALL_PROXY",
    ):
        assert excluded not in environment


def test_valkey_preflight_refuses_a_missing_executable(tmp_path):
    with pytest.raises(runner.LocalOracleError, match="real valkey-server"):
        runner._resolve_valkey_executable({"PATH": str(tmp_path)})


def test_missing_valkey_fails_before_minio_download(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(runner.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(runner.os, "environ", {"PATH": str(tmp_path)})
    monkeypatch.setattr(
        runner,
        "_download_and_verify",
        lambda _destination: pytest.fail("MinIO download started before Valkey proof"),
    )

    with pytest.raises(runner.LocalOracleError, match="real valkey-server"):
        runner.run_oracles()


@pytest.mark.parametrize(
    ("tests", "skipped", "expected_message"),
    (
        (3, 0, "exactly 4 tests"),
        (4, 1, "zero skipped tests"),
    ),
)
def test_pytest_receipt_refuses_fewer_or_skipped_tests(
    tmp_path,
    tests,
    skipped,
    expected_message,
):
    receipt = tmp_path / "receipt.xml"
    names = tuple(
        node_id.rsplit("::", 1)[1] for node_id in runner.REQUIRED_TEST_NODE_IDS
    )
    _write_receipt(receipt, tests=tests, skipped=skipped, names=names[:tests])

    with pytest.raises(runner.LocalOracleError, match=expected_message):
        runner._require_pytest_receipt(receipt)


@pytest.mark.parametrize("contents", (None, "not XML", "<testsuites />"))
def test_pytest_receipt_refuses_missing_malformed_or_ambiguous_receipts(
    tmp_path,
    contents,
):
    receipt = tmp_path / "receipt.xml"
    if contents is not None:
        receipt.write_text(contents, encoding="utf-8")

    with pytest.raises(runner.LocalOracleError):
        runner._require_pytest_receipt(receipt)


def test_pytest_receipt_refuses_two_direct_test_suites(tmp_path):
    receipt = tmp_path / "receipt.xml"
    root = ET.Element("testsuites")
    for _ in range(2):
        ET.SubElement(
            root,
            "testsuite",
            tests="4",
            failures="0",
            errors="0",
            skipped="0",
        )
    ET.ElementTree(root).write(receipt, encoding="utf-8", xml_declaration=True)

    with pytest.raises(
        runner.LocalOracleError,
        match="exactly one test suite",
    ):
        runner._require_pytest_receipt(receipt)


def test_pytest_receipt_refuses_duplicate_and_missing_required_identity(tmp_path):
    receipt = tmp_path / "receipt.xml"
    names = [node_id.rsplit("::", 1)[1] for node_id in runner.REQUIRED_TEST_NODE_IDS]
    names[-1] = names[0]
    _write_receipt(receipt, tests=4, skipped=0, names=tuple(names))

    with pytest.raises(
        runner.LocalOracleError,
        match="does not identify the four required tests",
    ):
        runner._require_pytest_receipt(receipt)


def test_pytest_receipt_refuses_a_wrong_test_identity(tmp_path):
    receipt = tmp_path / "receipt.xml"
    names = [node_id.rsplit("::", 1)[1] for node_id in runner.REQUIRED_TEST_NODE_IDS]
    names[-1] = "test_not_a_required_local_oracle"
    _write_receipt(receipt, tests=4, skipped=0, names=tuple(names))

    with pytest.raises(
        runner.LocalOracleError,
        match="does not identify the four required tests",
    ):
        runner._require_pytest_receipt(receipt)


@pytest.mark.parametrize(
    ("failures", "errors", "expected_message"),
    (
        (1, 0, "failures=1 errors=0"),
        (0, 1, "failures=0 errors=1"),
    ),
)
def test_pytest_receipt_refuses_nonzero_failure_or_error_counters(
    tmp_path,
    failures,
    errors,
    expected_message,
):
    receipt = tmp_path / "receipt.xml"
    names = tuple(
        node_id.rsplit("::", 1)[1] for node_id in runner.REQUIRED_TEST_NODE_IDS
    )
    _write_receipt(
        receipt,
        tests=4,
        skipped=0,
        names=names,
        failures=failures,
        errors=errors,
    )

    with pytest.raises(runner.LocalOracleError, match=expected_message):
        runner._require_pytest_receipt(receipt)


@pytest.mark.parametrize("case_status", ("failure", "error"))
def test_pytest_receipt_refuses_case_level_nonpass_elements(tmp_path, case_status):
    receipt = tmp_path / "receipt.xml"
    names = tuple(
        node_id.rsplit("::", 1)[1] for node_id in runner.REQUIRED_TEST_NODE_IDS
    )
    _write_receipt(
        receipt,
        tests=4,
        skipped=0,
        names=names,
        case_statuses=(case_status,),
    )

    with pytest.raises(
        runner.LocalOracleError,
        match="contains non-pass test status",
    ):
        runner._require_pytest_receipt(receipt)


def test_pytest_receipt_accepts_only_the_four_required_passes(tmp_path):
    receipt = tmp_path / "receipt.xml"
    names = tuple(
        node_id.rsplit("::", 1)[1] for node_id in runner.REQUIRED_TEST_NODE_IDS
    )
    _write_receipt(receipt, tests=4, skipped=0, names=names)

    assert runner._require_pytest_receipt(receipt) == "4 passed, 0 skipped"
