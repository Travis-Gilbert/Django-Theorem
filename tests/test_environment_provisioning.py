"""Signup gives each tenant a coding environment on a Fly Sprite and binds its store to it."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from sprites import FileNotFoundError_, NotFoundError, URLSettings
from sprites.exceptions import APIError
from sprites.exec import CompletedProcess
from sprites.services import ServiceStream
from sprites.types import ServiceState, ServiceWithState

from apps.tenancy.environment import (
    HOST_PATH,
    HOST_SERVICE,
    HOST_TOKEN_PATH,
    INSTALL_HOST,
    OPERATIONS_PATH,
    STORE_HOSTS_FILE,
    STORE_TOKEN_FILE,
    STORE_TOKEN_SECRET,
    EnvironmentProvisioningError,
    begin_environment,
    bind_store,
    host_token,
    provision_environment,
    sprite_name,
)
from apps.tenancy.models import EnvironmentHost, Store, StorePlacement, Tenant
from apps.tenancy.provisioning import provision, store_app_name
from tests.test_store_provisioning import VERIFYING_KEY, FakeFly

pytestmark = pytest.mark.django_db

HOST_SHA = "1" * 64


class FakeSprite:
    """One sprite: its URL, files, installed binary and the host service."""

    def __init__(self, name: str, *, auth: str = "public"):
        self.name = name
        self.url = f"https://{name}-bthwy.sprites.app"
        self.url_settings = URLSettings(auth=auth)
        self.labels: list[str] = []
        self.files: dict[str, tuple[str, int]] = {}
        self.binary_sha: str | None = None
        self.install_fails = False
        self.service: dict | None = None
        self.status = "stopped"
        self.installs = self.writes = self.definitions = self.starts = self.stops = 0

    def update_url_settings(self, settings: URLSettings) -> None:
        self.url_settings = settings

    def run(self, *args, env, capture_output, timeout):
        assert args[:3] == ("bash", "-c", INSTALL_HOST) and capture_output
        assert env["HOST_PATH"] == HOST_PATH
        if self.binary_sha == env["HOST_SHA256"]:
            return CompletedProcess(list(args), 0, b"unchanged\n", b"")
        if self.install_fails:
            return CompletedProcess(list(args), 1, b"", b"sha256sum: WARNING: 1 did NOT match")
        self.binary_sha = env["HOST_SHA256"]
        self.installs += 1
        return CompletedProcess(list(args), 0, b"installed\n", b"")

    def filesystem(self, working_dir: str):
        assert working_dir == "/"
        return FakeFilesystem(self)

    def get_service(self, name: str) -> ServiceWithState:
        if self.service is None:
            raise APIError(f"Service not found: {name}", status_code=404)
        return ServiceWithState(name=name, **self.service, state=ServiceState(name, self.status))

    def create_service(self, name: str, **definition) -> ServiceStream:
        assert name == HOST_SERVICE
        self.service = definition
        self.definitions += 1
        return ServiceStream([])

    def stop_service(self, name: str) -> ServiceStream:
        self.status = "stopped"
        self.stops += 1
        return ServiceStream([])

    def start_service(self, name: str) -> ServiceStream:
        assert self.service is not None
        self.status = "running"
        self.starts += 1
        return ServiceStream([])

    def answer(self, request: httpx.Request) -> httpx.Response:
        """theorem-ide-host as it runs: it admits only the bearer token in its
        token file, and only for the tenant it was started for."""
        assert request.url.path == OPERATIONS_PATH
        if self.status != "running":
            return httpx.Response(502, text="no service")
        token = self.files[self.service["env"]["THEOREM_ENVIRONMENT_TOKEN_FILE"]][0]
        tenant = self.service["env"]["THEOREM_ENVIRONMENT_TENANT"]
        if (
            request.headers.get("authorization") != f"Bearer {token}"
            or request.headers.get("x-theorem-tenant") != tenant
        ):
            return httpx.Response(401, json={"code": "host_unauthorized"})
        return httpx.Response(200, json={"operations": []})


class FakePath:
    def __init__(self, sprite: FakeSprite, path: str):
        self.sprite, self.path = sprite, path

    def read_text(self) -> str:
        if self.path not in self.sprite.files:
            raise FileNotFoundError_("read", self.path)
        return self.sprite.files[self.path][0]

    def write_text(self, data: str, mode: int) -> None:
        self.sprite.files[self.path] = (data, mode)
        self.sprite.writes += 1


class FakeFilesystem:
    def __init__(self, sprite: FakeSprite):
        self.sprite = sprite

    def __truediv__(self, path: str) -> FakePath:
        return FakePath(self.sprite, path)


class FakeSprites:
    def __init__(self):
        self.sprites: dict[str, FakeSprite] = {}
        self.created: list[dict] = []

    def get_sprite(self, name: str) -> FakeSprite:
        if name not in self.sprites:
            raise NotFoundError(f"Resource not found for get sprite '{name}'")
        return self.sprites[name]

    def create_sprite(self, name, *, url_settings, labels, wait_for_capacity) -> FakeSprite:
        sprite = FakeSprite(name, auth=url_settings.auth)
        sprite.labels = labels
        self.sprites[name] = sprite
        self.created.append({"name": name, "labels": labels, "wait": wait_for_capacity})
        return sprite

    def http(self) -> httpx.Client:
        def route(request: httpx.Request) -> httpx.Response:
            name = request.url.host.removesuffix("-bthwy.sprites.app")
            return self.sprites[name].answer(request)

        return httpx.Client(transport=httpx.MockTransport(route))

    def close(self) -> None:
        pass


@pytest.fixture
def env_configured(settings):
    settings.SPRITES_TOKEN = "sprites-test-token"
    settings.ENVIRONMENT_HOST_URL = "https://releases.example/theorem-ide-host"
    settings.ENVIRONMENT_HOST_SHA256 = HOST_SHA
    settings.ENVIRONMENT_TOKEN_KEY = "environment-test-key"
    settings.ENVIRONMENT_READY_TIMEOUT_SECONDS = 5
    settings.FLY_API_TOKEN = ""
    return settings


@pytest.fixture
def both_configured(env_configured):
    env_configured.FLY_API_TOKEN = "fly-test-token"
    env_configured.FLY_ORG_SLUG = "theorem-620"
    env_configured.STORE_IMAGE = "registry.fly.io/theorem-store-image@sha256:" + "0" * 64
    env_configured.STORE_SESSION_VERIFYING_KEYS_HEX = VERIFYING_KEY
    return env_configured


def _tenant() -> Tenant:
    return Tenant.objects.create(slug=f"Signup-{uuid4()}", display_name="New user")


def _provision(tenant: Tenant, sprites: FakeSprites) -> EnvironmentHost:
    return provision_environment(
        begin_environment(tenant), sprites, http=sprites.http(), poll_interval_seconds=0
    )


def test_signup_without_environment_config_leaves_tenant_without_one(settings):
    settings.SPRITES_TOKEN = ""
    tenant = _tenant()
    assert not EnvironmentHost.objects.filter(tenant=tenant).exists()


def test_signup_records_the_sprite_and_enqueues_both_builds(
    both_configured, django_capture_on_commit_callbacks, monkeypatch
):
    queued: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "apps.tenancy.tasks.provision_tenant_store.delay", lambda t: queued.append(("store", t))
    )
    monkeypatch.setattr(
        "apps.tenancy.tasks.provision_tenant_environment.delay",
        lambda t: queued.append(("environment", t)),
    )
    with django_capture_on_commit_callbacks(execute=True):
        tenant = _tenant()

    host = EnvironmentHost.objects.get(tenant=tenant)
    assert host.state == EnvironmentHost.State.PROVISIONING
    assert host.sprite == sprite_name(tenant) == f"theorem-env-{tenant.id.hex[:16]}"
    assert sorted(queued) == [("environment", str(tenant.pk)), ("store", str(tenant.pk))]


def test_provision_builds_a_public_sprite_running_the_tenants_host(env_configured):
    tenant = _tenant()
    sprites = FakeSprites()
    host = _provision(tenant, sprites)

    sprite = sprites.sprites[sprite_name(tenant)]
    assert sprites.created == [
        {
            "name": sprite.name,
            "labels": ["theorem-environment", f"tenant-{tenant.id.hex[:16]}"],
            "wait": True,
        }
    ]
    assert sprite.url_settings.auth == "public"
    assert sprite.binary_sha == HOST_SHA
    assert sprite.files[HOST_TOKEN_PATH] == (host_token(tenant), 0o600)
    assert sprite.service["cmd"] == HOST_PATH
    assert sprite.service["args"] == ["--http"]
    assert sprite.service["http_port"] == 8080
    assert sprite.service["env"]["THEOREM_ENVIRONMENT_TENANT"] == tenant.slug
    assert sprite.service["env"]["THEOREM_ENVIRONMENT_TOKEN_FILE"] == HOST_TOKEN_PATH
    assert sprite.status == "running"
    assert host.state == EnvironmentHost.State.SERVING
    assert host.url == sprite.url
    assert host.host_sha256 == HOST_SHA


def test_the_host_token_is_derived_never_stored(env_configured):
    tenant, other = _tenant(), _tenant()
    token = host_token(tenant)
    assert len(token) == 43 and len(token.encode()) >= 32
    assert token != host_token(other)
    host = _provision(tenant, FakeSprites())
    stored = json.dumps(
        {f.name: str(getattr(host, f.attname)) for f in EnvironmentHost._meta.concrete_fields}
    )
    assert token not in stored
    env_configured.ENVIRONMENT_TOKEN_KEY = "rotated-key"
    assert host_token(tenant) != token


def test_a_rerun_with_nothing_changed_touches_nothing(env_configured):
    tenant = _tenant()
    sprites = FakeSprites()
    _provision(tenant, sprites)
    sprite = sprites.sprites[sprite_name(tenant)]
    before = (sprite.installs, sprite.writes, sprite.definitions, sprite.starts, sprite.stops)

    host = _provision(tenant, sprites)

    assert (sprite.installs, sprite.writes, sprite.definitions, sprite.starts, sprite.stops) == before
    assert len(sprites.created) == 1
    assert host.state == EnvironmentHost.State.SERVING


def test_a_new_pinned_binary_is_installed_and_the_host_restarted(env_configured):
    tenant = _tenant()
    sprites = FakeSprites()
    _provision(tenant, sprites)
    sprite = sprites.sprites[sprite_name(tenant)]

    env_configured.ENVIRONMENT_HOST_SHA256 = "2" * 64
    host = _provision(tenant, sprites)

    assert sprite.binary_sha == "2" * 64
    assert (sprite.installs, sprite.stops, sprite.starts) == (2, 1, 2)
    assert host.host_sha256 == "2" * 64


def test_a_rotated_key_rewrites_the_token_and_restarts_the_host(env_configured):
    tenant = _tenant()
    sprites = FakeSprites()
    _provision(tenant, sprites)
    sprite = sprites.sprites[sprite_name(tenant)]

    env_configured.ENVIRONMENT_TOKEN_KEY = "rotated-key"
    _provision(tenant, sprites)

    assert sprite.files[HOST_TOKEN_PATH][0] == host_token(tenant)
    assert (sprite.installs, sprite.stops, sprite.starts) == (1, 1, 2)


def test_an_existing_sprite_behind_org_auth_is_made_public(env_configured):
    tenant = _tenant()
    sprites = FakeSprites()
    sprites.sprites[sprite_name(tenant)] = FakeSprite(sprite_name(tenant), auth="sprite")
    _provision(tenant, sprites)
    assert sprites.sprites[sprite_name(tenant)].url_settings.auth == "public"
    assert sprites.created == []


def test_a_host_that_admits_strangers_is_not_ready(env_configured):
    tenant = _tenant()
    sprites = FakeSprites()
    env_configured.ENVIRONMENT_READY_TIMEOUT_SECONDS = 0
    open_host = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"operations": []}))
    )
    with pytest.raises(EnvironmentProvisioningError, match="anonymous 200"):
        provision_environment(
            begin_environment(tenant), sprites, http=open_host, poll_interval_seconds=0
        )
    assert EnvironmentHost.objects.get(tenant=tenant).state == EnvironmentHost.State.PROVISIONING


def test_a_binary_that_fails_its_checksum_is_refused(env_configured):
    tenant = _tenant()
    sprites = FakeSprites()
    sprite = FakeSprite(sprite_name(tenant))
    sprite.install_fails = True
    sprites.sprites[sprite.name] = sprite
    with pytest.raises(EnvironmentProvisioningError, match="did NOT match"):
        _provision(tenant, sprites)
    assert sprite.service is None


def test_task_records_failure(env_configured, monkeypatch):
    from apps.tenancy import tasks

    tenant = _tenant()
    sprites = FakeSprites()
    sprite = FakeSprite(sprite_name(tenant))
    sprite.install_fails = True
    sprites.sprites[sprite.name] = sprite
    monkeypatch.setattr(tasks, "sprites_client_from_settings", lambda: sprites)
    with pytest.raises(EnvironmentProvisioningError):
        tasks.provision_tenant_environment.apply(args=[str(tenant.pk)]).get()
    host = EnvironmentHost.objects.get(tenant=tenant)
    assert host.state == EnvironmentHost.State.FAILED
    assert "did NOT match" in host.last_error


def _serving_store(tenant: Tenant, fly: FakeFly) -> StorePlacement:
    return provision(StorePlacement.objects.get(tenant=tenant), fly.client(), poll_interval_seconds=0)


def _bound_hosts(config: dict) -> dict:
    files = {f["guest_path"]: f for f in config["files"]}
    return json.loads(base64.b64decode(files[STORE_HOSTS_FILE]["raw_value"]))


def test_an_environment_finished_after_the_store_binds_it(both_configured):
    tenant = _tenant()
    fly = FakeFly()
    _serving_store(tenant, fly)
    assert fly.updates == []
    host = _provision(tenant, FakeSprites())

    assert bind_store(tenant, fly.client()) is True

    app = store_app_name(tenant)
    token = host_token(tenant)
    assert base64.b64decode(fly.secrets[app][STORE_TOKEN_SECRET]).decode() == token
    machine = fly.machines[app][0]
    config = machine["config"]
    assert machine["min_secrets_version"] == fly.secrets_version
    assert config["env"]["THEOREM_ENVIRONMENT_HOSTS_FILE"] == STORE_HOSTS_FILE
    # The binding the store's parser accepts: url and token_file, nothing else.
    assert _bound_hosts(config) == {tenant.slug: {"url": host.url, "token_file": STORE_TOKEN_FILE}}
    assert {"guest_path": STORE_TOKEN_FILE, "secret_name": STORE_TOKEN_SECRET} in config["files"]
    assert token not in json.dumps(config)
    # Everything else the store needs is still there.
    assert config["env"]["RUSTY_RED_TENANT_ALLOWLIST"] == tenant.slug

    assert bind_store(tenant, fly.client()) is False
    assert len(fly.updates) == 1


def test_a_store_built_after_the_environment_starts_bound(both_configured):
    tenant = _tenant()
    host = _provision(tenant, FakeSprites())
    fly = FakeFly()
    _serving_store(tenant, fly)

    machine = fly.machines[store_app_name(tenant)][0]
    assert fly.updates == []
    assert machine["min_secrets_version"] == fly.secrets_version == 1
    assert _bound_hosts(machine["config"]) == {
        tenant.slug: {"url": host.url, "token_file": STORE_TOKEN_FILE}
    }
    assert host_token(tenant) not in json.dumps(machine["config"])


def test_a_rotated_key_rebinds_the_store(both_configured):
    tenant = _tenant()
    fly = FakeFly()
    _provision(tenant, FakeSprites())
    _serving_store(tenant, fly)

    both_configured.ENVIRONMENT_TOKEN_KEY = "rotated-key"
    assert bind_store(tenant, fly.client()) is True
    app = store_app_name(tenant)
    assert base64.b64decode(fly.secrets[app][STORE_TOKEN_SECRET]).decode() == host_token(tenant)


def test_a_store_registered_by_hand_is_not_rebound(both_configured):
    tenant = _tenant()
    StorePlacement.objects.filter(tenant=tenant).delete()
    store = Store.objects.create(
        slug=f"hand-{uuid4().hex[:8]}", http_url="http://personal.internal:8380",
        grpc_url="http://personal.internal:8380",
    )
    StorePlacement.objects.create(tenant=tenant, store=store)
    _provision(tenant, FakeSprites())
    fly = FakeFly()
    assert bind_store(tenant, fly.client()) is False
    assert fly.calls == []


def test_nothing_binds_before_the_environment_serves(both_configured):
    tenant = _tenant()
    fly = FakeFly()
    _serving_store(tenant, fly)
    begin_environment(tenant)
    assert bind_store(tenant, fly.client()) is False
    assert fly.secrets == {}


def test_command_provisions_and_binds(both_configured, monkeypatch):
    from apps.tenancy.management.commands import provision_tenant_environment as command

    tenant = _tenant()
    fly = FakeFly()
    _serving_store(tenant, fly)
    sprites = FakeSprites()
    monkeypatch.setattr(command, "sprites_client_from_settings", lambda: sprites)
    monkeypatch.setattr(command, "client_from_settings", fly.client)
    real = command.provision_environment
    monkeypatch.setattr(
        command,
        "provision_environment",
        lambda host, client: real(host, client, http=sprites.http(), poll_interval_seconds=0),
    )
    call_command("provision_tenant_environment", tenant.slug)
    assert EnvironmentHost.objects.get(tenant=tenant).state == EnvironmentHost.State.SERVING
    assert len(fly.updates) == 1


def test_command_refuses_without_configuration(settings):
    settings.SPRITES_TOKEN = ""
    with pytest.raises(CommandError, match="SPRITES_TOKEN"):
        call_command("provision_tenant_environment", "Travis-Gilbert")


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", "curl", "sha256sum")),
    reason="needs bash, curl and sha256sum",
)
def test_the_install_script_replaces_only_with_the_pinned_binary(tmp_path):
    release = tmp_path / "release"
    release.write_bytes(b"theorem-ide-host build")
    pinned = sha256(release.read_bytes()).hexdigest()
    dest = tmp_path / "bin" / "theorem-ide-host"

    def install(digest: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-c", INSTALL_HOST],
            env={
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "HOST_PATH": str(dest),
                "HOST_URL": release.as_uri(),
                "HOST_SHA256": digest,
            },
            capture_output=True,
            text=True,
        )

    first = install(pinned)
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == "installed"
    assert dest.read_bytes() == release.read_bytes()
    assert dest.stat().st_mode & 0o777 == 0o755

    assert install(pinned).stdout.strip() == "unchanged"

    refused = install("f" * 64)
    assert refused.returncode != 0
    assert dest.read_bytes() == release.read_bytes()
    assert [p.name for p in dest.parent.iterdir()] == ["theorem-ide-host"]
