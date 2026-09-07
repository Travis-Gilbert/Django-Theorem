"""Embedding kNN plus admitted RustyRed edges, with extractive naming."""

from __future__ import annotations

from collections import Counter
import math
import re

import networkx as nx
import numpy as np

from .contracts import admitted_items, digest, matrix

STOPWORDS = frozenset(
    "a an and are as at be by for from in is it of on or that the this to was with you your".split()
)


def discover(payload: dict, *, tenant: str) -> dict:
    items = admitted_items(payload.get("items", []), tenant)
    vectors = matrix(items, "embedding")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if (norms == 0).any():
        raise ValueError("zero embeddings cannot establish discovery similarity")
    vectors = vectors / norms
    k = payload.get("neighbors", 8)
    resolution = float(payload.get("resolution", 1.0))
    if (
        not isinstance(k, int)
        or k < 1
        or not math.isfinite(resolution)
        or resolution <= 0
    ):
        raise ValueError("neighbors and resolution must be positive")
    ids = [item["id"] for item in items]
    position = {node_id: index for index, node_id in enumerate(ids)}
    entity_nodes = payload.get("entity_nodes", [])
    entities_by_id = (
        {node["id"]: node for node in admitted_items(entity_nodes, tenant)}
        if entity_nodes
        else {}
    )
    if set(entities_by_id) & set(ids):
        raise ValueError("entity IDs and batch item IDs must be disjoint")
    graph = nx.Graph()
    graph.add_nodes_from(ids)
    graph.add_nodes_from(sorted(entities_by_id))
    # One similarity row at a time bounds working memory independently of n².
    for index, node_id in enumerate(ids):
        scores = vectors @ vectors[index]
        nearest = sorted(
            (other for other in range(len(ids)) if other != index),
            key=lambda other: (-float(scores[other]), ids[other]),
        )[:k]
        for other in nearest:
            weight = max(0.0, float(scores[other]))
            if weight > 0:
                graph.add_edge(node_id, ids[other], weight=weight)
    for edge in payload.get("edges", []):
        if edge.get("tenant_id") != tenant:
            raise ValueError("edge tenant does not match admitted tenant")
        if (
            edge.get("tombstone")
            or edge.get("epistemic_status", "").lower() == "contradicted"
        ):
            continue
        source, target = edge.get("from_id"), edge.get("to_id")
        if source not in graph or target not in graph:
            raise ValueError(
                "discovery edges must join admitted batch items or materialized entity nodes"
            )
        weight = float(edge.get("weight", 1.0))
        if not math.isfinite(weight) or weight < 0:
            raise ValueError("graph weights must be finite and nonnegative")
        if source != target and weight > 0:
            graph.add_edge(
                source,
                target,
                weight=graph.get_edge_data(source, target, {}).get("weight", 0.0)
                + weight,
            )
    communities = (
        nx.community.louvain_communities(
            graph, weight="weight", resolution=resolution, seed=42
        )
        if graph.number_of_edges()
        else [{node_id} for node_id in ids]
    )
    proposals = []
    item_communities = [
        sorted(member for member in group if member in position)
        for group in communities
    ]
    for members in sorted(
        (group for group in item_communities if group), key=lambda group: group[0]
    ):
        terms, entities = Counter(), Counter()
        for member in members:
            item = items[position[member]]
            text = f"{item.get('title', '')} {item.get('text', '')}"
            terms.update(
                word
                for word in re.findall(r"[^\W\d_][\w-]{2,}", text.lower())
                if word not in STOPWORDS
            )
            entities.update(item.get("entities", []))
        top_terms = [
            term
            for term, _ in sorted(terms.items(), key=lambda pair: (-pair[1], pair[0]))[
                :6
            ]
        ]
        top_entities = [
            entity
            for entity, _ in sorted(
                entities.items(), key=lambda pair: (-pair[1], pair[0])
            )[:6]
        ]
        name = (
            " / ".join(top_terms[:3])
            or " / ".join(top_entities[:2])
            or f"Collection {len(proposals) + 1}"
        )
        selected = vectors[[position[member] for member in members]]
        n = len(members)
        cohesion = (
            float((np.square(selected.sum(axis=0)).sum() - n) / (n * (n - 1)))
            if n > 1
            else 1.0
        )
        proposals.append(
            {
                "proposal_id": f"index-proposal:{tenant}:{digest(members)}",
                "name": name,
                "members": members,
                "top_terms": top_terms,
                "top_entities": top_entities,
                "cohesion": max(-1.0, min(1.0, cohesion)),
            }
        )
    fitness = discovery_fitness(
        proposals,
        payload.get("training_filings", {}),
        payload.get("held_out_filings", {}),
        set(ids),
    )
    return {
        "proposals": proposals,
        "fitness": fitness,
        "item_count": len(items),
        "implementation": "networkx_louvain_embedding_knn",
    }


def discovery_fitness(
    proposals: list[dict], training: dict, held_out: dict, ids: set[str]
) -> dict | None:
    if not training and not held_out:
        return None
    if (
        not training
        or not held_out
        or set(training) & set(held_out)
        or not (set(training) | set(held_out)) <= ids
    ):
        raise ValueError(
            "discovery fitness requires disjoint training and held-out filings in the batch"
        )
    predicted = {}
    for proposal in proposals:
        counts = Counter(
            training[member] for member in proposal["members"] if member in training
        )
        label = (
            sorted(counts, key=lambda label: (-counts[label], label))[0]
            if counts
            else None
        )
        predicted.update({member: label for member in proposal["members"]})
    return {
        "accuracy": sum(
            predicted.get(member) == label for member, label in held_out.items()
        )
        / len(held_out),
        "held_out_count": len(held_out),
    }
