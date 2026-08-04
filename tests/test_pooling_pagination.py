"""A12 — pagination smoke under DISABLE_SERVER_SIDE_CURSORS (PgBouncer)."""

from __future__ import annotations

import pytest
from django.conf import settings
from django.test import override_settings

from apps.tenancy.models import Tenant


@pytest.mark.django_db
@override_settings()
def test_paginate_tenants_without_server_side_cursor():
    assert settings.DATABASES["default"].get("DISABLE_SERVER_SIDE_CURSORS") is True

    Tenant.objects.bulk_create(
        [
            Tenant(slug=f"tenant-page-{i:03d}", display_name=f"Tenant {i}")
            for i in range(50)
        ]
    )
    assert Tenant.objects.count() >= 50

    # Slice pagination (offset/limit) — must not require a server-side cursor.
    page = list(Tenant.objects.order_by("slug")[10:20])
    assert len(page) == 10

    # iterator() with chunk_size uses FETCH without HOLD when cursors disabled;
    # under SQLite / Postgres with DISABLE_SERVER_SIDE_CURSORS it must not raise.
    seen = 0
    for _row in Tenant.objects.order_by("slug").iterator(chunk_size=10):
        seen += 1
    assert seen >= 50
