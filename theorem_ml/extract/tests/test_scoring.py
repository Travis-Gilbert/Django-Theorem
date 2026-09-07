"""Real River learning, persistence, and cold-start evidence boundaries."""
from datetime import datetime, timedelta, timezone
import math

import pytest

from theorem_ml.extract.scoring import (
    RiverOnlineScorer, document_features, learn_document, score_document,
)


def state():
    return {'observed_at': '2026-09-05T12:00:00+00:00',
            'schema': {'Lease': {'fields': [{'key': 'rent'}, {'key': 'landlord'}]}},
            'records': {'Lease': {'properties': {'rent': 1450}}},
            'claims': [{'verified': False, 'score': None}],
            'metadata': {'reliable': 1.0, 'useful': 1.0}}


def test_missing_measurements_are_explicit_and_metadata_is_not_fitness():
    features = document_features(state(), now=datetime(2026, 10, 5, 12, tzinfo=timezone.utc))
    assert features['values']['fresh'] == 0.5
    assert features['values']['specific'] == 0.5
    assert features['values']['independent'] is None
    assert features['values']['reliable'] is None
    assert features['values']['useful'] is None
    verified = state()
    verified['claims'] = [{'verified': True, 'score': 0.9}, {'verified': False, 'score': 0.1}]
    assert document_features(verified)['values']['reliable'] == 0.5


def test_untrained_score_is_none_and_does_not_block_admission(settings, tmp_path):
    settings.EXTRACTION_SCORER_DIR = tmp_path
    result = score_document(state(), tenant='tenant-a')
    assert result['status'] == 'untrained'
    assert result['score'] is None
    assert result['fitness'] == {'status': 'no_fitness', 'samples': 0, 'value': None}
    assert not list(tmp_path.rglob('model.river'))
    with pytest.raises(ValueError, match='identity'):
        score_document(state(), tenant='')


def test_real_river_learns_distinct_feedback_and_roundtrips_model():
    scorer = RiverOnlineScorer(n_trees=3, seed=42)
    low = {'fresh': 0.0, 'specific': 0.0}
    high = {'fresh': 1.0, 'specific': 1.0}
    assert scorer.predict_one(low) is None
    first = scorer.learn_one(low, 0.0)
    assert first['score_before'] is None
    for _ in range(100):
        scorer.learn_one(low, 0.0)
        scorer.learn_one(high, 1.0)
    low_prediction, high_prediction = scorer.predict_one(low), scorer.predict_one(high)
    assert 0 <= low_prediction < high_prediction <= 1
    assert high_prediction - low_prediction > 0.25
    assert scorer.fitness()['samples'] == 200
    assert math.isfinite(scorer.fitness()['value'])
    restored = RiverOnlineScorer.import_state(scorer.export_state('tenant-a'), tenant='tenant-a')
    assert restored.examples_seen == 201
    assert restored.predict_one(low) == low_prediction
    assert restored.predict_one(high) == high_prediction
    assert restored.fitness() == scorer.fitness()
    with pytest.raises(ValueError, match='tenant'):
        RiverOnlineScorer.import_state(scorer.export_state('tenant-a'), tenant='tenant-b')


def test_online_feedback_persists_per_tenant_and_reloads_for_admission(settings, tmp_path):
    settings.EXTRACTION_SCORER_DIR = tmp_path
    for label in [1.0, 0.8, 0.9]:
        assert learn_document(state(), label, tenant='tenant-a')['updated']
    result = score_document(state(), tenant='tenant-a')
    assert result['status'] == 'trained'
    assert result['examples_seen'] == 3
    assert 0 <= result['score'] <= 1
    assert result['fitness']['samples'] == 2
    assert score_document(state(), tenant='tenant-b')['score'] is None
    assert len(list(tmp_path.rglob('model.river'))) == 1


def test_invalid_and_stale_feedback_cannot_change_model():
    scorer = RiverOnlineScorer(n_trees=1)
    with pytest.raises(ValueError, match='finite'):
        scorer.learn_one({}, math.nan)
    with pytest.raises(ValueError, match='unit'):
        scorer.learn_one({}, 1.0, sample_weight=2)
    old = datetime.now(timezone.utc) - timedelta(days=31)
    assert not scorer.learn_one({}, 1.0, timestamp=old)['updated']
    assert scorer.examples_seen == 0
