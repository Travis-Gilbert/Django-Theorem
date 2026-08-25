"""Static import admission for Diagrams source."""

from __future__ import annotations

import ast


DANGEROUS_BUILTINS = {
    "compile",
    "eval",
    "exec",
    "getattr",
    "globals",
    "locals",
    "open",
    "setattr",
    "vars",
}


class DiagramSourceError(ValueError):
    pass


def _is_diagrams_module(module: str | None) -> bool:
    return module == "diagrams" or bool(module and module.startswith("diagrams."))


def validate_diagrams_source(source: str, *, max_bytes: int) -> ast.Module:
    encoded = source.encode("utf-8")
    if not encoded or len(encoded) > max_bytes:
        raise DiagramSourceError("source must be non-empty and within its size cap")
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise DiagramSourceError("source is not valid Python") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise DiagramSourceError("dunder attribute access is not allowed")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in DANGEROUS_BUILTINS
        ):
            raise DiagramSourceError(f"{node.func.id}() is not allowed")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _is_diagrams_module(alias.name):
                    raise DiagramSourceError(
                        f"import {alias.name!r} is outside the diagrams package"
                    )
        if isinstance(node, ast.ImportFrom):
            if node.level != 0 or not _is_diagrams_module(node.module):
                module = "." * node.level + (node.module or "")
                raise DiagramSourceError(
                    f"import {module!r} is outside the diagrams package"
                )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
        ):
            if not node.args or not isinstance(node.args[0], ast.Constant):
                raise DiagramSourceError("dynamic imports are not allowed")
            module = node.args[0].value
            if not isinstance(module, str) or not _is_diagrams_module(module):
                raise DiagramSourceError(
                    f"import {module!r} is outside the diagrams package"
                )
    return tree
