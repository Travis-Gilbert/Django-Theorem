"""Learned executor registration and explicit, concurrent-safe MLflow receipts."""

from __future__ import annotations

import os
import math
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .contracts import digest
from .corpus_filing import corpus_pass
from .discovery import discover
from .extraction import parse_paddle, spans_gliner2, claims_minicheck, resolve_link


@dataclass(frozen=True)
class ExecutorRegistration:
    name: str
    execute: Callable
    tier: str = "Learned"
    fitness_metric: str = "held_out_filing_accuracy"


EXECUTORS = {
    "discovery": ExecutorRegistration("discovery", discover),
    "corpus_filing": ExecutorRegistration("corpus_filing", corpus_pass),
    "parse.paddle": ExecutorRegistration("parse.paddle", parse_paddle, fitness_metric="exact_text_f1"),
    "spans.gliner2": ExecutorRegistration("spans.gliner2", spans_gliner2, fitness_metric="span_f1"),
    "claims.verify.minicheck": ExecutorRegistration("claims.verify.minicheck", claims_minicheck, fitness_metric="gold_agreement"),
    "resolve.link": ExecutorRegistration("resolve.link", resolve_link, fitness_metric="resolution_accuracy"),
}


def extraction_tracking_uri(tracking_uri=None):
    """Normal pipeline observations use the configured store or local SQLite."""
    configured = tracking_uri or os.environ.get("MLFLOW_TRACKING_URI")
    if configured:
        return configured
    from django.conf import settings
    base = settings.BASE_DIR if settings.configured else Path(__file__).resolve().parents[2]
    directory = Path(base) / "var"
    directory.mkdir(parents=True, exist_ok=True)
    return "sqlite:///" + str(directory / "mlflow.db")


def _experiment_id(client, name):
    from mlflow.exceptions import MlflowException
    experiment = client.get_experiment_by_name(name)
    if experiment is not None:
        return experiment.experiment_id
    try:
        return client.create_experiment(name)
    except MlflowException:
        experiment = client.get_experiment_by_name(name)
        if experiment is None:
            raise
        return experiment.experiment_id


def _measured_fitness(client, tenant, registration):
    experiment = client.get_experiment_by_name(f"theorem-extraction/{tenant}")
    if experiment is None:
        return None
    token = None
    while True:
        runs = client.search_runs([experiment.experiment_id],
            filter_string=f"tags.`theorem.executor` = '{registration.name}' and "
                          "tags.`theorem.evidence` = 'model_harness' and attributes.status = 'FINISHED'",
            order_by=["attributes.start_time DESC"], max_results=100, page_token=token)
        for run in runs:
            value = run.data.metrics.get(registration.fitness_metric)
            if value is not None and math.isfinite(value):
                return {"value": value, "metric": registration.fitness_metric,
                        "mlflow_run_id": run.info.run_id}
        token = runs.token
        if not token:
            return None


@contextmanager
def observe_execution(name, payload, *, tenant, state, tracking_uri=None,
                      evidence_class="runtime_observation"):
    """Observe the caller's real operation; never invoke a substitute executor.

    Elapsed time is measured here. Quality is only linked from a successful,
    separately measured harness run and is never copied into runtime metrics.
    """
    if not tenant or not tenant.strip() or payload.get("tenant_id", tenant) != tenant:
        raise ValueError("request tenant must match the admitted principal")
    registration = EXECUTORS[name]
    from mlflow import MlflowClient
    client = MlflowClient(tracking_uri=extraction_tracking_uri(tracking_uri))
    fitness = _measured_fitness(client, tenant, registration)
    tags = {"theorem.tenant": tenant, "theorem.executor": name,
            "theorem.tier": registration.tier, "theorem.evidence": evidence_class,
            "theorem.input_digest": digest(payload),
            "theorem.fitness_status": "measured_reference" if fitness else "not_measured"}
    if fitness:
        tags["theorem.fitness_run_id"] = fitness["mlflow_run_id"]
    run = client.create_run(_experiment_id(client, f"theorem-extraction-runtime/{tenant}"), tags=tags)
    reference = {"name": name, "mlflow_run_id": run.info.run_id, "status": "RUNNING",
                 "fitness": fitness, "evidence_class": evidence_class}
    state.setdefault("executor_runs", []).append(reference)
    started = time.perf_counter()
    try:
        yield reference
    except BaseException:
        reference["status"] = "FAILED"
        client.set_terminated(run.info.run_id, status="FAILED")
        raise
    else:
        reference["status"] = "FINISHED"
        client.set_terminated(run.info.run_id, status="FINISHED")
    finally:
        client.log_metric(run.info.run_id, "elapsed_seconds", time.perf_counter() - started)


def execute(
    name: str, payload: dict, *, tenant: str, tracking_uri: str | None = None
) -> dict:
    if not tenant or not tenant.strip() or payload.get("tenant_id", tenant) != tenant:
        raise ValueError("request tenant must match the admitted principal")
    if name not in EXECUTORS:
        raise ValueError("unknown Index executor")
    tracking_uri = extraction_tracking_uri(tracking_uri)
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
                run_id,
                "theorem.fitness_status",
                "not_measured_exact_singleton"
                if result["implementation"] == "exact_singleton_centroid"
                else "not_measured_no_gold" if name in {"parse.paddle", "spans.gliner2", "claims.verify.minicheck", "resolve.link"}
                else "not_measured_no_held_out_filings",
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


def list_executors(*, tenant: str, tracking_uri: str | None = None) -> list[dict]:
    """Read measured harness fitness; an unmeasured executor has a null value."""
    if not tenant or not tenant.strip():
        raise ValueError("An admitted tenant is required")
    from mlflow import MlflowClient
    client = MlflowClient(tracking_uri=extraction_tracking_uri(tracking_uri))
    rows = []
    for name, registration in EXECUTORS.items():
        row = {"name": name, "tier": registration.tier,
               "fitness_metric": registration.fitness_metric, "fitness": None,
               "mlflow_run_id": None, "fitness_status": "not_measured"}
        fitness = _measured_fitness(client, tenant, registration)
        if fitness:
            row.update(fitness=fitness["value"], mlflow_run_id=fitness["mlflow_run_id"],
                       fitness_status="measured")
        rows.append(row)
    return rows
