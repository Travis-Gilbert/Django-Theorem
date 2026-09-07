"""Durable document jobs and a separate, scheduled ambiguity batch."""
from __future__ import annotations

from decimal import Decimal

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.orchestration.models import Job
from theorem_ml.extract.claims.escalate import BatchDecomposer, FrontierBinding
from theorem_ml.extract.claims.verify import ClaimVerifier
from theorem_ml.extract.elements import DocElementTree
from theorem_ml.extract.temporal import temporal_properties

from .models import ExtractionRun
from .services.capture_admission import AdmissionPipeline


@shared_task(name='apps.extraction.document_tasks.admit_document')
def admit_document(run_id):
    return AdmissionPipeline().execute(run_id)


@shared_task(name='apps.extraction.document_tasks.sweep_document_jobs')
def sweep_document_jobs():
    counts = {'admission': 0, 'claim_escalation': 0}
    for run in ExtractionRun.objects.filter(orchestration_job__status='queued').only('id', 'kind'):
        task = admit_document if run.kind == 'admission' else escalate_claims
        task.delay(str(run.id))
        counts[run.kind] += 1
    return counts


@shared_task(name='apps.extraction.document_tasks.escalate_claims')
def escalate_claims(run_id):
    run = ExtractionRun.objects.select_related('artifact__tenant').get(pk=run_id)
    if run.kind != 'claim_escalation':
        raise ValueError('generative batch requires its own claim_escalation run')
    if not Job.objects.filter(pk=run.orchestration_job_id, status='queued').update(status='running', started_at=timezone.now()):
        return {'status': run.status, 'reused': True}
    try:
        bindings = [FrontierBinding(name=item['name'], model=item['model'], endpoint=item['endpoint'],
                    api_key=item['api_key'], batch_price=Decimal(str(item['batch_price'])))
                    for item in settings.EXTRACTION_FRONTIER_BINDINGS]
        tree = DocElementTree.from_dict(run.output['document'])
        batch = BatchDecomposer(bindings)
        run.status = 'decomposing'
        run.save(update_fields=['status', 'updated_at'])
        claims = batch.decompose(tree, run.ambiguity, run_kind=run.kind)
        verifier = ClaimVerifier(cutoff=float(run.artifact.metadata.get('minicheck_cutoff', 0.5)))
        pipeline = AdmissionPipeline()
        verified = []
        if claims:
            with pipeline._observe(run, run.output, 'claims.verify.minicheck',
                    {'document': tree.to_dict(), 'claims': [c.to_dict() for c in claims],
                     'cutoff': verifier.cutoff}, verifier):
                verified = verifier.verify(tree, claims)
        # Mentions use the same already resolved/pending span records as the
        # deterministic pass; no generated entity name becomes a graph ID.
        temporal = temporal_properties(run.output['observed_at'])
        existing = {c['id']: c for c in run.output['claims']}
        for claim in verified:
            claim.entity_mentions = [e for e in run.output['entities'] if e['element_id'] == claim.element_id
                                     and claim.byte_start <= e['byte_start'] < claim.byte_end]
            existing.setdefault(claim.id, {**claim.to_dict(), **temporal, 'tenant_id': run.artifact.tenant.slug,
                                          'exclusive_predicate': False})
        run.output['claims'] = list(existing.values())
        pipeline._distill(run, run.output)
        with transaction.atomic():
            run.status = 'succeeded'
            run.stage_history = [{'stage': 'DecomposeBatch', 'completed_at': timezone.now().isoformat()},
                                 {'stage': 'Validate', 'completed_at': timezone.now().isoformat()},
                                 {'stage': 'Distill', 'completed_at': timezone.now().isoformat()}]
            run.save(update_fields=['status', 'stage_history', 'output', 'updated_at'])
            Job.objects.filter(pk=run.orchestration_job_id).update(status='succeeded', ended_at=timezone.now())
        return {'status': 'succeeded', 'claims': len(verified)}
    except Exception as exc:
        run.status, run.error = 'failed', str(exc)
        run.save(update_fields=['status', 'error', 'output', 'updated_at'])
        Job.objects.filter(pk=run.orchestration_job_id).update(status='failed', error=str(exc), ended_at=timezone.now())
        raise
