from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationInfo, model_validator

from pre_pr_verify.models import FrozenModel, RawPath, SHA256_PATTERN


SEMANTIC_ASSESSMENT_SCHEMA_VERSION = "1.0.0"

# These are contract bounds, not token-accounting or retrieval limits.
MAX_AXIS_RATIONALE_CHARS = 2_048
MAX_FINDING_TITLE_CHARS = 256
MAX_FINDING_ID_CHARS = 128
MAX_FINDING_EXPLANATION_CHARS = 4_096
MAX_REFERENCE_IDENTIFIER_CHARS = 512
MAX_REFERENCE_DETAIL_CHARS = 512
MAX_REQUIREMENT_RATIONALE_CHARS = 1_024
MAX_FINDINGS = 128
MAX_REFERENCES_PER_FINDING = 16
MAX_COMPARISONS = 64
MAX_COMPARISON_SOURCES = 16
MAX_FINDING_IDS_PER_AXIS = 128
MAX_LIMIT_GAPS = 16
MAX_LIMIT_FIELD_CHARS = 256
MAX_CONTEXT_MATCHED_TERMS = 64
MAX_CONTEXT_EXCERPT_CHARS = 2_048

FindingIdentifier = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_FINDING_ID_CHARS,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    ),
]
SourceIdentifier = Annotated[
    str,
    Field(min_length=64, max_length=64, pattern=SHA256_PATTERN),
]
ContextTerm = Annotated[str, Field(min_length=1, max_length=256)]


class SemanticAxis(StrEnum):
    SPEC = "spec"
    STANDARDS = "standards"
    IMPACT = "impact"
    TEST_SUFFICIENCY = "test_sufficiency"
    CONTEXTUAL_SECURITY = "contextual_security"


class SemanticStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class FindingState(StrEnum):
    CONFIRMED = "confirmed"
    UNVERIFIED = "unverified"
    EVIDENCE_GAP = "evidence_gap"


class FindingSeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingCategory(StrEnum):
    SPEC_MISMATCH = "spec_mismatch"
    SPEC_PARTIAL = "spec_partial"
    SPEC_CONTRADICTION = "spec_contradiction"
    OUT_OF_SCOPE = "out_of_scope"
    STANDARD_VIOLATION = "standard_violation"
    IMPACT_REGRESSION = "impact_regression"
    TEST_GAP = "test_gap"
    CONTEXTUAL_SECURITY = "contextual_security"
    UNSUPPORTED_SUSPICION = "unsupported_suspicion"


class EvidenceReferenceKind(StrEnum):
    CHANGE_PATH = "change_path"
    DISCOVERY_SOURCE = "discovery_source"
    PLAN_CHECK = "plan_check"
    EXECUTION = "execution"
    SOURCE_PRESERVATION = "source_preservation"


class RequirementRelation(StrEnum):
    COMPLEMENTARY = "complementary"
    CONTRADICTORY = "contradictory"


class SemanticLimitConcern(StrEnum):
    PROSE = "prose"
    IDENTIFIER = "identifier"
    SEMANTIC_COLLECTION = "semantic_collection"


class SemanticEvidenceReference(FrozenModel):
    kind: EvidenceReferenceKind
    identifier: str = Field(
        min_length=1, max_length=MAX_REFERENCE_IDENTIFIER_CHARS
    )
    detail: str = Field(min_length=1, max_length=MAX_REFERENCE_DETAIL_CHARS)
    changeset_identity: str = Field(pattern=SHA256_PATTERN)
    discovery_identity: str = Field(pattern=SHA256_PATTERN)
    plan_identity: str = Field(pattern=SHA256_PATTERN)
    evidence_identity: str = Field(pattern=SHA256_PATTERN)


class RequirementComparison(FrozenModel):
    source_ids: list[SourceIdentifier] = Field(
        min_length=2, max_length=MAX_COMPARISON_SOURCES
    )
    relation: RequirementRelation
    rationale: str = Field(min_length=1, max_length=MAX_REQUIREMENT_RATIONALE_CHARS)

    @model_validator(mode="after")
    def validate_sources(self) -> RequirementComparison:
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("requirement comparison source IDs must be unique")
        if self.source_ids != sorted(self.source_ids):
            raise ValueError("requirement comparison source IDs must be canonical")
        return self


class SemanticContextItem(FrozenModel):
    path: RawPath
    matched_terms: list[ContextTerm] = Field(
        min_length=1, max_length=MAX_CONTEXT_MATCHED_TERMS
    )
    excerpt: str = Field(min_length=1, max_length=MAX_CONTEXT_EXCERPT_CHARS)

    @model_validator(mode="after")
    def validate_context(self) -> SemanticContextItem:
        if self.matched_terms != sorted(set(self.matched_terms)):
            raise ValueError("semantic context terms must be canonical")
        return self


class SemanticFinding(FrozenModel):
    finding_id: FindingIdentifier
    axis: SemanticAxis
    category: FindingCategory
    state: FindingState
    severity: FindingSeverity
    blocking: bool
    title: str = Field(min_length=1, max_length=MAX_FINDING_TITLE_CHARS)
    explanation: str = Field(
        min_length=1, max_length=MAX_FINDING_EXPLANATION_CHARS
    )
    evidence: list[SemanticEvidenceReference] = Field(
        min_length=1, max_length=MAX_REFERENCES_PER_FINDING
    )

    @model_validator(mode="after")
    def validate_finding(self) -> SemanticFinding:
        expected_axis = {
            FindingCategory.SPEC_MISMATCH: SemanticAxis.SPEC,
            FindingCategory.SPEC_PARTIAL: SemanticAxis.SPEC,
            FindingCategory.SPEC_CONTRADICTION: SemanticAxis.SPEC,
            FindingCategory.OUT_OF_SCOPE: SemanticAxis.SPEC,
            FindingCategory.STANDARD_VIOLATION: SemanticAxis.STANDARDS,
            FindingCategory.IMPACT_REGRESSION: SemanticAxis.IMPACT,
            FindingCategory.TEST_GAP: SemanticAxis.TEST_SUFFICIENCY,
            FindingCategory.CONTEXTUAL_SECURITY: SemanticAxis.CONTEXTUAL_SECURITY,
            FindingCategory.UNSUPPORTED_SUSPICION: SemanticAxis.CONTEXTUAL_SECURITY,
        }[self.category]
        if self.axis is not expected_axis:
            raise ValueError("finding category is incompatible with its axis")
        if self.blocking and self.state is not FindingState.CONFIRMED:
            raise ValueError("only confirmed findings may be blocking")
        if self.category is FindingCategory.UNSUPPORTED_SUSPICION and (
            self.state is FindingState.CONFIRMED or self.blocking
        ):
            raise ValueError("unsupported suspicion cannot be a confirmed blocker")
        identifiers = [
            (reference.kind.value, reference.identifier) for reference in self.evidence
        ]
        if identifiers != sorted(set(identifiers)):
            raise ValueError("finding evidence references must be canonical")
        return self


class SemanticAxisAssessment(FrozenModel):
    axis: SemanticAxis
    status: SemanticStatus
    rationale: str = Field(min_length=1, max_length=MAX_AXIS_RATIONALE_CHARS)
    finding_ids: list[FindingIdentifier] = Field(
        default_factory=list, max_length=MAX_FINDING_IDS_PER_AXIS
    )
    required_evidence_gap: bool = False

    @model_validator(mode="after")
    def validate_axis(self) -> SemanticAxisAssessment:
        if self.finding_ids != sorted(set(self.finding_ids)):
            raise ValueError("axis finding IDs must be canonical")
        if self.required_evidence_gap and self.status is not SemanticStatus.INCONCLUSIVE:
            raise ValueError("an evidence gap requires an inconclusive axis")
        return self


class SemanticLimitGap(FrozenModel):
    concern: SemanticLimitConcern
    field: str = Field(min_length=1, max_length=MAX_LIMIT_FIELD_CHARS)
    limit: int = Field(ge=1)
    observed: int = Field(ge=1)
    affected_axes: list[SemanticAxis] = Field(
        min_length=1, max_length=len(SemanticAxis)
    )
    input_identity: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_gap(self) -> SemanticLimitGap:
        if self.observed <= self.limit:
            raise ValueError("semantic limit gap must exceed its limit")
        expected_axes = sorted(set(self.affected_axes), key=lambda axis: axis.value)
        if self.affected_axes != expected_axes:
            raise ValueError("semantic limit gap axes must be canonical")
        return self


class SemanticReferenceSet(FrozenModel):
    count: int = Field(ge=0)
    identity: str = Field(pattern=SHA256_PATTERN)


class SemanticReferenceIndex(FrozenModel):
    changed_paths: SemanticReferenceSet
    discovery_sources: SemanticReferenceSet
    plan_checks: SemanticReferenceSet
    execution_ordinals: SemanticReferenceSet
    preservation_ordinals: SemanticReferenceSet
    identity: str = Field(pattern=SHA256_PATTERN)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"identity"})

    @model_validator(mode="after")
    def validate_index(self) -> SemanticReferenceIndex:
        if self.identity != hash_payload(self.semantic_payload()):
            raise ValueError("semantic reference index identity does not match payload")
        return self


class SemanticAssessment(FrozenModel):
    contract: Literal["semantic_assessment"] = "semantic_assessment"
    schema_version: Literal["1.0.0"] = "1.0.0"
    changeset_identity: str = Field(pattern=SHA256_PATTERN)
    discovery_identity: str = Field(pattern=SHA256_PATTERN)
    plan_identity: str = Field(pattern=SHA256_PATTERN)
    evidence_identity: str = Field(pattern=SHA256_PATTERN)
    requirement_comparisons: list[RequirementComparison] = Field(
        default_factory=list, max_length=MAX_COMPARISONS
    )
    limit_gaps: list[SemanticLimitGap] = Field(
        default_factory=list, max_length=MAX_LIMIT_GAPS
    )
    axes: list[SemanticAxisAssessment] = Field(min_length=len(SemanticAxis), max_length=len(SemanticAxis))
    findings: list[SemanticFinding] = Field(
        default_factory=list, max_length=MAX_FINDINGS
    )
    reference_index: SemanticReferenceIndex
    identity: str = Field(pattern=SHA256_PATTERN)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"identity"})

    @model_validator(mode="after")
    def validate_contract(
        self, info: ValidationInfo
    ) -> SemanticAssessment:
        expected_index = info.context.get("semantic_scope") if info.context else None
        expected_identities = (
            info.context.get("scope_identities") if info.context else None
        )
        if not isinstance(expected_index, SemanticReferenceIndex) or not isinstance(
            expected_identities, tuple
        ):
            raise ValueError(
                "semantic assessment requires canonical scope validation"
            )
        if self.reference_index != expected_index:
            raise ValueError("semantic reference index does not match bound evidence")
        if (
            self.changeset_identity,
            self.discovery_identity,
            self.plan_identity,
            self.evidence_identity,
        ) != expected_identities:
            raise ValueError("semantic assessment identities do not match bound evidence")
        expected_axes = list(SemanticAxis)
        if [axis.axis for axis in self.axes] != expected_axes:
            raise ValueError("semantic axes must be complete and canonically ordered")
        comparison_keys = [tuple(item.source_ids) for item in self.requirement_comparisons]
        if comparison_keys != sorted(set(comparison_keys)):
            raise ValueError("requirement comparisons must be canonical")
        gap_keys = [(gap.concern.value, gap.field) for gap in self.limit_gaps]
        if gap_keys != sorted(set(gap_keys)):
            raise ValueError("semantic limit gaps must be canonical")
        finding_ids = [finding.finding_id for finding in self.findings]
        if finding_ids != sorted(set(finding_ids)):
            raise ValueError("semantic finding IDs must be unique and canonical")
        known_findings = set(finding_ids)
        owners: dict[str, SemanticAxis] = {}
        for axis in self.axes:
            if not set(axis.finding_ids) <= known_findings:
                raise ValueError("axis references an unknown semantic finding")
            for finding_id in axis.finding_ids:
                if finding_id in owners:
                    raise ValueError("semantic finding ownership must be unique")
                owners[finding_id] = axis.axis
        if set(owners) != known_findings:
            raise ValueError("semantic finding ownership is incomplete; orphan finding")
        finding_by_id = {finding.finding_id: finding for finding in self.findings}
        if any(
            finding_by_id[finding_id].axis is not owner
            for finding_id, owner in owners.items()
        ):
            raise ValueError("semantic finding ownership does not match its declared axis")
        for axis in self.axes:
            if axis.status is SemanticStatus.PASS and any(
                finding_by_id[finding_id].state is FindingState.CONFIRMED
                and finding_by_id[finding_id].blocking
                for finding_id in axis.finding_ids
            ):
                raise ValueError("PASS axis cannot own a confirmed blocking finding")
        for finding in self.findings:
            for reference in finding.evidence:
                if (
                    reference.changeset_identity != self.changeset_identity
                    or reference.discovery_identity != self.discovery_identity
                    or reference.plan_identity != self.plan_identity
                    or reference.evidence_identity != self.evidence_identity
                ):
                    raise ValueError("semantic evidence reference is not bound")
        axis_by_kind = {axis.axis: axis for axis in self.axes}
        for gap in self.limit_gaps:
            if gap.concern is not SemanticLimitConcern.SEMANTIC_COLLECTION:
                continue
            for affected_axis in gap.affected_axes:
                axis = axis_by_kind[affected_axis]
                if (
                    axis.status is not SemanticStatus.INCONCLUSIVE
                    or not axis.required_evidence_gap
                ):
                    raise ValueError(
                        "semantic collection overflow requires an inconclusive evidence-gap axis"
                    )
        if self.identity != hash_payload(self.semantic_payload()):
            raise ValueError("semantic assessment identity does not match payload")
        return self


def hash_payload(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
