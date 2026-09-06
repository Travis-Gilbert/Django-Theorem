"""Per-field metrics; omitted evidence never becomes a hallucination count."""

from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
import math
import unicodedata


MISSING = object()


def normalized_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def score_field(predicted, gold, correctness: str, *, tolerance: float = 0,
                aliases: list[str] = (), item_class: str = "exact") -> dict:
    if predicted is MISSING:
        return {"outcome": "omitted", "score": 0.0}
    if gold is MISSING:
        return {"outcome": "hallucinated", "score": 0.0}
    if correctness == "exact":
        matches = type(predicted) is type(gold) and predicted == gold
    elif correctness == "tolerance":
        if isinstance(predicted, bool) or isinstance(gold, bool):
            matches = False
        else:
            try:
                left, right = float(predicted), float(gold)
                matches = math.isfinite(left) and math.isfinite(right) and abs(left - right) <= tolerance
            except (TypeError, ValueError):
                matches = False
    elif correctness == "semantic":
        # Human-authored aliases constitute the equivalence oracle. No model's
        # generated synonym is used as its own correctness evidence.
        matches = isinstance(predicted, str) and normalized_name(predicted) in {
            normalized_name(x) for x in [gold, *aliases]}
    elif correctness == "alignment":
        if not isinstance(predicted, list) or not isinstance(gold, list):
            matches = False
        else:
            from scipy.optimize import linear_sum_assignment
            costs = [[1 - score_field(p, g, item_class, tolerance=tolerance, aliases=aliases)["score"]
                      for g in gold] for p in predicted]
            if not gold and not predicted:
                return {"outcome": "correct", "score": 1.0}
            if not gold or not predicted:
                return {"outcome": "incorrect", "score": 0.0}
            rows, cols = linear_sum_assignment(costs)
            matched = sum(1 - costs[r][c] for r, c in zip(rows, cols))
            score = matched / max(len(predicted), len(gold))
            return {"outcome": "correct" if score == 1 else "incorrect", "score": score}
    else:
        raise ValueError(f"Unknown correctness class: {correctness}")
    return {"outcome": "correct" if matches else "incorrect", "score": float(matches)}


def score_fields(predicted: dict, gold: dict) -> dict:
    fields = {}
    for name in sorted(set(predicted) | set(gold)):
        expected = gold.get(name)
        fields[name] = score_field(predicted.get(name, MISSING),
            expected["value"] if expected is not None else MISSING,
            expected.get("class", "exact") if expected is not None else "exact",
            tolerance=expected.get("tolerance", 0) if expected else 0,
            aliases=expected.get("aliases", []) if expected else [],
            item_class=expected.get("item_class", "exact") if expected else "exact")
    return {"fields": fields, "omitted": sum(f["outcome"] == "omitted" for f in fields.values()),
            "hallucinated": sum(f["outcome"] == "hallucinated" for f in fields.values()),
            "accuracy": sum(f["score"] for f in fields.values()) / len(fields) if fields else None}


def f1(predicted, gold) -> dict:
    left, right = Counter(predicted), Counter(gold)
    tp = sum((left & right).values())
    precision = tp / sum(left.values()) if left else float(not right)
    recall = tp / sum(right.values()) if right else float(not left)
    return {"precision": precision, "recall": recall,
            "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
            "predicted": sum(left.values()), "gold": sum(right.values())}


def score_parse(tree, gold: dict) -> dict:
    """Expose each parse dimension; do not hide reading order in an aggregate."""
    tree.validate()
    texts = [e.text for e in tree.elements if e.text]
    expected = gold["texts"]
    exact = f1(texts, expected)
    # Match repeated lines one occurrence at a time, not list.index(), so a
    # duplicate paragraph cannot stand in for an omitted later occurrence.
    available = {}
    for index, text in enumerate(expected):
        available.setdefault(text, []).append(index)
    order = []
    for text in texts:
        if available.get(text):
            order.append(available[text].pop(0))
    pairs = len(order) * (len(order) - 1) // 2
    inversions = sum(order[i] > order[j] for i in range(len(order)) for j in range(i + 1, len(order)))
    coverage = len(order) / len(expected) if expected else float(not texts)
    reading_order = ((1 - inversions / pairs) if pairs else 1.0) * coverage
    tables = [e.attributes.get("rows", []) for e in tree.elements if e.kind == "Table"]
    merges = [e.attributes.get("merged_cells", []) for e in tree.elements if e.kind == "Table"]
    headings = [(e.text, e.attributes.get("level", 1)) for e in tree.elements if e.kind == "SectionHeader"]
    def tuple_tree(value):
        if isinstance(value, dict):
            return tuple(sorted((k, tuple_tree(v)) for k, v in value.items()))
        if isinstance(value, list):
            return tuple(tuple_tree(v) for v in value)
        return value
    return {"exact_text_f1": exact["f1"],
            "text_similarity": SequenceMatcher(None, "\n".join(expected), "\n".join(texts), autojunk=False).ratio(),
            "reading_order": reading_order,
            "table_structure_f1": f1([tuple_tree(x) for x in tables], [tuple_tree(x) for x in gold.get("tables", [])])["f1"],
            "merged_cells_f1": f1([tuple_tree(x) for x in merges], [tuple_tree(x) for x in gold.get("merged_cells", [])])["f1"],
            "heading_hierarchy_f1": f1(headings, [tuple(x) for x in gold.get("headings", [])])["f1"],
            "span_recoverability": sum(tree.text.encode()[e.byte_start:e.byte_end] == e.text.encode() for e in tree.elements) / len(tree.elements) if tree.elements else 0.0}


def atom_stability(runs: list[list[str]], embed) -> float:
    """Cosine alignment of actual claim embeddings, penalizing atom-count drift."""
    import numpy as np
    from scipy.optimize import linear_sum_assignment

    if len(runs) < 3:
        raise ValueError("Atom stability requires at least three runs")
    similarities = []
    for i, left in enumerate(runs):
        for right in runs[i + 1:]:
            if not left or not right:
                similarities.append(float(not left and not right))
                continue
            a, b = np.asarray(embed(left)), np.asarray(embed(right))
            if a.ndim != 2 or b.ndim != 2 or a.shape[1] != b.shape[1]:
                raise ValueError("Claim encoder returned incompatible embeddings")
            an, bn = np.linalg.norm(a, axis=1), np.linalg.norm(b, axis=1)
            if not np.isfinite(a).all() or not np.isfinite(b).all() or (an == 0).any() or (bn == 0).any():
                raise ValueError("Claim embeddings must be finite and nonzero")
            cosine = (a @ b.T) / (an[:, None] * bn[None, :])
            rows, cols = linear_sum_assignment(-cosine)
            similarities.append(float(cosine[rows, cols].clip(-1, 1).sum() / max(len(left), len(right))))
    return sum(similarities) / len(similarities)
