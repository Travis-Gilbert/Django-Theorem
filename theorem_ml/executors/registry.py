"""Learned executor registration and explicit, concurrent-safe MLflow receipts."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from .contracts import digest
from .corpus_filing import corpus_pass
from .discovery import discover


@dataclass(frozen=True)
class ExecutorRegistration:
    name: str
    execute: Callable
    tier: str = "Learned"
    fitness_metric: str = "held_out_filing_accuracy"


EXECUTORS = {
    "discovery": ExecutorRegistration("discovery", discover),
    "corpus_filing": ExecutorRegistration("corpus_filing", corpus_pass),
}


def execute(
    name: str, payload: dict, *, tenant: str, tracking_uri: str | None = None
) -> dict:
    if not tenant or not tenant.strip() or payload.get("tenant_id", tenant) != tenant:
        raise ValueError("request tenant must match the admitted principal")
    if name not in EXECUTORS:
        raise ValueError("unknown Index executor")
    tracking_uri = tracking_uri or os.environ.get("MLFLOW_TRACKING_URI")
    if not tracking_uri:
        raise RuntimeError(
            "MLFLOW_TRACKING_URI is required for receipted executor runs"
        )
    from mlflow import MlflowClient
    from mlflow.exceptions import MlflowException

    client = MlflowClient(tracking_uri=tracking_uri)
    experiment_name = f"theorem-index/{tenant}"
    experiment = client.get_experiment_by_name(experiment_name)
    if experiment is None:
        try:
            experiment_id = client.create_experiment(experiment_name)
        except MlflowException:
            experiment = client.get_experiment_by_name(experiment_name)
            if experiment is None:
                raise
            experiment_id = experiment.experiment_id
    else:
        experiment_id = experiment.experiment_id
    registration = EXECUTORS[name]
    run = client.create_run(
        experiment_id,
        tags={
            "theorem.tenant": tenant,
            "theorem.executor": name,
            "theorem.tier": registration.tier,
            "theorem.input_digest": digest(payload),
        },
    )
    run_id = run.info.run_id
    try:
        result = registration.execute(payload, tenant=tenant)
        fitness = result.get("fitness")
        if fitness is not None:
            client.log_metric(run_id, registration.fitness_metric, fitness["accuracy"])
            client.log_metric(run_id, "held_out_count", fitness["held_out_count"])
        else:
            client.set_tag(
                run_id, "theorem.fitness_status", "not_measured_no_held_out_filings"
            )
        client.log_param(run_id, "implementation", result["implementation"])
        client.log_param(run_id, "output_digest", digest(result))
        client.set_terminated(run_id, status="FINISHED")
    except Exception:
        client.set_terminated(run_id, status="FAILED")
        raise
    result["executor"] = {
        "name": name,
        "tier": registration.tier,
        "mlflow_run_id": run_id,
        "fitness_metric": registration.fitness_metric,
    }
    if name == "discovery":
        # The Rust caller persists these nodes and owns subsequent accept/rename/
        # dismiss. Returning a proposal never means its collection was accepted.
        result["proposal_nodes"] = [
            {
                "id": proposal["proposal_id"],
                "labels": ["ProposedCollection"],
                "properties": {
                    **proposal,
                    "tenant_id": tenant,
                    "state": "proposed",
                    "mlflow_run_id": run_id,
                },
            }
            for proposal in result["proposals"]
        ]
    return result
