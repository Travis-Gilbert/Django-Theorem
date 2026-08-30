"""Authenticated internal rendering APIs."""

from __future__ import annotations

import subprocess
import time

from ninja import Router
from ninja.errors import HttpError

from apps.keys.auth import RENDERING_RENDER_SCOPE, require_machine_key
from apps.orchestration.artifacts import (
    ArtifactConfigurationError,
    ArtifactNotFoundError,
    ArtifactStorageError,
    ArtifactStore,
    ArtifactValidationError,
    is_sha256_digest,
    sha256_digest,
)
from apps.rendering.contracts import (
    DiagramsRenderRequest,
    PlantUmlRenderRequest,
    RefreshedRenderArtifact,
    RenderArtifact,
    RenderDescriptorRequest,
    RenderDescriptorResponse,
    RenderResponse,
)
from apps.rendering.service import (
    RenderExecutionError,
    RenderExecutionTimeout,
    render_diagrams,
    render_plantuml,
)
from apps.rendering.validation import DiagramSourceError

router = Router(tags=["rendering"])


def _render_and_store(request, *, renderer: str, render):
    principal = require_machine_key(request, scope=RENDERING_RENDER_SCOPE)
    try:
        payload, media_type, version = render()
        store = ArtifactStore.from_settings()
        stored = store.write_render_artifact(
            principal.tenant.id, payload, media_type=media_type
        )
        download_url = store.presign_get(principal.tenant.id, stored.artifact_key)
    except (ValueError, DiagramSourceError, ArtifactValidationError) as exc:
        raise HttpError(422, str(exc)) from exc
    except RenderExecutionTimeout as exc:
        raise HttpError(504, str(exc)) from exc
    except (
        RenderExecutionError,
        ArtifactConfigurationError,
        ArtifactStorageError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        raise HttpError(503, "renderer unavailable") from exc
    artifact = RenderArtifact(
        artifact_id=stored.payload_digest,
        artifact_key=stored.artifact_key,
        payload_digest=stored.payload_digest,
        content_type=stored.media_type,
        byte_length=stored.byte_length,
        download_url=download_url,
    )
    return RenderResponse(renderer=renderer, renderer_version=version, artifact=artifact)


@router.post("/plantuml", response=RenderResponse)
def plantuml_render(request, body: PlantUmlRenderRequest):
    return _render_and_store(
        request,
        renderer="plantuml",
        render=lambda: render_plantuml(body.source),
    )


@router.post("/diagrams", response=RenderResponse)
def diagrams_render(request, body: DiagramsRenderRequest):
    return _render_and_store(
        request,
        renderer="diagrams",
        render=lambda: render_diagrams(body.source, body.format),
    )


@router.post("/descriptor", response=RenderDescriptorResponse)
def refresh_render_descriptor(request, body: RenderDescriptorRequest):
    principal = require_machine_key(request, scope=RENDERING_RENDER_SCOPE)
    try:
        if (
            body.artifact_id != body.payload_digest
            or not is_sha256_digest(body.payload_digest)
        ):
            raise ArtifactValidationError("render artifact digest is malformed")
        extension = "svg" if body.content_type == "image/svg+xml" else "png"
        store = ArtifactStore.from_settings()
        expected_key = store.render_artifact_key(
            principal.tenant.id, body.payload_digest, extension
        )
        if body.artifact_key != expected_key:
            raise ArtifactValidationError(
                "artifact_key is outside the admitted tenant render scope"
            )
        payload = store.get_bytes(principal.tenant.id, body.artifact_key)
        if sha256_digest(payload) != body.payload_digest:
            raise ArtifactValidationError(
                "render artifact bytes do not match the durable payload digest"
            )
        download_url = store.presign_get(principal.tenant.id, body.artifact_key)
    except (ValueError, ArtifactValidationError) as exc:
        raise HttpError(422, str(exc)) from exc
    except ArtifactNotFoundError as exc:
        raise HttpError(404, "render artifact missing") from exc
    except (
        ArtifactConfigurationError,
        ArtifactStorageError,
        OSError,
    ) as exc:
        raise HttpError(503, "render artifact unavailable") from exc

    artifact = RefreshedRenderArtifact(
        artifact_id=body.artifact_id,
        artifact_key=body.artifact_key,
        payload_digest=body.payload_digest,
        content_type=body.content_type,
        byte_length=len(payload),
        download_url=download_url,
        expires_at_ms=int((time.time() + store.presign_seconds) * 1000),
    )
    return RenderDescriptorResponse(renderer=body.renderer, artifact=artifact)
