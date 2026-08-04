"""D4 — membership Valkey invalidate called from webhook handler."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from django.test import Client

from apps.identity.webhooks import make_test_signature
from apps.tenancy.models import Tenant
from apps.tenancy.tenant_cache import (
    clear_memory_cache,
    get_tenant_slug_for_org,
    set_tenant_slug_for_org,
)


@pytest.fixture
def client():
    return Client()


@pytest.mark.django_db
def test_membership_webhook_calls_invalidate(client):
    Tenant.objects.create(
        slug="acme",
        display_name="Acme",
        workos_organization_id="org_1",
    )
    payload = {
        "event": "organization_membership.created",
        "data": {
            "id": "om_inv_1",
            "organization_id": "org_1",
            "user_id": "user_inv",
            "role": {"slug": "member"},
        },
    }
    body = json.dumps(payload).encode()
    sig = make_test_signature(body)

    with patch("apps.identity.webhooks.invalidate_membership") as mock_inv:
        resp = client.post(
            "/webhooks/workos",
            data=body,
            content_type="application/json",
            headers={"WorkOS-Signature": sig},
        )
        assert resp.status_code == 200
        mock_inv.assert_called_once_with("acme", "user_inv")


@pytest.mark.django_db
def test_membership_delete_calls_invalidate(client):
    from apps.identity.models import Membership, User

    tenant = Tenant.objects.create(
        slug="acme-del",
        display_name="Acme Del",
        workos_organization_id="org_del",
    )
    user = User.objects.create(workos_user_id="user_del")
    Membership.objects.create(
        tenant=tenant,
        user=user,
        workos_membership_id="om_del",
    )
    payload = {
        "event": "organization_membership.deleted",
        "data": {
            "id": "om_del",
            "organization_id": "org_del",
            "user_id": "user_del",
        },
    }
    body = json.dumps(payload).encode()
    sig = make_test_signature(body)

    with patch("apps.identity.webhooks.invalidate_membership") as mock_inv:
        resp = client.post(
            "/webhooks/workos",
            data=body,
            content_type="application/json",
            headers={"WorkOS-Signature": sig},
        )
        assert resp.status_code == 200
        mock_inv.assert_called_once_with("acme-del", "user_del")
    assert Membership.objects.filter(workos_membership_id="om_del").count() == 0


@pytest.mark.django_db
def test_org_webhook_warms_tenant_slug_cache(client):
    clear_memory_cache()
    payload = {
        "event": "organization.created",
        "data": {
            "id": "org_warm",
            "name": "Warm Co",
            "slug": "warm-co",
        },
    }
    body = json.dumps(payload).encode()
    sig = make_test_signature(body)
    with patch("apps.identity.webhooks.set_tenant_slug_for_org", wraps=set_tenant_slug_for_org) as mock_set:
        resp = client.post(
            "/webhooks/workos",
            data=body,
            content_type="application/json",
            headers={"WorkOS-Signature": sig},
        )
        assert resp.status_code == 200
        mock_set.assert_called_once()
        assert mock_set.call_args[0][0] == "org_warm"
        assert mock_set.call_args[0][1] == "warm-co"
    assert get_tenant_slug_for_org("org_warm") == "warm-co"
