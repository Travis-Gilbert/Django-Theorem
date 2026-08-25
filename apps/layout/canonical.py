"""Canonical DOT serialization for deterministic cache and execution input."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from decimal import Decimal

from apps.layout.contracts import LayoutEdge, LayoutNode
from apps.layout.policy import LayoutPolicy


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _number(value: float) -> str:
    decimal = Decimal(str(value)).normalize()
    rendered = format(decimal, "f")
    return "0" if rendered in {"-0", ""} else rendered


def _node_statement(node: LayoutNode) -> str:
    width = _number(node.w_px / 72.0)
    height = _number(node.h_px / 72.0)
    return (
        f"{_quote(node.id)} [fixedsize=true, height={_quote(height)}, "
        f"label={_quote('')}, shape=box, width={_quote(width)}];"
    )


def _verification_rank_groups(
    nodes: list[LayoutNode], edges: list[LayoutEdge]
) -> list[tuple[str, str]]:
    kinds = {node.id: node.kind.lower() for node in nodes}
    groups: set[tuple[str, str]] = set()
    for edge in edges:
        left_kind = kinds[edge.from_]
        right_kind = kinds[edge.to]
        if left_kind in {"work", "implementation"} and right_kind in {
            "verify",
            "verification",
        }:
            groups.add((edge.from_, edge.to))
        elif right_kind in {"work", "implementation"} and left_kind in {
            "verify",
            "verification",
        }:
            groups.add((edge.to, edge.from_))
    return sorted(groups)


def canonical_dot(
    nodes: list[LayoutNode],
    edges: list[LayoutEdge],
    policy: LayoutPolicy,
    *,
    focus_id: str | None,
) -> str:
    """Return byte-stable DOT with all user-controlled strings quoted."""
    lines = ["strict digraph theorem_layout {"]
    graph_attrs = dict(policy.graph_attrs)
    if focus_id is not None:
        graph_attrs["root"] = focus_id
    if graph_attrs:
        rendered = ", ".join(
            f"{key}={_quote(value)}" for key, value in sorted(graph_attrs.items())
        )
        lines.append(f"  graph [{rendered}];")

    sorted_nodes = sorted(nodes, key=lambda item: item.id)
    clustered_ids: set[str] = set()
    if policy.cluster_nodes:
        clusters: dict[str, list[LayoutNode]] = defaultdict(list)
        for node in sorted_nodes:
            if node.cluster:
                clusters[node.cluster].append(node)
                clustered_ids.add(node.id)
        for cluster, members in sorted(clusters.items()):
            lines.append(f"  subgraph {_quote(f'cluster_{cluster}')} {{")
            for node in members:
                lines.append(f"    {_node_statement(node)}")
            lines.append("  }")

    for node in sorted_nodes:
        if node.id not in clustered_ids:
            lines.append(f"  {_node_statement(node)}")

    for edge in sorted(edges, key=lambda item: (item.from_, item.to, item.id)):
        lines.append(
            f"  {_quote(edge.from_)} -> {_quote(edge.to)} "
            f"[id={_quote(edge.id)}, label={_quote('')}];"
        )

    if policy.verify_sibling_ranks:
        for work_id, verify_id in _verification_rank_groups(nodes, edges):
            lines.append(
                f"  {{ rank=same; {_quote(work_id)}; {_quote(verify_id)}; }}"
            )
    lines.append("}")
    return "\n".join(lines) + "\n"


def validate_graph(nodes: list[LayoutNode], edges: list[LayoutEdge]) -> None:
    if not 1 <= len(nodes) <= 2_000:
        raise ValueError("nodes must contain between 1 and 2000 entries")
    if len(edges) > 10_000:
        raise ValueError("edges must contain at most 10000 entries")
    node_ids = [node.id for node in nodes]
    edge_ids = [edge.id for edge in edges]
    if len(node_ids) != len(set(node_ids)):
        raise ValueError("node ids must be unique")
    if len(edge_ids) != len(set(edge_ids)):
        raise ValueError("edge ids must be unique")
    known_nodes = set(node_ids)
    for node in nodes:
        if (
            not math.isfinite(node.w_px)
            or not math.isfinite(node.h_px)
            or not 1.0 <= node.w_px <= 10_000.0
            or not 1.0 <= node.h_px <= 10_000.0
        ):
            raise ValueError("node dimensions must be finite and in [1, 10000] px")
    for edge in edges:
        if edge.from_ not in known_nodes or edge.to not in known_nodes:
            raise ValueError("every edge endpoint must identify a request node")
