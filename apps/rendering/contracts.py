"""Typed rendering request and response contracts."""

from __future__ import annotations

from typing import Literal

from ninja import Schema
from pydantic import ConfigDict, field_validator


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
