"""Strict theorem.competence.v1 wire schemas shared with Theorem Rust clients."""

from __future__ import annotations

import math
from typing import Any, ClassVar, Literal, Self
from uuid import UUID

from ninja import Schema
from pydantic import ConfigDict, Field, model_validator

COMPETENCE_EXCHANGE_SCHEMA = "theorem.competence.v1"
COMPETENCE_EXCHANGE_FIXTURE_DIGEST = (
    "sha256:2b6d4a1f23cd527170259f0c3ba0c0c9cae6ed2c009dead34786acef2344ba88"
)
PromotionEvidenceClass = Literal[
    "deterministic_fixture", "local", "ci", "hosted", "live"
]
PracticeEpisodeRole = Literal["training", "held_out"]
CompetenceJobState = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "refused",
    "canceled",
]
EVIDENCE_CLASS_RANK = {
    "deterministic_fixture": 0,
    "local": 1,
    "ci": 2,
    "hosted": 3,
    "live": 4,
}


def is_sha256_address(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(
        character in "0123456789abcdef" for character in digest
    )


class StrictSchema(Schema):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class CompetenceRequestScope(StrictSchema):
    tenant_id: UUID
    project_id: UUID
    pack_content_hash: str
    candidate_id: str
    candidate_lineage_id: str


class ImportanceWeightedSelectionOutcome(StrictSchema):
    decision_ref: str
    policy_id: str
    policy_version: int = Field(ge=0)
    seed: int = Field(ge=0)
    behavior_probability: float
    target_probability: float
    importance_weight: float
    observed_outcome: float
    weighted_outcome: float


class CompetenceEvidenceSummary(StrictSchema):
    causal_lineage_id: str
    role: PracticeEpisodeRole
    episode_ref: str
    episode_digest: str
    survived: bool
    source_evidence_refs: list[str]
    selection: ImportanceWeightedSelectionOutcome | None = None


class PreviousCompetenceScorer(StrictSchema):
    scorer_id: str
    scorer_version: str
    model_artifact_id: str
    prior_pack_id: str


class CompetenceFitRequest(StrictSchema):
    contract: str
    message_kind: Literal["fit_request", "refit_request"]
    operation_id: str
    scope: CompetenceRequestScope
    training_corpus_digest: str
    evaluation_corpus_digest: str
    evidence: list[CompetenceEvidenceSummary]
    w02_decision_refs: list[str]
    source_evidence_refs: list[str]
    previous_scorer: PreviousCompetenceScorer | None = None
    minimum_posterior_mean: float
    requested_oracle_class: PromotionEvidenceClass
    substitution_allowed: bool
    live_oracle_required: bool


class BetaBernoulli(StrictSchema):
    alpha: float
    beta: float

    @model_validator(mode="after")
    def positive_finite_parameters(self) -> Self:
        if not math.isfinite(self.alpha) or not math.isfinite(self.beta):
            raise ValueError("beta parameters must be finite")
        if self.alpha <= 0.0 or self.beta <= 0.0:
            raise ValueError("beta parameters must be positive")
        return self


class ContentAddressedArtifact(StrictSchema):
    artifact_id: str
    kind: Literal["scorer_model", "prior_pack"]
    payload_digest: str
    media_type: str
    byte_length: int = Field(ge=0)

    @model_validator(mode="after")
    def content_address_matches_payload(self) -> Self:
        if self.artifact_id != self.payload_digest or not is_sha256_address(
            self.payload_digest
        ):
            raise ValueError("artifact identity must equal its sha256 payload digest")
        if not self.media_type.strip():
            raise ValueError("artifact media_type is required")
        return self


class CompetencePriorPackArtifact(StrictSchema):
    prior_pack_id: str
    artifact: ContentAddressedArtifact

    @model_validator(mode="after")
    def prior_pack_kind_is_exact(self) -> Self:
        if not self.prior_pack_id.strip() or self.artifact.kind != "prior_pack":
            raise ValueError("prior pack identity or artifact kind is invalid")
        return self


class PosteriorReceipt(StrictSchema):
    kind: str
    model_id: str
    prior: Any
    observations: Any
    posterior: Any
    metadata: Any
    receipt_hash: str

    @model_validator(mode="after")
    def receipt_identity_is_content_addressed(self) -> Self:
        if (
            not self.kind.strip()
            or not self.model_id.strip()
            or not is_sha256_address(self.receipt_hash)
        ):
            raise ValueError("posterior receipt identity is invalid")
        return self


class CompetenceScorerArtifact(StrictSchema):
    scorer_id: str
    scorer_version: str
    model_artifact: ContentAddressedArtifact
    prior_pack: CompetencePriorPackArtifact
    prior: BetaBernoulli
    posterior: BetaBernoulli
    posterior_receipt: PosteriorReceipt
    minimum_posterior_mean: float
    distinct_lineage_ids: list[str]
    selection_decision_refs: list[str]
    oracle_class: PromotionEvidenceClass
    evidence_class: PromotionEvidenceClass
    substitution_allowed: bool
    live_oracle_required: bool
    accepted: bool

    @model_validator(mode="after")
    def scorer_contract_is_complete(self) -> Self:
        if (
            not self.scorer_id.strip()
            or not self.scorer_version.strip()
            or self.model_artifact.kind != "scorer_model"
            or self.posterior_receipt.model_id != self.scorer_id
            or not math.isfinite(self.minimum_posterior_mean)
            or not 0.0 <= self.minimum_posterior_mean <= 1.0
            or self.distinct_lineage_ids != sorted(set(self.distinct_lineage_ids))
            or not self.distinct_lineage_ids
            or self.selection_decision_refs != sorted(set(self.selection_decision_refs))
            or not self.selection_decision_refs
            or (self.live_oracle_required and self.evidence_class != "live")
            or (
                not self.substitution_allowed
                and EVIDENCE_CLASS_RANK[self.evidence_class]
                < EVIDENCE_CLASS_RANK[self.oracle_class]
            )
        ):
            raise ValueError("scorer artifact is incomplete or below its oracle class")
        return self


class CompetenceRefusal(StrictSchema):
    contract: Literal["theorem.competence.v1"] = COMPETENCE_EXCHANGE_SCHEMA
    message_kind: Literal["refusal"] = "refusal"
    code: str
    detail: str
    retryable: bool


class CompetenceSubmissionReceipt(StrictSchema):
    contract: Literal["theorem.competence.v1"] = COMPETENCE_EXCHANGE_SCHEMA
    message_kind: Literal["submission_receipt"] = "submission_receipt"
    job_id: UUID
    operation_id: str
    request_digest: str
    status: CompetenceJobState
    reused: bool


class CompetenceJobStatus(StrictSchema):
    contract: Literal["theorem.competence.v1"] = COMPETENCE_EXCHANGE_SCHEMA
    message_kind: Literal["job_status"] = "job_status"
    job_id: UUID
    operation_id: str
    request_digest: str
    status: CompetenceJobState
    scope: CompetenceRequestScope
    result: CompetenceScorerArtifact | None = None
    refusal: CompetenceRefusal | None = None


class CompetenceCleanupRequest(StrictSchema):
    contract: str
    message_kind: Literal["cleanup_request"]
    cleanup_operation_id: str
    job_id: UUID
    scope: CompetenceRequestScope
    artifact_ids: list[str]


class CompetenceCleanupReceipt(StrictSchema):
    contract: Literal["theorem.competence.v1"] = COMPETENCE_EXCHANGE_SCHEMA
    message_kind: Literal["cleanup_receipt"] = "cleanup_receipt"
    receipt_id: str
    cleanup_operation_id: str
    request_digest: str
    job_id: UUID
    scope: CompetenceRequestScope
    artifact_ids: list[str]
    cleaned: bool
    reused: bool
