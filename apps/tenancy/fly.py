"""Fly Machines API client for the store provisioner.

Every ``ensure_*`` call looks the resource up by name before creating it, so a
retried provisioning run finds what an earlier attempt already made instead of
making a second one. The Machines API has no idempotency key; the name is it.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx


class FlyApiError(RuntimeError):
    """Fly rejected a request or returned something the provisioner cannot use."""


class FlyMachinesClient:
    def __init__(
        self,
        *,
        api_token: str,
        org_slug: str,
        base_url: str = "https://api.machines.dev/v1",
        timeout_seconds: float = 90.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_token.strip():
            raise FlyApiError("FLY_API_TOKEN is required to provision stores")
        if not org_slug.strip():
            raise FlyApiError("FLY_ORG_SLUG is required to provision stores")
        self.org_slug = org_slug.strip()
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {api_token.strip()}",
                "Accept": "application/json",
            },
            # The wait endpoint holds the request open for up to its timeout.
            timeout=timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, *, allow_404: bool = False, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise FlyApiError(f"Fly {method} {path} failed: {exc}") from exc
        if allow_404 and response.status_code == 404:
            return None
        if response.is_error:
            raise FlyApiError(
                f"Fly {method} {path} returned {response.status_code}: {response.text[:500]}"
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise FlyApiError(f"Fly {method} {path} returned non-JSON") from exc

    @staticmethod
    def _app(app: str) -> str:
        return f"/apps/{quote(app, safe='')}"

    def ensure_app(self, name: str) -> dict[str, Any]:
        existing = self._request("GET", self._app(name), allow_404=True)
        if existing is not None:
            org = (existing.get("organization") or {}).get("slug")
            if org and org != self.org_slug:
                raise FlyApiError(f"Fly app {name} belongs to {org}, not {self.org_slug}")
            return existing
        self._request("POST", "/apps", json={"app_name": name, "org_slug": self.org_slug})
        return self._request("GET", self._app(name))

    def ensure_volume(self, app: str, *, name: str, region: str, size_gb: int) -> dict[str, Any]:
        volumes = self._request("GET", f"{self._app(app)}/volumes") or []
        for volume in volumes:
            if volume.get("name") == name and volume.get("state") not in ("destroyed", "destroying"):
                return volume
        return self._request(
            "POST",
            f"{self._app(app)}/volumes",
            json={"name": name, "region": region, "size_gb": size_gb, "encrypted": True},
        )

    def ensure_machine(self, app: str, *, name: str, region: str, config: dict[str, Any]) -> dict[str, Any]:
        machines = self._request("GET", f"{self._app(app)}/machines") or []
        for machine in machines:
            if machine.get("name") == name and machine.get("state") not in ("destroyed", "destroying"):
                return machine
        return self._request(
            "POST",
            f"{self._app(app)}/machines",
            json={"name": name, "region": region, "config": config},
        )

    def wait_started(self, app: str, machine_id: str, *, timeout_seconds: int = 60) -> None:
        self._request(
            "GET",
            f"{self._app(app)}/machines/{quote(machine_id, safe='')}/wait",
            params={"state": "started", "timeout": timeout_seconds},
        )

    def machine(self, app: str, machine_id: str) -> dict[str, Any]:
        return self._request("GET", f"{self._app(app)}/machines/{quote(machine_id, safe='')}")
