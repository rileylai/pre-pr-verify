from __future__ import annotations

import base64
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import combinations
from typing import Any, Mapping

from pydantic import ValidationError

from pre_pr_verify.discovery_models import (
    DiscoveryResult,
    RequirementResolutionStatus,
)
from pre_pr_verify.models import ChangeSet, FileKind, RawPath
from pre_pr_verify.semantic_models import (
    EvidenceReferenceKind,
    FindingCategory,
    FindingState,
    LegacySemanticAssessment,
    RequirementComparison,
    RequirementRelation,
    SemanticAssessment,
    SemanticAxis,
    SemanticAxisAssessment,
    SemanticContextItem,
    SemanticEvidenceReference,
    SemanticFinding,
    SemanticLimitConcern,
    SemanticLimitGap,
    SemanticReferenceIndex,
    SemanticReferenceSet,
    SemanticStatus,
    MAX_COMPARISONS,
    MAX_COMPARISON_SOURCES,
    MAX_FINDINGS,
    MAX_LIMIT_GAPS,
    hash_payload,
)
from pre_pr_verify.verification_models import VerificationEvidence, VerificationPlan


MAX_CONTEXT_TERMS = 64
MAX_CONTEXT_TERM_BYTES = 256
MAX_CONTEXT_ITEMS = 128
MAX_CONTEXT_TOTAL_BYTES = 4_194_304
MAX_CONTEXT_EXCERPT_CHARS = 2_048

_SPEC_CATEGORIES = {
    "spec_mismatch",
    "spec_partial",
    "spec_contradiction",
    "out_of_scope",
}


class SemanticLimitExceeded(ValueError):
    """A structured 1.5 artifact limit signal; no complete assessment exists."""

    def __init__(self, gap: SemanticLimitGap, values: Iterable[Any] = ()) -> None:
        self.gap = gap
        self.values = tuple(values)
        super().__init__(
            f"semantic {gap.concern.value} limit exceeded at {gap.field}: "
            f"observed {gap.observed}, limit {gap.limit}"
        )


@dataclass(frozen=True)
class SemanticSourceContent:
    """Complete captured text for progressive semantic inspection.

    This runtime view is not persisted in SemanticAssessment. Its locator and
    content identity point back to the canonical ChangeSet.
    """

    path: RawPath
    content_identity: str
    text: str


def iter_semantic_sources(changeset: ChangeSet) -> Iterable[SemanticSourceContent]:
    """Yield complete captured UTF-8 sources without applying preview bounds."""

    blobs = {blob.sha256: blob.data_b64 for blob in changeset.contents}
    for change in sorted(changeset.changes, key=lambda item: item.effective.path.to_bytes()):
        state = change.effective
        if (
            state.kind is not FileKind.REGULAR
            or not state.content_captured
            or state.binary is True
        ):
            continue
        encoded = blobs.get(state.content_identity)
        if encoded is None:
            continue
        raw = base64.b64decode(encoded, validate=True)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        yield SemanticSourceContent(
            path=state.path,
            content_identity=state.content_identity,
            text=text,
        )


def _limit_gap(
    concern: SemanticLimitConcern,
    field: str,
    limit: int,
    observed: int,
    affected_axes: Iterable[SemanticAxis],
    value: Any,
) -> SemanticLimitGap:
    def jsonable(item: Any) -> Any:
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        if isinstance(item, list | tuple):
            return [jsonable(child) for child in item]
        if isinstance(item, dict):
            return {str(key): jsonable(child) for key, child in item.items()}
        return item

    value = jsonable(value)
    return SemanticLimitGap(
        concern=concern,
        field=field,
        limit=limit,
        observed=observed,
        affected_axes=sorted(set(affected_axes), key=lambda axis: axis.value),
        input_identity=hash_payload({"value": value}),
    )


def _bounded_collection(
    values: Iterable[Any],
    *,
    limit: int,
    field: str,
    affected_axes: Iterable[SemanticAxis],
) -> list[Any]:
    bounded: list[Any] = []
    for value in values:
        bounded.append(value)
        if len(bounded) > limit:
            raise SemanticLimitExceeded(
                _limit_gap(
                    SemanticLimitConcern.SEMANTIC_COLLECTION,
                    field,
                    limit,
                    len(bounded),
                    affected_axes,
                    bounded,
                ),
                bounded,
            )
    return bounded


def collect_semantic_context(
    changeset: ChangeSet,
    terms: Iterable[str],
    *,
    max_items: int = MAX_CONTEXT_ITEMS,
) -> list[SemanticContextItem]:
    """Search captured effective regular-file content without executing code.

    This is deliberately generic text context, not an AST or dependency
    engine. It reads only content already bound to the ChangeSet, so callers
    cannot accidentally mix a later repository moment into semantic review.
    """

    term_values = _bounded_collection(
        terms,
        limit=MAX_CONTEXT_TERMS,
        field="semantic_context.terms",
        affected_axes=list(SemanticAxis),
    )
    normalized_terms = tuple(sorted({term for term in term_values if term}))
    for index, term in enumerate(normalized_terms):
        term_bytes = len(term.encode("utf-8"))
        if term_bytes > MAX_CONTEXT_TERM_BYTES:
            raise SemanticLimitExceeded(
                _limit_gap(
                    SemanticLimitConcern.PROSE,
                    f"semantic_context.terms.{index}",
                    MAX_CONTEXT_TERM_BYTES,
                    term_bytes,
                    list(SemanticAxis),
                    term,
                )
            )
    if not 0 < max_items <= MAX_CONTEXT_ITEMS:
        raise ValueError("semantic context item bound is invalid")

    items: list[SemanticContextItem] = []
    total_bytes = 0
    for source in iter_semantic_sources(changeset):
        text = source.text
        matched = [term for term in normalized_terms if term in text]
        if not matched:
            continue
        first = min(text.index(term) for term in matched)
        start = max(0, first - 512)
        excerpt = text[start : start + MAX_CONTEXT_EXCERPT_CHARS]
        excerpt_bytes = len(excerpt.encode("utf-8"))
        if len(items) >= max_items or total_bytes + excerpt_bytes > MAX_CONTEXT_TOTAL_BYTES:
            raise SemanticLimitExceeded(
                _limit_gap(
                    SemanticLimitConcern.SEMANTIC_COLLECTION,
                    "semantic_context.items",
                    min(max_items, MAX_CONTEXT_TOTAL_BYTES),
                    len(items) + 1,
                    list(SemanticAxis),
                    [item.model_dump(mode="json") for item in items]
                    + [{"path": source.path.raw_b64}],
                )
            )
        items.append(
            SemanticContextItem(
                path=source.path,
                matched_terms=matched,
                excerpt=excerpt,
            )
        )
        total_bytes += excerpt_bytes
    return items


def bind_semantic_reference(
    kind: EvidenceReferenceKind,
    identifier: str,
    detail: str,
    *,
    changeset_identity: str,
    discovery_identity: str,
    plan_identity: str,
    evidence_identity: str,
) -> SemanticEvidenceReference:
    return SemanticEvidenceReference(
        kind=kind,
        identifier=identifier,
        detail=detail,
        changeset_identity=changeset_identity,
        discovery_identity=discovery_identity,
        plan_identity=plan_identity,
        evidence_identity=evidence_identity,
    )


def _semantic_reference_set(values: Iterable[str | int]) -> SemanticReferenceSet:
    canonical = sorted(set(values))
    return SemanticReferenceSet(
        count=len(canonical),
        identity=hash_payload({"values": canonical}),
    )


def _reference_index(
    changeset: ChangeSet,
    discovery: DiscoveryResult,
    plan: VerificationPlan,
    evidence: VerificationEvidence,
) -> SemanticReferenceIndex:
    provisional = SemanticReferenceIndex.model_construct(
        changed_paths=_semantic_reference_set(
            change.effective.path.raw_b64 for change in changeset.changes
        ),
        discovery_sources=_semantic_reference_set(
            source.source_id for source in discovery.sources
        ),
        plan_checks=_semantic_reference_set(check.check_id for check in plan.checks),
        execution_ordinals=_semantic_reference_set(
            item.ordinal for item in evidence.executions
        ),
        preservation_ordinals=_semantic_reference_set(
            item.ordinal for item in evidence.source_preservation_failures
        ),
        identity="",
    )
    return SemanticReferenceIndex.model_validate(
        {
            **provisional.semantic_payload(),
            "identity": hash_payload(provisional.semantic_payload()),
        }
    )


def canonical_winning_requirement_set(
    discovery: DiscoveryResult,
) -> SemanticReferenceSet:
    return _semantic_reference_set(
        discovery.requirement_resolution.candidate_source_ids
    )


def _requirement_comparison_limit_gap(
    comparisons: list[RequirementComparison],
) -> SemanticLimitGap | None:
    """Bind an actual concrete-comparison collection overflow to its records."""

    if len(comparisons) != MAX_COMPARISONS:
        return None

    canonical_comparisons = [
        comparison.model_dump(mode="json")
        for comparison in sorted(
            comparisons,
            key=lambda value: tuple(value.source_ids),
        )
    ]
    value = {
        "comparison_requirement": {
            "representation": "bounded-record-collection",
            "retained_comparison_identity": hash_payload(
                {"comparisons": canonical_comparisons}
            ),
            "retained_comparison_count": len(canonical_comparisons),
        },
    }
    return _limit_gap(
        SemanticLimitConcern.SEMANTIC_COLLECTION,
        "requirement_comparisons",
        MAX_COMPARISONS,
        MAX_COMPARISONS + 1,
        [SemanticAxis.SPEC],
        value,
    )


def _legacy_requirement_comparison_limit_gap(
    discovery: DiscoveryResult,
    comparisons: list[RequirementComparison],
) -> SemanticLimitGap | None:
    """Bind a real comparison-record collection overflow to its retained input.

    The comparison record bound is enforced by the producer collection itself.
    This helper is intentionally driven by the retained records at that
    boundary, not by the theoretical number of candidate pairs.  The retained
    records and winning candidate IDs form the gap input identity so a caller
    cannot substitute an unrelated generic overflow.
    """

    winning = sorted(discovery.requirement_resolution.candidate_source_ids)
    if len(winning) <= 1 or len(winning) <= MAX_COMPARISON_SOURCES:
        return None
    if len(comparisons) != MAX_COMPARISONS:
        return None

    winning_set = set(winning)
    covered_pairs: set[tuple[str, str]] = set()
    for comparison in comparisons:
        if not set(comparison.source_ids) <= winning_set:
            return None
        comparison_pairs = set(combinations(comparison.source_ids, 2))
        if covered_pairs.intersection(comparison_pairs):
            return None
        covered_pairs.update(comparison_pairs)
    if covered_pairs == set(combinations(winning, 2)):
        return None

    canonical_comparisons = [
        comparison.model_dump(mode="json")
        for comparison in sorted(
            comparisons,
            key=lambda value: tuple(value.source_ids),
        )
    ]
    value = {
        "winning_requirement_source_ids": winning,
        "comparison_requirement": {
            "representation": "bounded-record-collection",
            "retained_comparison_identity": hash_payload(
                {"comparisons": canonical_comparisons}
            ),
            "retained_comparison_count": len(canonical_comparisons),
        },
    }
    return _limit_gap(
        SemanticLimitConcern.SEMANTIC_COLLECTION,
        "requirement_comparisons",
        MAX_COMPARISONS,
        MAX_COMPARISONS + 1,
        [SemanticAxis.SPEC],
        value,
    )


def _validate_legacy_requirement_completeness(
    discovery: DiscoveryResult,
    comparisons: list[RequirementComparison],
    limit_gaps: list[SemanticLimitGap],
) -> None:
    expected_limit_gap = _legacy_requirement_comparison_limit_gap(
        discovery, comparisons
    )
    supplied_limit_gaps = [
        gap
        for gap in limit_gaps
        if gap.field == "requirement_comparisons"
        or gap.field.startswith("requirement_comparisons.")
    ]
    if supplied_limit_gaps != (
        [expected_limit_gap] if expected_limit_gap is not None else []
    ):
        raise ValueError("requirement comparison limit gap does not match derived capacity")

    winning = sorted(discovery.requirement_resolution.candidate_source_ids)
    if len(winning) <= 1:
        if comparisons:
            raise ValueError("requirement comparisons require multiple winning candidates")
        return
    winning_set = set(winning)
    covered_pairs: set[tuple[str, str]] = set()
    for comparison in comparisons:
        if not set(comparison.source_ids) <= winning_set:
            raise ValueError("requirement comparison must use winning candidates")
        if len(comparison.source_ids) < 2:
            raise ValueError("requirement comparison must contain at least two candidates")
        comparison_pairs = set(combinations(comparison.source_ids, 2))
        if covered_pairs.intersection(comparison_pairs):
            raise ValueError("requirement comparisons overlap ambiguously")
        covered_pairs.update(comparison_pairs)
    required_pairs = set(combinations(winning, 2))
    if not required_pairs <= covered_pairs:
        if expected_limit_gap is not None:
            return
        raise ValueError("semantic assessment omits a winning requirement comparison")
    if expected_limit_gap is not None:
        raise ValueError("complete requirement comparisons cannot carry a limit gap")


def _validate_requirement_reviewed_set(
    discovery: DiscoveryResult,
    reviewed_requirement_sources: SemanticReferenceSet,
    axes: list[SemanticAxisAssessment],
) -> None:
    expected = canonical_winning_requirement_set(discovery)
    if reviewed_requirement_sources == expected:
        return
    spec_axis = next(axis for axis in axes if axis.axis is SemanticAxis.SPEC)
    if (
        spec_axis.status is not SemanticStatus.INCONCLUSIVE
        or not spec_axis.required_evidence_gap
    ):
        raise ValueError(
            "reviewed winning requirement set mismatch requires an inconclusive Spec evidence gap"
        )


def _validate_requirement_completeness(
    discovery: DiscoveryResult,
    comparisons: list[RequirementComparison],
    limit_gaps: list[SemanticLimitGap],
) -> None:
    supplied_limit_gaps = [
        gap
        for gap in limit_gaps
        if gap.field == "requirement_comparisons"
        or gap.field.startswith("requirement_comparisons.")
    ]
    if supplied_limit_gaps:
        expected_limit_gap = _requirement_comparison_limit_gap(comparisons)
        if supplied_limit_gaps != (
            [expected_limit_gap] if expected_limit_gap is not None else []
        ):
            raise ValueError("requirement comparison limit gap does not match derived capacity")

    winning = sorted(discovery.requirement_resolution.candidate_source_ids)
    if len(winning) <= 1:
        if comparisons:
            raise ValueError("requirement comparisons require multiple winning candidates")
        return
    winning_set = set(winning)
    for comparison in comparisons:
        if not set(comparison.source_ids) <= winning_set:
            raise ValueError("requirement comparison must use winning candidates")


def _validate_requirement_semantics(
    discovery: DiscoveryResult,
    assessment: SemanticAssessment,
) -> None:
    spec_axis = next(
        axis for axis in assessment.axes if axis.axis is SemanticAxis.SPEC
    )
    findings = {
        finding.finding_id: finding for finding in assessment.findings
    }
    spec_findings = [findings[finding_id] for finding_id in spec_axis.finding_ids]

    if (
        discovery.requirement_resolution.status
        is RequirementResolutionStatus.MISSING
    ):
        if (
            spec_axis.status is not SemanticStatus.INCONCLUSIVE
            or not spec_axis.required_evidence_gap
            or not any(
                finding.state is FindingState.EVIDENCE_GAP
                and finding.category in {
                    FindingCategory.SPEC_MISMATCH,
                    FindingCategory.SPEC_PARTIAL,
                    FindingCategory.SPEC_CONTRADICTION,
                    FindingCategory.OUT_OF_SCOPE,
                }
                for finding in spec_findings
            )
        ):
            raise ValueError(
                "missing requirements require an inconclusive Spec evidence gap"
            )

    contradictory = [
        comparison
        for comparison in assessment.requirement_comparisons
        if comparison.relation is RequirementRelation.CONTRADICTORY
    ]
    if not contradictory:
        return
    if (
        spec_axis.status is not SemanticStatus.INCONCLUSIVE
        or not spec_axis.required_evidence_gap
    ):
        raise ValueError(
            "contradictory requirements require an inconclusive Spec evidence gap"
        )
    contradiction_findings = [
        finding
        for finding in spec_findings
        if finding.category is FindingCategory.SPEC_CONTRADICTION
        and finding.state is FindingState.EVIDENCE_GAP
        and not finding.blocking
    ]
    for comparison in contradictory:
        if not any(
            set(comparison.source_ids)
            <= {
                reference.identifier
                for reference in finding.evidence
                if reference.kind is EvidenceReferenceKind.DISCOVERY_SOURCE
            }
            for finding in contradiction_findings
        ):
            raise ValueError(
                "contradictory requirements lack a bound Spec conflict finding"
            )


def build_semantic_assessment(
    changeset: ChangeSet,
    discovery: DiscoveryResult,
    plan: VerificationPlan,
    evidence: VerificationEvidence,
    *,
    axes: Iterable[SemanticAxisAssessment],
    reviewed_requirement_sources: SemanticReferenceSet,
    findings: Iterable[SemanticFinding] = (),
    requirement_comparisons: Iterable[RequirementComparison] = (),
    limit_gaps: Iterable[SemanticLimitGap] = (),
) -> SemanticAssessment:
    """Bind model/Skill semantic judgments to one deterministic review scope."""

    if changeset.empty:
        raise ValueError("empty ChangeSet is nothing_to_review")

    axis_values = _bounded_collection(
        axes,
        limit=len(SemanticAxis),
        field="axes",
        affected_axes=list(SemanticAxis),
    )
    finding_values = sorted(
        _bounded_collection(
            findings,
            limit=MAX_FINDINGS,
            field="findings",
            affected_axes=list(SemanticAxis),
        ),
        key=lambda finding: finding.finding_id,
    )
    try:
        comparison_values = sorted(
            _bounded_collection(
                requirement_comparisons,
                limit=MAX_COMPARISONS,
                field="requirement_comparisons",
                affected_axes=[SemanticAxis.SPEC],
            ),
            key=lambda comparison: tuple(comparison.source_ids),
        )
    except SemanticLimitExceeded as error:
        retained = [
            value
            for value in error.values[:MAX_COMPARISONS]
            if isinstance(value, RequirementComparison)
        ]
        derived_gap = _requirement_comparison_limit_gap(retained)
        if derived_gap is None:
            raise
        raise SemanticLimitExceeded(derived_gap, error.values) from error
    gap_values = sorted(
        _bounded_collection(
            limit_gaps,
            limit=MAX_LIMIT_GAPS,
            field="limit_gaps",
            affected_axes=list(SemanticAxis),
        ),
        key=lambda gap: (gap.concern.value, gap.field),
    )

    index = _reference_index(changeset, discovery, plan, evidence)
    _validate_requirement_reviewed_set(
        discovery,
        reviewed_requirement_sources,
        axis_values,
    )
    _validate_requirement_completeness(discovery, comparison_values, gap_values)

    provisional = SemanticAssessment.model_construct(
        changeset_identity=changeset.identity,
        discovery_identity=discovery.identity,
        plan_identity=plan.identity,
        evidence_identity=evidence.identity,
        reviewed_requirement_sources=reviewed_requirement_sources,
        requirement_comparisons=comparison_values,
        limit_gaps=gap_values,
        axes=axis_values,
        findings=finding_values,
        reference_index=index,
        identity="",
    )
    return load_semantic_assessment(
        {
            **provisional.semantic_payload(),
            "identity": hash_payload(provisional.semantic_payload()),
        },
        changeset,
        discovery,
        plan,
        evidence,
    )


def load_semantic_assessment(
    payload: Mapping[str, Any],
    changeset: ChangeSet,
    discovery: DiscoveryResult,
    plan: VerificationPlan,
    evidence: VerificationEvidence,
) -> SemanticAssessment | LegacySemanticAssessment:
    """Deserialize only through the same scope/reference checks as producers."""

    if changeset.empty:
        raise ValueError("empty ChangeSet is nothing_to_review")
    if plan.changeset_identity != changeset.identity:
        raise ValueError("semantic plan is not bound to the ChangeSet")
    if plan.discovery_identity != discovery.identity:
        raise ValueError("semantic plan is not bound to Discovery")
    if evidence.plan.identity != plan.identity:
        raise ValueError("semantic evidence is not bound to the VerificationPlan")
    if (
        evidence.plan.changeset_identity != changeset.identity
        or evidence.plan.discovery_identity != discovery.identity
    ):
        raise ValueError("semantic evidence scope identities do not match")

    expected_index = _reference_index(changeset, discovery, plan, evidence)
    schema_version = payload.get("schema_version")
    if schema_version == "1.0.0":
        assessment_type = LegacySemanticAssessment
    elif schema_version == "1.1.0":
        assessment_type = SemanticAssessment
    else:
        raise ValueError(f"unsupported SemanticAssessment schema version: {schema_version!r}")
    try:
        assessment = assessment_type.model_validate(
            dict(payload),
            context={
                "semantic_scope": expected_index,
                "scope_identities": (
                    changeset.identity,
                    discovery.identity,
                    plan.identity,
                    evidence.identity,
                ),
            },
        )
    except ValidationError as error:
        _raise_structured_limit(error, payload)
        raise

    valid_paths = {
        change.effective.path.raw_b64 for change in changeset.changes
    }
    valid_sources = {source.source_id: source for source in discovery.sources}
    valid_checks = {check.check_id for check in plan.checks}
    valid_executions = {str(item.ordinal) for item in evidence.executions}
    valid_preservation = {
        str(item.ordinal) for item in evidence.source_preservation_failures
    }
    for finding in assessment.findings:
        references = finding.evidence
        for reference in references:
            valid = {
                EvidenceReferenceKind.CHANGE_PATH: reference.identifier in valid_paths,
                EvidenceReferenceKind.DISCOVERY_SOURCE: reference.identifier in valid_sources,
                EvidenceReferenceKind.PLAN_CHECK: reference.identifier in valid_checks,
                EvidenceReferenceKind.EXECUTION: reference.identifier in valid_executions,
                EvidenceReferenceKind.SOURCE_PRESERVATION: reference.identifier in valid_preservation,
            }[reference.kind]
            if not valid:
                raise ValueError(
                    f"semantic evidence reference does not exist: {reference.kind.value}:{reference.identifier}"
                )

        cited_sources = {
            reference.identifier
            for reference in references
            if reference.kind is EvidenceReferenceKind.DISCOVERY_SOURCE
        }
        if finding.state is FindingState.CONFIRMED:
            if finding.category.value == "standard_violation":
                if not set(discovery.standards_source_ids).intersection(cited_sources):
                    raise ValueError(
                        "confirmed standard violation requires a canonical Standards source"
                    )
            elif finding.category.value in _SPEC_CATEGORIES:
                winning = set(discovery.requirement_resolution.candidate_source_ids)
                if not winning or not winning.intersection(cited_sources):
                    raise ValueError(
                        "confirmed spec finding must cite a winning requirement source"
                    )
            elif finding.category.value == "contextual_security":
                if not any(
                    reference.kind is EvidenceReferenceKind.CHANGE_PATH
                    for reference in references
                ):
                    raise ValueError(
                        "confirmed contextual security finding requires changed-path evidence"
                    )
            elif finding.category.value == "test_gap":
                if not any(
                    reference.kind
                    in {
                        EvidenceReferenceKind.CHANGE_PATH,
                        EvidenceReferenceKind.PLAN_CHECK,
                        EvidenceReferenceKind.EXECUTION,
                    }
                    for reference in references
                ):
                    raise ValueError("confirmed test gap lacks behavior evidence")
            elif finding.category.value == "impact_regression":
                if not any(
                    reference.kind
                    in {
                        EvidenceReferenceKind.CHANGE_PATH,
                        EvidenceReferenceKind.EXECUTION,
                    }
                    for reference in references
                ):
                    raise ValueError("confirmed impact finding lacks behavior evidence")
    if isinstance(assessment, LegacySemanticAssessment):
        _validate_legacy_requirement_completeness(
            discovery,
            list(assessment.requirement_comparisons),
            list(assessment.limit_gaps),
        )
    else:
        _validate_requirement_reviewed_set(
            discovery,
            assessment.reviewed_requirement_sources,
            list(assessment.axes),
        )
        _validate_requirement_completeness(
            discovery,
            list(assessment.requirement_comparisons),
            list(assessment.limit_gaps),
        )
    _validate_requirement_semantics(discovery, assessment)
    return assessment


def _raise_structured_limit(
    error: ValidationError, payload: Mapping[str, Any]
) -> None:
    for item in error.errors():
        error_type = item["type"]
        if error_type not in {"string_too_long", "too_long"}:
            continue
        location = item["loc"]
        value: Any = payload
        try:
            for part in location:
                value = value[part]  # type: ignore[index]
        except (KeyError, IndexError, TypeError):
            value = item.get("input", "")
        context = item.get("ctx") or {}
        limit = int(context.get("max_length", 1))
        observed = len(value) if hasattr(value, "__len__") else limit + 1
        affected_axes: list[SemanticAxis] = list(SemanticAxis)
        if location and location[0] == "requirement_comparisons":
            affected_axes = [SemanticAxis.SPEC]
        elif len(location) >= 2 and location[0] in {"axes", "findings"}:
            try:
                axis_value = payload[location[0]][location[1]]["axis"]  # type: ignore[index]
                affected_axes = [SemanticAxis(axis_value)]
            except (KeyError, IndexError, TypeError, ValueError):
                pass
        identifier_fields = {"finding_id", "finding_ids", "source_ids", "identifier"}
        if error_type == "too_long":
            concern = SemanticLimitConcern.SEMANTIC_COLLECTION
        elif any(part in identifier_fields for part in location if isinstance(part, str)):
            concern = SemanticLimitConcern.IDENTIFIER
        else:
            concern = SemanticLimitConcern.PROSE
        raise SemanticLimitExceeded(
            _limit_gap(
                concern,
                ".".join(str(part) for part in location),
                limit,
                observed,
                affected_axes,
                value,
            )
        ) from error
