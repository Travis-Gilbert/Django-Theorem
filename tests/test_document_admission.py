"""Persisted local admission contracts; model/MCP stand-ins are named fixtures."""
from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError
from django.test import Client

from apps.extraction.models import Artifact, ExtractionRun
from apps.extraction.services.capture_admission import (
    STAGES,
    AdmissionPipeline,
    capture,
    schedule,
)
from apps.keys.mint import mint_api_key
from apps.orchestration.models import Job
from apps.tenancy.models import Tenant
from theorem_ml.extract.claims.decompose import Decomposition


class FixtureSchemaClient:
    def __init__(self, *, derive_claims=False):
        self.derive_claims = derive_claims
        self.calls = []

    def effective_fields(self, tenant, object_type):
        return {'fields': [{'key': 'deadline', 'field_type': {'kind': 'date'}}],
                'derive_claims': self.derive_claims}

    def call(self, tenant, action, **args):
        self.calls.append((tenant, action, args))
        if action == 'extract_fields':
            return {'properties': {}, 'attempts': []}
        assert action == 'publish'
        return {'published': True}


class FixtureSpanExtractor:
    def extract(self, tree, tenant, object_types):
        return SimpleNamespace(spans=[], classifications={})


class FixtureAmbiguousDecomposer:
    def decompose(self, tree, **kwargs):
        return Decomposition([], [{'element_id': tree.elements[0].id, 'quote': tree.text,
                                   'reason': 'finite_verb_without_claim'}])


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug='admission-a', display_name='Admission')


def fixture_pipeline(schema=None, **kwargs):
    return AdmissionPipeline(schema=schema or FixtureSchemaClient(), spans=FixtureSpanExtractor(),
                             scorer=lambda state, tenant: {'status': 'fixture_only', 'score': None}, **kwargs)


def test_capture_is_idempotent_tenant_owned_and_immutable(tenant):
    first, created = capture(tenant, text='A document.')
    assert created
    again, created = capture(tenant, text='A document.')
    assert again.id == first.id and not created
    other = Tenant.objects.create(slug='admission-b', display_name='B')
    assert capture(other, text='A document.')[0].id != first.id
    first.source_text = 'Changed'
    with pytest.raises(ValidationError):
        first.save()


def test_all_eight_stages_order_once_and_persist_summary(tenant):
    artifact, _ = capture(tenant, text='Rent is 1450 dollars.')
    run, _ = schedule(artifact, ['Lease'])
    pipeline = fixture_pipeline()
    pipeline.execute(run.id)
    run.refresh_from_db()
    assert [stage['stage'] for stage in run.stage_history] == list(STAGES)
    assert run.status == 'succeeded'
    artifact.refresh_from_db()
    assert artifact.ingestion_status == 'extracted' and artifact.layers
    before = list(run.stage_history)
    assert pipeline.execute(run.id)['reused']
    run.refresh_from_db()
    assert run.stage_history == before
    assert schedule(artifact, ['Lease'])[0].id == run.id


def test_failure_checkpoint_resume_skips_completed_stages(tenant):
    artifact, _ = capture(tenant, text='Rent is 1450 dollars.')
    run, _ = schedule(artifact, ['Lease'])
    pipeline = fixture_pipeline()
    def fail_score(state, tenant):
        raise RuntimeError('fixture scorer unavailable')
    pipeline.scorer = fail_score
    with pytest.raises(RuntimeError):
        pipeline.execute(run.id)
    run.refresh_from_db()
    assert [s['stage'] for s in run.stage_history] == list(STAGES[:3])
    Job.objects.filter(pk=run.orchestration_job_id).update(status='queued')
    pipeline.scorer = lambda state, tenant: {'status': 'fixture_only'}
    pipeline.execute(run.id)
    run.refresh_from_db()
    assert [s['stage'] for s in run.stage_history] == list(STAGES)
    assert sum(action == 'extract_fields' for _, action, _ in pipeline.schema.calls) == 1


def test_ambiguity_is_queued_as_separate_run_without_generating(tenant, monkeypatch):
    from apps.extraction.document_tasks import escalate_claims
    dispatched = []
    monkeypatch.setattr(escalate_claims, 'delay', lambda *args: dispatched.append(args))
    artifact, _ = capture(tenant, text='The deadline moved.')
    run, _ = schedule(artifact, ['Lease'])
    fixture_pipeline(FixtureSchemaClient(derive_claims=True), decomposer=FixtureAmbiguousDecomposer()).execute(run.id)
    batch = ExtractionRun.objects.get(artifact=artifact, kind='claim_escalation')
    assert batch.id != run.id and batch.status == 'queued' and batch.ambiguity
    assert not dispatched


def test_api_admits_tenant_and_denies_other_artifact(tenant):
    key = mint_api_key(tenant, scopes=['extraction:*'])
    client = Client(HTTP_AUTHORIZATION='Bearer ' + key.plaintext)
    captured = client.post('/internal/extraction/artifacts', data={'text': 'A source.'}, content_type='application/json')
    assert captured.status_code == 200, captured.content
    other = Tenant.objects.create(slug='other', display_name='Other')
    artifact, _ = capture(other, text='Other source.')
    denied = client.post(f'/internal/extraction/artifacts/{artifact.id}/extract',
                         data={'object_types': ['Lease']}, content_type='application/json')
    assert denied.status_code == 404
    unauthenticated = Client().post('/internal/extraction/artifacts', data={'text': 'Bad'}, content_type='application/json')
    assert unauthenticated.status_code in {401, 403}
    assert Artifact.objects.filter(tenant=tenant).count() == 1


def test_failed_run_is_rescheduled_without_repeating_completed_stages(tenant):
    artifact, _ = capture(tenant, text='Retry this source.')
    run, _ = schedule(artifact, ['Lease'])
    pipeline = fixture_pipeline()
    pipeline.scorer = lambda state, tenant: (_ for _ in ()).throw(RuntimeError('transient fixture failure'))
    with pytest.raises(RuntimeError):
        pipeline.execute(run.id)
    artifact.refresh_from_db()
    assert artifact.ingestion_status == 'failed'
    resumed, created = schedule(artifact, ['Lease'])
    assert resumed.id == run.id and not created and resumed.status == 'queued'
    pipeline.scorer = lambda state, tenant: {'status': 'fixture_only'}
    pipeline.execute(resumed.id)
    run.refresh_from_db()
    assert run.status == 'succeeded'
    assert sum(action == 'extract_fields' for _, action, _ in pipeline.schema.calls) == 1


def test_supplied_decomposer_cannot_bypass_current_types_opt_in(tenant):
    class ForbiddenDecomposer:
        def decompose(self, *args, **kwargs):
            pytest.fail('A type without derive_claims must never decompose')
    artifact, _ = capture(tenant, text='This sentence has a verb.')
    run, _ = schedule(artifact, ['Lease'])
    pipeline = fixture_pipeline(FixtureSchemaClient(derive_claims=False), decomposer=ForbiddenDecomposer())
    pipeline.execute(run.id)
    run.refresh_from_db()
    assert run.output['claims'] == []
