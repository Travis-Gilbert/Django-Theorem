"""Isolated interpreter entry point for already-validated Diagrams source."""

from __future__ import annotations

import builtins
import json
import os
import sys
from pathlib import Path
from types import ModuleType

from diagrams import Diagram


def _diagrams_only_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level != 0 or not (name == "diagrams" or name.startswith("diagrams.")):
        raise ImportError("imports are restricted to diagrams")
    if not fromlist:
        if name == "diagrams":
            raise ImportError("the diagrams package must not be imported directly")
        return builtins.__import__(name, globals, locals, fromlist, level)
    if "*" in fromlist:
        raise ImportError("wildcard diagrams imports are not allowed")
    module = builtins.__import__(name, globals, locals, fromlist, level)
    for symbol in fromlist:
        value = getattr(module, symbol, None)
        owner = (
            value.__name__
            if isinstance(value, ModuleType)
            else getattr(value, "__module__", None)
        )
        if owner != "diagrams" and not (owner and owner.startswith("diagrams.")):
            raise ImportError("imports are restricted to diagrams-owned symbols")
    return module


def main() -> int:
    request = json.load(sys.stdin)
    workdir = Path(request["workdir"]).resolve(strict=True)
    output_format = request["format"]
    if output_format not in {"png", "svg"}:
        raise ValueError("unsupported output format")
    os.chdir(workdir)

    original_init = Diagram.__init__

    def controlled_init(instance, *args, **kwargs):
        kwargs["filename"] = str(workdir / "render")
        kwargs["outformat"] = output_format
        kwargs["show"] = False
        return original_init(instance, *args, **kwargs)

    Diagram.__init__ = controlled_init
    controlled_builtins = dict(vars(builtins))
    controlled_builtins["__import__"] = _diagrams_only_import
    namespace = {"__builtins__": controlled_builtins, "__name__": "__main__"}
    exec(compile(request["source"], "<diagrams-source>", "exec"), namespace)

    output_path = workdir / f"render.{output_format}"
    if not output_path.is_file():
        raise RuntimeError("diagrams did not produce the requested artifact")
    print(json.dumps({"output_path": str(output_path)}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
