"""Restricted renderer admission, execution boundary, and API oracles."""

from __future__ import annotations

import json
import resource
import sys
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

import pytest
from django.test import Client
from django.test import override_settings
from django.utils import timezone

from apps.keys.mint import mint_api_key
from apps.rendering.validation import DiagramSourceError, validate_diagrams_source
from apps.rendering.service import (
    RenderExecutionError,
    RenderExecutionTimeout,
    _run,
    _resource_limits,
    render_diagrams,
    render_plantuml,
)
from apps.tenancy.models import Tenant

SAFE_DIAGRAM = """\
from diagrams import Diagram
from diagrams.aws.compute import EC2

with Diagram("Service", direction="LR"):
    api = EC2("api")
    worker = EC2("worker")
    api >> worker
"""


@dataclass(frozen=True)
class StoredFixture:
    artifact_key: str
    payload_digest: str
    media_type: str
    byte_length: int


class RecordingArtifactStore:
    def __init__(self):
        self.writes = []

    def write_render_artifact(self, tenant_id, payload, *, media_type):
        digest = "sha256:" + "a" * 64
        extension = "svg" if media_type == "image/svg+xml" else "png"
        self.writes.append((tenant_id, payload, media_type))
        return StoredFixture(
            artifact_key=f"tenants/{tenant_id}/renders/{'a' * 64}.{extension}",
            payload_digest=digest,
            media_type=media_type,
            byte_length=len(payload),
        )

    def presign_get(self, _tenant_id, artifact_key):
        return f"https://storage.example/{artifact_key}?signature=fixture"


@pytest.fixture
def rendering_client(db):
    tenant = Tenant.objects.create(
        slug=f"rendering-{uuid4()}", display_name="Rendering tenant"
    )
    minted = mint_api_key(tenant, scopes=["rendering:render"])
    client = Client(HTTP_AUTHORIZATION=f"Bearer {minted.plaintext}")
    client.tenant = tenant
    return client


def post_json(client, path, payload):
    return client.post(path, data=json.dumps(payload), content_type="application/json")


def test_diagrams_validator_accepts_the_supported_language():
    tree = validate_diagrams_source(SAFE_DIAGRAM, max_bytes=16_384)
    assert tree.body


def test_diagrams_validator_accepts_owned_symbols_modules_and_aliases():
    source = """\
from diagrams import Diagram as D
from diagrams.aws import compute as aws_compute
from diagrams.aws.compute import EC2 as Instance

with D("Service"):
    Instance("api")
"""

    assert validate_diagrams_source(source, max_bytes=16_384).body


@pytest.mark.parametrize(
    "source",
    [
        "import os\nfrom diagrams import Diagram\nwith Diagram('x'): pass",
        "from os import path\nfrom diagrams import Diagram\nwith Diagram('x'): pass",
        "from diagrams import Diagram\nx = __import__('subprocess')",
        'from diagrams import Diagram\nx = Diagram.__init__.__globals__["__builtins__"]',
        "from diagrams import Diagram\nx = globals()",
        "from diagrams import os\nos.system('echo escaped')",
        "from diagrams import os as diagrams_os\ndiagrams_os.system('echo escaped')",
        "from diagrams import Path\nPath('/tmp/escaped').touch()",
        "from diagrams import *\nos.system('echo escaped')",
    ],
)
def test_diagrams_validator_rejects_escape_surfaces(source):
    with pytest.raises(DiagramSourceError):
        validate_diagrams_source(source, max_bytes=16_384)


def test_diagrams_refusal_happens_before_process_creation(monkeypatch):
    monkeypatch.setattr(
        "apps.rendering.service._run",
        lambda *_args, **_kwargs: pytest.fail("refused source reached a worker"),
    )
    with pytest.raises(DiagramSourceError):
        render_diagrams(
            "from diagrams import os\nos.system('echo escaped')",
            "png",
        )


@override_settings(RENDER_SUBPROCESS_TIMEOUT_SECONDS=0.05)
def test_subprocess_timeout_kills_the_worker_group():
    with pytest.raises(RenderExecutionTimeout):
        _run(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            stdin=b"fixture",
        )


@override_settings(RENDER_OUTPUT_MAX_BYTES=4)
def test_subprocess_output_cap_is_enforced():
    with pytest.raises(RenderExecutionError, match="size cap"):
        _run([sys.executable, "-c", "print('oversize')"], stdin=b"fixture")


@override_settings(
    RENDER_CPU_SECONDS=7,
    RENDER_MEMORY_BYTES=123_456,
    RENDER_OUTPUT_MAX_BYTES=654_321,
)
def test_linux_worker_installs_cpu_and_address_space_limits(monkeypatch):
    calls = []
    monkeypatch.setattr("apps.rendering.service.SUPPORTS_RLIMIT_AS", True)
    monkeypatch.setattr(
        "apps.rendering.service.resource.setrlimit",
        lambda resource_id, limits: calls.append((resource_id, limits)),
    )

    _resource_limits()

    assert calls == [
        (resource.RLIMIT_CPU, (7, 7)),
        (resource.RLIMIT_FSIZE, (654_321, 654_321)),
        (resource.RLIMIT_AS, (123_456, 123_456)),
    ]


def test_renderer_subprocess_environment_excludes_parent_secrets(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    monkeypatch.setenv("THEOREM_MACHINE_KEY", "thk_secret")
    from apps.rendering.service import _subprocess_environment

    environment = _subprocess_environment()

    assert "DATABASE_URL" not in environment
    assert "THEOREM_MACHINE_KEY" not in environment
    assert environment["PYTHONHASHSEED"] == "0"


def test_plantuml_command_forces_sandbox_and_server_owned_svg(
    tmp_path, monkeypatch
):
    jar_path = tmp_path / "plantuml.jar"
    jar_path.write_bytes(b"fixture")
    calls = []

    def record(command, *, stdin, cwd=None):
        calls.append((command, stdin, cwd))
        return b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'

    monkeypatch.setattr("apps.rendering.service._run", record)
    with override_settings(
        PLANTUML_JAR_PATH=str(jar_path),
        PLANTUML_VERSION="1.2026.6",
        PLANTUML_SECURITY_PROFILE="SANDBOX",
    ):
        _payload, media_type, version = render_plantuml(
            "@startuml\nAlice -> Bob\n@enduml"
        )

    command, stdin, cwd = calls[0]
    assert "-DPLANTUML_SECURITY_PROFILE=SANDBOX" in command
    assert command[-2:] == ["-tsvg", "-pipe"]
    assert stdin.startswith(b"@startuml")
    assert cwd is None
    assert media_type == "image/svg+xml"
    assert version == "1.2026.6"


@pytest.mark.django_db
def test_plantuml_renders_stores_and_presigns(rendering_client, monkeypatch):
    store = RecordingArtifactStore()
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
    monkeypatch.setattr(
        "apps.rendering.api.render_plantuml",
        lambda _source: (svg, "image/svg+xml", "1.2026.6"),
    )
    monkeypatch.setattr(
        "apps.rendering.api.ArtifactStore.from_settings", lambda: store
    )

    response = post_json(
        rendering_client,
        "/internal/rendering/plantuml",
        {
            "contract": "theorem.rendering.v1",
            "source": "@startuml\nAlice -> Bob\n@enduml",
            "format": "svg",
        },
    )

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["renderer"] == "plantuml"
    assert body["renderer_version"] == "1.2026.6"
    assert body["artifact"]["artifact_id"] == "sha256:" + "a" * 64
    assert body["artifact"]["artifact_key"].startswith(
        f"tenants/{rendering_client.tenant.id}/renders/"
    )
    assert body["artifact"]["download_url"].startswith("https://storage.example/")
    assert store.writes == [(rendering_client.tenant.id, svg, "image/svg+xml")]


@pytest.mark.django_db
def test_diagrams_renders_png_through_same_artifact_lane(
    rendering_client, monkeypatch
):
    store = RecordingArtifactStore()
    png = b"\x89PNG\r\n\x1a\nfixture"
    monkeypatch.setattr(
        "apps.rendering.api.render_diagrams",
        lambda _source, _format: (png, "image/png", "0.25.1"),
    )
    monkeypatch.setattr(
        "apps.rendering.api.ArtifactStore.from_settings", lambda: store
    )

    response = post_json(
        rendering_client,
        "/internal/rendering/diagrams",
        {
            "contract": "theorem.rendering.v1",
            "source": SAFE_DIAGRAM,
            "format": "png",
        },
    )

    assert response.status_code == 200, response.content
    assert response.json()["renderer"] == "diagrams"
    assert response.json()["artifact"]["content_type"] == "image/png"
    assert store.writes == [(rendering_client.tenant.id, png, "image/png")]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path,payload",
    [
        (
            "/internal/rendering/plantuml",
            {"source": "@startuml\nAlice -> Bob\n@enduml", "format": "svg"},
        ),
        (
            "/internal/rendering/diagrams",
            {"source": SAFE_DIAGRAM, "format": "png"},
        ),
    ],
)
def test_rendering_routes_require_exact_machine_scope(path, payload, db):
    tenant = Tenant.objects.create(slug=f"scope-{uuid4()}", display_name="Scope")
    assert post_json(Client(), path, payload).status_code == 401

    weak = mint_api_key(tenant, scopes=["layout:compute"])
    weak_client = Client(HTTP_AUTHORIZATION=f"Bearer {weak.plaintext}")
    assert post_json(weak_client, path, payload).status_code == 403

    revoked = mint_api_key(tenant, scopes=["rendering:render"])
    revoked.api_key.revoked_at = timezone.now()
    revoked.api_key.save(update_fields=["revoked_at"])
    revoked_client = Client(HTTP_AUTHORIZATION=f"Bearer {revoked.plaintext}")
    assert post_json(revoked_client, path, payload).status_code == 401

    expired = mint_api_key(
        tenant,
        scopes=["rendering:render"],
        expires_at=timezone.now() - timedelta(seconds=1),
    )
    expired_client = Client(HTTP_AUTHORIZATION=f"Bearer {expired.plaintext}")
    assert post_json(expired_client, path, payload).status_code == 401

    tenant.is_active = False
    tenant.save(update_fields=["is_active"])
    inactive = mint_api_key(tenant, scopes=["rendering:render"])
    inactive_client = Client(HTTP_AUTHORIZATION=f"Bearer {inactive.plaintext}")
    assert post_json(inactive_client, path, payload).status_code == 403
