"""Build a new tenant's RustyRed store on Fly and place the tenant on it.

Signup creates a tenant; ``begin_provisioning`` gives it a store row and a
placement in ``provisioning``. ``provision`` then creates the Fly app, volume
and machine in the Theorem org, waits for the store's readiness check, and
moves the placement to ``serving``. The front routes to the store over Fly's
private network (``<app>.internal``), so the front must run in the same org.

The store holds no secret. It admits only its own tenant and checks the
front's short-lived signed tokens against ``STORE_SESSION_VERIFYING_KEYS_HEX``,
a public key. The only credential here is ``FLY_API_TOKEN``, held by Django.
"""

from __future__ import annotations

from time import monotonic, sleep

from django.conf import settings
from django.db import transaction

from apps.tenancy.fly import FlyApiError, FlyMachinesClient
from apps.tenancy.models import Store, StorePlacement, Tenant

STORE_PORT = 8380
VOLUME_NAME = "rustyred_data"
MACHINE_NAME = "store"
READY_CHECK = "store_ready"


class ProvisioningError(RuntimeError):
    """The store could not be built; the placement records why."""


def provisioning_configured() -> bool:
    return bool(
        settings.FLY_API_TOKEN
        and settings.STORE_IMAGE
        and settings.STORE_SESSION_VERIFYING_KEYS_HEX
    )


def store_app_name(tenant: Tenant) -> str:
    # Fly app names are global, lowercase and at most 63 characters; the slug
    # is neither, the tenant id is both stable and unique.
    return f"{settings.STORE_FLY_APP_PREFIX}-{tenant.id.hex[:16]}"


def store_url(app: str) -> str:
    return f"http://{app}.internal:{STORE_PORT}"


def begin_provisioning(tenant: Tenant) -> StorePlacement:
    """Record the store a tenant will get, before anything exists on Fly."""
    with transaction.atomic():
        existing = StorePlacement.objects.select_for_update().filter(tenant=tenant).first()
        if existing is not None:
            return existing
        app = store_app_name(tenant)
        store, _ = Store.objects.get_or_create(
            slug=app,
            defaults={"http_url": store_url(app), "grpc_url": store_url(app), "fly_app": app},
        )
        return StorePlacement.objects.create(
            tenant=tenant, store=store, state=StorePlacement.State.PROVISIONING
        )


def machine_config(tenant: Tenant, app: str, volume_id: str) -> dict:
    memory_mb = settings.STORE_MEMORY_MB
    memory_bytes = memory_mb * 1024 * 1024
    return {
        "image": settings.STORE_IMAGE,
        "guest": {"cpu_kind": "shared", "cpus": settings.STORE_CPUS, "memory_mb": memory_mb},
        # Fly mounts a volume by its id, not its name.
        "mounts": [{"volume": volume_id, "path": "/data"}],
        "restart": {"policy": "always"},
        "metadata": {"theorem_tenant": tenant.slug, "theorem_role": "store"},
        "env": {
            "PORT": str(STORE_PORT),
            "RUSTY_RED_HOST": "[::]",
            "RUSTY_RED_MODE": "embedded",
            "RUSTY_RED_DATA_DIR": "/data",
            "RUSTY_RED_DEPLOYMENT_ID": f"fly:{app}",
            "RUSTY_RED_REQUIRE_VOLUME": "true",
            "RUSTY_RED_VOLUME_MOUNTED": "true",
            "RUSTY_RED_REQUIRE_AUTH": "true",
            "RUSTY_RED_SESSION_VERIFYING_KEYS_HEX": settings.STORE_SESSION_VERIFYING_KEYS_HEX,
            "RUSTY_RED_TENANT_ADMISSION": "allowlist",
            "RUSTY_RED_TENANT_ALLOWLIST": tenant.slug,
            "RUSTY_RED_MCP_DEFAULT_TENANT": tenant.slug,
            "RUSTY_RED_PREWARM_TENANTS": tenant.slug,
            "RUSTY_RED_MCP_READ_ONLY": "false",
            # The same proportions as the personal store: a quarter of the
            # machine per tenant budget, and a ceiling with runtime headroom.
            "RUSTY_RED_TENANT_MEMORY_QUOTA_BYTES": str(memory_bytes // 4),
            "RUSTY_RED_PROCESS_MEMORY_QUOTA_BYTES": str(memory_bytes // 4),
            "RUSTY_RED_RSS_CEILING_BYTES": str(memory_bytes * 3 // 8),
            "RUSTY_RED_RESIDENT_EDGE_BUDGET_BYTES": str(memory_bytes // 32),
            "RUSTY_RED_GLOBAL_OVERHEAD_RESERVE_BYTES": str(memory_bytes // 16),
        },
        "checks": {
            READY_CHECK: {
                "type": "http",
                "port": STORE_PORT,
                "method": "get",
                "path": "/ready",
                "interval": "15s",
                "timeout": "10s",
                "grace_period": "1m",
            }
        },
    }


def provision(
    placement: StorePlacement,
    client: FlyMachinesClient,
    *,
    poll_interval_seconds: float = 5.0,
) -> StorePlacement:
    """Build the placement's store and serve the tenant from it.

    Safe to run again after a crash or a failure: each Fly resource is found by
    name before it is created. Raises ``FlyApiError`` for a failure worth
    retrying and ``ProvisioningError`` when the store never became ready.
    """
    placement = StorePlacement.objects.select_related("tenant", "store").get(pk=placement.pk)
    if placement.state == StorePlacement.State.SERVING:
        return placement
    if placement.state == StorePlacement.State.FAILED:
        placement.retry_provisioning()
    if placement.state != StorePlacement.State.PROVISIONING:
        raise ProvisioningError(f"{placement} is {placement.state}, not provisioning")

    tenant, store = placement.tenant, placement.store
    app = store.fly_app or store.slug
    region = settings.STORE_REGION
    client.ensure_app(app)
    volume = client.ensure_volume(
        app, name=VOLUME_NAME, region=region, size_gb=settings.STORE_VOLUME_GB
    )
    volume_id = volume.get("id")
    if not volume_id:
        raise FlyApiError(f"Fly returned no volume id for {app}")
    machine = client.ensure_machine(
        app, name=MACHINE_NAME, region=region, config=machine_config(tenant, app, volume_id)
    )
    machine_id = machine.get("id")
    if not machine_id:
        raise FlyApiError(f"Fly returned no machine id for {app}")
    client.wait_started(app, machine_id)

    deadline = monotonic() + settings.STORE_PROVISION_TIMEOUT_SECONDS
    while True:
        checks = client.machine(app, machine_id).get("checks") or []
        if any(c.get("name") == READY_CHECK and c.get("status") == "passing" for c in checks):
            break
        if monotonic() >= deadline:
            raise ProvisioningError(
                f"{app} did not pass {READY_CHECK} within "
                f"{settings.STORE_PROVISION_TIMEOUT_SECONDS}s"
            )
        sleep(poll_interval_seconds)

    placement.mark_provisioned()
    return placement


def client_from_settings() -> FlyMachinesClient:
    return FlyMachinesClient(
        api_token=settings.FLY_API_TOKEN,
        org_slug=settings.FLY_ORG_SLUG,
        base_url=settings.FLY_API_BASE,
    )
