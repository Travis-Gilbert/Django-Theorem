"""Build a tenant's coding environment now, retry a failed one, or bring a
serving one up to the pinned host binary.

    manage.py provision_tenant_environment <tenant-slug>

Signup runs the same steps in the background. Run this for a tenant created
before environments were configured, after a ``failed`` run, or after moving
``ENVIRONMENT_HOST_URL`` and ``ENVIRONMENT_HOST_SHA256`` to a new build. When
the tenant's store serves, it is bound to the environment too.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from sprites import SpriteError

from apps.tenancy.environment import (
    EnvironmentProvisioningError,
    begin_environment,
    bind_store,
    environment_configured,
    provision_environment,
    sprites_client_from_settings,
)
from apps.tenancy.fly import FlyApiError
from apps.tenancy.models import EnvironmentHost, Tenant
from apps.tenancy.provisioning import client_from_settings, provisioning_configured


class Command(BaseCommand):
    help = "Provision a tenant's coding environment on a Fly Sprite and bind its store to it."

    def add_arguments(self, parser):
        parser.add_argument("tenant", help="Tenant slug")

    def handle(self, *args, tenant: str, **options):
        if not environment_configured():
            raise CommandError(
                "set SPRITES_TOKEN, ENVIRONMENT_HOST_URL, ENVIRONMENT_HOST_SHA256 and "
                "ENVIRONMENT_TOKEN_KEY first"
            )
        try:
            row = Tenant.objects.get(slug=tenant)
        except Tenant.DoesNotExist as error:
            raise CommandError(f"no tenant {tenant}") from error
        host = begin_environment(row)
        sprites = sprites_client_from_settings()
        try:
            host = provision_environment(host, sprites)
        except (SpriteError, EnvironmentProvisioningError) as error:
            host.refresh_from_db()
            if host.state == EnvironmentHost.State.PROVISIONING:
                host.mark_failed(str(error))
            raise CommandError(str(error)) from error
        finally:
            sprites.close()
        self.stdout.write(f"{host} at {host.url}")

        if not provisioning_configured():
            return
        fly = client_from_settings()
        try:
            if bind_store(row, fly):
                self.stdout.write(f"bound {row.slug}'s store to it")
        except FlyApiError as error:
            raise CommandError(f"binding {row.slug}'s store failed: {error}") from error
        finally:
            fly.close()
