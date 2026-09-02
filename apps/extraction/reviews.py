"""Candidate identity shared by API and admin review writes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def candidate_digest(tenant_id: str, candidate: Mapping[str, Any]) -> str:
    identity = [
        tenant_id,
        candidate.get("stage"),
        candidate.get("passage_id"),
        candidate.get("subject"),
        candidate.get("predicate"),
        candidate.get("object"),
    ]
    if candidate.get("stage") == "typed":
        record = candidate.get("record_json")
        if isinstance(record, str):
            try:
                record = json.loads(record)
            except json.JSONDecodeError:
                pass
        if isinstance(record, (Mapping, list)):
            record = json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        identity.append(record)
    payload = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()


def is_candidate_digest(value: object) -> bool:
    if not isinstance(value, str):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)
