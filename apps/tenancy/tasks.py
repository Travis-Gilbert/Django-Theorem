"""Celery tasks that build a new tenant's store and coding environment."""

from __future__ import annotations

import logging

from celery import shared_task
from sprites import SpriteError

from apps.tenancy.environment import (
    EnvironmentProvisioningError,
    begin_environment,
    bind_store,
    provision_environment,
    sprites_client_from_settings,
)
from apps.tenancy.fly import FlyApiError
from apps.tenancy.models import EnvironmentHost, StorePlacement, Tenant
from apps.tenancy.provisioning import (
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
        placement = provision(placement, client)
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


def _fail(placement: StorePlacement, error: str) -> None:
    placement.refresh_from_db()
    if placement.state == StorePlacement.State.PROVISIONING:
        placement.mark_failed(error)


@shared_task(
    name="apps.tenancy.tasks.provision_tenant_environment", bind=True, max_retries=MAX_RETRIES
)
def provision_tenant_environment(self, tenant_id: str) -> dict:
    tenant = Tenant.objects.get(pk=tenant_id)
    host = begin_environment(tenant)
    sprites = sprites_client_from_settings()
    try:
        host = provision_environment(host, sprites)
        bound = False
        if provisioning_configured():
            fly = client_from_settings()
            try:
                bound = bind_store(tenant, fly)
            finally:
                fly.close()
    except (SpriteError, FlyApiError) as exc:
        if self.request.retries < MAX_RETRIES:
            logger.warning("environment for %s failed, retrying: %s", tenant.slug, exc)
            raise self.retry(exc=exc, countdown=30 * 2**self.request.retries) from exc
        _fail_environment(host, str(exc))
        raise
    except EnvironmentProvisioningError as exc:
        _fail_environment(host, str(exc))
        raise
    finally:
        sprites.close()
    return {"tenant": tenant.slug, "sprite": host.sprite, "state": host.state, "store_bound": bound}


def _fail_environment(host: EnvironmentHost, error: str) -> None:
    host.refresh_from_db()
    if host.state == EnvironmentHost.State.PROVISIONING:
        host.mark_failed(error)
