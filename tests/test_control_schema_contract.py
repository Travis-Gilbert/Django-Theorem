"""A3 companion — control schema contract on the Django side.

The live Postgres oracle remains Theorem's ignored catalog test:

    CONTROL_DATABASE_URL=... cargo test -p rustyred-thg-catalog \\
      --lib control_schema_contract_holds -- --ignored

This module freezes the Django half of that contract: every table/column the
RustyRed control read-model declares must exist on a Django model whose
``db_table`` matches, so migration drift fails before the live DB check.
"""

from __future__ import annotations

from django.apps import apps

# Keep in lockstep with rustyred_thg_catalog::CONTROL_SCHEMA_CONTRACT.
CONTROL_SCHEMA_CONTRACT: list[tuple[str, str]] = [
    ("control_tenant", "id"),
    ("control_tenant", "slug"),
    ("control_tenant", "display_name"),
    ("control_tenant", "is_active"),
    ("control_project", "tenant_id"),
    ("control_project", "slug"),
    ("control_project", "display_name"),
    ("control_subscription", "tenant_id"),
    ("control_subscription", "plan_code"),
    ("control_subscription", "status"),
    ("control_plan", "code"),
    ("control_plan", "limits"),
    ("control_apikey", "id"),
    ("control_apikey", "tenant_id"),
    ("control_apikey", "key_hash"),
    ("control_apikey", "scopes"),
    ("control_apikey", "revoked_at"),
    ("control_apikey", "expires_at"),
    ("control_trainingrun", "id"),
    ("control_trainingrun", "tenant_id"),
    ("control_trainingrun", "taskset_ref"),
    ("control_trainingrun", "status"),
    ("control_trainingrun", "config_digest"),
]


def _models_by_db_table() -> dict[str, type]:
    out: dict[str, type] = {}
    for model in apps.get_models():
        table = model._meta.db_table
        if table.startswith("control_"):
            out[table] = model
    return out


def _column_names(model: type) -> set[str]:
    """Concrete DB column names, including FK db_column overrides like plan_code."""
    names: set[str] = set()
    for field in model._meta.local_fields:
        column = getattr(field, "column", None)
        if isinstance(column, str) and column:
            names.add(column)
        attname = getattr(field, "attname", None)
        if isinstance(attname, str) and attname:
            names.add(attname)
    return names


def test_control_schema_contract_column_count():
    assert len(CONTROL_SCHEMA_CONTRACT) == 23


def test_control_schema_contract_models_declare_required_columns():
    by_table = _models_by_db_table()
    missing: list[str] = []
    for table, column in CONTROL_SCHEMA_CONTRACT:
        model = by_table.get(table)
        if model is None:
            missing.append(f"{table}.{column} (no Django model with db_table={table})")
            continue
        if column not in _column_names(model):
            missing.append(f"{table}.{column} (model {model.__name__})")
    assert not missing, "control schema contract drift:\n" + "\n".join(missing)


def test_control_schema_models_stay_in_control_namespace():
    for table, model in _models_by_db_table().items():
        assert table.startswith("control_"), (
            f"{model.__name__} uses db_table={table}; control-plane tables must "
            "remain under the control_ prefix / control schema"
        )
