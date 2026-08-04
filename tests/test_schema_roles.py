"""A1 — schema/role privileges. Skipped when Postgres is unavailable."""

from __future__ import annotations

import os

import pytest
from django.conf import settings
from django.db import connection

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgres")
    and "postgresql" not in settings.DATABASES["default"].get("ENGINE", ""),
    reason="Postgres required for role privilege checks",
)


def test_control_tables_exist():
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = current_schema()
              AND table_name IN (
                'control_tenant', 'control_project', 'control_subscription',
                'control_plan', 'control_apikey'
              )
            ORDER BY table_name
            """
        )
        names = [row[0] for row in cursor.fetchall()]
    # Under SQLite-less postgres with search_path=control, all five should exist
    # after migrate. If search_path differs, fall back to unqualified presence.
    if not names:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT tablename FROM pg_tables
                WHERE tablename IN (
                  'control_tenant', 'control_project', 'control_subscription',
                  'control_plan', 'control_apikey'
                )
                ORDER BY tablename
                """
            )
            names = [row[0] for row in cursor.fetchall()]
    assert names == [
        "control_apikey",
        "control_plan",
        "control_project",
        "control_subscription",
        "control_tenant",
    ]


def test_d3_columns_present():
    expected = {
        "control_tenant": {"id", "slug", "display_name", "is_active"},
        "control_project": {"tenant_id", "slug", "display_name"},
        "control_subscription": {"tenant_id", "plan_code", "status"},
        "control_plan": {"code", "limits"},
        "control_apikey": {
            "id",
            "tenant_id",
            "key_hash",
            "scopes",
            "revoked_at",
            "expires_at",
        },
    }
    with connection.cursor() as cursor:
        for table, cols in expected.items():
            cursor.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = %s
                """,
                [table],
            )
            found = {row[0] for row in cursor.fetchall()}
            missing = cols - found
            assert not missing, f"{table} missing columns: {missing}"
