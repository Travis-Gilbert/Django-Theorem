"""Build a tenant's store on Fly now, or retry one that failed.

    manage.py provision_tenant_store <tenant-slug>

Signup runs the same steps in the background. Run this to provision a tenant
created before provisioning was configured, or to retry a ``failed``
placement; every step finds what an earlier attempt already made.
"""

from __future__ import annotations

from uuid import uuid4

from django.core.management.base import BaseCommand, CommandError

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


class Command(BaseCommand):
    help = "Provision a tenant's RustyRed store on Fly and place the tenant on it."

    def add_arguments(self, parser):
        parser.add_argument("tenant", help="Tenant slug")

    def handle(self, *args, tenant: str, **options):
        if not provisioning_configured():
            raise CommandError(
                "set FLY_API_TOKEN, STORE_IMAGE and STORE_SESSION_VERIFYING_KEYS_HEX first"
            )
        try:
            row = Tenant.objects.get(slug=tenant)
        except Tenant.DoesNotExist as error:
            raise CommandError(f"no tenant {tenant}") from error
        placement = begin_provisioning(row)
        if placement.state == StorePlacement.State.FAILED:
            placement.retry_provisioning()
        client = client_from_settings()
        try:
            placement = provision(placement, client, claimant=f"command:{uuid4().hex}")
        except BuildClaimed as error:
            raise CommandError(f"{error}; wait for it, or for its claim to lapse") from error
        except (FlyApiError, ProvisioningError) as error:
            placement.refresh_from_db()
            if placement.state == StorePlacement.State.PROVISIONING:
                placement.mark_failed(str(error))
            raise CommandError(str(error)) from error
        finally:
            client.close()
        self.stdout.write(f"{placement} (epoch {placement.epoch})")
