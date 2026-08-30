"""Isolated pygraphviz worker. It accepts one JSON object on stdin."""

from __future__ import annotations

import json
import sys

import pygraphviz


def main() -> int:
    request = json.load(sys.stdin)
    graph = pygraphviz.AGraph(string=request["dot"])
    graph.layout(prog=request["engine"])
    positions: dict[str, dict[str, float]] = {}
    for node_id in request["node_ids"]:
        raw = graph.get_node(node_id).attr.get("pos", "")
        parts = raw.rstrip("!").split(",")
        if len(parts) != 2:
            raise RuntimeError(f"Graphviz omitted a position for {node_id}")
        positions[node_id] = {
            "x": round(float(parts[0]), 6),
            "y": round(float(parts[1]), 6),
        }
    json.dump(positions, sys.stdout, sort_keys=True, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
