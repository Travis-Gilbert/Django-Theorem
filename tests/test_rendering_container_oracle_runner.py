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


def test_dockerignore_contract_rejects_missing_secret_and_state_exclusions(tmp_path):
    dockerignore = tmp_path / ".dockerignore"
    dockerignore.write_text(".git\n.env\n", encoding="utf-8")

    with pytest.raises(runner.ContainerOracleError, match=".dockerignore"):
        runner.verify_dockerignore(dockerignore)


def test_checked_in_dockerignore_is_the_canonical_security_contract():
    declared = tuple(
        line.strip()
        for line in (runner.REPOSITORY_ROOT / ".dockerignore")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )

    assert declared == runner.DOCKERIGNORE_REQUIRED_PATTERNS


def test_dockerignore_verifier_accepts_only_the_exact_reviewed_policy(tmp_path):
    dockerignore = tmp_path / ".dockerignore"
    dockerignore.write_text(
        "\n".join(runner.DOCKERIGNORE_REQUIRED_PATTERNS) + "\n",
        encoding="utf-8",
    )

    runner.verify_dockerignore(dockerignore)


@pytest.mark.parametrize(
    "weakening_rule",
    ("!private-signing.key", "!.env.production"),
)
def test_dockerignore_verifier_rejects_appended_weakening_negations(
    tmp_path, weakening_rule
):
    dockerignore = tmp_path / ".dockerignore"
    dockerignore.write_text(
        "\n".join((*runner.DOCKERIGNORE_REQUIRED_PATTERNS, weakening_rule)) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(runner.ContainerOracleError, match="exact reviewed policy"):
        runner.verify_dockerignore(dockerignore)


def test_dockerignore_verifier_rejects_duplicate_entries(tmp_path):
    dockerignore = tmp_path / ".dockerignore"
    dockerignore.write_text(
        "\n".join((*runner.DOCKERIGNORE_REQUIRED_PATTERNS, ".git")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(runner.ContainerOracleError, match="exact reviewed policy"):
        runner.verify_dockerignore(dockerignore)


def test_dockerignore_verifier_rejects_semantic_reordering(tmp_path):
    reordered = list(runner.DOCKERIGNORE_REQUIRED_PATTERNS)
    safe_example = reordered.pop(reordered.index("!.env.example"))
    reordered.insert(reordered.index(".env"), safe_example)
    dockerignore = tmp_path / ".dockerignore"
    dockerignore.write_text("\n".join(reordered) + "\n", encoding="utf-8")

    with pytest.raises(runner.ContainerOracleError, match="exact reviewed policy"):
        runner.verify_dockerignore(dockerignore)


@pytest.mark.parametrize("missing_pattern", runner.DOCKERIGNORE_REQUIRED_PATTERNS)
def test_every_dockerignore_security_pattern_is_drift_gated(
    tmp_path, missing_pattern
):
    remaining = [
        pattern
        for pattern in runner.DOCKERIGNORE_REQUIRED_PATTERNS
        if pattern != missing_pattern
    ]
    dockerignore = tmp_path / ".dockerignore"
    dockerignore.write_text("\n".join(remaining) + "\n", encoding="utf-8")

    with pytest.raises(runner.ContainerOracleError, match=".dockerignore"):
        runner.verify_dockerignore(dockerignore)


@pytest.mark.parametrize(
    "endpoint",
    ("tcp://127.0.0.1:2375", "tcp://builder.internal:2376", "ssh://builder"),
)
def test_remote_runtime_endpoints_are_refused(endpoint):
    with pytest.raises(runner.ContainerOraclePrerequisiteError, match="local Unix"):
        runner.require_local_runtime_endpoint(endpoint)


def test_local_unix_runtime_endpoint_is_admitted():
    assert runner.require_local_runtime_endpoint(
        "unix:///Users/example/.docker/run/docker.sock"
    ) == "/Users/example/.docker/run/docker.sock"


def test_runtime_commands_remain_bound_to_the_proven_local_endpoint(tmp_path):
    endpoint = "unix:///Users/example/.docker/run/docker.sock"
    runtime = runner.RuntimeCandidate(
        "docker", "/usr/local/bin/docker", "usable", "local", endpoint
    )

    command = runner._build_command(runtime, "oracle:tag", tmp_path / "image-id")

    assert command[:3] == ["/usr/local/bin/docker", "--host", endpoint]
    assert "--iidfile" in command


@pytest.mark.parametrize(
    ("output", "expected"),
    (
        ("dot - graphviz version 2.42.2 (0)", "2.42.2"),
        ('openjdk version "17.0.20" 2026-01-20', "17.0.20"),
    ),
)
def test_exact_runtime_version_parsers(output, expected):
    parser = (
        runner.parse_graphviz_version
        if output.startswith("dot")
        else runner.parse_java_version
    )
    assert parser(output) == expected


def test_near_match_runtime_versions_are_not_exact():
    assert runner.parse_graphviz_version(
        "dot - graphviz version 2.42.20 (0)"
    ) != "2.42.2"
    assert runner.parse_java_version(
        'openjdk version "17.0.200" 2026-01-20'
    ) != "17.0.20"


def test_per_invocation_tags_use_a_random_nonce(monkeypatch):
    nonces = iter(("a" * 32, "b" * 32))
    monkeypatch.setattr(runner.secrets, "token_hex", lambda _bytes: next(nonces))

    first = runner.new_image_tag("contextdigest")
    second = runner.new_image_tag("contextdigest")

    assert first != second
    assert first.endswith("-" + "a" * 32)
    assert second.endswith("-" + "b" * 32)


def test_preexisting_tag_is_refused_before_build(monkeypatch):
    runtime = runner.RuntimeCandidate(
        "docker", "/usr/local/bin/docker", "usable", "local", "unix:///tmp/docker.sock"
    )
    monkeypatch.setattr(
        runner,
        "inspect_image_id",
        lambda _runtime, _tag: "sha256:" + "a" * 64,
    )

    with pytest.raises(runner.ContainerOracleError, match="pre-existing"):
        runner.reserve_image_tag(runtime, "theorem-rendering-oracle:fixture")


def test_concurrent_retag_is_not_deleted(monkeypatch):
    runtime = runner.RuntimeCandidate(
        "docker", "/usr/local/bin/docker", "usable", "local", "unix:///tmp/docker.sock"
    )
    monkeypatch.setattr(
        runner,
        "inspect_image_id",
        lambda _runtime, _tag: "sha256:" + "b" * 64,
    )
    monkeypatch.setattr(
        runner,
        "_run_bounded",
        lambda *_args, **_kwargs: pytest.fail("concurrently retagged image was deleted"),
    )

    with pytest.raises(runner.ContainerOracleError, match="ownership changed"):
        runner.cleanup_created_image(
            runtime,
            "theorem-rendering-oracle:fixture",
            "sha256:" + "a" * 64,
        )


def test_cleanup_targets_immutable_id_when_tag_changes_after_inspection(monkeypatch):
    runtime = runner.RuntimeCandidate(
        "docker", "/usr/local/bin/docker", "usable", "local", "unix:///tmp/docker.sock"
    )
    created_image_id = "sha256:" + "a" * 64
    replacement_image_id = "sha256:" + "b" * 64
    current_tag_owner = [created_image_id]
    removal_commands = []

    monkeypatch.setattr(
        runner,
        "inspect_image_id",
        lambda _runtime, _tag: current_tag_owner[0],
    )

    def run(command, **_kwargs):
        removal_commands.append(command)
        current_tag_owner[0] = replacement_image_id
        return 0, "removed helper image"

    monkeypatch.setattr(runner, "_run_bounded", run)

    with pytest.raises(runner.ContainerOracleError, match="ownership changed"):
        runner.cleanup_created_image(
            runtime,
            "theorem-rendering-oracle:fixture",
            created_image_id,
        )

    assert removal_commands[0][-1] == created_image_id


def test_cleanup_failure_preserves_the_primary_failure():
    primary = runner.ContainerOracleError("probe failed")
    cleanup = runner.ContainerOracleError("cleanup failed")

    with pytest.raises(runner.ContainerOracleError, match="probe failed.*cleanup failed"):
        runner.raise_after_cleanup(primary, cleanup)


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

    assert "version == '2.42.2'" in probe
    assert "version == '17.0.20'" in probe
