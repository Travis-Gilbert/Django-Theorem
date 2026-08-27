"""Restricted renderer admission, execution boundary, and API oracles."""

from __future__ import annotations

import json
import resource
import sys
from hashlib import sha256
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

import pytest
from django.test import Client
from django.test import override_settings
from django.utils import timezone

from apps.keys.mint import mint_api_key
from apps.orchestration.artifacts import ArtifactNotFoundError, sha256_digest
from apps.rendering.validation import DiagramSourceError, validate_diagrams_source
from apps.rendering.service import (
    PLANTUML_JAVA_RUNTIME_FLAGS,
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


class DescriptorArtifactStore:
    def __init__(self, *, tenant_id, payload, extension):
        self.tenant_id = tenant_id
        self.payload = payload
        self.payload_digest = sha256_digest(payload)
        self.artifact_key = (
            f"tenants/{tenant_id}/renders/"
            f"{self.payload_digest.removeprefix('sha256:')}.{extension}"
        )
        self.presign_seconds = 120
        self.reads = []
        self.presigns = []

    def validate_key(self, tenant_id, artifact_key):
        if tenant_id != self.tenant_id or artifact_key != self.artifact_key:
            raise ValueError("artifact_key is outside the admitted tenant render scope")
        return artifact_key

    def render_artifact_key(self, tenant_id, payload_digest, extension):
        if tenant_id != self.tenant_id or payload_digest != self.payload_digest:
            raise ValueError("render identity is outside the admitted tenant scope")
        return (
            f"tenants/{tenant_id}/renders/"
            f"{payload_digest.removeprefix('sha256:')}.{extension}"
        )

    def get_bytes(self, tenant_id, artifact_key):
        self.validate_key(tenant_id, artifact_key)
        self.reads.append((tenant_id, artifact_key))
        return self.payload

    def presign_get(self, tenant_id, artifact_key):
        self.validate_key(tenant_id, artifact_key)
        self.presigns.append((tenant_id, artifact_key))
        return f"https://storage.example/{artifact_key}?signature={len(self.presigns)}"


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


def test_renderer_subprocess_environment_admits_only_an_absolute_graphviz_bin(
    tmp_path,
):
    graphviz_bin = tmp_path / "graphviz" / "bin"
    graphviz_bin.mkdir(parents=True)

    with override_settings(RENDER_GRAPHVIZ_BIN_DIR=str(graphviz_bin)):
        from apps.rendering.service import _subprocess_environment

        environment = _subprocess_environment()

    assert environment["PATH"].split(":", 1)[0] == str(graphviz_bin.resolve())

    with override_settings(RENDER_GRAPHVIZ_BIN_DIR="relative/bin"):
        with pytest.raises(RenderExecutionError, match="absolute"):
            _subprocess_environment()


def test_plantuml_command_forces_sandbox_and_server_owned_svg(tmp_path, monkeypatch):
    jar_path = tmp_path / "plantuml.jar"
    jar_path.write_bytes(b"fixture")
    jar_digest = sha256(b"fixture").hexdigest()
    calls = []

    def record(command, *, stdin, cwd=None):
        calls.append((command, stdin, cwd))
        if command[-1] == "-version":
            return b"PlantUML version 1.2026.6 / fixture\n"
        return b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'

    monkeypatch.setattr("apps.rendering.service._run", record)
    with override_settings(
        PLANTUML_JAR_PATH=str(jar_path),
        PLANTUML_VERSION="1.2026.6",
        PLANTUML_SHA256=jar_digest,
        PLANTUML_SECURITY_PROFILE="SANDBOX",
    ):
        _payload, media_type, version = render_plantuml(
            "@startuml\nAlice -> Bob\n@enduml"
        )

    assert calls[0][0][-1] == "-version"
    assert all(flag in calls[0][0] for flag in PLANTUML_JAVA_RUNTIME_FLAGS)
    command, stdin, cwd = calls[1]
    assert all(flag in command for flag in PLANTUML_JAVA_RUNTIME_FLAGS)
    assert "-DPLANTUML_SECURITY_PROFILE=SANDBOX" in command
    assert command.index("-DPLANTUML_SECURITY_PROFILE=SANDBOX") < command.index("-jar")
    assert command[-2:] == ["-tsvg", "-pipe"]
    assert stdin.startswith(b"@startuml")
    assert cwd is None
    assert media_type == "image/svg+xml"
    assert version == "1.2026.6"


def test_plantuml_refuses_a_checksum_mismatch_before_java(tmp_path, monkeypatch):
    jar_path = tmp_path / "plantuml.jar"
    jar_path.write_bytes(b"unreviewed")
    monkeypatch.setattr(
        "apps.rendering.service._run",
        lambda *_args, **_kwargs: pytest.fail("unverified jar reached Java"),
    )

    with override_settings(
        PLANTUML_JAR_PATH=str(jar_path),
        PLANTUML_VERSION="1.2026.6",
        PLANTUML_SHA256="0" * 64,
        PLANTUML_SECURITY_PROFILE="SANDBOX",
    ):
        with pytest.raises(RenderExecutionError, match="checksum"):
            render_plantuml("@startuml\nAlice -> Bob\n@enduml")


def test_plantuml_refuses_a_release_identity_mismatch(tmp_path, monkeypatch):
    jar_path = tmp_path / "plantuml.jar"
    jar_path.write_bytes(b"reviewed")

    monkeypatch.setattr(
        "apps.rendering.service._run",
        lambda *_args, **_kwargs: b"PlantUML version 1.2026.5 / wrong\n",
    )
    with override_settings(
        PLANTUML_JAR_PATH=str(jar_path),
        PLANTUML_VERSION="1.2026.6",
        PLANTUML_SHA256=sha256(b"reviewed").hexdigest(),
        PLANTUML_SECURITY_PROFILE="SANDBOX",
    ):
        with pytest.raises(RenderExecutionError, match="release identity"):
            render_plantuml("@startuml\nAlice -> Bob\n@enduml")


def test_plantuml_refuses_a_structured_diagnostic_svg(tmp_path, monkeypatch):
    jar_path = tmp_path / "plantuml.jar"
    jar_path.write_bytes(b"reviewed")
    diagnostic_svg = b"""\
<svg xmlns="http://www.w3.org/2000/svg">
  <text fill="#FF0000" font-weight="700">Cannot open URL</text>
</svg>
"""

    def run(command, *, stdin, cwd=None):
        if command[-1] == "-version":
            return b"PlantUML version 1.2026.6 / fixture\n"
        return diagnostic_svg

    monkeypatch.setattr("apps.rendering.service._run", run)
    with override_settings(
        PLANTUML_JAR_PATH=str(jar_path),
        PLANTUML_VERSION="1.2026.6",
        PLANTUML_SHA256=sha256(b"reviewed").hexdigest(),
        PLANTUML_SECURITY_PROFILE="SANDBOX",
    ):
        with pytest.raises(RenderExecutionError, match="diagnostic SVG"):
            render_plantuml("@startuml\nAlice -> Bob\n@enduml")


@pytest.mark.parametrize(
    "ordinary_text",
    ("Cannot include", "Cannot open URL", "Syntax Error?"),
)
def test_plantuml_accepts_diagnostic_phrases_in_valid_diagram_text(
    tmp_path, monkeypatch, ordinary_text
):
    jar_path = tmp_path / "plantuml.jar"
    jar_path.write_bytes(b"reviewed")
    valid_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'data-diagram-type="DESCRIPTION">'
        f'<text fill="#FF0000" font-weight="700">{ordinary_text}</text>'
        "</svg>"
    ).encode()

    def run(command, *, stdin, cwd=None):
        if command[-1] == "-version":
            return b"PlantUML version 1.2026.6 / fixture\n"
        return valid_svg

    monkeypatch.setattr("apps.rendering.service._run", run)
    with override_settings(
        PLANTUML_JAR_PATH=str(jar_path),
        PLANTUML_VERSION="1.2026.6",
        PLANTUML_SHA256=sha256(b"reviewed").hexdigest(),
        PLANTUML_SECURITY_PROFILE="SANDBOX",
    ):
        payload, media_type, version = render_plantuml(
            f'@startuml\nrectangle "{ordinary_text}"\n@enduml'
        )

    assert payload == valid_svg
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
    monkeypatch.setattr("apps.rendering.api.ArtifactStore.from_settings", lambda: store)

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
def test_diagrams_renders_png_through_same_artifact_lane(rendering_client, monkeypatch):
    store = RecordingArtifactStore()
    png = b"\x89PNG\r\n\x1a\nfixture"
    monkeypatch.setattr(
        "apps.rendering.api.render_diagrams",
        lambda _source, _format: (png, "image/png", "0.25.1"),
    )
    monkeypatch.setattr("apps.rendering.api.ArtifactStore.from_settings", lambda: store)

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
    "renderer,content_type,extension,payload",
    [
        ("plantuml", "image/svg+xml", "svg", b"<svg>plantuml</svg>"),
        ("diagrams", "image/svg+xml", "svg", b"<svg>diagrams</svg>"),
        ("diagrams", "image/png", "png", b"\x89PNG\r\n\x1a\ndiagrams"),
    ],
)
def test_descriptor_refresh_revalidates_tenant_bytes_and_returns_a_fresh_url(
    rendering_client,
    monkeypatch,
    renderer,
    content_type,
    extension,
    payload,
):
    store = DescriptorArtifactStore(
        tenant_id=rendering_client.tenant.id,
        payload=payload,
        extension=extension,
    )
    monkeypatch.setattr("apps.rendering.api.ArtifactStore.from_settings", lambda: store)
    request = {
        "contract": "theorem.rendering.v1",
        "renderer": renderer,
        "artifact_id": store.payload_digest,
        "artifact_key": store.artifact_key,
        "payload_digest": store.payload_digest,
        "content_type": content_type,
    }

    first = post_json(rendering_client, "/internal/rendering/descriptor", request)
    second = post_json(rendering_client, "/internal/rendering/descriptor", request)

    assert first.status_code == 200, first.content
    assert second.status_code == 200, second.content
    first_body = first.json()
    second_body = second.json()
    assert first_body["contract"] == "theorem.rendering.v1"
    assert first_body["renderer"] == renderer
    assert first_body["artifact"]["artifact_id"] == store.payload_digest
    assert first_body["artifact"]["artifact_key"] == store.artifact_key
    assert first_body["artifact"]["payload_digest"] == store.payload_digest
    assert first_body["artifact"]["content_type"] == content_type
    assert first_body["artifact"]["byte_length"] == len(payload)
    assert first_body["artifact"]["expires_at_ms"] > int(
        timezone.now().timestamp() * 1000
    )
    assert first_body["artifact"]["download_url"].endswith("signature=1")
    assert second_body["artifact"]["download_url"].endswith("signature=2")
    assert store.reads == [
        (rendering_client.tenant.id, store.artifact_key),
        (rendering_client.tenant.id, store.artifact_key),
    ]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "override",
    [
        {"contract": "theorem.rendering.v2"},
        {"renderer": "unknown"},
        {"renderer": "plantuml", "content_type": "image/png"},
        {"content_type": "text/html"},
        {"artifact_id": "sha256:" + "b" * 64},
        {"payload_digest": "not-a-digest"},
        {"artifact_key": "tenants/other/renders/cross-tenant.svg"},
    ],
)
def test_descriptor_refresh_fails_closed_for_untrusted_metadata(
    rendering_client, monkeypatch, override
):
    payload = b"<svg>fixture</svg>"
    store = DescriptorArtifactStore(
        tenant_id=rendering_client.tenant.id,
        payload=payload,
        extension="svg",
    )
    monkeypatch.setattr("apps.rendering.api.ArtifactStore.from_settings", lambda: store)
    request = {
        "contract": "theorem.rendering.v1",
        "renderer": "plantuml",
        "artifact_id": store.payload_digest,
        "artifact_key": store.artifact_key,
        "payload_digest": store.payload_digest,
        "content_type": "image/svg+xml",
    }
    request.update(override)

    response = post_json(rendering_client, "/internal/rendering/descriptor", request)

    assert response.status_code == 422, response.content
    assert store.presigns == []


@pytest.mark.django_db
def test_descriptor_refresh_reports_missing_storage_without_minting_a_url(
    rendering_client, monkeypatch
):
    payload = b"<svg>missing</svg>"
    store = DescriptorArtifactStore(
        tenant_id=rendering_client.tenant.id,
        payload=payload,
        extension="svg",
    )
    monkeypatch.setattr(
        store,
        "get_bytes",
        lambda *_args: (_ for _ in ()).throw(ArtifactNotFoundError("missing")),
    )
    monkeypatch.setattr("apps.rendering.api.ArtifactStore.from_settings", lambda: store)

    response = post_json(
        rendering_client,
        "/internal/rendering/descriptor",
        {
            "contract": "theorem.rendering.v1",
            "renderer": "plantuml",
            "artifact_id": store.payload_digest,
            "artifact_key": store.artifact_key,
            "payload_digest": store.payload_digest,
            "content_type": "image/svg+xml",
        },
    )

    assert response.status_code == 404, response.content
    assert store.presigns == []


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
        (
            "/internal/rendering/descriptor",
            {
                "contract": "theorem.rendering.v1",
                "renderer": "plantuml",
                "artifact_id": "sha256:" + "a" * 64,
                "artifact_key": "tenants/fixture/renders/" + "a" * 64 + ".svg",
                "payload_digest": "sha256:" + "a" * 64,
                "content_type": "image/svg+xml",
            },
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
