"""End-to-end byte and deadline admission for theorem.layout.v1."""

from __future__ import annotations

import io
import json
import socketserver
import threading
import time
from uuid import uuid4

import pytest
from django.test import Client, RequestFactory, override_settings

from apps.layout import cache as layout_cache
from apps.keys.mint import mint_api_key
from apps.layout.budget import (
    LAYOUT_REQUEST_TIMEOUT_SECONDS,
    MAX_LAYOUT_REQUEST_BYTES,
    MAX_LAYOUT_RESPONSE_BYTES,
)
from apps.layout.cache import clear_memory_cache
from apps.layout.cache import set_cached_response
from apps.layout.contracts import LayoutRequest
from apps.layout.middleware import LayoutRequestBudgetMiddleware
from apps.layout.service import (
    LayoutExecutionTimeout,
    LayoutResponseTooLarge,
    _enforce_response_budget,
    compute_layout,
)
from apps.tenancy.models import Tenant


def _request_payload() -> dict[str, object]:
    return {
        "contract": "theorem.layout.v1",
        "graph_class": "plan_dag",
        "nodes": [
            {"id": "W01", "w_px": 120, "h_px": 48, "kind": "work"},
            {"id": "V01", "w_px": 96, "h_px": 40, "kind": "verification"},
        ],
        "edges": [{"id": "e01", "from": "W01", "to": "V01", "kind": "verifies"}],
        "params": {},
    }


def _body_at_size(size: int) -> bytes:
    encoded = json.dumps(_request_payload(), separators=(",", ":")).encode()
    assert len(encoded) <= size
    return encoded + (b" " * (size - len(encoded)))


@pytest.fixture
def admitted_client(db):
    tenant = Tenant.objects.create(
        slug=f"budget-{uuid4()}", display_name="Budget tenant"
    )
    minted = mint_api_key(tenant, scopes=["layout:compute"])
    return Client(HTTP_AUTHORIZATION=f"Bearer {minted.plaintext}")


@pytest.mark.django_db
def test_layout_route_accepts_exact_two_mib_request_and_propagates_deadline(
    admitted_client, monkeypatch
):
    observed_deadlines: list[tuple[float, float]] = []

    def compute(_body, *, tenant_slug, deadline):
        assert tenant_slug.startswith("budget-")
        observed_deadlines.append((deadline, time.monotonic()))
        return b'{"contract":"theorem.layout.v1"}'

    monkeypatch.setattr("apps.layout.api.compute_layout", compute)
    response = admitted_client.generic(
        "POST",
        "/internal/layout/compute",
        data=_body_at_size(MAX_LAYOUT_REQUEST_BYTES),
        content_type="application/json",
    )

    assert MAX_LAYOUT_REQUEST_BYTES == 2 * 1024 * 1024
    assert response.status_code == 200, response.content
    assert len(observed_deadlines) == 1
    deadline, observed_at = observed_deadlines[0]
    assert 0 < deadline - observed_at <= LAYOUT_REQUEST_TIMEOUT_SECONDS


@pytest.mark.django_db
def test_layout_route_refuses_two_mib_plus_one_before_layout_work(
    admitted_client, monkeypatch
):
    monkeypatch.setattr(
        "apps.layout.api.compute_layout",
        lambda *_args, **_kwargs: pytest.fail("oversized request reached layout work"),
    )

    response = admitted_client.generic(
        "POST",
        "/internal/layout/compute",
        data=_body_at_size(MAX_LAYOUT_REQUEST_BYTES + 1),
        content_type="application/json",
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "layout request exceeds 2097152 bytes"


class _ReadSpy(io.BytesIO):
    read_calls = 0

    def read(self, size=-1):
        self.read_calls += 1
        return super().read(size)


def test_layout_middleware_refuses_oversized_declared_length_before_body_read():
    request = RequestFactory().post(
        "/internal/layout/compute", data=b"{}", content_type="application/json"
    )
    stream = _ReadSpy(b"{}")
    request._stream = stream
    request.META["CONTENT_LENGTH"] = str(MAX_LAYOUT_REQUEST_BYTES + 1)
    middleware = LayoutRequestBudgetMiddleware(
        lambda _request: pytest.fail("oversized request reached downstream")
    )

    response = middleware(request)

    assert response.status_code == 413
    assert stream.read_calls == 0


def test_layout_middleware_does_not_apply_upload_budget_to_other_routes():
    request = RequestFactory().post(
        "/internal/rendering/plantuml",
        data=b"x" * (MAX_LAYOUT_REQUEST_BYTES + 1),
        content_type="application/octet-stream",
    )
    stream = _ReadSpy(b"not-read")
    request._stream = stream
    middleware = LayoutRequestBudgetMiddleware(lambda _request: object())

    response = middleware(request)

    assert response is not None
    assert stream.read_calls == 0


@pytest.mark.parametrize("declared_length", (None, "1"))
def test_layout_middleware_refuses_missing_or_lying_length_overflow(declared_length):
    request = RequestFactory().post(
        "/internal/layout/compute",
        data=b"x" * (MAX_LAYOUT_REQUEST_BYTES + 1),
        content_type="application/json",
    )
    if declared_length is None:
        request.META.pop("CONTENT_LENGTH", None)
    else:
        request.META["CONTENT_LENGTH"] = declared_length
    middleware = LayoutRequestBudgetMiddleware(
        lambda _request: pytest.fail("stream overflow reached downstream")
    )

    response = middleware(request)

    assert response.status_code == 413


def test_layout_response_budget_accepts_exact_two_mib_and_refuses_plus_one():
    exact = b"x" * MAX_LAYOUT_RESPONSE_BYTES
    assert MAX_LAYOUT_RESPONSE_BYTES == 2 * 1024 * 1024
    assert _enforce_response_budget(exact) is exact

    with pytest.raises(LayoutResponseTooLarge, match="2097152 bytes"):
        _enforce_response_budget(exact + b"x")


def test_oversized_cached_response_is_refused_before_graphviz(monkeypatch):
    body = LayoutRequest.model_validate(_request_payload())
    monkeypatch.setattr(
        "apps.layout.service.get_cached_response",
        lambda *_args, **_kwargs: b"x" * (MAX_LAYOUT_RESPONSE_BYTES + 1),
    )
    monkeypatch.setattr(
        "apps.layout.service._execute_worker",
        lambda *_args, **_kwargs: pytest.fail("oversized cache reached Graphviz"),
    )

    with pytest.raises(LayoutResponseTooLarge, match="2097152 bytes"):
        compute_layout(body, tenant_slug="tenant")


def test_whole_layout_deadline_includes_graphviz_completion(monkeypatch):
    class Clock:
        now = 100.0

        def monotonic(self) -> float:
            return self.now

    clock = Clock()
    body = LayoutRequest.model_validate(_request_payload())
    monkeypatch.setattr("apps.layout.service.monotonic", clock.monotonic)
    monkeypatch.setattr(
        "apps.layout.service.get_cached_response",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr("apps.layout.service.graphviz_version", lambda: "2.43.0")

    def worker(_dot, _engine, node_ids, *, timeout_seconds):
        assert timeout_seconds == 8.0
        clock.now += LAYOUT_REQUEST_TIMEOUT_SECONDS + 0.01
        return {node_id: {"x": 1.0, "y": 2.0} for node_id in node_ids}

    monkeypatch.setattr("apps.layout.service._execute_worker", worker)

    with pytest.raises(LayoutExecutionTimeout, match="whole-request deadline"):
        compute_layout(
            body,
            tenant_slug="tenant",
            deadline=clock.now + LAYOUT_REQUEST_TIMEOUT_SECONDS,
        )


@pytest.mark.django_db
def test_layout_route_returns_typed_whole_deadline_timeout(
    admitted_client, monkeypatch
):
    monkeypatch.setattr(
        "apps.layout.api.compute_layout",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            LayoutExecutionTimeout("layout request exceeded its whole-request deadline")
        ),
    )

    response = admitted_client.post(
        "/internal/layout/compute",
        data=json.dumps(_request_payload()),
        content_type="application/json",
    )

    assert response.status_code == 504
    assert "whole-request deadline" in response.json()["detail"]


def test_stalled_loopback_cache_is_bounded_and_layout_fails_open(monkeypatch):
    release = threading.Event()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.recv(4_096)
            release.wait(timeout=2.0)

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = Server(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    body = LayoutRequest.model_validate(_request_payload())
    monkeypatch.setattr("apps.layout.service.graphviz_version", lambda: "2.43.0")
    monkeypatch.setattr(
        "apps.layout.service._execute_worker",
        lambda _dot, _engine, node_ids, *, timeout_seconds: {
            node_id: {"x": 1.0, "y": 2.0} for node_id in node_ids
        },
    )
    clear_memory_cache()
    started = time.monotonic()
    try:
        with override_settings(
            VALKEY_URL=f"redis://127.0.0.1:{server.server_address[1]}/0",
            REDIS_URL="",
        ):
            encoded = compute_layout(
                body,
                tenant_slug="stalled-cache",
                deadline=started + 2.0,
            )
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1.0)
        clear_memory_cache()

    assert json.loads(encoded)["contract"] == "theorem.layout.v1"
    assert time.monotonic() - started < 1.5


def test_cache_write_is_skipped_when_whole_request_budget_is_insufficient(
    monkeypatch,
):
    now = 100.0
    monkeypatch.setattr("apps.layout.cache.time.monotonic", lambda: now)
    monkeypatch.setattr(
        "apps.layout.cache._redis_client",
        lambda *_args, **_kwargs: pytest.fail("late cache write opened Valkey"),
    )
    clear_memory_cache()

    set_cached_response(
        "tenant",
        "late-write",
        b"value",
        deadline=now + 0.01,
    )

    assert not layout_cache._memory
