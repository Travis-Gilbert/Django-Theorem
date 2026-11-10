"""Give each tenant a coding environment on a Fly Sprite, bound to its store.

Signup creates a tenant; ``begin_environment`` records the sprite it will get
and ``provision_environment`` builds it: the sprite, the pinned
``theorem-ide-host`` binary, the host's token file and the service that runs
it. The host answers only for its tenant, so provisioning waits until it
refuses an anonymous request and accepts the tenant's token.

``bind_store`` then tells the tenant's store where its host is. The store
reads ``THEOREM_ENVIRONMENT_HOSTS_FILE`` and the token file named there; both
reach its machine through Fly, the token as an app secret, so the machine
config never holds it. The store and the sprite are built in parallel, and
whichever finishes second makes the binding.

The bearer token is never stored. ``host_token`` derives it from
``ENVIRONMENT_TOKEN_KEY`` and the tenant id, so Django, the sprite and the
store agree on it without a table holding it, and rotating the key rotates
every tenant's token on the next provisioning run.

A sprite's URL is public: the host's own bearer token is its authentication.
Sprites' ``sprite`` URL auth would demand the org token in the same
``Authorization`` header, which the store must never hold.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from time import monotonic, sleep

import httpx
from django.conf import settings
from sprites import FileNotFoundError_, NotFoundError, SpritesClient, URLSettings
from sprites.exceptions import APIError

from apps.tenancy.fly import FlyMachinesClient
from apps.tenancy.models import EnvironmentHost, StorePlacement, Tenant

HOME = "/home/sprite"
HOST_SERVICE = "theorem-ide-host"
HOST_PORT = 8080
HOST_PATH = f"{HOME}/.local/bin/theorem-ide-host"
HOST_TOKEN_PATH = f"{HOME}/.config/theorem/environment-token"
WORKSPACE_ROOT = f"{HOME}/theorem-environment"
OPERATIONS_PATH = "/api/environment/v1/operations"

STORE_MACHINE_NAME = "store"
STORE_HOSTS_FILE = "/run/secrets/environment-hosts.json"
STORE_TOKEN_FILE = "/run/secrets/environment-token"
STORE_TOKEN_SECRET = "THEOREM_ENVIRONMENT_TOKEN_B64"

# Replace the binary only when its digest differs from the pinned one, and
# only with a download that matches it. Prints whether anything changed.
INSTALL_HOST = r"""
set -euo pipefail
if [ -x "$HOST_PATH" ] && [ "$(sha256sum "$HOST_PATH" | cut -d' ' -f1)" = "$HOST_SHA256" ]; then
  echo unchanged
  exit 0
fi
mkdir -p "$(dirname "$HOST_PATH")"
tmp="$(mktemp "$HOST_PATH.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
curl -fsSL --retry 3 -o "$tmp" "$HOST_URL"
echo "$HOST_SHA256  $tmp" | sha256sum --check --quiet -
chmod 0755 "$tmp"
mv -f "$tmp" "$HOST_PATH"
trap - EXIT
echo installed
"""


class EnvironmentProvisioningError(RuntimeError):
    """The environment could not be built; the host row records why."""


def environment_configured() -> bool:
    return bool(
        settings.SPRITES_TOKEN
        and settings.ENVIRONMENT_HOST_URL
        and settings.ENVIRONMENT_HOST_SHA256
        and settings.ENVIRONMENT_TOKEN_KEY
    )


def sprite_name(tenant: Tenant) -> str:
    # Sprite names are lowercase and unique in the org; the slug is neither.
    return f"{settings.ENVIRONMENT_SPRITE_PREFIX}-{tenant.id.hex[:16]}"


def host_token(tenant: Tenant) -> str:
    digest = hmac.new(
        settings.ENVIRONMENT_TOKEN_KEY.encode(),
        f"theorem-environment-host:{tenant.id}".encode(),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def begin_environment(tenant: Tenant) -> EnvironmentHost:
    """Record the sprite a tenant will get, before anything exists on Sprites."""
    host, _ = EnvironmentHost.objects.get_or_create(
        tenant=tenant, defaults={"sprite": sprite_name(tenant)}
    )
    return host


def provision_environment(
    host: EnvironmentHost,
    sprites: SpritesClient,
    *,
    http: httpx.Client | None = None,
    poll_interval_seconds: float = 2.0,
) -> EnvironmentHost:
    """Build the tenant's sprite, or bring a serving one up to the pinned host.

    Safe to run again: the sprite is found by name, the binary and token are
    replaced only when they differ, and the service restarts only when
    something it runs changed. Raises ``SpriteError`` for a failure worth
    retrying and ``EnvironmentProvisioningError`` otherwise.
    """
    host = EnvironmentHost.objects.select_related("tenant").get(pk=host.pk)
    if host.state == EnvironmentHost.State.FAILED:
        host.retry()
    tenant = host.tenant
    token = host_token(tenant)

    sprite = _ensure_sprite(sprites, host.sprite, tenant)
    changed = _install_host(sprite)
    changed = _write_token(sprite, token) or changed
    _ensure_service(sprite, tenant, restart=changed)
    if not sprite.url:
        raise EnvironmentProvisioningError(f"sprite {host.sprite} has no URL")
    _wait_ready(sprite.url, tenant, token, http, poll_interval_seconds)

    host.mark_serving(url=sprite.url, host_sha256=settings.ENVIRONMENT_HOST_SHA256)
    return host


def store_binding(tenant: Tenant) -> dict | None:
    """What the tenant's store machine needs to reach its host, or None while
    there is no serving host to reach."""
    host = EnvironmentHost.objects.filter(
        tenant=tenant, state=EnvironmentHost.State.SERVING
    ).first()
    if host is None or not host.url or not settings.ENVIRONMENT_TOKEN_KEY:
        return None
    token = host_token(tenant)
    hosts = {tenant.slug: {"url": host.url, "token_file": STORE_TOKEN_FILE}}
    return {
        "env": {"THEOREM_ENVIRONMENT_HOSTS_FILE": STORE_HOSTS_FILE},
        # A fingerprint, so a rotated token changes the config and the
        # machine restarts with the new secret.
        "metadata": {
            "theorem_environment_token": hashlib.sha256(token.encode()).hexdigest()[:16]
        },
        "files": [
            {
                "guest_path": STORE_HOSTS_FILE,
                "raw_value": base64.b64encode(json.dumps(hosts, sort_keys=True).encode()).decode(),
            },
            {"guest_path": STORE_TOKEN_FILE, "secret_name": STORE_TOKEN_SECRET},
        ],
    }


def with_binding(config: dict, binding: dict) -> dict:
    owned = {f["guest_path"] for f in binding["files"]}
    return {
        **config,
        "env": {**(config.get("env") or {}), **binding["env"]},
        "metadata": {**(config.get("metadata") or {}), **binding["metadata"]},
        "files": [f for f in config.get("files") or [] if f.get("guest_path") not in owned]
        + binding["files"],
    }


def set_store_token(client: FlyMachinesClient, app: str, tenant: Tenant) -> int:
    """Put the tenant's host token where the store's token file reads it."""
    value = base64.b64encode(host_token(tenant).encode()).decode()
    return client.set_secret(app, STORE_TOKEN_SECRET, value)


def bind_store(tenant: Tenant, client: FlyMachinesClient) -> bool:
    """Point the tenant's store at its host; True when the machine changed.

    Does nothing until both the store and the host serve, for a store an
    operator registered by hand (its fly.toml carries the binding), or when the
    machine already carries this binding.
    """
    binding = store_binding(tenant)
    placement = (
        StorePlacement.objects.select_related("store").filter(tenant=tenant).first()
    )
    if (
        binding is None
        or placement is None
        or placement.state != StorePlacement.State.SERVING
        or not placement.store.fly_app
    ):
        return False
    app = placement.store.fly_app
    machine = client.find_machine(app, STORE_MACHINE_NAME)
    if machine is None:
        return False
    config = machine.get("config") or {}
    if _carries(config, binding):
        return False
    version = set_store_token(client, app, tenant)
    client.update_machine(
        app, machine["id"], config=with_binding(config, binding), min_secrets_version=version
    )
    client.wait_started(app, machine["id"])
    return True


def sprites_client_from_settings() -> SpritesClient:
    return SpritesClient(settings.SPRITES_TOKEN, base_url=settings.SPRITES_API_BASE)


def _carries(config: dict, binding: dict) -> bool:
    # Compare only what the binding owns: Fly fills in the rest of the config.
    env = config.get("env") or {}
    metadata = config.get("metadata") or {}
    files = {f.get("guest_path"): f for f in config.get("files") or []}
    return (
        all(env.get(k) == v for k, v in binding["env"].items())
        and all(metadata.get(k) == v for k, v in binding["metadata"].items())
        and all(
            all(files.get(f["guest_path"], {}).get(k) == v for k, v in f.items())
            for f in binding["files"]
        )
    )


def _ensure_sprite(sprites: SpritesClient, name: str, tenant: Tenant):
    try:
        sprite = sprites.get_sprite(name)
    except NotFoundError:
        return sprites.create_sprite(
            name,
            url_settings=URLSettings(auth="public"),
            labels=["theorem-environment", f"tenant-{tenant.id.hex[:16]}"],
            wait_for_capacity=True,
        )
    if sprite.url_settings is None or sprite.url_settings.auth != "public":
        sprite.update_url_settings(URLSettings(auth="public"))
    return sprite


def _install_host(sprite) -> bool:
    result = sprite.run(
        "bash",
        "-c",
        INSTALL_HOST,
        env={
            "HOST_PATH": HOST_PATH,
            "HOST_URL": settings.ENVIRONMENT_HOST_URL,
            "HOST_SHA256": settings.ENVIRONMENT_HOST_SHA256,
        },
        capture_output=True,
        timeout=300,
    )
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode(errors="replace")
        raise EnvironmentProvisioningError(
            f"installing theorem-ide-host on {sprite.name} failed: {stderr[-500:]}"
        )
    return (result.stdout or b"").decode(errors="replace").strip().endswith("installed")


def _write_token(sprite, token: str) -> bool:
    path = sprite.filesystem("/") / HOST_TOKEN_PATH
    try:
        if path.read_text().strip() == token:
            return False
    except FileNotFoundError_:
        pass
    path.write_text(token, mode=0o600)
    return True


def _service_definition(tenant: Tenant) -> dict:
    return {
        "cmd": HOST_PATH,
        "args": ["--http"],
        "http_port": HOST_PORT,
        "dir": HOME,
        "env": {
            "RUST_LOG": "info",
            "SHELL": "/bin/bash",
            "THEOREM_ENVIRONMENT_LISTEN": f"0.0.0.0:{HOST_PORT}",
            "THEOREM_ENVIRONMENT_ROOT": WORKSPACE_ROOT,
            "THEOREM_ENVIRONMENT_TENANT": tenant.slug,
            "THEOREM_ENVIRONMENT_TOKEN_FILE": HOST_TOKEN_PATH,
        },
    }


def _ensure_service(sprite, tenant: Tenant, *, restart: bool) -> None:
    desired = _service_definition(tenant)
    try:
        current = sprite.get_service(HOST_SERVICE)
    except APIError as error:
        if error.status_code != 404:
            raise
        current = None
    defined = current is not None and all(
        getattr(current, key) == value for key, value in desired.items()
    )
    if not defined:
        sprite.create_service(HOST_SERVICE, **desired).close()
    state = sprite.get_service(HOST_SERVICE).state
    running = state is not None and state.status in ("running", "starting")
    if running and (restart or not defined):
        sprite.stop_service(HOST_SERVICE).close()
        running = False
    if not running:
        sprite.start_service(HOST_SERVICE).close()


def _wait_ready(
    url: str,
    tenant: Tenant,
    token: str,
    http: httpx.Client | None,
    poll_interval_seconds: float,
) -> None:
    """Wait until the host refuses a stranger and answers its tenant.

    The first request also wakes a sleeping sprite.
    """
    client = http or httpx.Client(timeout=30.0)
    endpoint = url.rstrip("/") + OPERATIONS_PATH
    bound = {"Authorization": f"Bearer {token}", "x-theorem-tenant": tenant.slug}
    deadline = monotonic() + settings.ENVIRONMENT_READY_TIMEOUT_SECONDS
    try:
        while True:
            try:
                anonymous = client.get(endpoint).status_code
                answered = client.get(endpoint, headers=bound).status_code
                if anonymous == 401 and answered == 200:
                    return
                seen = f"anonymous {anonymous}, tenant {answered}"
            except httpx.HTTPError as error:
                seen = str(error)
            if monotonic() >= deadline:
                raise EnvironmentProvisioningError(
                    f"{url} did not answer as {tenant.slug}'s host within "
                    f"{settings.ENVIRONMENT_READY_TIMEOUT_SECONDS}s ({seen})"
                )
            sleep(poll_interval_seconds)
    finally:
        if http is None:
            client.close()
