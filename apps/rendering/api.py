"""Authenticated internal rendering APIs."""

from __future__ import annotations

import subprocess

from ninja import Router
from ninja.errors import HttpError

from apps.keys.auth import RENDERING_RENDER_SCOPE, require_machine_key
from apps.orchestration.artifacts import (
    ArtifactConfigurationError,
    ArtifactStorageError,
    ArtifactStore,
    ArtifactValidationError,
)
from apps.rendering.contracts import (
    DiagramsRenderRequest,
    PlantUmlRenderRequest,
    RenderArtifact,
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
