"""Tenant-local online admission scoring using the existing River scorer.

Source: Travis-Gilbert/Theseus, apps/notebook/river_scorer.py at
509e2c496405f1e25a79acda220294e9656aef58. The ARFRegressor constructor,
feature projection, predict/learn loop, and versioned model persistence are
adapted from RiverOnlineScorer. Provider/connection/federation dependencies are
removed. Cold-start predictions are unavailable instead of upstream's 0.5;
tenant isolation, atomic persistence, missing-feature masks, and prequential
fitness accounting are added here.

Freshness uses the existing 30-day formula from search/native/index.py at
1c61cab3bbd4cb67ab018f3d6cab1c8834a84c54. Other measurements are explicit below.
These admission features differ from connection features: existing connection
checkpoints must not be loaded as admission models. Training requires actual
feedback, never labels derived from the model's own score.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import math
import os
from pathlib import Path
import pickle
import tempfile


UPSTREAM_REVISION = '509e2c496405f1e25a79acda220294e9656aef58'
FEATURES = ('fresh', 'independent', 'specific', 'reliable', 'useful')
FEATURE_ORDER = tuple(FEATURES) + tuple(f'{name}_missing' for name in FEATURES)
STATE_HEADER = b'THEOREM_ADMISSION_RIVER_V1\n'


def _finite(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _datetime(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc)


def document_features(state: dict, *, now: datetime | None = None) -> dict:
    """Return measured inputs and explicit unknowns, never invented priors.

    Specificity means the fraction of declared fields populated by extraction;
    reliability is the fraction of claims passing a completed verifier call.
    Neither claims to measure general source truth. Independence needs separate
    source-lineage evidence and usefulness needs outcome feedback; the current
    admission state provides neither, so both remain unknown.
    """
    now = now or datetime.now(timezone.utc)
    observed = _datetime(state.get('observed_at'))
    values = dict.fromkeys(FEATURES)
    evidence = {}
    if observed is not None:
        age = max((now - observed).total_seconds() / 86400, 0.0)
        values['fresh'] = round(1.0 / (1.0 + age / 30.0), 6)
        evidence['fresh'] = {'measurement': 'source_age_days', 'value': age}
    declared = set()
    for type_name, schema in state.get('schema', {}).items():
        for field in schema.get('fields', []):
            if field.get('key'):
                declared.add((type_name, field['key']))
    populated = set()
    for type_name, record in state.get('records', {}).items():
        for field, value in record.get('properties', {}).items():
            if (type_name, field) in declared and value is not None:
                populated.add((type_name, field))
    if declared:
        values['specific'] = len(populated) / len(declared)
        evidence['specific'] = {'measurement': 'populated_declared_fields',
                                'populated': len(populated), 'declared': len(declared)}
    # Decomposition initializes verified=False before MiniCheck runs; a boolean
    # alone is not evidence of completed verification. Require its finite score.
    scored_claims = [claim for claim in state.get('claims', [])
                     if _finite(claim.get('score')) is not None and isinstance(claim.get('verified'), bool)]
    if scored_claims:
        accepted = sum(claim['verified'] for claim in scored_claims)
        values['reliable'] = accepted / len(scored_claims)
        evidence['reliable'] = {'measurement': 'verified_claim_fraction',
                                'accepted': accepted, 'scored': len(scored_claims)}
    return {'values': values, 'evidence': evidence,
            'missing': [key for key, value in values.items() if value is None]}


class RiverOnlineScorer:
    """The existing ARFRegressor wrapper, scoped to admission measurements."""

    def __init__(self, *, feature_order=FEATURE_ORDER, n_trees: int = 10, seed: int = 42):
        from river import forest

        self.feature_order = list(feature_order)
        self.n_trees = int(max(1, n_trees))
        self.seed = int(seed)
        self.model = forest.ARFRegressor(n_models=self.n_trees, seed=self.seed)
        self.examples_seen = 0
        self.last_updated_at = None
        self.fitness_samples = 0
        self.squared_error = 0.0

    def _to_sample(self, feature_vector: dict) -> dict[str, float]:
        sample = {}
        for name in FEATURES:
            value = _finite(feature_vector.get(name))
            if value is not None and not 0 <= value <= 1:
                raise ValueError(f'{name} must be in [0, 1]')
            sample[name] = value if value is not None else -1.0
            sample[f'{name}_missing'] = float(value is None)
        return {key: sample[key] for key in self.feature_order}

    def predict_one(self, feature_vector: dict) -> float | None:
        sample = self._to_sample(feature_vector)
        if self.examples_seen == 0:
            return None
        prediction = _finite(self.model.predict_one(sample))
        if prediction is None:
            raise ValueError('River returned a non-finite prediction')
        return max(0.0, min(1.0, prediction))

    def learn_one(self, feature_vector: dict, label: float, *, timestamp=None,
                  sample_weight: float = 1.0) -> dict:
        """Update from observed feedback; report pre-update prediction error."""
        label = _finite(label)
        if label is None or not 0 <= label <= 1:
            raise ValueError('feedback label must be finite and in [0, 1]')
        # The pinned River forest performs its own Poisson weighting; its
        # **kwargs parameter does not forward sample_weight to the trees.
        # Refuse unsupported weights rather than silently pretend they apply.
        if sample_weight != 1.0:
            raise ValueError('this River forest supports unit feedback weights only')
        if timestamp is not None:
            observed = _datetime(timestamp)
            if observed is None:
                raise ValueError('feedback timestamp must include a timezone')
            if (datetime.now(timezone.utc) - observed).total_seconds() > 30 * 86400:
                return {'updated': False, 'discarded_old_sample': True}
        before = self.predict_one(feature_vector)
        self.model.learn_one(self._to_sample(feature_vector), label)
        self.examples_seen += 1
        self.last_updated_at = datetime.now(timezone.utc).isoformat()
        if before is not None:
            self.fitness_samples += 1
            self.squared_error += (before - label) ** 2
        after = self.predict_one(feature_vector)
        return {'updated': True, 'discarded_old_sample': False,
                'score_before': before, 'score_after': after,
                'loss_delta': None if before is None else (before - label) ** 2 - (after - label) ** 2}

    def fitness(self) -> dict:
        if not self.fitness_samples:
            return {'status': 'no_fitness', 'samples': 0, 'value': None}
        return {'status': 'measured', 'kind': 'prequential_mse',
                'samples': self.fitness_samples, 'value': self.squared_error / self.fitness_samples}

    def export_state(self, tenant: str) -> bytes:
        payload = {'version': 1, 'tenant': tenant, 'feature_order': self.feature_order,
                   'n_trees': self.n_trees, 'seed': self.seed, 'examples_seen': self.examples_seen,
                   'last_updated_at': self.last_updated_at, 'model': self.model,
                   'fitness_samples': self.fitness_samples, 'squared_error': self.squared_error}
        return STATE_HEADER + pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)

    @classmethod
    def import_state(cls, data: bytes, *, tenant: str):
        """Load only this service's trusted local model files, never uploads."""
        from river.forest import ARFRegressor

        if not data.startswith(STATE_HEADER):
            raise ValueError('invalid admission scorer header')
        payload = pickle.loads(data[len(STATE_HEADER):])
        if payload.get('version') != 1 or payload.get('tenant') != tenant:
            raise ValueError('incompatible scorer version or tenant')
        if payload.get('feature_order') != list(FEATURE_ORDER):
            raise ValueError('incompatible admission feature schema')
        if not isinstance(payload['model'], ARFRegressor):
            raise ValueError('stored model is not the declared River forest')
        scorer = cls(feature_order=payload['feature_order'], n_trees=payload['n_trees'], seed=payload['seed'])
        scorer.model = payload['model']
        scorer.examples_seen = int(payload['examples_seen'])
        scorer.last_updated_at = payload['last_updated_at']
        scorer.fitness_samples = int(payload['fitness_samples'])
        scorer.squared_error = float(payload['squared_error'])
        return scorer


def _tenant(tenant: str) -> str:
    if not isinstance(tenant, str) or not tenant.strip():
        raise ValueError('verified tenant identity is required')
    return tenant


def _store_root() -> Path:
    from django.conf import settings

    if settings.configured and hasattr(settings, 'EXTRACTION_SCORER_DIR'):
        return Path(settings.EXTRACTION_SCORER_DIR)
    return Path(os.environ.get('THEOREM_EXTRACTION_SCORER_DIR', Path.home() / '.theorem' / 'extraction-scorers'))


@contextmanager
def _locked_model(tenant: str):
    tenant = _tenant(tenant)
    directory = _store_root() / hashlib.sha256(tenant.encode()).hexdigest()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / 'model.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield directory / 'model.river'
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _save(scorer: RiverOnlineScorer, path: Path, tenant: str):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.model-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(scorer.export_state(tenant))
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def score_document(state: dict, *, tenant: str) -> dict:
    features = document_features(state)
    with _locked_model(tenant) as path:
        if not path.exists():
            return {'executor': 'admission.river', 'status': 'untrained', 'score': None,
                    'examples_seen': 0, 'fitness': {'status': 'no_fitness', 'samples': 0, 'value': None},
                    'features': features}
        scorer = RiverOnlineScorer.import_state(path.read_bytes(), tenant=tenant)
        score = scorer.predict_one(features['values'])
        return {'executor': 'admission.river', 'status': 'trained' if score is not None else 'untrained',
                'score': score, 'examples_seen': scorer.examples_seen,
                'fitness': scorer.fitness(), 'features': features}


def learn_document(state: dict, label: float, *, tenant: str, timestamp=None) -> dict:
    """Train only when the owner supplies actual outcome feedback."""
    features = document_features(state)
    with _locked_model(tenant) as path:
        scorer = (RiverOnlineScorer.import_state(path.read_bytes(), tenant=tenant)
                  if path.exists() else RiverOnlineScorer())
        outcome = scorer.learn_one(features['values'], label, timestamp=timestamp)
        if outcome['updated']:
            _save(scorer, path, tenant)
        return {**outcome, 'examples_seen': scorer.examples_seen, 'fitness': scorer.fitness()}
