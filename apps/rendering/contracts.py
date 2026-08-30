"""Typed rendering request and response contracts."""

from __future__ import annotations

from typing import Literal

from ninja import Schema
from pydantic import ConfigDict, field_validator, model_validator


class StrictSchema(Schema):
    model_config = ConfigDict(extra="forbid")


class PlantUmlRenderRequest(StrictSchema):
    contract: Literal["theorem.rendering.v1"] = "theorem.rendering.v1"
    source: str
    format: Literal["svg"] = "svg"

    @field_validator("source")
    @classmethod
    def source_is_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source must not be empty")
        return value


class DiagramsRenderRequest(StrictSchema):
    contract: Literal["theorem.rendering.v1"] = "theorem.rendering.v1"
    source: str
    format: Literal["png", "svg"] = "png"

    @field_validator("source")
    @classmethod
    def source_is_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source must not be empty")
        return value


class RenderArtifact(StrictSchema):
    artifact_id: str
    artifact_key: str
    payload_digest: str
    content_type: str
    byte_length: int
    download_url: str


class RenderResponse(StrictSchema):
    contract: Literal["theorem.rendering.v1"] = "theorem.rendering.v1"
    renderer: Literal["plantuml", "diagrams"]
    renderer_version: str
    artifact: RenderArtifact


class RenderDescriptorRequest(StrictSchema):
    contract: Literal["theorem.rendering.v1"] = "theorem.rendering.v1"
    renderer: Literal["plantuml", "diagrams"]
    artifact_id: str
    artifact_key: str
    payload_digest: str
    content_type: Literal["image/svg+xml", "image/png"]

    @field_validator("artifact_id", "artifact_key", "payload_digest")
    @classmethod
    def identity_is_bounded(cls, value: str) -> str:
        if not value.strip() or len(value) > 512 or any(
            character in value for character in ("\n", "\r", "\0")
        ):
            raise ValueError("artifact identity is malformed")
        return value

    @model_validator(mode="after")
    def renderer_and_media_type_agree(self):
        if self.renderer == "plantuml" and self.content_type != "image/svg+xml":
            raise ValueError("PlantUML descriptors require SVG content")
        return self


class RefreshedRenderArtifact(StrictSchema):
    artifact_id: str
    artifact_key: str
    payload_digest: str
    content_type: Literal["image/svg+xml", "image/png"]
    byte_length: int
    download_url: str
    expires_at_ms: int


class RenderDescriptorResponse(StrictSchema):
    contract: Literal["theorem.rendering.v1"] = "theorem.rendering.v1"
    renderer: Literal["plantuml", "diagrams"]
    artifact: RefreshedRenderArtifact
