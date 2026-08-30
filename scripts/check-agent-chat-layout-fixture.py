#!/usr/bin/env python3
"""Reproduce the Agent Chat layout fixture from its pinned portable Plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = (
    REPOSITORY_ROOT / "contracts/theorem.layout.v1.agent-chat-plan.fixture.json"
)
SOURCE_COMMIT = "f125a04118fce2e7a971b89d663d24a4cf2caa43"
SOURCE_PLAN_PATH = "plans/THEOREMWEB-AGENT-CHAT-1.0/plan-definition.json"
SOURCE_PLAN_ID = "THEOREMWEB-AGENT-CHAT-1.0"
SOURCE_GENERATION = 15


class FixtureCheckError(RuntimeError):
    """The pinned source or committed fixture violates the expected contract."""


def canonical_json(value: Any) -> bytes:
    """Encode canonical UTF-8 JSON for stable content addressing."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sha256_receipt(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value)).hexdigest()}"


def load_pinned_plan(theorem_repository: Path) -> dict[str, Any]:
    if not theorem_repository.is_dir():
        raise FixtureCheckError(
            f"Theorem repository does not exist: {theorem_repository}"
        )
    result = subprocess.run(
        [
            "git",
            "-C",
            str(theorem_repository),
            "show",
            f"{SOURCE_COMMIT}:{SOURCE_PLAN_PATH}",
        ],
        capture_output=True,
        text=True,
        timeout=15.0,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "git show failed"
        raise FixtureCheckError(f"cannot read pinned Agent Chat plan: {detail}")
    return json.loads(result.stdout)


def node_projection(task_id: str) -> dict[str, object]:
    if task_id.startswith("W"):
        return {"id": task_id, "w_px": 144.0, "h_px": 56.0, "kind": "work"}
    if task_id.startswith("V"):
        return {
            "id": task_id,
            "w_px": 112.0,
            "h_px": 48.0,
            "kind": "verification",
        }
    return {"id": task_id, "w_px": 128.0, "h_px": 52.0, "kind": "plan"}


def project_layout_request(
    plan: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if plan.get("plan_id") != SOURCE_PLAN_ID:
        raise FixtureCheckError("pinned source has the wrong plan_id")
    if plan.get("generation") != SOURCE_GENERATION:
        raise FixtureCheckError("pinned source has the wrong generation")
    tasks = plan.get("tasks")
    if not isinstance(tasks, list):
        raise FixtureCheckError("pinned source tasks must be a list")
    task_ids = [task.get("id") for task in tasks]
    if any(not isinstance(task_id, str) or not task_id for task_id in task_ids):
        raise FixtureCheckError("every pinned source task must have an id")
    if len(task_ids) != len(set(task_ids)):
        raise FixtureCheckError("pinned source task ids are not unique")

    tasks_by_id = {task["id"]: task for task in tasks}
    projected_edges: list[dict[str, str]] = []
    topology_edges: list[dict[str, str]] = []
    edge_number = 0
    for target in tasks:
        dependencies = target.get("depends_on", [])
        if not isinstance(dependencies, list):
            raise FixtureCheckError(f"task {target['id']} depends_on must be a list")
        for source_id in dependencies:
            if source_id not in tasks_by_id:
                raise FixtureCheckError(
                    f"task {target['id']} depends on unknown task {source_id}"
                )
            edge_number += 1
            edge_kind = (
                "verifies"
                if tasks_by_id[source_id].get("verify_sibling") == target["id"]
                else "dependency"
            )
            projected_edges.append(
                {
                    "from": source_id,
                    "to": target["id"],
                    "kind": edge_kind,
                    "id": f"e{edge_number:03d}",
                }
            )
            topology_edges.append({"from": source_id, "to": target["id"]})

    projected_edges.sort(key=lambda edge: (edge["from"], edge["to"], edge["id"]))
    topology = {
        "nodes": sorted(task_ids),
        "edges": sorted(
            topology_edges,
            key=lambda edge: (edge["from"], edge["to"]),
        ),
    }
    request = {
        "contract": "theorem.layout.v1",
        "graph_class": "plan_dag",
        "nodes": sorted(
            (node_projection(task_id) for task_id in task_ids),
            key=lambda node: node["id"],
        ),
        "edges": projected_edges,
        "params": {},
    }
    return request, topology


def check_fixture(theorem_repository: Path) -> str:
    plan = load_pinned_plan(theorem_repository)
    request, topology = project_layout_request(plan)
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    evidence = fixture.get("fixture_evidence", {})

    if len(request["nodes"]) != 31 or len(request["edges"]) != 44:
        raise FixtureCheckError("pinned source is no longer the required 31/44 plan")
    verify_edges = [edge for edge in request["edges"] if edge["kind"] == "verifies"]
    if len(verify_edges) != 12:
        raise FixtureCheckError("pinned source does not yield 12 verify siblings")
    if fixture.get("layout_request") != request:
        raise FixtureCheckError(
            "committed layout fixture differs from the pinned plan projection"
        )

    topology_digest = sha256_receipt(topology)
    request_digest = sha256_receipt(request)
    expected_evidence = {
        "source_plan": SOURCE_PLAN_ID,
        "source_generation": SOURCE_GENERATION,
        "source_commit": SOURCE_COMMIT,
        "source_topology_digest": topology_digest,
        "layout_request_digest": request_digest,
    }
    for key, expected in expected_evidence.items():
        if evidence.get(key) != expected:
            raise FixtureCheckError(
                f"fixture evidence {key} differs: {evidence.get(key)!r} != {expected!r}"
            )
    return (
        f"valid: plan={SOURCE_PLAN_ID} generation={SOURCE_GENERATION} "
        f"nodes={len(request['nodes'])} edges={len(request['edges'])} "
        f"verify_siblings={len(verify_edges)} "
        f"topology_digest={topology_digest} request_digest={request_digest}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--theorem-repo",
        type=Path,
        required=True,
        help="Theorem Git repository containing the pinned source commit",
    )
    args = parser.parse_args()
    try:
        print(check_fixture(args.theorem_repo.resolve()))
    except (
        FixtureCheckError,
        json.JSONDecodeError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        print(f"fixture check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
