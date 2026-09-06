"""Exercise the actual MCP wire codec against a named fixture transport."""

import json
import httpx
import pytest

from theorem_ml.extract.spans.labels import SchemaClient, SchemaProtocolError


def test_effective_fields_uses_registered_tool_and_preserves_tenant_auth(monkeypatch):
    seen = []
    def fixture_server(request):
        payload = json.loads(request.content)
        seen.append(payload)
        assert request.headers["authorization"] == "Bearer fixture-token"
        if payload["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                "result": {"protocolVersion": "2025-03-26"}}, headers={"mcp-session-id": "fixture-session"})
        assert request.headers["mcp-session-id"] == "fixture-session"
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        assert payload["params"] == {"name": "schema_effective_fields",
                                      "arguments": {"tenant": "tenant-a", "object_type": "Lease"}}
        return httpx.Response(200, text='event: message\ndata: '+json.dumps({"jsonrpc": "2.0", "id": 2,
            "result": {"content": [{"type": "text", "text": json.dumps({"fields": []})}]}})+'\n\n',
            headers={"content-type": "text/event-stream"})
    actual_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: actual_client(transport=httpx.MockTransport(fixture_server), **kwargs))
    assert SchemaClient("https://fixture.invalid/mcp", "fixture-token").effective_fields("tenant-a", "Lease") == {"fields": []}
    assert len(seen) == 3


def test_schema_refuses_missing_identity_before_network():
    with pytest.raises(ValueError, match="tenant"):
        SchemaClient().effective_fields("", "Lease")


def test_mcp_error_cannot_be_read_as_empty_schema():
    response = httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "error": {"code": -32603}},
                              request=httpx.Request("POST", "https://fixture.invalid"))
    with pytest.raises(SchemaProtocolError):
        SchemaClient._response(response, 2)
