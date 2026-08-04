"""D6 — usage.accumulate bumps control_usage counters."""

from __future__ import annotations

from datetime import date

import pytest

from apps.billing.models import Usage
from apps.billing.usage import accumulate
from apps.tenancy.models import Tenant


@pytest.mark.django_db
def test_accumulate_bumps_counters():
    tenant = Tenant.objects.create(slug="usage-acme", display_name="Usage Acme")
    start = date(2026, 8, 1)
    end = date(2026, 8, 31)

    row = accumulate(tenant.id, "harness_turns", 3, start, end)
    assert row.counters["harness_turns"] == 3
    assert Usage.objects.filter(tenant=tenant).count() == 1

    row2 = accumulate(str(tenant.id), "harness_turns", 2, start, end)
    assert row2.id == row.id
    assert row2.counters["harness_turns"] == 5

    accumulate(tenant.id, "embedding_tokens", 100, start, end)
    row.refresh_from_db()
    assert row.counters["harness_turns"] == 5
    assert row.counters["embedding_tokens"] == 100
