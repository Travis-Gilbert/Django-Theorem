"""RunPod Pod REST client for long-running prime-rl training."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import monotonic, sleep
from typing import Any
from urllib.parse import quote

import httpx


class RunpodPodConfigurationError(RuntimeError):
    """The control plane lacks a usable Pod API configuration."""


class RunpodPodApiError(RuntimeError):
    """The RunPod Pod API rejected a request or returned an invalid response."""


class RunpodPodTimeoutError(RuntimeError):
    """A Pod did not reach a terminal state before its deadline."""


@dataclass(frozen=True)
class CreatedPod:
    pod_id: str
    desired_status: str


class RunpodPodClient:
    """Small client for the current ``https://rest.runpod.io/v1/pods`` API."""

    TERMINAL_STATUSES = frozenset({"EXITED", "TERMINATED", "FAILED"})

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://rest.runpod.io/v1",
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._client = client
        if not self.api_key:
            raise RunpodPodConfigurationError(
                "RUNPOD_API_KEY is required for prime-rl training"
            )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"

        def send(client: httpx.Client) -> dict[str, Any]:
            try:
                response = client.request(method, url, headers=self._headers, **kwargs)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise RunpodPodApiError(f"RunPod {method} {url} failed: {exc}") from exc
            if response.status_code == 204 or not response.content:
                return {}
            try:
                payload = response.json()
            except ValueError as exc:
                raise RunpodPodApiError("RunPod returned a non-JSON response") from exc
            if not isinstance(payload, dict):
                raise RunpodPodApiError("RunPod returned a non-object JSON response")
            return payload

        if self._client is not None:
            return send(self._client)
        with httpx.Client(timeout=self.timeout_seconds) as client:
            return send(client)

    def create(self, payload: Mapping[str, Any]) -> CreatedPod:
        response = self._request("POST", "pods", json=dict(payload))
        pod_id = str(response.get("id") or "").strip()
        desired_status = str(response.get("desiredStatus") or "").strip().upper()
        if not pod_id:
            raise RunpodPodApiError("RunPod create response did not include id")
        return CreatedPod(pod_id=pod_id, desired_status=desired_status)

    def get(self, pod_id: str) -> dict[str, Any]:
        return self._request("GET", f"pods/{quote(pod_id, safe='')}")

    def terminate(self, pod_id: str) -> None:
        self._request("DELETE", f"pods/{quote(pod_id, safe='')}")

    def wait(
        self,
        pod_id: str,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float,
        canceled: Callable[[], bool] | None = None,
        on_update: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        deadline = monotonic() + timeout_seconds
        interval = max(poll_interval_seconds, 0.1)
        while True:
            if canceled is not None and canceled():
                self.terminate(pod_id)
                return {"id": pod_id, "desiredStatus": "CANCELED"}
            state = self.get(pod_id)
            if on_update is not None:
                on_update(state)
            status = str(state.get("desiredStatus") or "").upper()
            if status in self.TERMINAL_STATUSES:
                return state
            if monotonic() >= deadline:
                try:
                    self.terminate(pod_id)
                except RunpodPodApiError:
                    pass
                raise RunpodPodTimeoutError(
                    f"RunPod Pod {pod_id} exceeded the {timeout_seconds:g}s deadline"
                )
            sleep(interval)
