"""Celery task that builds a new tenant's store."""

from __future__ import annotations

import logging

from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.tenancy.fly import FlyApiError
from apps.tenancy.models import StorePlacement, Tenant
from apps.tenancy.provisioning import (
    BuildClaimed,
    ProvisioningError,
    begin_provisioning,
    client_from_settings,
    provision,
    provisioning_configured,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 5


@shared_task(name="apps.tenancy.tasks.provision_tenant_store", bind=True, max_retries=MAX_RETRIES)
def provision_tenant_store(self, tenant_id: str) -> dict:
    tenant = Tenant.objects.get(pk=tenant_id)
    placement = begin_provisioning(tenant)
    client = client_from_settings()
    try:
        # A retry keeps its task id, so it keeps the build it claimed.
        placement = provision(placement, client, claimant=f"task:{self.request.id}")
    except BuildClaimed:
        # The sweep can queue a second job while the first waits for a
        # worker; whichever starts second stands down here.
        logger.info("store build for %s is already running", tenant.slug)
        return {"tenant": tenant.slug, "store": placement.store.slug, "state": "claimed"}
    except FlyApiError as exc:
        # Fly answers 5xx and rate limits during incidents; try again with
        # backoff, and record the failure only when the retries run out.
        if self.request.retries < MAX_RETRIES:
            logger.warning("provisioning %s failed, retrying: %s", tenant.slug, exc)
            raise self.retry(exc=exc, countdown=30 * 2**self.request.retries) from exc
        _fail(placement, str(exc))
        raise
    except ProvisioningError as exc:
        _fail(placement, str(exc))
        raise
    finally:
        client.close()
    return {"tenant": tenant.slug, "store": placement.store.slug, "state": placement.state}


@shared_task(name="apps.tenancy.tasks.sweep_stranded_store_provisioning")
def sweep_stranded_store_provisioning() -> int:
    """Queue the build again for placements whose job was lost.

    Each attempt claims its placement as it starts, so a placement with no live
    claim that nothing has touched for ``STORE_PROVISION_STRANDED_AFTER_SECONDS``
    has no live job. A job still waiting for a worker may be queued twice; the
    build claim lets only one of them build.
    """
    if not provisioning_configured():
        return 0
    now = timezone.now()
    cutoff = now - timedelta(seconds=settings.STORE_PROVISION_STRANDED_AFTER_SECONDS)
    stranded = list(
        StorePlacement.objects.filter(state=StorePlacement.State.PROVISIONING, updated_at__lt=cutoff)
        .filter(Q(build_claimed_until__isnull=True) | Q(build_claimed_until__lt=now))
        .values_list("pk", "tenant_id")
    )
    for placement_id, tenant_id in stranded:
        # Queue first: if the broker refuses, the placement stays stale and the
        # next sweep tries again. Then touch it, so the next sweep leaves it
        # alone while the job waits for a worker.
        provision_tenant_store.delay(str(tenant_id))
        StorePlacement.objects.filter(pk=placement_id).update(updated_at=timezone.now())
    if stranded:
        logger.warning("queued %d stranded store builds", len(stranded))
    return len(stranded)


def _fail(placement: StorePlacement, error: str) -> None:
    placement.refresh_from_db()
    if placement.state == StorePlacement.State.PROVISIONING:
        placement.mark_failed(error)
