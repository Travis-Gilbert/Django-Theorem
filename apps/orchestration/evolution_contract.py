"""Rust-owned evolution wire contract; parsing never grants job or artifact authority."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

MAX_MESSAGE_BYTES = 4 * 1024 * 1024
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts/theorem.knowledge_evolution.v1.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text())
Draft202012Validator.check_schema(SCHEMA)
_VALIDATOR = Draft202012Validator(SCHEMA)


class ContractError(ValueError):
    """Redacted boundary failure; candidate values never appear in diagnostics."""


@dataclass(frozen=True, slots=True, init=False)
class EvolutionMessage:
    """Validated immutable bytes; callers receive copies of decoded data."""

    _payload: bytes

    def __init__(self, payload: bytes):
        if len(payload) > MAX_MESSAGE_BYTES:
            raise ContractError("message_too_large")
        try:
            value = json.loads(payload.decode("utf-8"))
            _VALIDATOR.validate(value)
            canonical = json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
            object.__setattr__(self, "_payload", canonical)
        except (ValueError, UnicodeError, RecursionError, ValidationError):
            raise ContractError("invalid_evolution_contract") from None

    @classmethod
    def parse(cls, payload: bytes) -> EvolutionMessage:
        return cls(payload)

    def to_bytes(self) -> bytes:
        return self._payload

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self._payload).hexdigest()

    def bind_to(self, request: EvolutionMessage) -> None:
        """Check exact request binding, not current lease authority or artifact contents."""
        result_value = json.loads(self._payload)
        request_value = json.loads(request._payload)
        if result_value["message_kind"] != "result" or request_value["message_kind"] != "request":
            raise ContractError("binding_mismatch:message_kind")
        if result_value["request_digest"] != request.digest():
            raise ContractError("binding_mismatch:request_digest")
        for field in ("operation_id", "campaign_id", "attempt_epoch", "base_revisions", "protocol", "environment_snapshot", "engines"):
            if result_value[field] != request_value[field]:
                raise ContractError(f"binding_mismatch:{field}")
