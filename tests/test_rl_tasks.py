"""Arrow ingestion and RunPod Pod lifecycle oracles for the RL worker."""

from __future__ import annotations

import json

import httpx
import pytest

from theorem_control.rl.ingest import parse_traces
from theorem_control.rl.runpod import RunpodPodClient, RunpodPodTimeoutError


def test_trace_parser_rejects_forged_digest_and_tripwire_reward():
    forged = {
        "task_key": "repair-1",
        "trace": {"reply": "changed"},
        "trace_digest": "sha256:" + "0" * 64,
        "reward": 1.0,
        "metrics": {"resolved": 1.0},
    }
    with pytest.raises(ValueError, match="digest mismatch"):
        parse_traces((json.dumps(forged) + "\n").encode())

    tripped = {
        "task_key": "repair-1",
        "trace": {"reply": "changed"},
        "reward": 1.0,
        "metrics": {"resolved": 1.0},
        "tripwire_flags": ["manage_py_edited"],
    }
    with pytest.raises(ValueError, match="despite a tripwire"):
        parse_traces((json.dumps(tripped) + "\n").encode())


def test_trace_parser_accepts_canonical_verifiers_v1_shape():
    trace = {
        "task": {
            "key": "models-repair-01",
            "data": {"task_id": "models-repair-01"},
        },
        "rewards": {"resolved": {"score": 1.0, "weight": 1.0}},
        "metrics": {
            "project_imports": 1.0,
            "tripwire_fired": 0.0,
        },
        "info": {
            "django_v1": {
                "tripwires": {"manage_py_edited": False},
            }
        },
    }

    rows = parse_traces((json.dumps(trace) + "\n").encode())

    assert rows[0]["task_key"] == "models-repair-01"
    assert rows[0]["reward"] == 1.0
    assert rows[0]["resolved"] == 1.0
    assert rows[0]["tripwire_flags"] == []


def test_pod_client_creates_polls_and_terminates():
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(200, json={"id": "pod-1", "desiredStatus": "RUNNING"})
        if request.method == "GET":
            return httpx.Response(200, json={"id": "pod-1", "desiredStatus": "EXITED"})
        return httpx.Response(204)

    client = RunpodPodClient(
        api_key="secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    pod = client.create({"name": "training", "imageName": "image@sha256:abc"})
    state = client.wait(pod.pod_id, timeout_seconds=1, poll_interval_seconds=0.1)
    client.terminate(pod.pod_id)

    assert state["desiredStatus"] == "EXITED"
    assert requests == [
        ("POST", "/v1/pods"),
        ("GET", "/v1/pods/pod-1"),
        ("DELETE", "/v1/pods/pod-1"),
    ]


def test_pod_client_terminates_on_deadline(monkeypatch):
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json={"id": "pod-1", "desiredStatus": "RUNNING"})
        return httpx.Response(204)

    ticks = iter([0.0, 1.0])
    monkeypatch.setattr("theorem_control.rl.runpod.monotonic", lambda: next(ticks))
    monkeypatch.setattr("theorem_control.rl.runpod.sleep", lambda _seconds: None)
    client = RunpodPodClient(
        api_key="secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RunpodPodTimeoutError):
        client.wait("pod-1", timeout_seconds=0.5, poll_interval_seconds=0.1)
    assert requests[-1] == ("DELETE", "/v1/pods/pod-1")
