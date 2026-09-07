"""Authenticated HTTP proof using real discovery and a local MLflow store."""

import json

import pytest

pytest.importorskip("django")

from django.test import Client
from apps.keys.mint import mint_api_key
from apps.tenancy.models import Tenant


@pytest.mark.django_db
def test_discovery_http_admits_machine_key_and_enforces_tenant(tmp_path, monkeypatch):
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tmp_path.as_uri())
    tenant = Tenant.objects.create(slug="index-test", display_name="Index")
    key = mint_api_key(tenant, scopes=["offload:invoke"])
    client = Client(HTTP_AUTHORIZATION=f"Bearer {key.plaintext}")
    payload = {
        "executor": "discovery",
        "items": [
            {
                "id": "item-a",
                "tenant_id": tenant.slug,
                "embedding": [1.0, 0.0],
                "title": "Rust compiler",
            }
        ],
    }
    response = client.post(
        "/internal/index/execute",
        data=json.dumps(payload),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    assert response.json()["executor"]["mlflow_run_id"]
    assert (
        response.json()["proposal_nodes"][0]["properties"]["tenant_id"] == tenant.slug
    )
    payload["items"][0]["tenant_id"] = "another-tenant"
    response = client.post(
        "/internal/index/execute",
        data=json.dumps(payload),
        content_type="application/json",
    )
    assert response.status_code == 422
    assert (
        Client()
        .post(
            "/internal/index/execute",
            data=json.dumps(payload),
            content_type="application/json",
        )
        .status_code
        == 401
    )


@pytest.mark.django_db
def test_discovery_http_rejects_insufficient_scope():
    tenant = Tenant.objects.create(slug="index-scope", display_name="Index")
    key = mint_api_key(tenant, scopes=["offload:read"])
    client = Client(HTTP_AUTHORIZATION=f"Bearer {key.plaintext}")
    assert (
        client.post(
            "/internal/index/execute",
            data=json.dumps({"executor": "discovery", "items": []}),
            content_type="application/json",
        ).status_code
        == 403
    )
