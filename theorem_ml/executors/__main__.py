"""Receipted local entry: python -m theorem_ml.executors --tenant ... --input ..."""

import argparse
import json
from pathlib import Path

from .registry import EXECUTORS, execute


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--executor", choices=EXECUTORS, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--tracking-uri")
    args = parser.parse_args()
    result = execute(
        args.executor,
        json.loads(args.input.read_text()),
        tenant=args.tenant,
        tracking_uri=args.tracking_uri,
    )
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
