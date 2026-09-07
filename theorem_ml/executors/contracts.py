"""Materialized, tenant-admitted inputs supplied by the Rust graph authority."""

from __future__ import annotations

import hashlib
import json
import numpy as np


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def admitted_items(items: list[dict], tenant: str) -> list[dict]:
    if not tenant or not tenant.strip():
        raise ValueError("an admitted tenant is required")
    if not items:
        raise ValueError("at least one materialized item is required")
    ids = set()
    for item in items:
        if item.get("tenant_id") != tenant:
            raise ValueError("item tenant does not match the admitted tenant")
        node_id = item.get("id")
        if not isinstance(node_id, str) or not node_id or node_id in ids:
            raise ValueError("item IDs must be nonempty and unique")
        ids.add(node_id)
        if item.get("tombstone"):
            raise ValueError("deleted items cannot enter a learned executor")
        if not isinstance(item.get("entities", []), list) or any(
            not isinstance(entity, str) or not entity
            for entity in item.get("entities", [])
        ):
            raise ValueError("entities must be a list of nonempty entity IDs")
    return sorted(items, key=lambda item: item["id"])


def matrix(items: list[dict], key: str) -> np.ndarray:
    try:
        result = np.asarray([item[key] for item in items], dtype=np.float32)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"items require rectangular {key} vectors") from error
    if result.ndim != 2 or not result.shape[1] or not np.isfinite(result).all():
        raise ValueError(
            f"{key} must contain finite, nonempty vectors; missing embeddings are not synthesized"
        )
    return result


def validate_layout(layout: dict, width: int) -> tuple[int, int]:
    if (
        layout.get("feature_dim") != width
        or not layout.get("schema_anchor")
        or not layout.get("object_type")
    ):
        raise ValueError(
            "feature vectors must match an identified schema FeatureLayout"
        )
    offset = 0
    embedding = None
    categories = []
    for block in layout.get("blocks", []):
        kind = block.get("kind", {})
        name = kind.get("kind")
        if name == "enum_one_hot":
            size, category = len(kind["variants"]), 0
        elif name == "relation_affinity":
            size, category = 1, 1
        elif name == "date_cyclic":
            size, category = 2, 2
        elif name == "membrane":
            size, category = 1, 3
        elif name == "embedding":
            size, category = kind["dim"], 4
            if embedding is not None or not isinstance(size, int) or size <= 0:
                raise ValueError(
                    "corpus prototype updates require one nonempty embedding block"
                )
            embedding = (offset, size)
        else:
            raise ValueError("unknown FeatureLayout block")
        if block.get("offset") != offset:
            raise ValueError("FeatureLayout blocks must be contiguous")
        offset += size
        categories.append(category)
    if offset != width or categories != sorted(categories) or embedding is None:
        raise ValueError("FeatureLayout width, block ordering, or embedding is invalid")
    return embedding
