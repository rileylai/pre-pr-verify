from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, ValidationInfo, model_validator

from pre_pr_verify.models import FrozenModel, SHA256_PATTERN
from pre_pr_verify.semantic_models import (
    FindingIdentifier,
    FindingState,
    SemanticAxis,
    SemanticFinding,
)
from pre_pr_verify.verification_models import (
    CheckKind,
    CheckOrigin,
    ExecutionStatus,
    FailureKind,
    RequirementLevel,
)


REVIEW_ARTIFACT_SCHEMA_VERSION = "1.0.0"
MAX_REDUCER_REASONS = 16
MAX_REASON_CHARS = 512
MAX_CHECK_SUMMARIES = 256
MAX_EVIDENCE_GAPS = 256
MAX_GAP_REFERENCES = 16
MAX_REFERENCE_CHARS = 512
MAX_VERIFIER_FIELD_CHARS = 256


class ReviewMode(StrEnum):
    FULL = "full"


class AxisStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class ReviewVerdict(StrEnum):
    READY = "READY"
    NEEDS_CHANGES = "NEEDS_CHANGES"
    INCONCLUSIVE = "INCONCLUSIVE"


class CheckOutcome(StrEnum):
    SATISFIED = "satisfied"
    EVIDENCE_GAP = "evidence_gap"
    PASSED = ExecutionStatus.PASSED
    FAILED = ExecutionStatus.FAILED
    NOT_RUN = ExecutionStatus.NOT_RUN
    TIMED_OUT = ExecutionStatus.TIMED_OUT
    ERRORED = ExecutionStatus.ERRORED
    CANCELLED = ExecutionStatus.CANCELLED


class EvidenceGapKind(StrEnum):
    SEMANTIC = "semantic"
    VERIFICATION = "verification"
    SOURCE_PRESERVATION = "source_preservation"


class VerifierIdentity(FrozenModel):
    version: str = Field(min_length=1, max_length=MAX_VERIFIER_FIELD_CHARS)
    commit_or_build: str = Field(min_length=1, max_length=MAX_VERIFIER_FIELD_CHARS)


class ArtifactBindings(FrozenModel):
    changeset_identity: str = Field(pattern=SHA256_PATTERN)
    discovery_identity: str = Field(pattern=SHA256_PATTERN)
    plan_identity: str = Field(pattern=SHA256_PATTERN)
    evidence_identity: str = Field(pattern=SHA256_PATTERN)
    semantic_assessment_identity: str = Field(pattern=SHA256_PATTERN)


class AxisResult(FrozenModel):
    axis: SemanticAxis
    status: AxisStatus
    finding_ids: list[FindingIdentifier] = Field(default_factory=list, max_length=128)
    required_evidence_gap: bool = False
    reducer_reasons: list[str] = Field(
        min_length=1,
        max_length=MAX_REDUCER_REASONS,
    )

    @model_validator(mode="after")
    def validate_axis_result(self) -> AxisResult:
        if self.finding_ids != sorted(set(self.finding_ids)):
            raise ValueError("axis finding IDs must be canonical")
        if any(not reason or len(reason) > MAX_REASON_CHARS for reason in self.reducer_reasons):
            raise ValueError("axis reducer reason is outside its bound")
        if self.required_evidence_gap and self.status is AxisStatus.PASS:
            raise ValueError("axis with a required evidence gap cannot pass")
        return self


class VerificationCheckSummary(FrozenModel):
    check_id: str | None = Field(default=None, min_length=1, max_length=128)
    check_id_sha256: str = Field(pattern=SHA256_PATTERN)
    requirement_level: RequirementLevel
    kind: CheckKind
    origin: CheckOrigin
    outcome: CheckOutcome
    execution_ordinal: int | None = Field(default=None, ge=0)
    failure_kind: FailureKind | None = None
    required_evidence_gap: bool = False

    @model_validator(mode="after")
    def validate_summary(self) -> VerificationCheckSummary:
        if self.kind is CheckKind.STRUCTURAL_INVARIANT:
            if self.execution_ordinal is not None or self.failure_kind is not None:
                raise ValueError("structural summary cannot claim an execution")
            if self.outcome not in {CheckOutcome.SATISFIED, CheckOutcome.EVIDENCE_GAP}:
                raise ValueError("structural summary has an invalid outcome")
        elif self.execution_ordinal is None or self.outcome in {
            CheckOutcome.SATISFIED,
            CheckOutcome.EVIDENCE_GAP,
        }:
            raise ValueError("command summary must bind an execution outcome")
        if self.kind is CheckKind.COMMAND:
            expected_gap = (
                self.requirement_level is RequirementLevel.REQUIRED
                and not (
                    self.outcome is CheckOutcome.PASSED
                    or (
                        self.outcome is CheckOutcome.FAILED
                        and self.failure_kind is FailureKind.VERIFICATION
                    )
                )
            )
            if self.required_evidence_gap != expected_gap:
                raise ValueError("command summary evidence-gap classification is inconsistent")
        return self


class EvidenceGap(FrozenModel):
    gap_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    kind: EvidenceGapKind
    affected_axes: list[SemanticAxis] = Field(min_length=1, max_length=len(SemanticAxis))
    summary: str = Field(min_length=1, max_length=MAX_REASON_CHARS)
    references: list[str] = Field(min_length=1, max_length=MAX_GAP_REFERENCES)

    @model_validator(mode="after")
    def validate_gap(self) -> EvidenceGap:
        if self.affected_axes != sorted(set(self.affected_axes), key=lambda item: item.value):
            raise ValueError("evidence-gap axes must be canonical")
        if self.references != sorted(set(self.references)):
            raise ValueError("evidence-gap references must be canonical")
        if any(len(reference) > MAX_REFERENCE_CHARS for reference in self.references):
            raise ValueError("evidence-gap reference is outside its bound")
        return self


class CollectionReferenceIndex(FrozenModel):
    count: int = Field(ge=0)
    retained_count: int = Field(ge=0)
    omitted_count: int = Field(ge=0)
    blocking_count: int = Field(default=0, ge=0)
    required_gap_count: int = Field(default=0, ge=0)
    identity: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_counts(self) -> CollectionReferenceIndex:
        if self.retained_count + self.omitted_count != self.count:
            raise ValueError("collection reference counts are inconsistent")
        if self.blocking_count > self.count or self.required_gap_count > self.count:
            raise ValueError("collection classification counts exceed the collection")
        return self


class ReviewArtifact(FrozenModel):
    contract: Literal["review_artifact"] = "review_artifact"
    schema_version: Literal["1.0.0"] = "1.0.0"
    review_mode: Literal[ReviewMode.FULL] = ReviewMode.FULL
    verifier: VerifierIdentity
    bindings: ArtifactBindings
    verdict: ReviewVerdict
    verdict_reasons: list[str] = Field(min_length=1, max_length=MAX_REDUCER_REASONS)
    axes: list[AxisResult] = Field(min_length=len(SemanticAxis), max_length=len(SemanticAxis))
    checks: list[VerificationCheckSummary] = Field(max_length=MAX_CHECK_SUMMARIES)
    check_index: CollectionReferenceIndex
    findings: list[SemanticFinding] = Field(max_length=128)
    evidence_gaps: list[EvidenceGap] = Field(max_length=MAX_EVIDENCE_GAPS)
    evidence_gap_index: CollectionReferenceIndex
    identity: str = Field(pattern=SHA256_PATTERN)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"identity"})

    @model_validator(mode="after")
    def validate_artifact(self, info: ValidationInfo) -> ReviewArtifact:
        expected = info.context.get("review_artifact") if info.context else None
        if not isinstance(expected, dict):
            raise ValueError("ReviewArtifact requires canonical bound-input validation")
        if self.semantic_payload() != expected:
            raise ValueError("ReviewArtifact contradicts canonical reduction")
        if [axis.axis for axis in self.axes] != list(SemanticAxis):
            raise ValueError("review axes must be complete and canonical")
        finding_ids = [finding.finding_id for finding in self.findings]
        if finding_ids != sorted(set(finding_ids)):
            raise ValueError("review findings must be unique and canonical")
        owners = [finding_id for axis in self.axes for finding_id in axis.finding_ids]
        if sorted(owners) != finding_ids or len(owners) != len(set(owners)):
            raise ValueError("review finding ownership must be exact")
        finding_by_id = {finding.finding_id: finding for finding in self.findings}
        if any(
            finding_by_id[finding_id].axis is not axis.axis
            for axis in self.axes
            for finding_id in axis.finding_ids
        ):
            raise ValueError("review finding ownership contradicts its axis")
        if any(not reason or len(reason) > MAX_REASON_CHARS for reason in self.verdict_reasons):
            raise ValueError("verdict reducer reason is outside its bound")
        check_keys = [check.check_id_sha256 for check in self.checks]
        if check_keys != sorted(set(check_keys)):
            raise ValueError("review check summaries must be canonical")
        if self.check_index.retained_count != len(self.checks):
            raise ValueError("review check index does not match retained summaries")
        if [gap.gap_id for gap in self.evidence_gaps] != sorted(
            set(gap.gap_id for gap in self.evidence_gaps)
        ):
            raise ValueError("review evidence gaps must be unique and canonical")
        if self.evidence_gap_index.retained_count != len(self.evidence_gaps):
            raise ValueError("review gap index does not match retained gaps")
        if self.identity != hash_payload(self.semantic_payload()):
            raise ValueError("ReviewArtifact identity does not match payload")
        return self


def has_confirmed_blocker(findings: list[SemanticFinding]) -> bool:
    return any(
        finding.state is FindingState.CONFIRMED and finding.blocking
        for finding in findings
    )


def hash_payload(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
