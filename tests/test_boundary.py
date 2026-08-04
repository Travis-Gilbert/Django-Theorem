"""A11 — plane-rule boundary test.

Fails if any module under apps/ or theorem_control/ imports a forbidden graph /
RustyRed client name. Allowlist: bridges.rust_provenance (HTTP write-back only).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = [ROOT / "apps", ROOT / "theorem_control"]
ALLOWLIST_RELATIVE = {
    "bridges/rust_provenance.py",
}

FORBIDDEN = frozenset(
    {
        "rustyred",
        "thg_core",
        "graph_store",
        "spine_client",
        "redcore",
    }
)


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for base in SCAN_DIRS:
        if not base.exists():
            continue
        files.extend(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    return files


def _imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
                names.add(node.module)
                for alias in node.names:
                    names.add(alias.name)
    return names


def test_no_forbidden_graph_imports():
    violations: list[str] = []
    for path in _iter_python_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWLIST_RELATIVE:
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            violations.append(f"{rel}: syntax error {exc}")
            continue
        imported = _imported_names(tree)
        # Also scan source tokens for dotted forms that may not be top-level imports
        # but still constitute a forbidden dependency (from X import graph_store).
        hit = sorted(name for name in imported if any(f in name for f in FORBIDDEN))
        # Tighten: only flag when a forbidden token is an import root or submodule path.
        real = []
        for name in hit:
            parts = name.replace("/", ".").split(".")
            if any(p in FORBIDDEN for p in parts):
                real.append(name)
        if real:
            violations.append(f"{rel}: forbidden imports {real}")

    assert not violations, "Plane-rule violations:\n" + "\n".join(violations)


def test_allowlisted_provenance_bridge_exists():
    bridge = ROOT / "bridges" / "rust_provenance.py"
    assert bridge.is_file(), "bridges.rust_provenance must exist for D8 write-back"
    tree = ast.parse(bridge.read_text(encoding="utf-8"))
    imported = _imported_names(tree)
    for name in imported:
        segments = set(name.split("."))
        assert not (FORBIDDEN & segments), f"forbidden import path: {name}"


def test_settings_pooling_flags():
    from django.conf import settings

    db = settings.DATABASES["default"]
    assert db.get("DISABLE_SERVER_SIDE_CURSORS") is True
    assert db.get("CONN_MAX_AGE") == 0
