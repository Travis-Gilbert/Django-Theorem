"""Typed theorem.layout.v1 request and response contracts."""

from __future__ import annotations

from typing import Literal

from ninja import Field, Schema
from pydantic import ConfigDict, field_validator

GraphClass = Literal[
    "plan_dag",
    "derivation",
    "query_plan",
    "code_map_small",
    "code_map_large",
    "neighborhood",
    "ego_radial",
    "cyclic_ring",
    "containment",
]


class StrictSchema(Schema):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class LayoutNode(StrictSchema):
    id: str
    w_px: float
    h_px: float
    kind: str = "node"
    cluster: str | None = None

    @field_validator("id", "kind")
    @classmethod
    def non_empty_identifier(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class LayoutEdge(StrictSchema):
    id: str
    from_: str = Field(alias="from")
    to: str
    kind: str = "edge"

    @field_validator("id", "from_", "to", "kind")
    @classmethod
    def non_empty_identifier(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class LayoutParams(StrictSchema):
    focus_id: str | None = None


class LayoutRequest(StrictSchema):
    contract: Literal["theorem.layout.v1"] = "theorem.layout.v1"
    graph_class: GraphClass | None = None
    nodes: list[LayoutNode]
    edges: list[LayoutEdge]
    params: LayoutParams = Field(default_factory=LayoutParams)


class LayoutPosition(StrictSchema):
    id: str
    x_px: float
    y_px: float


class LayoutResponse(StrictSchema):
    contract: Literal["theorem.layout.v1"] = "theorem.layout.v1"
    positions: list[LayoutPosition]
    engine: str
    graphviz_version: str
    policy_id: GraphClass
    input_digest: str
