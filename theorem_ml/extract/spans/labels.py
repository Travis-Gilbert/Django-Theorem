"""Fresh tenant effective-fields discovery over the schema MCP surface."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from typing import Any

import httpx


class SchemaProtocolError(RuntimeError):
    pass


class SchemaClient:
    """Synchronous Streamable HTTP MCP client with explicit tenant and auth.

    Each label compilation re-reads effective_fields. A local label cache would
    conceal a user's declaration changes on the next extraction run.
    """

    def __init__(self, url: str | None = None, token: str | None = None) -> None:
        self.url = url or os.environ.get("THEOREM_SCHEMA_MCP_URL", "")
        self.token = token or os.environ.get("THEOREM_SCHEMA_MCP_TOKEN", "")

    @staticmethod
    def _response(response: httpx.Response, request_id: int) -> dict:
        response.raise_for_status()
        if "text/event-stream" in response.headers.get("content-type", ""):
            messages = []
            for event in response.text.replace("\r\n", "\n").split("\n\n"):
                data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
                if data:
                    messages.append(json.loads(data))
            matching = [m for m in messages if m.get("id") == request_id]
            if not matching:
                raise SchemaProtocolError("MCP response omitted the requested result")
            payload = matching[-1]
        else:
            payload = response.json()
        if payload.get("id") != request_id or "error" in payload:
            raise SchemaProtocolError("Schema MCP request failed or returned a mismatched id")
        return payload["result"]

    def call(self, tenant: str, action: str, **arguments: Any) -> dict:
        names = {"effective_fields": "schema_effective_fields",
                 "extract_fields": "schema_extract_fields",
                 "link_or_pend": "resolve_link_or_pend", "read": "extraction_read",
                 "publish": "extraction_publish"}
        if action not in names:
            raise SchemaProtocolError(f"Unsupported extraction MCP action: {action}")
        return self.call_tool(tenant, names[action], {"tenant": tenant, **arguments})

    def call_tool(self, tenant: str, name: str, arguments: dict) -> dict:
        """Call an explicit tool; schema/record tools derive tenant from auth.

        Their strict input schemas do not accept an invented tenant argument.
        Callers must check returned tenant ownership before using created IDs.
        """
        if not tenant or not tenant.strip():
            raise ValueError("An authenticated tenant is required")
        if not self.url or not self.token:
            raise SchemaProtocolError("Configure THEOREM_SCHEMA_MCP_URL and THEOREM_SCHEMA_MCP_TOKEN")
        headers = {"Authorization": f"Bearer {self.token}",
                   "Accept": "application/json, text/event-stream"}
        with httpx.Client(timeout=60, headers=headers) as client:
            response = client.post(self.url, json={"jsonrpc": "2.0", "id": 1,
                "method": "initialize", "params": {"protocolVersion": "2025-03-26",
                "capabilities": {}, "clientInfo": {"name": "theorem-extraction", "version": "2.0"}}})
            initialized = self._response(response, 1)
            if response.headers.get("mcp-session-id"):
                client.headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
            client.headers["MCP-Protocol-Version"] = initialized["protocolVersion"]
            client.post(self.url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).raise_for_status()
            response = client.post(self.url, json={"jsonrpc": "2.0", "id": 2,
                "method": "tools/call", "params": {"name": name, "arguments": arguments}})
            result = self._response(response, 2)
        if result.get("isError"):
            raise SchemaProtocolError("Schema tool rejected the request")
        if "structuredContent" in result:
            return result["structuredContent"]
        for content in result.get("content", []):
            if content.get("type") == "text":
                value = json.loads(content["text"])
                if isinstance(value, dict):
                    return value
        raise SchemaProtocolError("Schema tool returned no structured result")

    def effective_fields(self, tenant: str, object_type: str) -> dict:
        result = self.call(tenant, "effective_fields", object_type=object_type)
        fields = result.get("fields")
        if not isinstance(fields, list):
            raise SchemaProtocolError("effective_fields must return a fields array")
        return result


@dataclass(frozen=True)
class LabelSpec:
    label: str
    description: str
    object_type: str
    field_key: str
    target_object_type_id: str | None = None


@dataclass
class LabelSchema:
    labels: list[LabelSpec] = field(default_factory=list)
    classifications: dict[str, dict] = field(default_factory=dict)


def labels_from_fields(object_type: str, fields: list[dict]) -> LabelSchema:
    schema = LabelSchema()
    seen = set()
    for spec in fields:
        key = spec["key"]
        if key in seen:
            raise SchemaProtocolError(f"Duplicate effective field: {object_type}.{key}")
        seen.add(key)
        field_type = spec["field_type"]
        kind = field_type["kind"]
        # Type-qualified labels prevent different declared types sharing a key
        # from overwriting one another in a composed extraction schema.
        label = f"{object_type}.{key}"
        if kind == "relation" or spec.get("span", False):
            schema.labels.append(LabelSpec(label, spec.get("description") or spec.get("label") or key,
                object_type, key, field_type.get("target_object_type_id") if kind == "relation" else None))
        if kind in {"enum", "enum_many"}:
            schema.classifications[label] = {"labels": list(field_type["variants"]),
                                            "multi_label": kind == "enum_many"}
    return schema


def compile_labels(client: SchemaClient, tenant: str, object_types: list[str]) -> LabelSchema:
    combined = LabelSchema()
    for name in dict.fromkeys(object_types):
        declared = client.effective_fields(tenant, name)
        schema = labels_from_fields(declared.get("name_singular", name), declared["fields"])
        combined.labels.extend(schema.labels)
        combined.classifications.update(schema.classifications)
    return combined
