"""Deterministic Beta-Bernoulli fitter for theorem.competence.v1 evidence."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

from apps.competence.contract import (
    BetaBernoulli,
    CompetenceFitRequest,
    CompetenceScorerArtifact,
    canonical_hex_digest,
)
from apps.orchestration.artifacts import sha256_digest

FITTER_VERSION = "beta-bernoulli-ipw-v1"
SCORER_MEDIA_TYPE = "application/vnd.theorem.competence-scorer+json"
PRIOR_PACK_MEDIA_TYPE = "application/vnd.theorem.prior-pack+json"


class CompetenceFitRefusal(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class FittedCompetence:
    scorer: CompetenceScorerArtifact
    model_payload: bytes
    prior_pack_payload: bytes


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _scorer_id(request: CompetenceFitRequest) -> str:
    scope_digest = hashlib.sha256(
        canonical_json_bytes(request.scope.model_dump(mode="json"))
    ).hexdigest()
    return f"capability-competence:{scope_digest[:32]}"


def _beta_observe(
    prior: dict[str, float], outcome: float, weight: float
) -> dict[str, float]:
    return {
        "alpha": prior["alpha"] + outcome * weight,
        "beta": prior["beta"] + (1.0 - outcome) * weight,
    }


def _validate_learning_evidence(request: CompetenceFitRequest) -> None:
    for item in request.evidence:
        if item.role != "held_out":
            continue
        selection = item.selection
        if selection is None:
            raise CompetenceFitRefusal(
                "missing_selection_evidence",
                "held-out competence evidence requires W02 selection evidence",
            )
        values = [
            selection.behavior_probability,
            selection.target_probability,
            selection.importance_weight,
            selection.observed_outcome,
            selection.weighted_outcome,
        ]
        if (
            not all(math.isfinite(value) for value in values)
            or not 0.0 < selection.behavior_probability <= 1.0
            or not 0.0 <= selection.target_probability <= 1.0
            or selection.importance_weight < 0.0
            or not 0.0 <= selection.observed_outcome <= 1.0
            or not 0.0 <= selection.weighted_outcome <= selection.importance_weight
        ):
            raise CompetenceFitRefusal(
                "invalid_selection_evidence",
                "selection probabilities and weighted outcomes must be finite probabilities",
            )


def _starting_prior(
    request: CompetenceFitRequest,
    previous_scorer: CompetenceScorerArtifact | None,
    previous_request: CompetenceFitRequest | None,
) -> dict[str, float]:
    if request.message_kind == "fit_request":
        return {"alpha": 1.0, "beta": 1.0}
    if previous_scorer is None or previous_request is None:
        raise CompetenceFitRefusal(
            "previous_scorer_not_found",
            "refit requires the exact prior scorer inside this tenant and project",
        )
    previous = request.previous_scorer
    if previous is None or (
        previous.scorer_id != previous_scorer.scorer_id
        or previous.scorer_version != previous_scorer.scorer_version
        or previous.model_artifact_id != previous_scorer.model_artifact.artifact_id
        or previous.prior_pack_id != previous_scorer.prior_pack.prior_pack_id
    ):
        raise CompetenceFitRefusal(
            "previous_scorer_mismatch",
            "refit previous scorer identity is not exact",
        )
    if previous_request.scope != request.scope:
        raise CompetenceFitRefusal(
            "previous_scorer_scope_mismatch",
            "refit cannot move a scorer across package or candidate scope",
        )
    previous_lineages = {item.causal_lineage_id for item in previous_request.evidence}
    current_lineages = {item.causal_lineage_id for item in request.evidence}
    overlap = sorted(previous_lineages & current_lineages)
    if overlap:
        raise CompetenceFitRefusal(
            "refit_lineage_overlap",
            "refit evidence overlaps causal lineage already consumed by the prior scorer",
        )
    return previous_scorer.posterior.model_dump(mode="json")


def fit_competence(
    request: CompetenceFitRequest,
    request_digest: str,
    *,
    previous_scorer: CompetenceScorerArtifact | None = None,
    previous_request: CompetenceFitRequest | None = None,
) -> FittedCompetence:
    """Fit one immutable scorer from sufficient evidence only."""
    _validate_learning_evidence(request)
    prior = _starting_prior(request, previous_scorer, previous_request)
    training = sorted(
        (item for item in request.evidence if item.role == "training"),
        key=lambda item: item.causal_lineage_id,
    )
    held_out = sorted(
        (item for item in request.evidence if item.role == "held_out"),
        key=lambda item: item.causal_lineage_id,
    )
    for item in training:
        prior = _beta_observe(prior, float(item.survived), 1.0)

    posterior = dict(prior)
    observations: list[dict[str, Any]] = []
    for item in held_out:
        selection = item.selection
        if selection is None:  # guarded above; keeps static narrowing explicit
            raise CompetenceFitRefusal(
                "missing_selection_evidence",
                "held-out competence evidence requires W02 selection evidence",
            )
        posterior = _beta_observe(
            posterior,
            selection.observed_outcome,
            selection.importance_weight,
        )
        observations.append(selection.model_dump(mode="json"))

    scorer_id = _scorer_id(request)
    digest_suffix = request_digest.removeprefix("sha256:")
    scorer_version = f"{FITTER_VERSION}:{digest_suffix[:24]}"
    distinct_lineage_ids = sorted(item.causal_lineage_id for item in held_out)
    selection_decision_refs = sorted(
        item.selection.decision_ref for item in held_out if item.selection is not None
    )
    prior_pack_value = {
        "schema": "theorem.competence-prior-pack.v1",
        "fitter_version": FITTER_VERSION,
        "request_digest": request_digest,
        "scope": request.scope.model_dump(mode="json"),
        "training_corpus_digest": request.training_corpus_digest,
        "prior": prior,
        "training_lineage_ids": [item.causal_lineage_id for item in training],
        "previous_scorer": (
            request.previous_scorer.model_dump(mode="json")
            if request.previous_scorer is not None
            else None
        ),
    }
    prior_pack_payload = canonical_json_bytes(prior_pack_value)
    prior_pack_digest = sha256_digest(prior_pack_payload)

    model_value = {
        "schema": "theorem.competence-scorer.v1",
        "fitter_version": FITTER_VERSION,
        "scorer_id": scorer_id,
        "scorer_version": scorer_version,
        "request_digest": request_digest,
        "scope": request.scope.model_dump(mode="json"),
        "training_corpus_digest": request.training_corpus_digest,
        "evaluation_corpus_digest": request.evaluation_corpus_digest,
        "prior_pack_id": prior_pack_digest,
        "prior": prior,
        "posterior": posterior,
        "distinct_lineage_ids": distinct_lineage_ids,
        "selection_decision_refs": selection_decision_refs,
        "previous_scorer": (
            request.previous_scorer.model_dump(mode="json")
            if request.previous_scorer is not None
            else None
        ),
    }
    model_payload = canonical_json_bytes(model_value)
    model_digest = sha256_digest(model_payload)
    metadata = {
        "scope": {
            "tenant_id": str(request.scope.tenant_id),
            "project_id": str(request.scope.project_id),
        },
        "pack_content_hash": request.scope.pack_content_hash,
        "candidate_id": request.scope.candidate_id,
        "candidate_lineage_id": request.scope.candidate_lineage_id,
        "evaluation_corpus_digest": request.evaluation_corpus_digest,
        "scorer_version": scorer_version,
        "model_artifact_hash": model_digest,
        "prior_pack_id": prior_pack_digest,
        "distinct_lineage_ids": distinct_lineage_ids,
        "selection_decision_refs": selection_decision_refs,
    }
    receipt_payload = {
        "kind": "capability_competence",
        "model_id": scorer_id,
        "prior": prior,
        "observations": observations,
        "posterior": posterior,
        "metadata": metadata,
    }
    receipt = {**receipt_payload, "receipt_hash": canonical_hex_digest(receipt_payload)}
    posterior_mean = posterior["alpha"] / (posterior["alpha"] + posterior["beta"])
    scorer = CompetenceScorerArtifact.model_validate(
        {
            "scorer_id": scorer_id,
            "scorer_version": scorer_version,
            "model_artifact": {
                "artifact_id": model_digest,
                "kind": "scorer_model",
                "payload_digest": model_digest,
                "media_type": SCORER_MEDIA_TYPE,
                "byte_length": len(model_payload),
            },
            "prior_pack": {
                "prior_pack_id": prior_pack_digest,
                "artifact": {
                    "artifact_id": prior_pack_digest,
                    "kind": "prior_pack",
                    "payload_digest": prior_pack_digest,
                    "media_type": PRIOR_PACK_MEDIA_TYPE,
                    "byte_length": len(prior_pack_payload),
                },
            },
            "prior": BetaBernoulli.model_validate(prior),
            "posterior": BetaBernoulli.model_validate(posterior),
            "posterior_receipt": receipt,
            "minimum_posterior_mean": request.minimum_posterior_mean,
            "distinct_lineage_ids": distinct_lineage_ids,
            "selection_decision_refs": selection_decision_refs,
            "oracle_class": request.requested_oracle_class,
            "evidence_class": request.requested_oracle_class,
            "substitution_allowed": request.substitution_allowed,
            "live_oracle_required": request.live_oracle_required,
            "accepted": posterior_mean >= request.minimum_posterior_mean,
        }
    )
    return FittedCompetence(
        scorer=scorer,
        model_payload=model_payload,
        prior_pack_payload=prior_pack_payload,
    )
