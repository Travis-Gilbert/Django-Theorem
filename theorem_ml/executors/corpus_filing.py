"""Real TabICLv2 inference emits prototypes/priors; never item filing decisions."""

from __future__ import annotations

import os
import hashlib
import numpy as np

from .contracts import admitted_items, matrix, validate_layout

TABICL_SOURCE_COMMIT = "0dbff3ec8fc68c123c87af77b0ea8b25cd2d23f3"
TABICL_CHECKPOINT = "tabicl-classifier-v2-20260212.ckpt"
TABICL_WEIGHTS_REVISION = "4dcd344ece2c00be9e831fdd35bed57b5ad83e19"
TABICL_CHECKPOINT_SHA256 = (
    "bdc7dbd5e4ff21f8f0456fcf90c6b7cdf72dbea960f2d05b19bec19f9b3d4ed0"
)


class CachedTabICLv2:
    """Cache each context; use binary one-vs-rest above the native ten-class limit.

    Upstream rejects KV caching with its many-class inference path. Decomposing
    explicitly preserves caching for every class rather than disabling it.
    """

    def __init__(self, *, model_path: str, device: str = "cpu"):
        from tabicl import TabICLClassifier

        self._classifier = TabICLClassifier
        self.model_path = model_path
        self.device = device
        self.models = []

    def _new(self):
        return self._classifier(
            model_path=self.model_path,
            checkpoint_version=TABICL_CHECKPOINT,
            allow_auto_download=False,
            kv_cache=True,
            support_many_classes=True,
            device=self.device,
            random_state=42,
        )

    def fit(self, features, labels):
        self.classes_ = np.unique(labels)
        if len(self.classes_) < 2:
            raise ValueError(
                "TabICL corpus fitting requires at least two filed collections"
            )
        self.models = []
        if len(self.classes_) <= 10:
            model = self._new()
            model.fit(features, labels)
            self.models.append(model)
        else:
            for label in self.classes_:
                model = self._new()
                model.fit(features, (labels == label).astype(np.int64))
                self.models.append(model)
        return self

    def predict_proba(self, features):
        if not self.models:
            raise ValueError("fit must establish a context before prediction")
        if len(self.models) == 1:
            model = self.models[0]
            columns = [list(model.classes_).index(label) for label in self.classes_]
            return np.asarray(model.predict_proba(features))[:, columns]
        scores = np.column_stack(
            [
                model.predict_proba(features)[:, list(model.classes_).index(1)]
                for model in self.models
            ]
        )
        totals = scores.sum(axis=1, keepdims=True)
        if (totals <= 0).any():
            raise ValueError("one-vs-rest probabilities have no positive mass")
        return scores / totals


def prototype_updates(
    features, probabilities, classes, embedding_offset, embedding_dim
):
    probabilities = np.asarray(probabilities, dtype=np.float64)
    if (
        probabilities.shape != (len(features), len(classes))
        or not np.isfinite(probabilities).all()
        or (probabilities < 0).any()
        or not np.allclose(probabilities.sum(axis=1), 1.0, atol=1e-5)
    ):
        raise ValueError(
            "classifier must emit a finite probability distribution per item"
        )
    embeddings = np.asarray(
        features[:, embedding_offset : embedding_offset + embedding_dim],
        dtype=np.float64,
    )
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    if (norms == 0).any():
        raise ValueError("prototype seeding requires nonzero ingest embeddings")
    embeddings = embeddings / norms
    updates = []
    for index, collection in enumerate(classes):
        weights = probabilities[:, index]
        mass = weights.sum()
        if mass <= 0:
            raise ValueError("a collection has no posterior mass")
        prototype = (embeddings * weights[:, None]).sum(axis=0) / mass
        norm = np.linalg.norm(prototype)
        if norm == 0:
            raise ValueError("a collection has a cancelling zero prototype")
        updates.append(
            {
                "collection": str(collection),
                "prototype": (prototype / norm).astype(np.float32).tolist(),
                "prior": float(mass / len(features)),
            }
        )
    return updates


def corpus_pass(payload: dict, *, tenant: str) -> dict:
    items = admitted_items(payload.get("items", []), tenant)
    held_out = (
        admitted_items(payload["held_out"], tenant) if payload.get("held_out") else []
    )
    if {item["id"] for item in items} & {item["id"] for item in held_out}:
        raise ValueError("held-out filings must be disjoint from the fitting context")
    features = matrix(items, "features")
    test_features = matrix(held_out, "features") if held_out else None
    offset, dim = validate_layout(payload.get("feature_layout", {}), features.shape[1])
    if not payload["feature_layout"]["object_type"].startswith(
        f"schema:object-type:{tenant}:"
    ):
        raise ValueError("FeatureLayout object type must belong to the admitted tenant")
    if test_features is not None and test_features.shape[1] != features.shape[1]:
        raise ValueError("held-out features do not match FeatureLayout")
    labels = np.asarray([item.get("collection", "") for item in items])
    truth = np.asarray([item.get("collection", "") for item in held_out])
    if not all(isinstance(label, str) and label for label in [*labels, *truth]):
        raise ValueError("corpus and held-out items require filed collection IDs")
    if not set(truth) <= set(labels):
        raise ValueError("held-out collection is absent from the fitting context")
    classes = np.unique(labels)
    if len(classes) == 1:
        # No classification uncertainty exists with one accepted target. This
        # is exact centroid algebra, not a TabICL invocation or quality oracle.
        return {
            "updates": prototype_updates(
                features, np.ones((len(items), 1)), classes, offset, dim
            ),
            "schema_anchor": payload["feature_layout"]["schema_anchor"],
            "fitness": None,
            "context_count": len(items),
            "implementation": "exact_singleton_centroid",
        }
    model_path = os.environ.get("THEOREM_TABICL_CHECKPOINT", "")
    if not model_path or not os.path.isfile(model_path):
        raise RuntimeError(
            "THEOREM_TABICL_CHECKPOINT must name the verified v2 checkpoint; automatic downloads are disabled"
        )
    with open(model_path, "rb") as checkpoint:
        if (
            hashlib.file_digest(checkpoint, "sha256").hexdigest()
            != TABICL_CHECKPOINT_SHA256
        ):
            raise ValueError(
                "TabICLv2 checkpoint does not match the pinned official weights"
            )
    model = CachedTabICLv2(
        model_path=model_path, device=os.environ.get("THEOREM_TABICL_DEVICE", "cpu")
    ).fit(features, labels)
    fitness = None
    if test_features is not None:
        held_probabilities = model.predict_proba(test_features)
        predictions = model.classes_[held_probabilities.argmax(axis=1)]
        fitness = {
            "accuracy": float(np.mean(predictions == truth)),
            "held_out_count": len(held_out),
        }
    updates = prototype_updates(
        features, model.predict_proba(features), model.classes_, offset, dim
    )
    return {
        "updates": updates,
        "schema_anchor": payload["feature_layout"]["schema_anchor"],
        "fitness": fitness,
        "context_count": len(items),
        "implementation": "tabiclv2_cached_native"
        if len(model.classes_) <= 10
        else "tabiclv2_cached_one_vs_rest",
        "checkpoint": TABICL_CHECKPOINT,
        "source_commit": TABICL_SOURCE_COMMIT,
    }
