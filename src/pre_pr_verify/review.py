from __future__ import annotations

import json
import hashlib
from collections.abc import Mapping
from typing import Any

from pre_pr_verify.discovery_models import DiscoveryResult
from pre_pr_verify.models import ChangeSet
from pre_pr_verify.review_models import (
    ArtifactBindings,
    AxisResult,
    AxisStatus,
    CheckOutcome,
    CollectionReferenceIndex,
    EvidenceGap,
    EvidenceGapKind,
    ReviewArtifact,
    ReviewVerdict,
    MAX_CHECK_SUMMARIES,
    MAX_EVIDENCE_GAPS,
    VerifierIdentity,
    VerificationCheckSummary,
    hash_payload,
    has_confirmed_blocker,
)
from pre_pr_verify.semantic import load_semantic_assessment
from pre_pr_verify.semantic_models import (
    FindingState,
    SemanticAssessment,
    SemanticAxis,
    SemanticStatus,
)
from pre_pr_verify.verification_models import (
    CheckKind,
    ExecutionStatus,
    FailureKind,
    RequirementLevel,
    VerificationEvidence,
    VerificationPlan,
)


_ALL_AXES: list[SemanticAxis] = sorted(list(SemanticAxis), key=str)


def _validate_bindings(
    changeset: ChangeSet,
    discovery: DiscoveryResult,
    plan: VerificationPlan,
    evidence: VerificationEvidence,
    assessment: SemanticAssessment,
) -> SemanticAssessment:
    if changeset.empty:
        raise ValueError("empty ChangeSet is nothing_to_review")
    if plan.changeset_identity != changeset.identity:
        raise ValueError("ReviewArtifact plan is not bound to the ChangeSet")
    if plan.discovery_identity != discovery.identity:
        raise ValueError("ReviewArtifact plan is not bound to Discovery")
    if evidence.plan.identity != plan.identity:
        raise ValueError("ReviewArtifact evidence is not bound to the plan")
    return load_semantic_assessment(
        assessment.model_dump(mode="json"),
        changeset,
        discovery,
        plan,
        evidence,
    )


def _check_summaries(
    plan: VerificationPlan, evidence: VerificationEvidence
) -> list[VerificationCheckSummary]:
    execution_by_id = {
        execution.result.request.check_id: execution for execution in evidence.executions
    }
    preservation_check_ids = {
        failure.check_id for failure in evidence.source_preservation_failures
    }
    summaries: list[VerificationCheckSummary] = []
    for check in plan.checks:
        check_digest = hashlib.sha256(check.check_id.encode("utf-8")).hexdigest()
        bounded_check_id = check.check_id if len(check.check_id) <= 128 else None
        if check.kind is CheckKind.STRUCTURAL_INVARIANT:
            preservation_gap = (
                check.check_id == "source-preservation" and bool(preservation_check_ids)
            )
            summaries.append(
                VerificationCheckSummary(
                    check_id=bounded_check_id,
                    check_id_sha256=check_digest,
                    requirement_level=check.requirement_level,
                    kind=check.kind,
                    origin=check.origin,
                    outcome=(
                        CheckOutcome.EVIDENCE_GAP
                        if preservation_gap
                        else CheckOutcome.SATISFIED
                    ),
                    required_evidence_gap=preservation_gap,
                )
            )
            continue
        execution = execution_by_id[check.check_id]
        result = execution.result
        summaries.append(
            VerificationCheckSummary(
                check_id=bounded_check_id,
                check_id_sha256=check_digest,
                requirement_level=check.requirement_level,
                kind=check.kind,
                origin=check.origin,
                outcome=CheckOutcome(result.status.value),
                execution_ordinal=execution.ordinal,
                failure_kind=result.failure_kind,
                required_evidence_gap=result.required_evidence_gap,
            )
        )
    return sorted(summaries, key=lambda item: item.check_id_sha256)


def _evidence_gaps(
    assessment: SemanticAssessment,
    evidence: VerificationEvidence,
) -> list[EvidenceGap]:
    gaps: list[EvidenceGap] = []
    for axis in assessment.axes:
        if axis.required_evidence_gap or axis.status is SemanticStatus.INCONCLUSIVE:
            gaps.append(
                EvidenceGap(
                    gap_id=f"semantic.{axis.axis.value}",
                    kind=EvidenceGapKind.SEMANTIC,
                    affected_axes=[axis.axis],
                    summary=f"Semantic assessment did not establish readiness for {axis.axis.value}.",
                    references=[f"semantic-axis:{axis.axis.value}"],
                )
            )
    for gap in assessment.limit_gaps:
        gaps.append(
            EvidenceGap(
                gap_id=(
                    f"semantic-limit.{gap.concern.value}."
                    f"{hashlib.sha256(gap.field.encode('utf-8')).hexdigest()}"
                ).replace("_", "-"),
                kind=EvidenceGapKind.SEMANTIC,
                affected_axes=gap.affected_axes,
                summary=f"Semantic input exceeded the canonical {gap.field} bound.",
                references=[f"semantic-limit:{gap.input_identity}"],
            )
        )
    for execution in evidence.executions:
        result = execution.result
        if result.required_evidence_gap:
            gaps.append(
                EvidenceGap(
                    gap_id=f"verification.{execution.ordinal}",
                    kind=EvidenceGapKind.VERIFICATION,
                    affected_axes=[SemanticAxis.TEST_SUFFICIENCY],
                    summary=f"Required check at execution {execution.ordinal} did not produce reliable evidence.",
                    references=[
                        f"execution:{execution.ordinal}",
                        "plan-check-sha256:"
                        + hashlib.sha256(result.request.check_id.encode("utf-8")).hexdigest(),
                    ],
                )
            )
    for failure in evidence.source_preservation_failures:
        gaps.append(
            EvidenceGap(
                gap_id=f"source-preservation.{failure.ordinal}",
                kind=EvidenceGapKind.SOURCE_PRESERVATION,
                affected_axes=_ALL_AXES,
                summary="Post-execution source preservation failed; review confidence is invalidated.",
                references=[f"execution:{failure.ordinal}", f"source-preservation:{failure.ordinal}"],
            )
        )
    return sorted(gaps, key=lambda gap: gap.gap_id)


def _reduce_axes(
    assessment: SemanticAssessment,
    evidence: VerificationEvidence,
    gaps: list[EvidenceGap],
) -> list[AxisResult]:
    findings = {finding.finding_id: finding for finding in assessment.findings}
    affected_by_gap = {
        axis for gap in gaps for axis in gap.affected_axes
    }
    required_verification_failures = [
        execution
        for execution in evidence.executions
        if execution.result.request.requirement_level is RequirementLevel.REQUIRED
        and execution.result.status is ExecutionStatus.FAILED
        and execution.result.failure_kind is FailureKind.VERIFICATION
    ]
    results: list[AxisResult] = []
    for semantic_axis in assessment.axes:
        owned = [findings[finding_id] for finding_id in semantic_axis.finding_ids]
        blocker = has_confirmed_blocker(owned)
        verification_failure = (
            semantic_axis.axis is SemanticAxis.TEST_SUFFICIENCY
            and bool(required_verification_failures)
        )
        reasons: list[str] = []
        if blocker:
            status = AxisStatus.FAIL
            reasons.append("confirmed blocking semantic finding")
        elif verification_failure:
            status = AxisStatus.FAIL
            reasons.append("required verification proved a change failure")
        elif semantic_axis.axis in affected_by_gap:
            status = AxisStatus.INCONCLUSIVE
            reasons.append("required evidence remains unresolved")
        elif semantic_axis.status is SemanticStatus.FAIL:
            status = AxisStatus.INCONCLUSIVE
            reasons.append("semantic FAIL lacks a confirmed blocking finding")
        else:
            status = AxisStatus(semantic_axis.status.value)
            reasons.append("validated semantic assessment is complete")
        results.append(
            AxisResult(
                axis=semantic_axis.axis,
                status=status,
                finding_ids=semantic_axis.finding_ids,
                required_evidence_gap=semantic_axis.axis in affected_by_gap,
                reducer_reasons=reasons,
            )
        )
    return results


def _reduce_verdict(
    axes: list[AxisResult],
    assessment: SemanticAssessment,
    evidence: VerificationEvidence,
    gaps: list[EvidenceGap],
) -> tuple[ReviewVerdict, list[str]]:
    semantic_blocker = has_confirmed_blocker(assessment.findings)
    verification_blocker = any(
        execution.result.request.requirement_level is RequirementLevel.REQUIRED
        and execution.result.status is ExecutionStatus.FAILED
        and execution.result.failure_kind is FailureKind.VERIFICATION
        for execution in evidence.executions
    )
    if semantic_blocker or verification_blocker:
        return ReviewVerdict.NEEDS_CHANGES, [
            "confirmed blocking defect takes precedence over unresolved evidence gaps"
        ]
    if gaps or any(axis.status is not AxisStatus.PASS for axis in axes):
        return ReviewVerdict.INCONCLUSIVE, [
            "readiness cannot be established from complete required evidence"
        ]
    return ReviewVerdict.READY, [
        "all five axes pass and all required verification evidence is complete"
    ]


def _collection_index(
    values: list[VerificationCheckSummary] | list[EvidenceGap],
    retained_count: int,
    *,
    blocking_count: int = 0,
    required_gap_count: int = 0,
) -> CollectionReferenceIndex:
    payloads = [value.model_dump(mode="json") for value in values]
    return CollectionReferenceIndex(
        count=len(values),
        retained_count=retained_count,
        omitted_count=len(values) - retained_count,
        blocking_count=blocking_count,
        required_gap_count=required_gap_count,
        identity=hash_payload({"values": payloads}),
    )


def _canonical_payload(
    changeset: ChangeSet,
    discovery: DiscoveryResult,
    plan: VerificationPlan,
    evidence: VerificationEvidence,
    assessment: SemanticAssessment,
    verifier: VerifierIdentity,
) -> dict[str, Any]:
    assessment = _validate_bindings(changeset, discovery, plan, evidence, assessment)
    all_gaps = _evidence_gaps(assessment, evidence)
    all_checks = _check_summaries(plan, evidence)
    axes = _reduce_axes(assessment, evidence, all_gaps)
    verdict, verdict_reasons = _reduce_verdict(
        axes, assessment, evidence, all_gaps
    )
    ordered_for_retention = sorted(
        all_checks,
        key=lambda check: (
            not (
                check.requirement_level is RequirementLevel.REQUIRED
                and check.outcome is CheckOutcome.FAILED
                and check.failure_kind is FailureKind.VERIFICATION
            ),
            not check.required_evidence_gap,
            check.check_id_sha256,
        ),
    )
    checks = sorted(
        ordered_for_retention[:MAX_CHECK_SUMMARIES],
        key=lambda check: check.check_id_sha256,
    )
    gaps = all_gaps[:MAX_EVIDENCE_GAPS]
    required_change_failures = sum(
        check.requirement_level is RequirementLevel.REQUIRED
        and check.outcome is CheckOutcome.FAILED
        and check.failure_kind is FailureKind.VERIFICATION
        for check in all_checks
    )
    required_check_gaps = sum(check.required_evidence_gap for check in all_checks)
    bindings = ArtifactBindings(
        changeset_identity=changeset.identity,
        discovery_identity=discovery.identity,
        plan_identity=plan.identity,
        evidence_identity=evidence.identity,
        semantic_assessment_identity=assessment.identity,
    )
    provisional = ReviewArtifact.model_construct(
        contract="review_artifact",
        schema_version="1.0.0",
        review_mode="full",
        verifier=verifier,
        bindings=bindings,
        verdict=verdict,
        verdict_reasons=verdict_reasons,
        axes=axes,
        checks=checks,
        check_index=_collection_index(
            all_checks,
            len(checks),
            blocking_count=required_change_failures,
            required_gap_count=required_check_gaps,
        ),
        findings=assessment.findings,
        evidence_gaps=gaps,
        evidence_gap_index=_collection_index(
            all_gaps,
            len(gaps),
            required_gap_count=len(all_gaps),
        ),
        identity="",
    )
    return provisional.semantic_payload()


def build_review_artifact(
    changeset: ChangeSet,
    discovery: DiscoveryResult,
    plan: VerificationPlan,
    evidence: VerificationEvidence,
    assessment: SemanticAssessment,
    *,
    verifier_version: str,
    verifier_commit_or_build: str,
) -> ReviewArtifact:
    verifier = VerifierIdentity(
        version=verifier_version,
        commit_or_build=verifier_commit_or_build,
    )
    payload = _canonical_payload(
        changeset, discovery, plan, evidence, assessment, verifier
    )
    return ReviewArtifact.model_validate(
        {**payload, "identity": hash_payload(payload)},
        context={"review_artifact": payload},
    )


def load_review_artifact(
    value: Mapping[str, Any] | str | bytes,
    changeset: ChangeSet,
    discovery: DiscoveryResult,
    plan: VerificationPlan,
    evidence: VerificationEvidence,
    assessment: SemanticAssessment,
    *,
    verifier_version: str,
    verifier_commit_or_build: str,
) -> ReviewArtifact:
    payload = json.loads(value) if isinstance(value, (str, bytes)) else dict(value)
    if payload.get("schema_version") != "1.0.0":
        raise ValueError(f"unsupported ReviewArtifact schema version: {payload.get('schema_version')!r}")
    verifier = VerifierIdentity(
        version=verifier_version,
        commit_or_build=verifier_commit_or_build,
    )
    expected = _canonical_payload(
        changeset, discovery, plan, evidence, assessment, verifier
    )
    return ReviewArtifact.model_validate(
        payload,
        context={"review_artifact": expected},
    )


def verdict_exit_code(verdict: ReviewVerdict) -> int:
    return {
        ReviewVerdict.READY: 0,
        ReviewVerdict.NEEDS_CHANGES: 1,
        ReviewVerdict.INCONCLUSIVE: 2,
    }[verdict]


def _reference_label(kind: str, identifier: str) -> str:
    if len(identifier) > 96:
        identifier = "sha256:" + hashlib.sha256(identifier.encode("utf-8")).hexdigest()
    return f"{kind}:{identifier}"


def _check_label(check: VerificationCheckSummary) -> str:
    return check.check_id or f"sha256:{check.check_id_sha256}"


def _report_text(value: str) -> str:
    escaped: list[str] = []
    for character in value:
        if character == "\n":
            escaped.append(r"\n")
        elif character == "\r":
            escaped.append(r"\r")
        elif character == "\t":
            escaped.append(r"\t")
        elif ord(character) < 32 or ord(character) == 127:
            escaped.append(f"\\x{ord(character):02x}")
        elif character in r"\`*_[]<>|#":
            escaped.append("\\" + character)
        else:
            escaped.append(character)
    rendered = "".join(escaped)
    if len(rendered) <= 240:
        return rendered
    return "detail-sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def render_markdown_report(artifact: ReviewArtifact) -> str:
    """Render only canonical artifact fields; verdict reduction happens elsewhere."""

    lines = [
        "# PrePR Verify Report",
        "",
        f"Verdict: **{artifact.verdict.value}**",
        "",
        "## Axes",
        "",
    ]
    lines.extend(
        f"- {axis.axis.value}: **{axis.status.value.upper()}**"
        for axis in artifact.axes
    )
    lines.extend(["", "## Verification", ""])
    shown_checks = artifact.checks[:12]
    lines.extend(
        f"- `{_check_label(check)}` ({check.requirement_level.value}): {check.outcome.value}"
        + (f"/{check.failure_kind.value}" if check.failure_kind is not None else "")
        + (f" [{_reference_label('execution', str(check.execution_ordinal))}]" if check.execution_ordinal is not None else "")
        for check in shown_checks
    )
    omitted_checks = artifact.check_index.count - len(shown_checks)
    if omitted_checks:
        lines.append(
            f"- {omitted_checks} additional checks are referenced by "
            f"check-set `{artifact.check_index.identity}`."
        )
    blocking = [finding for finding in artifact.findings if finding.blocking]
    blocking_checks = [
        check
        for check in artifact.checks
        if check.requirement_level is RequirementLevel.REQUIRED
        and check.outcome is CheckOutcome.FAILED
        and check.failure_kind is FailureKind.VERIFICATION
    ]
    other = [finding for finding in artifact.findings if not finding.blocking]
    shown_blocking = blocking[:12]
    remaining_blocking_slots = 12 - len(shown_blocking)
    shown_blocking_checks = blocking_checks[:remaining_blocking_slots]
    remaining_finding_slots = max(
        0, 12 - len(shown_blocking) - len(shown_blocking_checks)
    )
    shown_other = other[:remaining_finding_slots]
    lines.extend(["", "## Blocking findings", ""])
    blocking_lines = [
            f"- `{finding.finding_id}` [{finding.axis.value}]: {_report_text(finding.title)} — "
            + ", ".join(
                _reference_label(reference.kind.value, reference.identifier)
                for reference in finding.evidence[:2]
            )
            + (
                f", +{len(finding.evidence) - 2} references"
                if len(finding.evidence) > 2
                else ""
            )
            for finding in shown_blocking
        ]
    blocking_lines.extend(
        f"- `verification:{_check_label(check)}` [test_sufficiency]: required verification failed — "
        f"{_reference_label('execution', str(check.execution_ordinal))}"
        for check in shown_blocking_checks
    )
    omitted_blocking_checks = (
        artifact.check_index.blocking_count - len(shown_blocking_checks)
    )
    if omitted_blocking_checks:
        blocking_lines.append(
            f"- {omitted_blocking_checks} additional required verification failures "
            f"are referenced by check-set `{artifact.check_index.identity}`."
        )
    lines.extend(blocking_lines or ["- None."])
    if len(blocking) > len(shown_blocking):
        lines.append(
            f"- {len(blocking) - len(shown_blocking)} additional blocking findings "
            f"are available in SemanticAssessment `{artifact.bindings.semantic_assessment_identity}`."
        )
    lines.extend(["", "## Non-blocking and unverified findings", ""])
    lines.extend(
        [
            f"- `{finding.finding_id}` ({finding.state.value}) [{finding.axis.value}]: {_report_text(finding.title)} — "
            + ", ".join(
                _reference_label(reference.kind.value, reference.identifier)
                for reference in finding.evidence[:2]
            )
            + (
                f", +{len(finding.evidence) - 2} references"
                if len(finding.evidence) > 2
                else ""
            )
            for finding in shown_other
        ]
        or ["- None."]
    )
    if len(other) > len(shown_other):
        lines.append(
            f"- {len(other) - len(shown_other)} additional non-blocking findings "
            f"are available in SemanticAssessment `{artifact.bindings.semantic_assessment_identity}`."
        )
    lines.extend(["", "## Required evidence gaps", ""])
    shown_gaps = artifact.evidence_gaps[:12]
    lines.extend(
        [
            f"- `{gap.gap_id}`: {_report_text(gap.summary)} — "
            f"{', '.join(_reference_label('gap', reference) for reference in gap.references[:2])}"
            for gap in shown_gaps
        ]
        or ["- None."]
    )
    omitted_gaps = artifact.evidence_gap_index.count - len(shown_gaps)
    if omitted_gaps:
        lines.append(
            f"- {omitted_gaps} additional required gaps are referenced by "
            f"gap-set `{artifact.evidence_gap_index.identity}`."
        )
    lines.extend(
        [
            "",
            "## Artifact references",
            "",
            f"- ReviewArtifact: `{artifact.identity}`",
            f"- ChangeSet: `{artifact.bindings.changeset_identity}`",
            f"- DiscoveryResult: `{artifact.bindings.discovery_identity}`",
            f"- VerificationPlan: `{artifact.bindings.plan_identity}`",
            f"- VerificationEvidence: `{artifact.bindings.evidence_identity}`",
            f"- SemanticAssessment: `{artifact.bindings.semantic_assessment_identity}`",
            "",
        ]
    )
    return "\n".join(lines)
