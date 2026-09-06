"""Quote/verification contracts plus real spaCy deterministic decomposition."""
import os
from decimal import Decimal

import pytest

from theorem_ml.extract.claims.decompose import Claim, Decomposer
from theorem_ml.extract.claims.escalate import BatchDecomposer, FrontierBinding
from theorem_ml.extract.claims.resolve_span import resolve_span
from theorem_ml.extract.claims.verify import ClaimVerifier
from theorem_ml.extract.parse.router import parse_text


def test_quote_resolution_rejects_ambiguity_and_preserves_unicode():
    text = 'Renée said café. café.'
    with pytest.raises(ValueError):
        resolve_span(text, 'café')
    start, end = resolve_span(text, 'café', char_hint=text.index('café'))
    assert text.encode()[start:end].decode() == 'café'
    with pytest.raises(ValueError):
        resolve_span(text, 'invented')


class FixtureMiniCheckScorer:
    def __init__(self, scores):
        self.scores = scores

    def score(self, *, docs, claims):
        return [], self.scores, [], []


def test_verification_keeps_rejected_claim_as_evidence_and_strict_cutoff():
    tree = parse_text('Rent is 1450 dollars.', 's').tree
    element = tree.elements[0]
    claims = [Claim('Rent is 1450 dollars.', element.text, element.id, 0, len(element.text.encode()))]
    result = ClaimVerifier(scorer=FixtureMiniCheckScorer([0.5])).verify(tree, claims)
    assert result[0].score == 0.5 and not result[0].verified
    assert not claims[0].verified and claims[0].score is None
    with pytest.raises(ValueError):
        ClaimVerifier(scorer=FixtureMiniCheckScorer([float('nan')])).verify(tree, claims)


def test_bad_quote_never_reaches_verifier():
    tree = parse_text('Rent is 1450 dollars.', 's').tree
    claim = Claim('Bad', 'Invented', tree.elements[0].id, 0, 8)
    with pytest.raises(ValueError):
        ClaimVerifier(scorer=FixtureMiniCheckScorer([1])).verify(tree, [claim])


def test_batch_is_cheapest_binding_and_refuses_admission():
    bindings = [FrontierBinding('expensive', 'a', 'https://example.test/v1', '', Decimal(2)),
                FrontierBinding('cheap', 'b', 'https://example.test/v1', '', Decimal(1))]
    batch = BatchDecomposer(bindings)
    assert batch.binding.name == 'cheap'
    with pytest.raises(ValueError, match='separate'):
        batch.decompose(parse_text('An example.', 's').tree, [], run_kind='admission')


def test_real_dependency_parser_splits_clauses_stably_with_verbatim_quotes():
    text = 'Marie Curie, a physicist, discovered radium and won two Nobel Prizes.'
    tree = parse_text(text, 's').tree
    decomposer = Decomposer()
    runs = [decomposer.decompose(tree, derive_claims=True) for _ in range(3)]
    assert len(runs[0].claims) >= 3
    assert [[c.text for c in r.claims] for r in runs] == [[c.text for c in runs[0].claims]] * 3
    assert any('is a physicist' in c.text for c in runs[0].claims)
    assert any('discovered radium' in c.text and 'won' not in c.text for c in runs[0].claims)
    assert any('won two Nobel Prizes' in c.text and 'discovered' not in c.text for c in runs[0].claims)
    for claim in runs[0].claims:
        assert tree.elements[0].text.encode()[claim.byte_start:claim.byte_end].decode() == claim.quote
    assert decomposer.decompose(tree, derive_claims=False).claims == []


@pytest.mark.skipif(os.getenv('THEOREM_LIVE_MINICHECK') != '1', reason='requires pinned real MiniCheck weights')
def test_live_minicheck_entailment_disagreement():
    tree = parse_text('The monthly rent is 1450 dollars.', 's').tree
    e = tree.elements[0]
    claims = [Claim(text, e.text, e.id, 0, len(e.text.encode())) for text in
              ('Monthly rent is 1450 dollars.', 'Monthly rent is 7000 dollars.')]
    supported, unsupported = ClaimVerifier().verify(tree, claims)
    assert supported.score > unsupported.score
    assert supported.verified and not unsupported.verified
