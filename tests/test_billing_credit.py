"""Credit reads for the TheoremWeb status band: spend, allowance, and refusal."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from django.test import Client
from django.utils import timezone

from apps.billing.credit import calendar_period, credit_allowance
from apps.billing.models import Plan, Subscription, Usage
from apps.billing.usage import accumulate
from apps.keys.mint import mint_api_key
from apps.tenancy.models import Tenant


def _tenant(name: str) -> Tenant:
    return Tenant.objects.create(slug=f"{name}-{uuid.uuid4()}", display_name=name)


def _client(tenant: Tenant, scopes: list[str]) -> Client:
    client = Client()
    minted = mint_api_key(tenant, scopes=scopes)
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {minted.plaintext}"
    return client


def test_the_period_is_the_calendar_month():
    assert calendar_period(date(2026, 2, 14)) == (date(2026, 2, 1), date(2026, 2, 28))
    assert calendar_period(date(2028, 2, 29)) == (date(2028, 2, 1), date(2028, 2, 29))


@pytest.mark.django_db
def test_a_new_subscriber_has_spent_nothing_and_no_stated_limit():
    tenant = _tenant("fresh")
    response = _client(tenant, ["billing:read"]).get("/internal/billing/credit")
    assert response.status_code == 200, response.content
    start, end = calendar_period(timezone.now().date())
    assert response.json() == {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "used_usd": 0.0,
        "limit_usd": None,
    }


@pytest.mark.django_db
def test_spend_and_the_plan_allowance_are_reported_for_the_tenant_alone():
    tenant, other = _tenant("subscriber"), _tenant("neighbour")
    plan = Plan.objects.create(code=f"credit-{uuid.uuid4().hex[:8]}", limits={"monthly_credit_usd": 20})
    Subscription.objects.create(tenant=tenant, plan=plan)
    start, end = calendar_period(timezone.now().date())
    accumulate(tenant.id, "credit_usd_micros", 3_200_000, start, end)
    accumulate(other.id, "credit_usd_micros", 9_000_000, start, end)

    response = _client(tenant, ["billing:*"]).get("/internal/billing/credit")
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["used_usd"] == pytest.approx(3.2)
    assert body["limit_usd"] == pytest.approx(20.0)
    assert body["period_start"] == start.isoformat()
    assert body["period_end"] == end.isoformat()


@pytest.mark.django_db
def test_a_canceled_plan_states_no_limit_and_a_negative_one_is_ignored():
    tenant = _tenant("lapsed")
    canceled = Plan.objects.create(code=f"old-{uuid.uuid4().hex[:8]}", limits={"monthly_credit_usd": 50})
    Subscription.objects.create(tenant=tenant, plan=canceled, status=Subscription.Status.CANCELED)
    today = timezone.now().date()
    assert credit_allowance(tenant.id, today).limit_usd is None

    broken = Plan.objects.create(code=f"neg-{uuid.uuid4().hex[:8]}", limits={"monthly_credit_usd": -1})
    Subscription.objects.create(tenant=tenant, plan=broken)
    assert credit_allowance(tenant.id, today).limit_usd is None

    unreadable = Plan.objects.create(
        code=f"bad-{uuid.uuid4().hex[:8]}", limits={"monthly_credit_usd": "unlimited"}
    )
    tenant_unreadable = _tenant("unreadable")
    Subscription.objects.create(tenant=tenant_unreadable, plan=unreadable)
    assert credit_allowance(tenant_unreadable.id, today).limit_usd is None


@pytest.mark.django_db
def test_an_unreadable_meter_is_reported_as_nothing_spent():
    tenant = _tenant("corrupt")
    start, end = calendar_period(timezone.now().date())
    Usage.objects.create(
        tenant=tenant,
        period_start=start,
        period_end=end,
        counters={"credit_usd_micros": "not-a-meter"},
    )
    allowance = credit_allowance(tenant.id, timezone.now().date())
    assert allowance.used_usd == 0
    response = _client(tenant, ["billing:read"]).get("/internal/billing/credit")
    assert response.status_code == 200, response.content
    assert response.json()["used_usd"] == 0.0


@pytest.mark.django_db
def test_credit_is_refused_without_the_billing_scope():
    tenant = _tenant("scoped")
    assert Client().get("/internal/billing/credit").status_code == 401
    refused = _client(tenant, ["offload:*"]).get("/internal/billing/credit")
    assert refused.status_code == 403
    assert refused.json()["detail"] == "machine API key lacks billing:read scope"
    # The positive control in the same fixture: the scope is what was missing.
    assert _client(tenant, ["billing:read"]).get("/internal/billing/credit").status_code == 200
