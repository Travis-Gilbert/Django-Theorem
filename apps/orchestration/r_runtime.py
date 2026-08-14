"""R worker identity and lockfile verification.

This module intentionally imports ``rpy2`` only on the dedicated R worker. The
base web/Python worker image must not acquire an R runtime as an accidental
dependency.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from django.conf import settings


def renv_lockfile_hash(path: str | Path | None = None) -> str:
    """Return the immutable SHA-256 code reference for the active renv lockfile."""
    lockfile = Path(path or settings.RENV_LOCKFILE_PATH)
    if not lockfile.is_file():
        raise RuntimeError(f"R renv lockfile is missing: {lockfile}")
    return hashlib.sha256(lockfile.read_bytes()).hexdigest()


def runtime_identity() -> str:
    """Assert that rpy2 can load the pinned R runtime and return its version."""
    try:
        import rpy2
        from rpy2 import robjects
    except ImportError as exc:
        raise RuntimeError("rpy2 is not installed in this worker image") from exc
    r_version = str(robjects.r("R.version.string")[0])
    return f"{r_version}; rpy2 {rpy2.__version__}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the dedicated Theorem R worker")
    parser.add_argument("--check", action="store_true", help="validate R/rpy2 and renv.lock")
    args = parser.parse_args()
    if args.check:
        print(f"R runtime ready: {runtime_identity()}; lock={renv_lockfile_hash()}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the worker image entrypoint
    raise SystemExit(main())
