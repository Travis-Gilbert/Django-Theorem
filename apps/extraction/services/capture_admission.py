"""Document admission in the existing Django extraction/job ownership boundary.

Each checkpoint persists the complete resumable state before the next stage.
Generative claim decomposition is dispatched only by the escalation scheduler.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.orchestration.artifacts import ArtifactStore
from apps.orchestration.models import Job
from apps.tenancy.models import Tenant
from theorem_ml.executors.registry import observe_execution
from theorem_ml.extract.claims.decompose import Claim, Decomposer
from theorem_ml.extract.claims.verify import ClaimVerifier
from theorem_ml.extract.elements import DocElementTree
from theorem_ml.extract.layers import build_layers
from theorem_ml.extract.parse.router import ParserRouter, parse_text
from theorem_ml.extract.resolve import resolve_mentions
from theorem_ml.extract.spans.gliner2 import SpanExtractor
from theorem_ml.extract.spans.labels import SchemaClient
from theorem_ml.extract.temporal import source_time, temporal_properties

from ..models import Artifact, ExtractionRun

STAGES = ('Admit', 'Lift', 'Deduplicate', 'Score', 'Connect', 'Challenge', 'Validate', 'Distill')


def capture(tenant, *, text='', artifact_key='', filename='', source_url='',
            title='', metadata=None, store=None):
    if not tenant.is_active:
        raise ValueError('tenant is inactive')
    if bool(text) == bool(artifact_key):
        raise ValueError('supply exactly one of text or uploaded artifact_key')
    payload = text.encode() if text else (store or ArtifactStore.from_settings()).get_bytes(tenant.id, artifact_key)
    if not payload or len(payload) > settings.EXTRACTION_MAX_INPUT_BYTES:
        raise ValueError('capture is empty or exceeds EXTRACTION_MAX_INPUT_BYTES')
    if artifact_key and (not filename or Path(filename).name != filename):
        raise ValueError('an uploaded source requires a basename filename')
    digest = hashlib.sha256(payload).hexdigest()
    # Source URLs are provenance for uploaded bytes, never server-side fetches.
    with transaction.atomic():
        Tenant.objects.select_for_update().get(pk=tenant.pk)
        return Artifact.objects.get_or_create(tenant=tenant, source_sha256=digest, defaults={
            'source_text': text, 'artifact_key': artifact_key, 'filename': filename,
            'source_url': source_url, 'title': title, 'metadata': metadata or {},
            'capture_kind': 'url' if source_url else ('text' if text else 'file')})


def schedule(artifact, object_types, *, kind='admission', parent=None):
    if not object_types or any(not isinstance(t, str) or not t.strip() for t in object_types):
        raise ValueError('object_types must name at least one declared type')
    identity = json.dumps([str(artifact.id), sorted(set(object_types)), kind,
                           str(parent.id) if parent else None], separators=(',', ':'))
    operation_id = hashlib.sha256(identity.encode()).hexdigest()
    with transaction.atomic():
        Tenant.objects.select_for_update().get(pk=artifact.tenant_id)
        job, created = Job.objects.get_or_create(tenant_id=artifact.tenant_id, operation_id=operation_id,
            defaults={'operation': 'document.extraction.' + kind,
                      'input_payload_digest': artifact.source_sha256})
        if not created:
            run = job.document_extraction_run
            if job.status == 'failed':
                job.status, job.error, job.ended_at = 'queued', '', None
                job.save(update_fields=['status', 'error', 'ended_at'])
                run.status, run.error = 'queued', ''
                run.save(update_fields=['status', 'error', 'updated_at'])
                if kind == 'admission':
                    from ..document_tasks import admit_document
                    transaction.on_commit(lambda: admit_document.delay(str(run.id)))
            return run, False
        run = ExtractionRun.objects.create(artifact=artifact, orchestration_job=job,
            kind=kind, object_types=sorted(set(object_types)),
            ambiguity=parent.ambiguity if parent else [], output=parent.output if parent else {})
        if kind == 'admission':
            from ..document_tasks import admit_document
            transaction.on_commit(lambda: admit_document.delay(str(run.id)))
        return run, True


def _claim(value):
    return Claim(**{key: value[key] for key in Claim.__dataclass_fields__ if key in value})


class AdmissionPipeline:
    def __init__(self, *, schema=None, router=None, spans=None, decomposer=None, verifier=None, scorer=None):
        self.schema = schema or SchemaClient()
        self.router = router or ParserRouter()
        self.spans = spans or SpanExtractor(self.schema)
        self.decomposer = decomposer
        self.verifier = verifier
        self.scorer = scorer

    def execute(self, run_id):
        run = ExtractionRun.objects.select_related('artifact__tenant', 'orchestration_job').get(pk=run_id)
        if run.kind != 'admission':
            raise ValueError('admission cannot execute an escalation run')
        if not Job.objects.filter(pk=run.orchestration_job_id, status='queued').update(status='running', started_at=timezone.now()):
            return {'status': run.status, 'reused': True}
        state = dict(run.output)
        self.router.observer = lambda name, payload: self._observe(run, state, name, payload, self.router.paddle)
        self.spans.observer = lambda name, payload: self._observe(run, state, name, payload, self.spans)
        try:
            for stage in STAGES[len(run.stage_history):]:
                run.status = stage.lower()
                run.save(update_fields=['status', 'updated_at'])
                getattr(self, '_' + stage.lower())(run, state)
                run.stage_history.append({'stage': stage, 'completed_at': timezone.now().isoformat()})
                run.output = state
                run.save(update_fields=['stage_history', 'output', 'parser_receipt', 'ambiguity', 'updated_at'])
            with transaction.atomic():
                run.status = 'succeeded'
                run.save(update_fields=['status', 'updated_at'])
                Job.objects.filter(pk=run.orchestration_job_id).update(status='succeeded', ended_at=timezone.now())
                if run.ambiguity:
                    schedule(run.artifact, run.object_types, kind='claim_escalation', parent=run)
            return {'status': run.status, 'run_id': str(run.id)}
        except Exception as exc:
            run.status, run.error = 'failed', str(exc)
            # Preserve failed observations without advancing the stage checkpoint.
            run.output = state
            run.save(update_fields=['status', 'error', 'output', 'updated_at'])
            Job.objects.filter(pk=run.orchestration_job_id).update(status='failed', error=str(exc), ended_at=timezone.now())
            Artifact.objects.filter(pk=run.artifact_id).update(ingestion_status='failed')
            raise

    def _observe(self, run, state, name, payload, adapter):
        return observe_execution(name, {**payload, 'admission_run_id': str(run.id)},
            tenant=run.artifact.tenant.slug, state=state,
            evidence_class=getattr(adapter, 'evidence_class', 'runtime_observation'))

    def _admit(self, run, state):
        artifact = run.artifact
        if not artifact.tenant.is_active:
            raise ValueError('tenant is inactive')
        state['schema'] = {name: self.schema.effective_fields(artifact.tenant.slug, name) for name in run.object_types}
        state['observed_at'] = source_time(artifact.metadata, artifact.captured_at)

    def _lift(self, run, state):
        artifact = run.artifact
        source_id = f'artifact:{artifact.tenant.slug}:{artifact.id}'
        if artifact.source_text:
            parsed = parse_text(artifact.source_text, source_id)
        else:
            payload = ArtifactStore.from_settings().get_bytes(artifact.tenant_id, artifact.artifact_key)
            if hashlib.sha256(payload).hexdigest() != artifact.source_sha256:
                raise ValueError('uploaded artifact changed after capture')
            with TemporaryDirectory(prefix='theorem-document-') as directory:
                path = Path(directory) / artifact.filename
                path.write_bytes(payload)
                parsed = self.router.parse(path, source_id)
        if parsed.receipt.source_sha256 != artifact.source_sha256:
            raise ValueError('parser receipt does not match captured input')
        state['observed_at'] = source_time({**artifact.metadata, **parsed.metadata}, artifact.captured_at)
        tree = parsed.tree
        run.parser_receipt = parsed.receipt.to_dict()
        artifact.parsed_tree, artifact.ingestion_status = tree.to_dict(), 'parsed'
        artifact.save(update_fields=['parsed_tree', 'ingestion_status'])
        state['document'] = tree.to_dict()
        spans = self.spans.extract(tree, artifact.tenant.slug, run.object_types)
        state['spans'] = [s.to_dict() for s in spans.spans]
        state['classifications'] = spans.classifications
        state['records'] = {name: self.schema.call(artifact.tenant.slug, 'extract_fields',
            object_type=name, document=tree.to_dict(), spans=[s.global_span(tree) for s in spans.spans],
            observed_at=state['observed_at']) for name in run.object_types}
        # The second pass after resolution below attaches entity identities;
        # decomposition itself remains part of Lift, as specified.
        if any(s.get('derive_claims') for s in state['schema'].values()):
            self.decomposer = self.decomposer or Decomposer()
            result = self.decomposer.decompose(tree, derive_claims=True)
            state['claims'] = [c.to_dict() for c in result.claims]
            run.ambiguity = result.ambiguous
        else:
            state['claims'] = []

    def _deduplicate(self, run, state):
        mentions = [s for s in state['spans'] if s.get('target_object_type_id')]
        if mentions:
            with self._observe(run, state, 'resolve.link', {'spans': mentions}, self.schema):
                state['entities'] = resolve_mentions(mentions, tenant=run.artifact.tenant.slug, client=self.schema)
        else:
            state['entities'] = []
        if not any(schema.get('derive_claims') for schema in state['schema'].values()):
            state['claims'] = []
            return
        if self.decomposer is None and state['claims']:
            self.decomposer = Decomposer()
        if self.decomposer is not None:
            result = self.decomposer.decompose(DocElementTree.from_dict(state['document']),
                derive_claims=True, resolved_entities=state['entities'],
                declared_predicates=[f['key'] for schema in state['schema'].values() for f in schema['fields']])
            state['claims'] = [c.to_dict() for c in result.claims]
            run.ambiguity = result.ambiguous

    def _score(self, run, state):
        if self.scorer is None:
            from theorem_ml.extract.scoring import score_document
            self.scorer = score_document
        state['score'] = self.scorer(state, tenant=run.artifact.tenant.slug)

    def _connect(self, run, state):
        temporal = temporal_properties(state['observed_at'])
        state['entities'] = [{**e, **temporal} for e in state['entities']]
        state['claims'] = [{**c, **temporal, 'tenant_id': run.artifact.tenant.slug} for c in state['claims']]
        state['pending_relations'] = [e['pending_relation'] for e in state['entities'] if not e['entity_id']]

    def _challenge(self, run, state):
        # Candidate contradiction keys are prepared here; the Rust publication
        # rechecks current open facts and MiniCheck evidence inside its CAS.
        exclusive = {field['key'] for schema in state['schema'].values() for field in schema['fields']
                     if field.get('field_type', {}).get('kind') not in {'relation', 'enum_many'}}
        for claim in state['claims']:
            claim['exclusive_predicate'] = claim.get('predicate') in exclusive

    def _validate(self, run, state):
        if not state['claims']:
            return
        cutoff = float(run.artifact.metadata.get('minicheck_cutoff', 0.5))
        verifier = self.verifier or ClaimVerifier(cutoff=cutoff)
        with self._observe(run, state, 'claims.verify.minicheck',
                           {'document': state['document'], 'claims': state['claims'], 'cutoff': cutoff}, verifier):
            verified = verifier.verify(DocElementTree.from_dict(state['document']), [_claim(c) for c in state['claims']])
            state['claims'] = [{**old, **new.to_dict()}
                               for old, new in zip(state['claims'], verified, strict=True)]

    def _distill(self, run, state):
        tree = DocElementTree.from_dict(state['document'])
        fields = {}
        for type_name, record in state['records'].items():
            for attempt in record['attempts']:
                span = attempt.get('span')
                if not span:
                    continue
                owner = next((e for e in tree.elements if e.byte_start <= span['byte_start'] < e.byte_end), None)
                key = attempt.get('field', attempt.get('field_key'))
                if owner and key in record['properties']:
                    fields[f'{type_name}.{key}'] = {'element_id': owner.id, 'value': record['properties'][key]}
        layers = build_layers(tree, fields=fields, claims=state['claims'], entities=state['entities'])
        state['publication'] = self.schema.call(run.artifact.tenant.slug, 'publish', source_id=tree.source_id,
            document=tree.to_dict(), claims=state['claims'], entities=state['entities'],
            pending_relations=state['pending_relations'], layers=layers, observed_at=state['observed_at'])
        run.artifact.layers, run.artifact.ingestion_status = layers, 'extracted'
        run.artifact.save(update_fields=['layers', 'ingestion_status'])
