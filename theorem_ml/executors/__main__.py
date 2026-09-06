"""Receipted local entry: python -m theorem_ml.executors --tenant ... --input ..."""

import argparse
import json
from pathlib import Path

from .registry import EXECUTORS, execute, list_executors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=["execute", "list"], default="execute")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--executor", choices=EXECUTORS)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--tracking-uri")
    args = parser.parse_args()
    if args.action == "list":
        print(json.dumps(list_executors(tenant=args.tenant, tracking_uri=args.tracking_uri), sort_keys=True))
        return
    if args.executor is None or args.input is None:
        parser.error("execute requires --executor and --input")
    result = execute(
        args.executor,
        json.loads(args.input.read_text()),
        tenant=args.tenant,
        tracking_uri=args.tracking_uri,
    )
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
