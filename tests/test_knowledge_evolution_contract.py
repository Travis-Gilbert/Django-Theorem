"""A46 contract portion: real Python boundary, shared Rust-owned corpus."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path

import pytest

from apps.orchestration.evolution_contract import EvolutionMessage, ContractError

FIXTURE = json.loads((Path(__file__).parents[1] / 'contracts/theorem.knowledge_evolution.v1.fixture.json').read_text())

@pytest.mark.parametrize('kind', ['request', 'result'])
def test_round_trip_preserves_every_field(kind):
    value = FIXTURE[kind]
    message = EvolutionMessage.parse(json.dumps(value, ensure_ascii=False).encode())
    assert json.loads(message.to_bytes()) == value
    assert json.loads(message.to_bytes())['attempt_epoch'] == '9007199254740993'

@pytest.mark.parametrize('pointer,value', [
    (['contract'], 'theorem.knowledge_evolution.v2'),
    (['message_kind'], 'adopt'),
    (['attempt_epoch'], 9007199254740993),
    (['attempt_epoch'], '01'),
    (['base_revisions', 0, 'revision'], 'latest'),
    (['engines', 'recuris', 'source_revision'], 'main'),
    (['budget', 'model_calls'], None),
    (['budget', 'concurrency'], '0'),
    (['budget', 'cost_microusd'], '-1'),
    (['allowed_mutation_surfaces'], ['authorization']),
    (['effective_grant', 'payload_digest'], 'sha256:forged'),
    (['effective_grant', 'download_url'], 'https://secret.test/token'),
    (['tenant_slug'], 'other-tenant'),
])
def test_malformed_or_unknown_critical_inputs_refuse(pointer, value):
    message = deepcopy(FIXTURE['request'])
    current = message
    for part in pointer[:-1]:
        current = current[part]
    current[pointer[-1]] = value
    with pytest.raises(ContractError):
        EvolutionMessage.parse(json.dumps(message).encode())

@pytest.mark.parametrize('field', list(FIXTURE['request']))
def test_every_required_request_field_is_required(field):
    message = deepcopy(FIXTURE['request'])
    del message[field]
    with pytest.raises(ContractError):
        EvolutionMessage.parse(json.dumps(message).encode())

def test_result_binding_refuses_stale_attempt_and_altered_artifact():
    request = EvolutionMessage.parse(json.dumps(FIXTURE['request']).encode())
    result = EvolutionMessage.parse(json.dumps(FIXTURE['result']).encode())
    result.bind_to(request)
    for field, value in [('attempt_epoch', '1'), ('request_digest', 'sha256:'+'0'*64), ('environment_snapshot', FIXTURE['result']['coverage'])]:
        changed = deepcopy(FIXTURE['result'])
        changed[field] = value
        with pytest.raises(ContractError):
            EvolutionMessage.parse(json.dumps(changed).encode()).bind_to(request)

def test_ready_candidate_and_proof_cannot_omit_required_artifacts():
    for field, value in [('outcome', 'candidate_ready'), ('proof_outcome', 'proved')]:
        result = deepcopy(FIXTURE['result'])
        result[field] = value
        with pytest.raises(ContractError):
            EvolutionMessage.parse(json.dumps(result).encode())

@pytest.mark.parametrize('payload', [b'{private-value', b' ' * (4 * 1024 * 1024 + 1), json.dumps(FIXTURE['request']).encode('utf-16')])
def test_malformed_oversized_and_non_utf8_messages_refuse_without_echo(payload):
    with pytest.raises(ContractError) as error:
        EvolutionMessage.parse(payload)
    assert 'private-value' not in str(error.value)

def test_wrong_direction_cannot_bind():
    request = EvolutionMessage.parse(json.dumps(FIXTURE['request']).encode())
    result = EvolutionMessage.parse(json.dumps(FIXTURE['result']).encode())
    for left, right in [(request, request), (result, result), (request, result)]:
        with pytest.raises(ContractError):
            left.bind_to(right)

def test_exact_byte_limit_is_accepted():
    payload = json.dumps(FIXTURE['request']).encode()
    payload += b' ' * (4 * 1024 * 1024 - len(payload))
    assert json.loads(EvolutionMessage.parse(payload).to_bytes()) == FIXTURE['request']

def test_validated_message_cannot_be_replaced_without_validation():
    message = EvolutionMessage.parse(json.dumps(FIXTURE['request']).encode())
    with pytest.raises(FrozenInstanceError):
        message._payload = b'{}'

@pytest.mark.parametrize('field', ['operation_id', 'campaign_id', 'attempt_epoch', 'base_revisions', 'protocol', 'environment_snapshot', 'engines'])
def test_binding_rejects_each_inconsistent_result_identity(field):
    changed = deepcopy(FIXTURE['result'])
    if field in ('operation_id', 'campaign_id', 'attempt_epoch'):
        changed[field] = '1'
    elif field == 'base_revisions':
        changed[field][0]['revision'] = 'sha256:'+'0'*64
    elif field == 'engines':
        changed[field]['harness2']['source_revision'] = '0'*40
    else:
        changed[field]['payload_digest'] = 'sha256:'+'0'*64
    request = EvolutionMessage.parse(json.dumps(FIXTURE['request']).encode())
    with pytest.raises(ContractError, match='binding_mismatch:'+field):
        EvolutionMessage.parse(json.dumps(changed).encode()).bind_to(request)

@pytest.mark.parametrize('kind', ['request', 'result'])
def test_operation_identity_fits_existing_job_owner(kind):
    payload = deepcopy(FIXTURE[kind])
    payload['operation_id'] = 'x' * 129
    with pytest.raises(ContractError):
        EvolutionMessage.parse(json.dumps(payload).encode())
