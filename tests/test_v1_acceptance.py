from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from pre_pr_verify.discovery import ProvidedRequirement, discover_review_sources
from pre_pr_verify.executor import execute_verification_plan
from pre_pr_verify.git_capture import capture_changeset
from pre_pr_verify.models import ScopeMode
from pre_pr_verify.review import (
    build_review_artifact,
    load_review_artifact,
    render_markdown_report,
    verdict_exit_code,
)
from pre_pr_verify.review_models import (
    AxisStatus,
    ReviewArtifact,
    ReviewVerdict,
    hash_payload,
)
from pre_pr_verify.semantic import (
    SemanticLimitExceeded,
    bind_semantic_reference,
    build_semantic_assessment,
    canonical_winning_requirement_set,
)
from pre_pr_verify.semantic_models import (
    EvidenceReferenceKind,
    FindingCategory,
    FindingSeverity,
    FindingState,
    RequirementComparison,
    RequirementRelation,
    SemanticAxis,
    SemanticAxisAssessment,
    SemanticFinding,
    SemanticLimitConcern,
    SemanticReferenceSet,
    SemanticStatus,
    MAX_COMPARISONS,
)
from pre_pr_verify.verification import build_verification_plan, discover_canonical_checks
from pre_pr_verify.verification_models import (
    CapabilityName,
    ExecutionCapability,
    FailureKind,
    SourcePreservationFailure,
    build_verification_evidence,
)


VERIFIER_VERSION = "1.0.0-acceptance"
VERIFIER_BUILD = "acceptance-fixture"
OUTPUT_MARKER = "FULL-VERIFICATION-OUTPUT-MUST-STAY-REFERENCED"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def repository(
    tmp_path: Path,
    *,
    requirement: bool = True,
    command_exit: int = 0,
) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Acceptance")
    git(repo, "config", "user.email", "acceptance@example.invalid")
    if requirement:
        (repo / "README.md").write_text(
            "The public value must remain a positive integer.\n"
        )
    (repo / "AGENTS.md").write_text(
        "Preserve public contracts and use repository-native verification.\n"
    )
    (repo / "pyproject.toml").write_text(
        "[tool.pre-pr-verify.verification]\n"
        "checks = [\n"
        "  { id = \"acceptance-check\", level = \"required\", "
        f"argv = [\"python\", \"-c\", \"print('{OUTPUT_MARKER}'); raise SystemExit({command_exit})\"] }}\n"
        "]\n"
    )
    (repo / "app.py").write_text("value = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    (repo / "app.py").write_text("value = 2\n")
    return repo


def capability(*, output_only: bool = True) -> ExecutionCapability:
    available = [CapabilityName.OUTPUT_LIMITS] if output_only else []
    return ExecutionCapability(
        structured_argv=True,
        repository_bound_cwd=True,
        git_protection=True,
        source_preservation=True,
        authority_separation=True,
        secret_stripping=True,
        verdict_invariants=True,
        available=available,
        approval_waivable=[],
        approved_gaps=[],
    )


def axes(
    *,
    statuses: dict[SemanticAxis, SemanticStatus] | None = None,
    gaps: set[SemanticAxis] | None = None,
    findings: list[SemanticFinding] | None = None,
) -> list[SemanticAxisAssessment]:
    statuses = statuses or {}
    gaps = gaps or set()
    findings = findings or []
    return [
        SemanticAxisAssessment(
            axis=axis,
            status=statuses.get(axis, SemanticStatus.PASS),
            rationale=f"Acceptance assessment completed for {axis.value}.",
            finding_ids=sorted(
                finding.finding_id for finding in findings if finding.axis is axis
            ),
            required_evidence_gap=axis in gaps,
        )
        for axis in SemanticAxis
    ]


def reference(scope, kind: EvidenceReferenceKind, identifier: str):
    changeset, discovery, plan, evidence = scope
    return bind_semantic_reference(
        kind,
        identifier,
        "Canonical acceptance evidence.",
        changeset_identity=changeset.identity,
        discovery_identity=discovery.identity,
        plan_identity=plan.identity,
        evidence_identity=evidence.identity,
    )


def finding(
    scope,
    *,
    finding_id: str,
    axis: SemanticAxis,
    category: FindingCategory,
    state: FindingState,
    blocking: bool,
    references,
) -> SemanticFinding:
    return SemanticFinding(
        finding_id=finding_id,
        axis=axis,
        category=category,
        state=state,
        severity=FindingSeverity.HIGH,
        blocking=blocking,
        title=finding_id.replace("-", " "),
        explanation="Acceptance evidence supports this structured finding state.",
        evidence=list(references),
    )


def deterministic_scope(
    repo: Path,
    *,
    explicit_specs: list[ProvidedRequirement] | None = None,
    required_capabilities: list[CapabilityName] | None = None,
):
    changeset = capture_changeset(repo, "main", ScopeMode.PENDING)
    discovery = discover_review_sources(repo, explicit_specs=explicit_specs or [])
    plan = build_verification_plan(
        changeset,
        discovery,
        canonical_checks=discover_canonical_checks(repo),
        trusted_policy_checks=[],
        planner_additions=[],
    )
    evidence = execute_verification_plan(
        changeset,
        discovery,
        plan,
        capability(),
        timeout_seconds=5,
        output_limit_bytes=256,
        required_capabilities=(
            required_capabilities
            if required_capabilities is not None
            else [CapabilityName.OUTPUT_LIMITS]
        ),
    )
    return changeset, discovery, plan, evidence


def complete_review(
    scope,
    *,
    axis_values: list[SemanticAxisAssessment] | None = None,
    findings: list[SemanticFinding] | None = None,
    comparisons: list[RequirementComparison] | None = None,
    reviewed_requirement_sources: SemanticReferenceSet | None = None,
) -> tuple[ReviewArtifact, str]:
    changeset, discovery, plan, evidence = scope
    findings = findings or []
    assessment = build_semantic_assessment(
        changeset,
        discovery,
        plan,
        evidence,
        axes=axis_values or axes(findings=findings),
        reviewed_requirement_sources=(
            reviewed_requirement_sources
            or canonical_winning_requirement_set(discovery)
        ),
        findings=findings,
        requirement_comparisons=comparisons or [],
    )
    artifact = build_review_artifact(
        changeset,
        discovery,
        plan,
        evidence,
        assessment,
        verifier_version=VERIFIER_VERSION,
        verifier_commit_or_build=VERIFIER_BUILD,
    )
    loaded = load_review_artifact(
        artifact.model_dump_json(),
        changeset,
        discovery,
        plan,
        evidence,
        assessment,
        verifier_version=VERIFIER_VERSION,
        verifier_commit_or_build=VERIFIER_BUILD,
    )
    assert loaded == artifact
    assert artifact.bindings.changeset_identity == changeset.identity
    assert artifact.bindings.discovery_identity == discovery.identity
    assert artifact.bindings.plan_identity == plan.identity
    assert artifact.bindings.evidence_identity == evidence.identity
    assert artifact.bindings.semantic_assessment_identity == assessment.identity
    return artifact, render_markdown_report(
        loaded,
        changeset=changeset,
        discovery=discovery,
        plan=plan,
        evidence=evidence,
    )


def test_clean_valid_change_runs_complete_flow_to_ready(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    scope = deterministic_scope(repo)
    initial_identity = scope[0].identity
    artifact, report = complete_review(scope)

    assert artifact.verdict is ReviewVerdict.READY
    assert verdict_exit_code(artifact.verdict) == 0
    assert "Verdict: **READY**" in report
    assert OUTPUT_MARKER not in report
    assert OUTPUT_MARKER not in artifact.model_dump_json()
    assert len(scope[3].executions[0].result.stdout.excerpt.encode()) <= 256
    assert capture_changeset(repo, "main", ScopeMode.PENDING).identity == initial_identity


def test_confirmed_implementation_defect_needs_changes(tmp_path: Path) -> None:
    scope = deterministic_scope(repository(tmp_path))
    path = scope[0].changes[0].effective.path.raw_b64
    defect = finding(
        scope,
        finding_id="confirmed-impact-regression",
        axis=SemanticAxis.IMPACT,
        category=FindingCategory.IMPACT_REGRESSION,
        state=FindingState.CONFIRMED,
        blocking=True,
        references=[reference(scope, EvidenceReferenceKind.CHANGE_PATH, path)],
    )
    artifact, report = complete_review(
        scope,
        findings=[defect],
        axis_values=axes(
            statuses={SemanticAxis.IMPACT: SemanticStatus.FAIL}, findings=[defect]
        ),
    )

    assert artifact.verdict is ReviewVerdict.NEEDS_CHANGES
    assert verdict_exit_code(artifact.verdict) == 1
    assert "confirmed-impact-regression" in report


def test_unavailable_required_capability_is_inconclusive(tmp_path: Path) -> None:
    scope = deterministic_scope(
        repository(tmp_path),
        required_capabilities=[
            CapabilityName.OUTPUT_LIMITS,
            CapabilityName.NETWORK_ISOLATION,
        ],
    )
    artifact, report = complete_review(scope)

    assert artifact.verdict is ReviewVerdict.INCONCLUSIVE
    assert verdict_exit_code(artifact.verdict) == 2
    assert scope[3].executions[0].result.status.value == "not_run"
    assert "not_run/capability" in report


def test_empty_scope_is_nothing_to_review_without_verdict(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    git(repo, "checkout", "--", "app.py")
    changeset = capture_changeset(repo, "main", ScopeMode.PENDING)
    discovery = discover_review_sources(repo)
    plan = build_verification_plan(
        changeset,
        discovery,
        canonical_checks=[],
        trusted_policy_checks=[],
        planner_additions=[],
    )
    evidence = build_verification_evidence(plan, [])

    assert changeset.empty is True
    with pytest.raises(ValueError, match="nothing_to_review"):
        build_semantic_assessment(
            changeset,
            discovery,
            plan,
            evidence,
            axes=axes(),
            reviewed_requirement_sources=canonical_winning_requirement_set(discovery),
        )


def test_required_generic_nonzero_is_inconclusive(tmp_path: Path) -> None:
    scope = deterministic_scope(repository(tmp_path, command_exit=7))
    artifact, report = complete_review(scope)

    result = scope[3].executions[0].result
    assert artifact.verdict is ReviewVerdict.INCONCLUSIVE
    assert next(
        axis for axis in artifact.axes if axis.axis is SemanticAxis.TEST_SUFFICIENCY
    ).status is AxisStatus.INCONCLUSIVE
    assert result.status.value == "failed"
    assert result.failure_kind is FailureKind.UNCLASSIFIED
    assert result.required_evidence_gap is True
    assert "failed/unclassified" in report


def test_semantic_contradiction_is_inconclusive(tmp_path: Path) -> None:
    specs = [
        ProvidedRequirement("integer", "The result must be an integer."),
        ProvidedRequirement("string", "The result must be a string."),
    ]
    scope = deterministic_scope(repository(tmp_path), explicit_specs=specs)
    source_ids = sorted(scope[1].requirement_resolution.candidate_source_ids)
    conflict = finding(
        scope,
        finding_id="winning-requirements-conflict",
        axis=SemanticAxis.SPEC,
        category=FindingCategory.SPEC_CONTRADICTION,
        state=FindingState.EVIDENCE_GAP,
        blocking=False,
        references=[
            reference(scope, EvidenceReferenceKind.DISCOVERY_SOURCE, source_id)
            for source_id in source_ids
        ],
    )
    comparison = RequirementComparison(
        source_ids=source_ids,
        relation=RequirementRelation.CONTRADICTORY,
        rationale="The winning requirements define incompatible result types.",
    )
    artifact, _ = complete_review(
        scope,
        findings=[conflict],
        comparisons=[comparison],
        axis_values=axes(
            statuses={SemanticAxis.SPEC: SemanticStatus.INCONCLUSIVE},
            gaps={SemanticAxis.SPEC},
            findings=[conflict],
        ),
    )
    assert artifact.verdict is ReviewVerdict.INCONCLUSIVE


def test_nonempty_thirty_four_requirement_set_can_reach_ready(
    tmp_path: Path,
) -> None:
    specs = [
        ProvidedRequirement(f"requirement-{index}", f"Criterion {index} must hold.")
        for index in range(34)
    ]
    scope = deterministic_scope(repository(tmp_path), explicit_specs=specs)
    source_ids = sorted(scope[1].requirement_resolution.candidate_source_ids)
    assert scope[0].empty is False
    assert len(source_ids) == 34

    artifact, _ = complete_review(scope)

    assert artifact.verdict is ReviewVerdict.READY
    assert verdict_exit_code(artifact.verdict) == 0


def test_incomplete_thirty_four_requirement_set_is_inconclusive(
    tmp_path: Path,
) -> None:
    specs = [
        ProvidedRequirement(f"requirement-{index}", f"Criterion {index} must hold.")
        for index in range(34)
    ]
    scope = deterministic_scope(repository(tmp_path), explicit_specs=specs)
    expected = canonical_winning_requirement_set(scope[1])
    incomplete = expected.model_copy(update={"count": expected.count - 1})
    gap_axes = axes(
        statuses={SemanticAxis.SPEC: SemanticStatus.INCONCLUSIVE},
        gaps={SemanticAxis.SPEC},
    )

    artifact, _ = complete_review(
        scope,
        axis_values=gap_axes,
        reviewed_requirement_sources=incomplete,
    )

    assert artifact.verdict is ReviewVerdict.INCONCLUSIVE
    assert verdict_exit_code(artifact.verdict) == 2


def test_thirty_four_requirement_set_preserves_concrete_contradiction_evidence(
    tmp_path: Path,
) -> None:
    specs = [
        ProvidedRequirement(f"requirement-{index}", f"Criterion {index} must hold.")
        for index in range(34)
    ]
    scope = deterministic_scope(repository(tmp_path), explicit_specs=specs)
    source_ids = sorted(scope[1].requirement_resolution.candidate_source_ids)
    conflict_ids = sorted((source_ids[6], source_ids[18]))
    conflict = finding(
        scope,
        finding_id="winning-requirements-conflict-34",
        axis=SemanticAxis.SPEC,
        category=FindingCategory.SPEC_CONTRADICTION,
        state=FindingState.EVIDENCE_GAP,
        blocking=False,
        references=[
            reference(scope, EvidenceReferenceKind.DISCOVERY_SOURCE, source_id)
            for source_id in conflict_ids
        ],
    )
    comparison = RequirementComparison(
        source_ids=conflict_ids,
        relation=RequirementRelation.CONTRADICTORY,
        rationale="These two winning requirements are materially contradictory.",
    )

    artifact, _ = complete_review(
        scope,
        findings=[conflict],
        comparisons=[comparison],
        axis_values=axes(
            statuses={SemanticAxis.SPEC: SemanticStatus.INCONCLUSIVE},
            gaps={SemanticAxis.SPEC},
            findings=[conflict],
        ),
    )

    assert artifact.verdict is ReviewVerdict.INCONCLUSIVE
    assert verdict_exit_code(artifact.verdict) == 2


def test_missing_requirements_are_inconclusive(tmp_path: Path) -> None:
    scope = deterministic_scope(repository(tmp_path, requirement=False))
    path = scope[0].changes[0].effective.path.raw_b64
    gap = finding(
        scope,
        finding_id="requirements-unavailable",
        axis=SemanticAxis.SPEC,
        category=FindingCategory.SPEC_PARTIAL,
        state=FindingState.EVIDENCE_GAP,
        blocking=False,
        references=[reference(scope, EvidenceReferenceKind.CHANGE_PATH, path)],
    )
    artifact, _ = complete_review(
        scope,
        findings=[gap],
        axis_values=axes(
            statuses={SemanticAxis.SPEC: SemanticStatus.INCONCLUSIVE},
            gaps={SemanticAxis.SPEC},
            findings=[gap],
        ),
    )
    assert artifact.verdict is ReviewVerdict.INCONCLUSIVE


def test_source_preservation_failure_remains_separate(tmp_path: Path) -> None:
    changeset, discovery, plan, evidence = deterministic_scope(repository(tmp_path))
    execution = evidence.executions[0]
    preservation = SourcePreservationFailure(
        ordinal=execution.ordinal,
        check_id=execution.result.request.check_id,
        snapshot_identity=execution.snapshot.identity,
        reason="Acceptance fixture detected post-execution source mutation.",
    )
    evidence = build_verification_evidence(
        plan,
        [(execution.snapshot, execution.result)],
        source_preservation_failures=[preservation],
    )
    artifact, report = complete_review((changeset, discovery, plan, evidence))

    assert artifact.verdict is ReviewVerdict.INCONCLUSIVE
    assert execution.result.status.value == "passed"
    assert (
        f"source preservation: {execution.result.request.check_id} — "
        "source preservation failure"
    ) in report
    assert "source-preservation.0" not in report


def test_unsupported_suspicion_alone_stays_ready(tmp_path: Path) -> None:
    scope = deterministic_scope(repository(tmp_path))
    path = scope[0].changes[0].effective.path.raw_b64
    suspicion = finding(
        scope,
        finding_id="unsupported-security-suspicion",
        axis=SemanticAxis.CONTEXTUAL_SECURITY,
        category=FindingCategory.UNSUPPORTED_SUSPICION,
        state=FindingState.UNVERIFIED,
        blocking=False,
        references=[reference(scope, EvidenceReferenceKind.CHANGE_PATH, path)],
    )
    artifact, _ = complete_review(scope, findings=[suspicion])
    assert artifact.verdict is ReviewVerdict.READY


def test_forged_persisted_review_artifact_fails_closed(tmp_path: Path) -> None:
    scope = deterministic_scope(repository(tmp_path))
    changeset, discovery, plan, evidence = scope
    assessment = build_semantic_assessment(
        changeset,
        discovery,
        plan,
        evidence,
        axes=axes(),
        reviewed_requirement_sources=canonical_winning_requirement_set(discovery),
    )
    artifact = build_review_artifact(
        changeset,
        discovery,
        plan,
        evidence,
        assessment,
        verifier_version=VERIFIER_VERSION,
        verifier_commit_or_build=VERIFIER_BUILD,
    )
    payload = artifact.model_dump(mode="json")
    payload["verdict"] = "READY" if artifact.verdict.value != "READY" else "NEEDS_CHANGES"
    payload["identity"] = hash_payload(
        {key: value for key, value in payload.items() if key != "identity"}
    )

    with pytest.raises(ValidationError, match="contradicts canonical reduction"):
        load_review_artifact(
            json.dumps(payload),
            changeset,
            discovery,
            plan,
            evidence,
            assessment,
            verifier_version=VERIFIER_VERSION,
            verifier_commit_or_build=VERIFIER_BUILD,
        )
