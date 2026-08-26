"""Data-driven Graphviz engine policy and structural graph classification."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from apps.layout.contracts import GraphClass, LayoutEdge, LayoutNode, LayoutParams


@dataclass(frozen=True)
class LayoutPolicy:
    engine: str
    graph_attrs: Mapping[str, str] = field(default_factory=dict)
    large_engine: str | None = None
    large_node_threshold: int | None = None
    verify_sibling_ranks: bool = False
    cluster_nodes: bool = False
    focus_root: bool = False
    nonconstraining_edge_kinds: frozenset[str] = frozenset()

    def engine_for(self, node_count: int) -> str:
        if (
            self.large_engine is not None
            and self.large_node_threshold is not None
            and node_count >= self.large_node_threshold
        ):
            return self.large_engine
        return self.engine


def _attrs(**values: str) -> Mapping[str, str]:
    return MappingProxyType(values)


POLICIES: Mapping[GraphClass, LayoutPolicy] = MappingProxyType(
    {
        "plan_dag": LayoutPolicy(
            "dot",
            _attrs(rankdir="LR", ranksep="0.75", nodesep="0.35"),
            verify_sibling_ranks=True,
            nonconstraining_edge_kinds=frozenset({"verifies"}),
        ),
        "derivation": LayoutPolicy("dot", _attrs(rankdir="LR")),
        "query_plan": LayoutPolicy("dot", _attrs(rankdir="LR")),
        "code_map_small": LayoutPolicy("dot", _attrs(rankdir="TB")),
        "code_map_large": LayoutPolicy("sfdp", _attrs(overlap="prism")),
        "neighborhood": LayoutPolicy(
            "neato",
            _attrs(overlap="false"),
            large_engine="sfdp",
            large_node_threshold=200,
        ),
        "ego_radial": LayoutPolicy("twopi", focus_root=True),
        "cyclic_ring": LayoutPolicy("circo"),
        "containment": LayoutPolicy("osage", cluster_nodes=True),
    }
)

DENSE_NODE_THRESHOLD = 200
DENSE_EDGE_FACTOR = 2
CONTAINMENT_EDGE_KINDS = frozenset({"contains", "containment"})


def _is_dag(nodes: list[LayoutNode], edges: list[LayoutEdge]) -> bool:
    indegree = {node.id: 0 for node in nodes}
    outgoing = {node.id: [] for node in nodes}
    for edge in edges:
        if edge.from_ == edge.to:
            return False
        outgoing[edge.from_].append(edge.to)
        indegree[edge.to] += 1
    queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
    visited = 0
    while queue:
        node_id = queue.popleft()
        visited += 1
        for target in sorted(outgoing[node_id]):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    return visited == len(nodes)


def classify_graph(nodes: list[LayoutNode], edges: list[LayoutEdge]) -> GraphClass:
    """Select the documented automatic class using structural facts only."""
    if edges and all(edge.kind in CONTAINMENT_EDGE_KINDS for edge in edges):
        return "containment"
    if _is_dag(nodes, edges):
        return "derivation"
    if (
        len(nodes) >= DENSE_NODE_THRESHOLD
        and len(edges) >= DENSE_EDGE_FACTOR * len(nodes)
    ):
        return "code_map_large"
    return "neighborhood"


def resolve_policy(
    graph_class: GraphClass | None,
    nodes: list[LayoutNode],
    edges: list[LayoutEdge],
    params: LayoutParams,
) -> tuple[GraphClass, LayoutPolicy, str | None]:
    selected = graph_class or classify_graph(nodes, edges)
    policy = POLICIES[selected]
    focus_id = params.focus_id
    if policy.focus_root:
        focus_id = focus_id or min(node.id for node in nodes)
        if focus_id not in {node.id for node in nodes}:
            raise ValueError("params.focus_id must identify a request node")
    elif focus_id is not None:
        raise ValueError("params.focus_id is only valid for ego_radial")
    return selected, policy, focus_id
