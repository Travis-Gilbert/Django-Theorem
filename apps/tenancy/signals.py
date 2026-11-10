"""Start building a store for every tenant signup creates."""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.tenancy.models import Tenant
from apps.tenancy.provisioning import begin_provisioning, provisioning_configured

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Tenant, dispatch_uid="tenancy.provision_store_on_signup")
def provision_store_on_signup(sender, instance: Tenant, created: bool, raw: bool = False, **kwargs):
    if not created or raw:
        return
    if not provisioning_configured():
        logger.warning(
            "tenant %s has no store: set FLY_API_TOKEN, STORE_IMAGE and "
            "STORE_SESSION_VERIFYING_KEYS_HEX to provision one",
            instance.slug,
        )
        return
    begin_provisioning(instance)

    from apps.tenancy.tasks import provision_tenant_store

    tenant_id = str(instance.pk)
    # robust: a broker outage must not fail signup. The placement row already
    # records the pending build, and the stranded-provisioning sweep queues it.
    transaction.on_commit(lambda: provision_tenant_store.delay(tenant_id), robust=True)
