"""Integrity tests for the declared-image rendering gate."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

RUNNER_PATH = (
    Path(__file__).resolve().parents[1] / "scripts/run-rendering-container-oracles.py"
)
RUNNER_SPEC = importlib.util.spec_from_file_location(
    "rendering_container_oracle_runner", RUNNER_PATH
)
assert RUNNER_SPEC is not None and RUNNER_SPEC.loader is not None
runner = importlib.util.module_from_spec(RUNNER_SPEC)
sys.modules[RUNNER_SPEC.name] = runner
RUNNER_SPEC.loader.exec_module(runner)


def test_preflight_inventories_every_supported_runtime_candidate(monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda _name: None)

    inventory = runner.inspect_runtime_candidates()

    assert tuple(item.name for item in inventory) == (
        "docker",
        "podman",
        "colima",
        "limactl",
        "lima",
        "finch",
        "nerdctl",
        "container",
        "orbctl",
    )
    assert all(item.status == "missing" for item in inventory)
    with pytest.raises(runner.ContainerOraclePrerequisiteError, match="Docker-compatible"):
        runner.select_runtime(inventory)


def test_declared_dockerfile_contract_rejects_a_drifted_pin(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:latest\n", encoding="utf-8")

    with pytest.raises(runner.ContainerOracleError, match="Dockerfile"):
        runner.verify_declared_dockerfile(dockerfile)


def test_declared_requirements_require_exact_python_renderer_pins(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "pygraphviz==2.0.1\ndiagrams>=0.25.1\n", encoding="utf-8"
    )

    with pytest.raises(runner.ContainerOracleError, match="diagrams==0.25.1"):
        runner.verify_declared_requirements(requirements)


def test_container_probe_covers_every_declared_runtime_assertion():
    probe = runner.container_probe_script()

    for required in (
        "Graphviz 2.42.2",
        "pygraphviz==2.0.1",
        "diagrams==0.25.1",
        "OpenJDK 17.0.20",
        runner.PLANTUML_SHA256,
        "PlantUML version 1.2026.6",
        "DJANGO_SETTINGS_MODULE=theorem_control.settings",
        "manage.py check",
        "test_pinned_graphviz_cold_recompute_matches_exact_fixture_bytes",
    ):
        assert required in probe
