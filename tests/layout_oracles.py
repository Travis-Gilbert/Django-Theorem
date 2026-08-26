"""Shared assertions for local and hosted graph-layout acceptance oracles."""

from __future__ import annotations


def assert_plan_dependency_flow(
    payload: dict,
    positions: dict[str, dict],
    *,
    verify_tolerance_px: float = 1.0,
) -> None:
    """Require strict dependency progress and same-rank verification siblings."""
    for edge in payload["edges"]:
        source_x = positions[edge["from"]]["x_px"]
        target_x = positions[edge["to"]]["x_px"]
        if edge["kind"] == "verifies":
            assert abs(source_x - target_x) <= verify_tolerance_px, (
                f"verify sibling {edge['from']} -> {edge['to']} moved across ranks: "
                f"{source_x} != {target_x}"
            )
        else:
            assert target_x > source_x, (
                f"dependency {edge['from']} -> {edge['to']} did not progress "
                f"left-to-right: {source_x} -> {target_x}"
            )
