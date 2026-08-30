"""Static import admission for Diagrams source."""

from __future__ import annotations

import ast
import importlib
from types import ModuleType


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


def _is_diagrams_owned(value: object) -> bool:
    owner = (
        value.__name__
        if isinstance(value, ModuleType)
        else getattr(value, "__module__", None)
    )
    return _is_diagrams_module(owner)


def _resolve_diagrams_symbol(module_name: str, symbol: str) -> object:
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise DiagramSourceError(f"diagrams module {module_name!r} does not exist") from exc
    try:
        return getattr(module, symbol)
    except AttributeError:
        try:
            return importlib.import_module(f"{module_name}.{symbol}")
        except ImportError as exc:
            raise DiagramSourceError(
                f"diagrams module {module_name!r} does not export {symbol!r}"
            ) from exc


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
                if alias.name == "diagrams" or alias.asname is None:
                    raise DiagramSourceError(
                        "direct imports must bind an explicit diagrams submodule alias"
                    )
        if isinstance(node, ast.ImportFrom):
            if node.level != 0 or not _is_diagrams_module(node.module):
                module = "." * node.level + (node.module or "")
                raise DiagramSourceError(
                    f"import {module!r} is outside the diagrams package"
                )
            for alias in node.names:
                if alias.name == "*":
                    raise DiagramSourceError("wildcard diagrams imports are not allowed")
                value = _resolve_diagrams_symbol(node.module, alias.name)
                if not _is_diagrams_owned(value):
                    raise DiagramSourceError(
                        f"{node.module!r} exports {alias.name!r} from outside the diagrams package"
                    )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
        ):
            raise DiagramSourceError("dynamic imports are not allowed")
    return tree
