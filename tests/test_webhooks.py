"""A5 — WorkOS webhook signature + shadow rows."""

from __future__ import annotations

import json

import pytest
from django.test import Client

from apps.identity.models import Membership, User
from apps.identity.webhooks import make_test_signature
from apps.tenancy.models import Tenant


@pytest.fixture
def client():
    return Client()


@pytest.mark.django_db
def test_rejects_bad_signature(client):
    body = json.dumps({"event": "user.created", "data": {"id": "user_1"}}).encode()
    resp = client.post(
        "/webhooks/workos",
        data=body,
        content_type="application/json",
        headers={"WorkOS-Signature": "v1=deadbeef"},
    )
    assert resp.status_code == 401
    assert User.objects.count() == 0


def test_rejects_missing_signature(client):
    body = b'{"event":"user.created","data":{"id":"user_1"}}'
    resp = client.post("/webhooks/workos", data=body, content_type="application/json")
    assert resp.status_code == 401


@pytest.mark.django_db
def test_accepts_valid_signature_and_writes_membership(client):
    Tenant.objects.create(
        slug="acme",
        display_name="Acme",
        workos_organization_id="org_1",
    )
    payload = {
        "event": "organization_membership.created",
        "data": {
            "id": "om_1",
            "organization_id": "org_1",
            "user_id": "user_42",
            "role": {"slug": "admin"},
        },
    }
    body = json.dumps(payload).encode()
    sig = make_test_signature(body)
    resp = client.post(
        "/webhooks/workos",
        data=body,
        content_type="application/json",
        headers={"WorkOS-Signature": sig},
    )
    assert resp.status_code == 200
    user = User.objects.get(workos_user_id="user_42")
    m = Membership.objects.get(user=user, workos_membership_id="om_1")
    assert m.role == "admin"
    assert m.tenant.slug == "acme"
