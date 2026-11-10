"""Signup builds each new tenant a store on Fly and places the tenant on it."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction

from apps.tenancy.fly import FlyApiError, FlyMachinesClient
from apps.tenancy.models import PlacementTransitionError, Store, StorePlacement, Tenant
from apps.tenancy.provisioning import (
    ProvisioningError,
    begin_provisioning,
    provision,
    store_app_name,
)

pytestmark = pytest.mark.django_db

VERIFYING_KEY = "ab" * 32


class FakeFly:
    """The slice of the Machines API the provisioner uses, held in memory."""

    def __init__(self, *, ready_after: int = 1, fail_machine_create: int = 0):
        self.apps: dict[str, dict] = {}
        self.volumes: dict[str, list[dict]] = {}
        self.machines: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, str]] = []
        self.ready_after = ready_after
        self.polls = 0
        self.fail_machine_create = fail_machine_create

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path.removeprefix("/v1")
        self.calls.append((method, path))
        assert request.headers["authorization"] == "Bearer fly-test-token"
        parts = path.strip("/").split("/")
        body = json.loads(request.content) if request.content else {}
        if parts == ["apps"] and method == "POST":
            self.apps[body["app_name"]] = {
                "name": body["app_name"],
                "organization": {"slug": body["org_slug"]},
            }
            return httpx.Response(201, json={"id": body["app_name"]})
        app = parts[1]
        if len(parts) == 2:
            if app not in self.apps:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json=self.apps[app])
        kind = parts[2]
        if kind == "volumes":
            if method == "GET":
                return httpx.Response(200, json=self.volumes.get(app, []))
            volume = {"id": f"vol_{len(self.volumes.get(app, []))}", "state": "created", **body}
            self.volumes.setdefault(app, []).append(volume)
            return httpx.Response(200, json=volume)
        if kind == "machines" and len(parts) == 3:
            if method == "GET":
                return httpx.Response(200, json=self.machines.get(app, []))
            if self.fail_machine_create:
                self.fail_machine_create -= 1
                return httpx.Response(503, text="capacity")
            machine = {"id": f"m_{app}", "state": "created", "checks": [], **body}
            self.machines.setdefault(app, []).append(machine)
            return httpx.Response(200, json=machine)
        machine = self.machines[app][0]
        if len(parts) == 5 and parts[4] == "wait":
            assert request.url.params["state"] == "started"
            machine["state"] = "started"
            return httpx.Response(200, json={"ok": True})
        self.polls += 1
        status = "passing" if self.polls >= self.ready_after else "critical"
        return httpx.Response(
            200, json={**machine, "checks": [{"name": "store_ready", "status": status}]}
        )

    def client(self) -> FlyMachinesClient:
        return FlyMachinesClient(
            api_token="fly-test-token",
            org_slug="theorem-620",
            transport=httpx.MockTransport(self.handler),
        )


@pytest.fixture
def configured(settings):
    settings.FLY_API_TOKEN = "fly-test-token"
    settings.FLY_ORG_SLUG = "theorem-620"
    settings.STORE_IMAGE = "registry.fly.io/theorem-store-image@sha256:" + "0" * 64
    settings.STORE_SESSION_VERIFYING_KEYS_HEX = VERIFYING_KEY
    settings.STORE_PROVISION_TIMEOUT_SECONDS = 600
    return settings


def _tenant() -> Tenant:
    return Tenant.objects.create(slug=f"Signup-{uuid4()}", display_name="New user")


def test_signup_without_provisioning_config_leaves_tenant_unplaced(settings):
    settings.FLY_API_TOKEN = ""
    tenant = _tenant()
    assert not StorePlacement.objects.filter(tenant=tenant).exists()


def test_signup_places_tenant_in_provisioning_and_enqueues_the_build(
    configured, django_capture_on_commit_callbacks, monkeypatch
):
    queued: list[str] = []
    monkeypatch.setattr(
        "apps.tenancy.tasks.provision_tenant_store.delay", lambda tenant_id: queued.append(tenant_id)
    )
    with django_capture_on_commit_callbacks(execute=True):
        tenant = _tenant()

    placement = StorePlacement.objects.select_related("store").get(tenant=tenant)
    app = store_app_name(tenant)
    assert placement.state == StorePlacement.State.PROVISIONING
    assert placement.store.slug == app
    assert placement.store.fly_app == app
    assert placement.store.http_url == f"http://{app}.internal:8380"
    assert queued == [str(tenant.pk)]


def test_provision_builds_the_store_in_the_theorem_org_and_serves_the_tenant(configured):
    with transaction.atomic():
        tenant = _tenant()  # the signal already began provisioning
    fly = FakeFly(ready_after=3)
    placement = provision(StorePlacement.objects.get(tenant=tenant), fly.client(), poll_interval_seconds=0)

    app = store_app_name(tenant)
    assert placement.state == StorePlacement.State.SERVING
    assert placement.epoch == 1
    assert fly.apps[app]["organization"]["slug"] == "theorem-620"
    assert fly.volumes[app][0] | {"id": None} == {
        "id": None, "state": "created", "name": "rustyred_data", "region": "iad",
        "size_gb": 10, "encrypted": True,
    }
    machine = fly.machines[app][0]
    env = machine["config"]["env"]
    assert machine["config"]["image"] == configured.STORE_IMAGE
    # Fly mounts by volume id; the name is only how a retry finds the volume.
    assert machine["config"]["mounts"] == [{"volume": fly.volumes[app][0]["id"], "path": "/data"}]
    assert fly.volumes[app][0]["id"] != "rustyred_data"
    assert env["RUSTY_RED_TENANT_ALLOWLIST"] == tenant.slug
    assert env["RUSTY_RED_TENANT_ADMISSION"] == "allowlist"
    assert env["RUSTY_RED_REQUIRE_AUTH"] == "true"
    assert env["RUSTY_RED_SESSION_VERIFYING_KEYS_HEX"] == VERIFYING_KEY
    assert fly.polls == 3


def test_the_store_machine_carries_no_credential(configured):
    tenant = _tenant()
    fly = FakeFly()
    provision(StorePlacement.objects.get(tenant=tenant), fly.client(), poll_interval_seconds=0)
    config = fly.machines[store_app_name(tenant)][0]["config"]
    rendered = json.dumps(config)
    assert "fly-test-token" not in rendered
    secretish = [k for k in config["env"] if any(w in k for w in ("TOKEN", "SECRET", "SIGNING", "PASSWORD"))]
    assert secretish == []
    # The positive control: the public key is the one thing that does travel.
    assert config["env"]["RUSTY_RED_SESSION_VERIFYING_KEYS_HEX"] == VERIFYING_KEY


def test_a_retried_run_reuses_what_the_first_attempt_made(configured):
    tenant = _tenant()
    fly = FakeFly(fail_machine_create=1)
    placement = StorePlacement.objects.get(tenant=tenant)
    with pytest.raises(FlyApiError):
        provision(placement, fly.client(), poll_interval_seconds=0)
    assert StorePlacement.objects.get(tenant=tenant).state == StorePlacement.State.PROVISIONING

    provision(placement, fly.client(), poll_interval_seconds=0)
    app = store_app_name(tenant)
    assert len(fly.apps) == 1
    assert len(fly.volumes[app]) == 1
    assert len(fly.machines[app]) == 1
    assert fly.calls.count(("POST", "/apps")) == 1
    assert StorePlacement.objects.get(tenant=tenant).state == StorePlacement.State.SERVING


def test_a_store_that_never_becomes_ready_is_refused(configured):
    configured.STORE_PROVISION_TIMEOUT_SECONDS = 0
    tenant = _tenant()
    fly = FakeFly(ready_after=10_000)
    with pytest.raises(ProvisioningError):
        provision(StorePlacement.objects.get(tenant=tenant), fly.client(), poll_interval_seconds=0)


def test_a_fly_app_in_another_org_is_refused(configured):
    tenant = _tenant()
    fly = FakeFly()
    app = store_app_name(tenant)
    fly.apps[app] = {"name": app, "organization": {"slug": "personal"}}
    with pytest.raises(FlyApiError, match="belongs to personal"):
        provision(StorePlacement.objects.get(tenant=tenant), fly.client(), poll_interval_seconds=0)


def test_task_records_failure_when_retries_run_out(configured, monkeypatch):
    from apps.tenancy import tasks

    tenant = _tenant()
    fly = FakeFly(ready_after=10_000)
    configured.STORE_PROVISION_TIMEOUT_SECONDS = 0
    monkeypatch.setattr(tasks, "client_from_settings", fly.client)
    with pytest.raises(ProvisioningError):
        tasks.provision_tenant_store.apply(args=[str(tenant.pk)]).get()
    placement = StorePlacement.objects.get(tenant=tenant)
    assert placement.state == StorePlacement.State.FAILED
    assert "store_ready" in placement.last_error


def test_command_retries_a_failed_placement(configured, monkeypatch):
    from apps.tenancy.management.commands import provision_tenant_store as command

    tenant = _tenant()
    placement = StorePlacement.objects.get(tenant=tenant)
    placement.mark_failed("capacity")
    fly = FakeFly()
    monkeypatch.setattr(command, "client_from_settings", fly.client)
    call_command("provision_tenant_store", tenant.slug)
    placement.refresh_from_db()
    assert placement.state == StorePlacement.State.SERVING
    assert placement.last_error == ""


def test_command_refuses_without_configuration(settings):
    settings.FLY_API_TOKEN = ""
    with pytest.raises(CommandError, match="FLY_API_TOKEN"):
        call_command("provision_tenant_store", "Travis-Gilbert")


def test_begin_provisioning_keeps_an_existing_placement(configured):
    placement = StorePlacement.objects.get(tenant__slug="Travis-Gilbert")
    assert begin_provisioning(placement.tenant) == placement
    assert placement.store.slug == "travis-theorem-personal-store"


def test_a_provisioning_placement_cannot_be_moved_or_aborted(configured):
    tenant = _tenant()
    placement = StorePlacement.objects.get(tenant=tenant)
    other = Store.objects.create(slug=f"other-{uuid4().hex[:8]}", http_url="http://o:1", grpc_url="http://o:1")
    with pytest.raises(PlacementTransitionError):
        placement.begin_move(other)
    with pytest.raises(PlacementTransitionError):
        placement.abort_move()
    with pytest.raises(PlacementTransitionError):
        placement.retry_provisioning()


def test_a_broker_outage_does_not_fail_signup(configured, django_capture_on_commit_callbacks, monkeypatch):
    def refuse(tenant_id):
        raise ConnectionError("broker down")

    monkeypatch.setattr("apps.tenancy.tasks.provision_tenant_store.delay", refuse)
    with django_capture_on_commit_callbacks(execute=True):
        tenant = _tenant()
    assert StorePlacement.objects.get(tenant=tenant).state == StorePlacement.State.PROVISIONING


def test_the_sweep_queues_only_stranded_builds(configured, monkeypatch):
    from datetime import timedelta

    from django.utils import timezone

    from apps.tenancy import tasks

    queued: list[str] = []
    monkeypatch.setattr(tasks.provision_tenant_store, "delay", lambda tenant_id: queued.append(tenant_id))
    stale = timezone.now() - timedelta(seconds=configured.STORE_PROVISION_STRANDED_AFTER_SECONDS + 60)
    stranded, fresh, failed = _tenant(), _tenant(), _tenant()
    StorePlacement.objects.filter(tenant__in=[stranded, failed]).update(updated_at=stale)
    StorePlacement.objects.get(tenant=failed).mark_failed("capacity")
    StorePlacement.objects.filter(tenant=failed).update(updated_at=stale)

    assert tasks.sweep_stranded_store_provisioning() == 1
    assert queued == [str(stranded.pk)]
    # The serving personal tenant, the fresh build and the failed one are left alone.
    assert str(fresh.pk) not in queued and str(failed.pk) not in queued
    # Queuing touches the placement, so the next sweep does not queue it twice.
    assert tasks.sweep_stranded_store_provisioning() == 0
    assert queued == [str(stranded.pk)]


def test_the_sweep_does_nothing_without_configuration(settings, monkeypatch):
    from apps.tenancy import tasks

    settings.FLY_API_TOKEN = ""
    monkeypatch.setattr(tasks.provision_tenant_store, "delay", lambda tenant_id: pytest.fail("queued"))
    assert tasks.sweep_stranded_store_provisioning() == 0


def test_an_attempt_touches_its_placement(configured, monkeypatch):
    from datetime import timedelta

    from django.utils import timezone

    from apps.tenancy import tasks

    tenant = _tenant()
    stale = timezone.now() - timedelta(days=1)
    StorePlacement.objects.filter(tenant=tenant).update(updated_at=stale)
    fly = FakeFly()
    monkeypatch.setattr(tasks, "client_from_settings", fly.client)
    tasks.provision_tenant_store.apply(args=[str(tenant.pk)]).get()
    assert StorePlacement.objects.get(tenant=tenant).updated_at > stale


def test_the_admin_cannot_delete_a_placement(rf, admin_user):
    from django.contrib.admin.sites import site

    model_admin = site._registry[StorePlacement]
    request = rf.get("/")
    request.user = admin_user
    placement = StorePlacement.objects.get(tenant__slug="Travis-Gilbert")
    assert model_admin.has_delete_permission(request, placement) is False
    # The positive control: the admin still lets the same user view it.
    assert model_admin.has_view_permission(request, placement) is True


def test_a_provisioning_placement_has_no_target(configured):
    tenant = _tenant()
    other = Store.objects.create(slug=f"other-{uuid4().hex[:8]}", http_url="http://o:1", grpc_url="http://o:1")
    with pytest.raises(IntegrityError), transaction.atomic():
        StorePlacement.objects.filter(tenant=tenant).update(target_store=other)
