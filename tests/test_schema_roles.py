"""A1 — schema/role privileges. Skipped when Postgres is unavailable."""

from __future__ import annotations

import os

import pytest
from django.conf import settings
from django.db import connection, DatabaseError

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgres")
    and "postgresql" not in settings.DATABASES["default"].get("ENGINE", ""),
    reason="Postgres required for role privilege checks",
)


def _roles_applied() -> tuple[bool, str]:
    """Return (ok, reason) whether theorem_control / theorem_spine roles exist."""
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT rolname FROM pg_roles WHERE rolname IN ('theorem_control', 'theorem_spine')"
            )
            found = {row[0] for row in cursor.fetchall()}
    except Exception as exc:  # noqa: BLE001
        return False, f"could not query pg_roles: {exc}"
    missing = {"theorem_control", "theorem_spine"} - found
    if missing:
        return False, f"roles.sql not applied; missing roles: {sorted(missing)}"
    return True, ""


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
        "control_extractionjob": {
            "id",
            "tenant_id",
            "operation",
            "contract_version",
            "source_kind",
            "source_ref",
            "params",
            "params_hash",
            "status",
            "shard_count",
            "rows_total",
            "error",
            "created_at",
            "updated_at",
        },
        "control_extractionreview": {
            "id",
            "tenant_id",
            "job_id",
            "candidate_digest",
            "candidate_digest_version",
            "claim_id",
            "decision",
            "merge_target_claim_id",
            "reason",
            "reviewer",
            "created_at",
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


def _permission_denied(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "permission denied" in msg or "must be owner" in msg


@pytest.mark.django_db
def test_theorem_control_cannot_create_table_in_spine():
    ok, reason = _roles_applied()
    if not ok:
        pytest.skip(reason)
    with connection.cursor() as cursor:
        try:
            cursor.execute("SET ROLE theorem_control")
        except DatabaseError as exc:
            pytest.skip(f"cannot SET ROLE theorem_control: {exc}")
        try:
            with pytest.raises(DatabaseError) as excinfo:
                cursor.execute("CREATE TABLE spine.a1_forbidden_control_create (id int)")
            assert _permission_denied(excinfo.value), excinfo.value
        finally:
            cursor.execute("RESET ROLE")


@pytest.mark.django_db
def test_theorem_spine_cannot_insert_control_tenant():
    ok, reason = _roles_applied()
    if not ok:
        pytest.skip(reason)
    with connection.cursor() as cursor:
        try:
            cursor.execute("SET ROLE theorem_spine")
        except DatabaseError as exc:
            pytest.skip(f"cannot SET ROLE theorem_spine: {exc}")
        try:
            with pytest.raises(DatabaseError) as excinfo:
                cursor.execute(
                    """
                    INSERT INTO control.control_tenant (id, slug, display_name, is_active)
                    VALUES (gen_random_uuid(), 'a1-forbidden', 'forbidden', true)
                    """
                )
            assert _permission_denied(excinfo.value), excinfo.value
        finally:
            cursor.execute("RESET ROLE")


@pytest.mark.django_db
def test_theorem_spine_can_select_control_tenant():
    ok, reason = _roles_applied()
    if not ok:
        pytest.skip(reason)
    with connection.cursor() as cursor:
        try:
            cursor.execute("SET ROLE theorem_spine")
        except DatabaseError as exc:
            pytest.skip(f"cannot SET ROLE theorem_spine: {exc}")
        try:
            cursor.execute("SELECT id, slug, display_name, is_active FROM control.control_tenant LIMIT 1")
            cursor.fetchall()
        finally:
            cursor.execute("RESET ROLE")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("table", "columns"),
    [
        (
            "control_extractionjob",
            "id, tenant_id, operation, contract_version, source_kind, source_ref, "
            "params, params_hash, status, shard_count, rows_total, error, "
            "created_at, updated_at",
        ),
        (
            "control_extractionreview",
            "id, tenant_id, job_id, candidate_digest, candidate_digest_version, "
            "claim_id, decision, merge_target_claim_id, reason, reviewer, created_at",
        ),
    ],
)
def test_theorem_spine_can_select_extraction_read_models(table, columns):
    ok, reason = _roles_applied()
    if not ok:
        pytest.skip(reason)
    with connection.cursor() as cursor:
        try:
            cursor.execute("SET ROLE theorem_spine")
        except DatabaseError as exc:
            pytest.skip(f"cannot SET ROLE theorem_spine: {exc}")
        try:
            cursor.execute(f"SELECT {columns} FROM control.{table} LIMIT 1")
            cursor.fetchall()
        finally:
            cursor.execute("RESET ROLE")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "table",
    ["control_extractionjob", "control_extractionreview"],
)
def test_theorem_spine_cannot_insert_extraction_read_models(table):
    ok, reason = _roles_applied()
    if not ok:
        pytest.skip(reason)
    with connection.cursor() as cursor:
        try:
            cursor.execute("SET ROLE theorem_spine")
        except DatabaseError as exc:
            pytest.skip(f"cannot SET ROLE theorem_spine: {exc}")
        try:
            with pytest.raises(DatabaseError) as excinfo:
                cursor.execute(f"INSERT INTO control.{table} DEFAULT VALUES")
            assert _permission_denied(excinfo.value), excinfo.value
        finally:
            cursor.execute("RESET ROLE")
