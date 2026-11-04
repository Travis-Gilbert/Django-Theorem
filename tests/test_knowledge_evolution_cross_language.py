"""A46: compiled Rust producer/consumer and the Python orchestration boundary.

Run through Theorem's scripts/check-knowledge-evolution-contract.py. This file
never builds a mock Rust implementation or calls an execution provider.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess

import pytest

from apps.orchestration.evolution_contract import EvolutionMessage, SCHEMA_PATH

FIXTURE = json.loads((Path(__file__).parents[1] / 'contracts/theorem.knowledge_evolution.v1.fixture.json').read_text())

@pytest.fixture(scope='module')
def rust_driver():
    binary = os.environ.get('THEOREM_EVOLUTION_CONTRACT_BIN')
    if not binary:
        pytest.skip('Rust/Python gate requires the compiled Rust contract driver')
    assert Path(binary).is_file(), 'configured Rust driver is unavailable'
    return binary

def invoke(binary, payload):
    return subprocess.run([binary], input=payload, capture_output=True, check=False, timeout=20)

@pytest.mark.parametrize('kind', ['request', 'result'])
def test_both_producers_round_trip_with_equal_identity(rust_driver, kind):
    python_produced = EvolutionMessage.parse(json.dumps(FIXTURE[kind], ensure_ascii=False).encode())
    response = invoke(rust_driver, python_produced.to_bytes())
    assert response.returncode == 0, response.stderr.decode()
    rust_produced = json.loads(response.stdout)
    python_consumed = EvolutionMessage.parse(json.dumps(rust_produced['message'], ensure_ascii=False).encode())
    assert json.loads(python_consumed.to_bytes()) == FIXTURE[kind]
    assert rust_produced['digest'] == python_produced.digest() == python_consumed.digest()

@pytest.mark.parametrize('field', list(FIXTURE['request']))
def test_rust_refuses_missing_mandatory_semantics(rust_driver, field):
    request = deepcopy(FIXTURE['request'])
    del request[field]
    assert invoke(rust_driver, json.dumps(request).encode()).returncode == 2

@pytest.mark.parametrize('field,value', [
    ('contract', 'theorem.knowledge_evolution.v2'),
    ('message_kind', 'activate'),
    ('attempt_epoch', 9007199254740993),
    ('attempt_epoch', '01'),
    ('base_revisions', [{'component_id':'method', 'revision':'latest'}]),
    ('allowed_mutation_surfaces', ['authorization']),
    ('principal_ref', {'tenant':'other'}),
    ('effective_grant', {'artifact_id':'known-id'}),
])
def test_rust_and_python_refuse_same_critical_variants(rust_driver, field, value):
    request = deepcopy(FIXTURE['request'])
    request[field] = value
    payload = json.dumps(request).encode()
    with pytest.raises(ValueError):
        EvolutionMessage.parse(payload)
    assert invoke(rust_driver, payload).returncode == 2

def test_contract_copy_is_exactly_the_rust_owned_schema(rust_driver):
    source = Path(os.environ['THEOREM_EVOLUTION_SCHEMA'])
    assert SCHEMA_PATH.read_bytes() == source.read_bytes()
