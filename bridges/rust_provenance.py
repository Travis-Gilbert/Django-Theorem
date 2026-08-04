"""Provenance write-back client stub (D8).

Posts to THEOREM_API_BASE/rest/provenance/derivations with THEOREM_MACHINE_KEY.
Does not import any Theorem Rust crates or graph clients.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)


class StubProvenanceClient:
    """HTTP client stub for provenance write-back.

    When THEOREM_API_BASE / THEOREM_MACHINE_KEY are unset or the endpoint is
    unreachable, logs and returns without raising — live wiring replaces this.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        machine_key: str | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.base_url = (base_url or settings.THEOREM_API_BASE).rstrip("/")
        self.machine_key = machine_key if machine_key is not None else settings.THEOREM_MACHINE_KEY
        self.timeout_s = timeout_s

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/rest/provenance/derivations"

    def post_derivation(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not self.machine_key:
            logger.info(
                "StubProvenanceClient: THEOREM_MACHINE_KEY unset; skipping write-back to %s",
                self.endpoint,
            )
            return None
        try:
            import httpx
        except ImportError:
            logger.warning("StubProvenanceClient: httpx not installed; skipping write-back")
            return None
        headers = {
            "Authorization": f"Bearer {self.machine_key}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                resp = client.post(self.endpoint, json=payload, headers=headers)
                resp.raise_for_status()
                if resp.content:
                    return resp.json()
                return {"status": resp.status_code}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "StubProvenanceClient: post to %s failed (%s); lineage deferred",
                self.endpoint,
                exc,
            )
            return None
