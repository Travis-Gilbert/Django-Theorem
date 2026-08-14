"""RunPod Serverless v2 lifecycle client for data-science offload jobs.

The control plane submits asynchronous jobs, records the opaque RunPod job id
on ``control_job``, then polls the endpoint until it reaches a terminal state.
Payload bytes never pass through this client: the endpoint receives the
content-addressed Arrow descriptor and is responsible for its object-store
handoff.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import monotonic, sleep
from typing import Any
from urllib.parse import quote

import httpx


class RunpodConfigurationError(RuntimeError):
    """The deployment opted into RunPod without its required configuration."""


class RunpodApiError(RuntimeError):
    """RunPod rejected a request or returned an invalid response."""


class RunpodTimeoutError(RuntimeError):
    """The control-plane deadline elapsed before the job reached a terminal state."""


@dataclass(frozen=True)
class SubmittedRunpodJob:
    job_id: str
    status: str


class RunpodServerlessClient:
    """Minimal client for a single queue-based RunPod Serverless endpoint."""

    SUCCESS = "COMPLETED"
    FAILURE_STATUSES = frozenset({"ERROR", "FAILED", "CANCELLED", "TIMED_OUT"})

    def __init__(
        self,
        *,
        api_key: str,
        endpoint_id: str,
        base_url: str = "https://api.runpod.ai/v2",
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.endpoint_id = endpoint_id.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._client = client
        if not self.api_key:
            raise RunpodConfigurationError("RUNPOD_API_KEY is required for RunPod execution")
        if not self.endpoint_id:
            raise RunpodConfigurationError(
                "RUNPOD_SERVERLESS_ENDPOINT_ID is required for RunPod execution"
            )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _url(self, *parts: str) -> str:
        encoded = "/".join(quote(part, safe="") for part in parts)
        return f"{self.base_url}/{quote(self.endpoint_id, safe='')}/{encoded}"

    def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        def send(client: httpx.Client) -> dict[str, Any]:
            try:
                response = client.request(method, url, headers=self._headers, **kwargs)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise RunpodApiError(f"RunPod {method} {url} failed: {exc}") from exc
            try:
                payload = response.json()
            except ValueError as exc:
                raise RunpodApiError("RunPod returned a non-JSON response") from exc
            if not isinstance(payload, dict):
                raise RunpodApiError("RunPod returned a non-object JSON response")
            return payload

        if self._client is not None:
            return send(self._client)
        with httpx.Client(timeout=self.timeout_seconds) as client:
            return send(client)

    def submit(self, payload: Mapping[str, Any]) -> SubmittedRunpodJob:
        response = self._request("POST", self._url("run"), json={"input": dict(payload)})
        job_id = str(response.get("id") or "").strip()
        status = str(response.get("status") or "").strip().upper()
        if not job_id or not status:
            raise RunpodApiError("RunPod /run response did not include id and status")
        return SubmittedRunpodJob(job_id=job_id, status=status)

    def status(self, job_id: str) -> dict[str, Any]:
        payload = self._request("GET", self._url("status", job_id))
        status = str(payload.get("status") or "").strip().upper()
        if not status:
            raise RunpodApiError("RunPod /status response did not include status")
        payload["status"] = status
        return payload

    def cancel(self, job_id: str) -> dict[str, Any]:
        return self._request("POST", self._url("cancel", job_id))

    def wait(
        self,
        job_id: str,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float,
        on_update: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        deadline = monotonic() + timeout_seconds
        interval = max(poll_interval_seconds, 0.1)
        while True:
            state = self.status(job_id)
            if on_update is not None:
                on_update(state)
            if state["status"] == self.SUCCESS or state["status"] in self.FAILURE_STATUSES:
                return state
            if monotonic() >= deadline:
                try:
                    self.cancel(job_id)
                except RunpodApiError:
                    # The timeout is still authoritative even if the cancel request
                    # races an endpoint failure or a network interruption.
                    pass
                raise RunpodTimeoutError(
                    f"RunPod job {job_id} exceeded the {timeout_seconds:g}s control-plane deadline"
                )
            sleep(interval)
