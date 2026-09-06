import numpy as np
import pytest

from theorem_ml.extract.bench.score import atom_stability, f1, score_field, score_fields, score_parse
from theorem_ml.extract.elements import DocElement, build_tree


def test_omission_and_hallucination_are_separate_outcomes():
    result = score_fields({"unexpected": 42}, {"identifier": {"class": "exact", "value": "ID-1"}})
    assert result["omitted"] == result["hallucinated"] == 1
    assert result["fields"]["identifier"]["outcome"] == "omitted"
    assert result["fields"]["unexpected"]["outcome"] == "hallucinated"


def test_exact_identifiers_do_not_coerce_types_but_quantities_use_tolerance():
    assert score_field(1, "1", "exact")["score"] == 0
    assert score_field(1.009, 1, "tolerance", tolerance=.01)["score"] == 1
    assert score_field(float("nan"), 1, "tolerance", tolerance=.01)["score"] == 0
    assert score_field(True, 1, "tolerance")["score"] == 0


def test_semantic_name_equivalence_uses_authored_aliases():
    assert score_field(" THE ACME CORPORATION ", "Acme Corp", "semantic", aliases=["The Acme Corporation"])["score"] == 1
    assert score_field("another company", "Acme Corp", "semantic")["score"] == 0


def test_array_alignment_does_not_reuse_matches_for_duplicates():
    assert score_field(["b", "a"], ["a", "b"], "alignment")["score"] == 1
    assert score_field(["a", "a"], ["a", "b"], "alignment")["score"] == .5
    assert f1(["a", "a"], ["a"])["precision"] == .5


def test_parse_reading_order_penalizes_reversal_separately_from_exact_text():
    tree = build_tree("s", [DocElement("a", "Paragraph", "second"), DocElement("b", "Paragraph", "first")])
    result = score_parse(tree, {"texts": ["first", "second"]})
    assert result["exact_text_f1"] == 1
    assert result["reading_order"] == 0
    assert result["span_recoverability"] == 1


def test_atom_stability_penalizes_missing_atoms_using_real_vector_alignment():
    vectors = {"a": [1, 0], "b": [0, 1]}
    def embed(texts):
        return np.asarray([vectors[t] for t in texts])
    assert atom_stability([["a", "b"], ["b", "a"], ["a", "b"]], embed) == pytest.approx(1)
    assert atom_stability([["a", "b"], ["a"], ["a", "b"]], embed) < 1
    with pytest.raises(ValueError):
        atom_stability([["a"]] * 3, lambda texts: [[0, 0]])


def test_native_code_preservation_is_scored_against_complete_authored_source(tmp_path):
    from theorem_ml.extract.bench.__main__ import native_gold
    from theorem_ml.extract.parse.router import ParserRouter
    source = tmp_path / "source.py"
    source.write_text('"""Lease\namount: 2000\n"""\n\ndef payment(months):\n    return 2000 * months\n')
    gold = {"texts": ["Lease", "amount: 2000"], "headings": [["Lease", 1]]}
    record = {"category": "code", "source": source.name}
    parsed = ParserRouter().parse(source, "code")
    result = score_parse(parsed.tree, native_gold(record, gold, tmp_path))
    assert result["exact_text_f1"] == result["heading_hierarchy_f1"] == 1
    assert gold["texts"] == ["Lease", "amount: 2000"]
