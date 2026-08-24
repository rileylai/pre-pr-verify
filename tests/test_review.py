from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from pre_pr_verify.discovery import discover_review_sources
from pre_pr_verify.executor import execute_request
from pre_pr_verify.git_capture import capture_changeset
from pre_pr_verify.models import ScopeMode
from pre_pr_verify.review import (
    build_review_artifact,
    load_review_artifact,
    render_markdown_report,
    verdict_exit_code,
)
from pre_pr_verify.review_models import AxisStatus, ReviewVerdict, hash_payload
from pre_pr_verify.semantic import bind_semantic_reference, build_semantic_assessment
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
    SemanticStatus,
)
from pre_pr_verify.verification import (
    PlannerCheckInput,
    build_execution_request,
    build_verification_plan,
)
from pre_pr_verify.verification_models import (
    CapabilityName,
    ExecutionCapability,
    FailureKind,
    RequirementLevel,
    SnapshotManifest,
    SourcePreservationFailure,
    build_verification_evidence,
)


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


def base_scope(
    tmp_path: Path,
    *,
    requirement_text: str = "The public result must remain an integer.",
    command: tuple[str, ...] | None = None,
    missing_capability: bool = False,
    nonzero_failure_kind: FailureKind = FailureKind.VERIFICATION,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    if requirement_text:
        (repo / "README.md").write_text(requirement_text + "\n")
    (repo / "AGENTS.md").write_text("Preserve the repository's public contracts.\n")
    (repo / "app.py").write_text("value = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    (repo / "app.py").write_text("value = 2\n")
    changeset = capture_changeset(repo, "main", ScopeMode.PENDING)
    discovery = discover_review_sources(repo)
    additions = (
        [
            PlannerCheckInput(
                check_id="targeted-check",
                requirement_level=RequirementLevel.REQUIRED,
                selection_reason="Fixture check.",
                argv=command,
            )
        ]
        if command
        else []
    )
    plan = build_verification_plan(
        changeset,
        discovery,
        canonical_checks=[],
        trusted_policy_checks=[],
        planner_additions=additions,
    )
    executions = []
    if command:
        manifest_payload = {
            "materialization_ordinal": 0,
            "changeset_identity": changeset.identity,
            "discovery_identity": discovery.identity,
            "files": [],
            "complete": True,
            "materialization_failure": None,
        }
        manifest = SnapshotManifest(
            **manifest_payload,
            identity=hash_payload(manifest_payload),
        )
        check = next(check for check in plan.checks if check.check_id == "targeted-check")
        required_capabilities = [CapabilityName.OUTPUT_LIMITS]
        if missing_capability:
            required_capabilities.append(CapabilityName.NETWORK_ISOLATION)
        request = build_execution_request(
            check,
            manifest,
            timeout_seconds=2,
            output_limit_bytes=1024,
            required_capabilities=required_capabilities,
            nonzero_failure_kind=nonzero_failure_kind,
        )
        capability = ExecutionCapability(
            structured_argv=True,
            repository_bound_cwd=True,
            git_protection=True,
            source_preservation=True,
            authority_separation=True,
            secret_stripping=True,
            verdict_invariants=True,
            available=[CapabilityName.OUTPUT_LIMITS],
            approval_waivable=[],
            approved_gaps=[],
        )
        result = execute_request(request, capability, tmp_path)
        executions.append((manifest, result))
    evidence = build_verification_evidence(plan, executions)
    return changeset, discovery, plan, evidence


def semantic_axes(
    *,
    statuses: dict[SemanticAxis, SemanticStatus] | None = None,
    gaps: set[SemanticAxis] | None = None,
) -> list[SemanticAxisAssessment]:
    statuses = statuses or {}
    gaps = gaps or set()
    return [
        SemanticAxisAssessment(
            axis=axis,
            status=statuses.get(axis, SemanticStatus.PASS),
            rationale=f"Completed {axis.value} assessment.",
            required_evidence_gap=axis in gaps,
        )
        for axis in SemanticAxis
    ]


def make_finding(scope, *, axis: SemanticAxis, blocking: bool, state=FindingState.CONFIRMED):
    changeset, discovery, plan, evidence = scope
    category = {
        SemanticAxis.SPEC: FindingCategory.SPEC_MISMATCH,
        SemanticAxis.STANDARDS: FindingCategory.STANDARD_VIOLATION,
        SemanticAxis.IMPACT: FindingCategory.IMPACT_REGRESSION,
        SemanticAxis.TEST_SUFFICIENCY: FindingCategory.TEST_GAP,
        SemanticAxis.CONTEXTUAL_SECURITY: FindingCategory.CONTEXTUAL_SECURITY,
    }[axis]
    if axis is SemanticAxis.SPEC and discovery.requirement_resolution.candidate_source_ids:
        kind = EvidenceReferenceKind.DISCOVERY_SOURCE
        identifier = discovery.requirement_resolution.candidate_source_ids[0]
    elif axis is SemanticAxis.STANDARDS:
        kind = EvidenceReferenceKind.DISCOVERY_SOURCE
        identifier = discovery.standards_source_ids[0]
    else:
        kind = EvidenceReferenceKind.CHANGE_PATH
        identifier = changeset.changes[0].effective.path.raw_b64
    reference = bind_semantic_reference(
        kind,
        identifier,
        "Canonical fixture evidence.",
        changeset_identity=changeset.identity,
        discovery_identity=discovery.identity,
        plan_identity=plan.identity,
        evidence_identity=evidence.identity,
    )
    return SemanticFinding(
        finding_id=f"{axis.value}-finding",
        axis=axis,
        category=category,
        state=state,
        severity=FindingSeverity.HIGH,
        blocking=blocking,
        title=f"{axis.value} finding",
        explanation="Canonical evidence supports the structured finding.",
        evidence=[reference],
    )


def assessment(scope, *, axes=None, findings=(), comparisons=()):
    changeset, discovery, plan, evidence = scope
    findings = list(findings)
    values = list(axes or semantic_axes())
    values = [
        axis.model_copy(
            update={
                "finding_ids": sorted(
                    finding.finding_id for finding in findings if finding.axis is axis.axis
                )
            }
        )
        for axis in values
    ]
    return build_semantic_assessment(
        changeset,
        discovery,
        plan,
        evidence,
        axes=values,
        findings=findings,
        requirement_comparisons=comparisons,
    )


def artifact(scope, semantic):
    return build_review_artifact(
        *scope,
        semantic,
        verifier_version="0.1.0",
        verifier_commit_or_build="fixture-build",
    )


def load_artifact(payload, scope, semantic):
    return load_review_artifact(
        payload,
        *scope,
        semantic,
        verifier_version="0.1.0",
        verifier_commit_or_build="fixture-build",
    )


def test_complete_five_axis_pass_is_ready(tmp_path: Path) -> None:
    scope = base_scope(tmp_path)
    reviewed = artifact(scope, assessment(scope))

    assert reviewed.verdict is ReviewVerdict.READY
    assert all(axis.status is AxisStatus.PASS for axis in reviewed.axes)
    assert verdict_exit_code(reviewed.verdict) == 0
    assert all(check.outcome.value == "satisfied" for check in reviewed.checks)


@pytest.mark.parametrize("axis", list(SemanticAxis))
def test_confirmed_blocker_on_any_axis_needs_changes(tmp_path: Path, axis: SemanticAxis) -> None:
    scope = base_scope(tmp_path)
    finding = make_finding(scope, axis=axis, blocking=True)
    axes = semantic_axes(statuses={axis: SemanticStatus.FAIL})
    reviewed = artifact(scope, assessment(scope, axes=axes, findings=[finding]))

    assert reviewed.verdict is ReviewVerdict.NEEDS_CHANGES
    assert next(item for item in reviewed.axes if item.axis is axis).status is AxisStatus.FAIL
    assert verdict_exit_code(reviewed.verdict) == 1


def test_required_evidence_gap_without_defect_is_inconclusive(tmp_path: Path) -> None:
    scope = base_scope(tmp_path)
    gap_finding = make_finding(
        scope, axis=SemanticAxis.TEST_SUFFICIENCY, blocking=False, state=FindingState.EVIDENCE_GAP
    )
    axes = semantic_axes(
        statuses={SemanticAxis.TEST_SUFFICIENCY: SemanticStatus.INCONCLUSIVE},
        gaps={SemanticAxis.TEST_SUFFICIENCY},
    )
    reviewed = artifact(scope, assessment(scope, axes=axes, findings=[gap_finding]))

    assert reviewed.verdict is ReviewVerdict.INCONCLUSIVE
    assert reviewed.evidence_gaps
    assert verdict_exit_code(reviewed.verdict) == 2


def test_contradictory_winning_requirements_are_not_ready(tmp_path: Path) -> None:
    from pre_pr_verify.discovery import ProvidedRequirement, discover_review_sources

    scope = base_scope(tmp_path)
    changeset, _, _, _ = scope
    repo = Path(changeset.repository_root)
    discovery = discover_review_sources(
        repo,
        explicit_specs=[
            ProvidedRequirement("a", "Return integers."),
            ProvidedRequirement("b", "Return strings."),
        ],
    )
    plan = build_verification_plan(
        changeset, discovery, canonical_checks=[], trusted_policy_checks=[], planner_additions=[]
    )
    evidence = build_verification_evidence(plan, [])
    scope = (changeset, discovery, plan, evidence)
    sources = sorted(discovery.requirement_resolution.candidate_source_ids)
    comparison = RequirementComparison(
        source_ids=sources,
        relation=RequirementRelation.CONTRADICTORY,
        rationale="The requirements define incompatible result types.",
    )
    refs = [
        bind_semantic_reference(
            EvidenceReferenceKind.DISCOVERY_SOURCE,
            source,
            "Contradictory winning source.",
            changeset_identity=changeset.identity,
            discovery_identity=discovery.identity,
            plan_identity=plan.identity,
            evidence_identity=evidence.identity,
        )
        for source in sources
    ]
    finding = SemanticFinding(
        finding_id="requirement-conflict",
        axis=SemanticAxis.SPEC,
        category=FindingCategory.SPEC_CONTRADICTION,
        state=FindingState.EVIDENCE_GAP,
        severity=FindingSeverity.HIGH,
        blocking=False,
        title="Winning requirements conflict",
        explanation="The conflict remains unresolved.",
        evidence=refs,
    )
    axes = semantic_axes(
        statuses={SemanticAxis.SPEC: SemanticStatus.INCONCLUSIVE},
        gaps={SemanticAxis.SPEC},
    )

    reviewed = artifact(
        scope,
        assessment(scope, axes=axes, findings=[finding], comparisons=[comparison]),
    )
    assert reviewed.verdict is ReviewVerdict.INCONCLUSIVE


def test_missing_requirements_are_not_ready(tmp_path: Path) -> None:
    scope = base_scope(tmp_path, requirement_text="")
    finding = make_finding(
        scope, axis=SemanticAxis.SPEC, blocking=False, state=FindingState.EVIDENCE_GAP
    )
    axes = semantic_axes(
        statuses={SemanticAxis.SPEC: SemanticStatus.INCONCLUSIVE}, gaps={SemanticAxis.SPEC}
    )
    reviewed = artifact(scope, assessment(scope, axes=axes, findings=[finding]))
    assert reviewed.verdict is ReviewVerdict.INCONCLUSIVE


def test_failed_required_verification_needs_changes(tmp_path: Path) -> None:
    scope = base_scope(tmp_path, command=(sys.executable, "-c", "raise SystemExit(7)"))
    reviewed = artifact(scope, assessment(scope))

    assert reviewed.verdict is ReviewVerdict.NEEDS_CHANGES
    assert reviewed.axes[3].status is AxisStatus.FAIL
    check = next(item for item in reviewed.checks if item.kind.value == "command")
    assert check.outcome.value == "failed"
    assert "verification:targeted-check" in render_markdown_report(reviewed)


def test_required_not_run_capability_gap_is_inconclusive(tmp_path: Path) -> None:
    scope = base_scope(
        tmp_path, command=(sys.executable, "-c", "pass"), missing_capability=True
    )
    reviewed = artifact(scope, assessment(scope))

    assert reviewed.verdict is ReviewVerdict.INCONCLUSIVE
    assert reviewed.axes[3].status is AxisStatus.INCONCLUSIVE
    check = next(item for item in reviewed.checks if item.kind.value == "command")
    assert check.outcome.value == "not_run"


def test_required_failed_unclassified_is_preserved_as_inconclusive_gap(
    tmp_path: Path,
) -> None:
    scope = base_scope(
        tmp_path,
        command=(sys.executable, "-c", "raise SystemExit(2)"),
        nonzero_failure_kind=FailureKind.UNCLASSIFIED,
    )
    reviewed = artifact(scope, assessment(scope))
    check = next(item for item in reviewed.checks if item.kind.value == "command")
    report = render_markdown_report(reviewed)

    assert reviewed.verdict is ReviewVerdict.INCONCLUSIVE
    assert check.outcome.value == "failed"
    assert check.failure_kind is FailureKind.UNCLASSIFIED
    assert check.required_evidence_gap is True
    assert "targeted-check` (required): failed/unclassified" in report
    assert "verification.0" in report


def test_source_preservation_failure_is_separate_and_invalidates_all_axes(tmp_path: Path) -> None:
    scope = base_scope(tmp_path, command=(sys.executable, "-c", "pass"))
    changeset, discovery, plan, evidence = scope
    execution = evidence.executions[0]
    preservation = SourcePreservationFailure(
        ordinal=0,
        check_id="targeted-check",
        snapshot_identity=execution.snapshot.identity,
        reason="Author source changed after execution.",
    )
    evidence = build_verification_evidence(
        plan,
        [(execution.snapshot, execution.result)],
        source_preservation_failures=[preservation],
    )
    scope = changeset, discovery, plan, evidence
    reviewed = artifact(scope, assessment(scope))

    assert reviewed.verdict is ReviewVerdict.INCONCLUSIVE
    assert all(axis.status is AxisStatus.INCONCLUSIVE for axis in reviewed.axes)
    check = next(item for item in reviewed.checks if item.kind.value == "command")
    assert check.outcome.value == "passed"
    assert any(gap.kind.value == "source_preservation" for gap in reviewed.evidence_gaps)


def test_unsupported_suspicion_alone_does_not_force_failure(tmp_path: Path) -> None:
    scope = base_scope(tmp_path)
    suspicion = SemanticFinding(
        **make_finding(
            scope,
            axis=SemanticAxis.CONTEXTUAL_SECURITY,
            blocking=False,
            state=FindingState.UNVERIFIED,
        ).model_dump(mode="python"),
    )
    payload = suspicion.model_dump(mode="python")
    payload["category"] = FindingCategory.UNSUPPORTED_SUSPICION
    suspicion = SemanticFinding(**payload)
    reviewed = artifact(scope, assessment(scope, findings=[suspicion]))

    assert reviewed.verdict is ReviewVerdict.READY
    assert reviewed.axes[-1].status is AxisStatus.PASS


def test_blocker_precedes_evidence_gap(tmp_path: Path) -> None:
    scope = base_scope(
        tmp_path, command=(sys.executable, "-c", "pass"), missing_capability=True
    )
    blocker = make_finding(scope, axis=SemanticAxis.IMPACT, blocking=True)
    axes = semantic_axes(statuses={SemanticAxis.IMPACT: SemanticStatus.FAIL})
    reviewed = artifact(scope, assessment(scope, axes=axes, findings=[blocker]))

    assert reviewed.verdict is ReviewVerdict.NEEDS_CHANGES
    assert reviewed.evidence_gaps
    assert "takes precedence" in reviewed.verdict_reasons[0]


def test_loader_rejects_forged_identity_and_mismatched_inputs(tmp_path: Path) -> None:
    scope = base_scope(tmp_path)
    semantic = assessment(scope)
    reviewed = artifact(scope, semantic)
    payload = reviewed.model_dump(mode="json")
    payload["bindings"]["changeset_identity"] = "0" * 64
    payload["identity"] = hash_payload({k: v for k, v in payload.items() if k != "identity"})

    with pytest.raises(ValidationError, match="contradicts canonical reduction"):
        load_artifact(payload, scope, semantic)

    other_scope = base_scope(
        tmp_path / "other", requirement_text="A materially different requirement."
    )
    with pytest.raises(ValueError, match="bound|identities|reference"):
        load_artifact(reviewed.model_dump_json(), other_scope, semantic)


def test_loader_rejects_orphan_and_duplicate_finding_ownership(tmp_path: Path) -> None:
    scope = base_scope(tmp_path)
    finding = make_finding(scope, axis=SemanticAxis.IMPACT, blocking=True)
    axes = semantic_axes(statuses={SemanticAxis.IMPACT: SemanticStatus.FAIL})
    semantic = assessment(scope, axes=axes, findings=[finding])
    reviewed = artifact(scope, semantic)

    for mutate in ("orphan", "duplicate"):
        payload = reviewed.model_dump(mode="json")
        impact = next(axis for axis in payload["axes"] if axis["axis"] == "impact")
        if mutate == "orphan":
            impact["finding_ids"] = []
        else:
            spec = next(axis for axis in payload["axes"] if axis["axis"] == "spec")
            spec["finding_ids"] = [finding.finding_id]
        payload["identity"] = hash_payload({k: v for k, v in payload.items() if k != "identity"})
        with pytest.raises(ValidationError, match="contradicts canonical reduction"):
            load_artifact(payload, scope, semantic)


def test_loader_rejects_forged_verifier_provenance(tmp_path: Path) -> None:
    scope = base_scope(tmp_path)
    semantic = assessment(scope)
    reviewed = artifact(scope, semantic)
    payload = reviewed.model_dump(mode="json")
    payload["verifier"] = {
        "version": "999.0",
        "commit_or_build": "forged-trusted-release",
    }
    payload["identity"] = hash_payload(
        {key: value for key, value in payload.items() if key != "identity"}
    )

    with pytest.raises(ValidationError, match="contradicts canonical reduction"):
        load_artifact(payload, scope, semantic)


def test_semantic_inconclusive_without_input_gap_is_reported_as_insufficient(
    tmp_path: Path,
) -> None:
    scope = base_scope(tmp_path)
    semantic = assessment(
        scope,
        axes=semantic_axes(
            statuses={SemanticAxis.IMPACT: SemanticStatus.INCONCLUSIVE}
        ),
    )
    reviewed = artifact(scope, semantic)

    impact = next(axis for axis in reviewed.axes if axis.axis is SemanticAxis.IMPACT)
    assert reviewed.verdict is ReviewVerdict.INCONCLUSIVE
    assert impact.required_evidence_gap is True
    assert impact.reducer_reasons == ["required evidence remains unresolved"]
    assert any(gap.gap_id == "semantic.impact" for gap in reviewed.evidence_gaps)
    assert "semantic.impact" in render_markdown_report(reviewed)


def test_renderer_is_faithful_concise_and_does_not_duplicate_output(tmp_path: Path) -> None:
    marker = "FULL-STDOUT-SHOULD-NOT-APPEAR"
    scope = base_scope(tmp_path, command=(sys.executable, "-c", f"print({marker!r})"))
    reviewed = artifact(scope, assessment(scope))
    report = render_markdown_report(reviewed)

    assert f"Verdict: **{reviewed.verdict.value}**" in report
    assert all(f"{axis.axis.value}: **{axis.status.value.upper()}**" in report for axis in reviewed.axes)
    assert marker not in report
    assert scope[3].executions[0].result.stdout.sha256 not in report
    assert reviewed.identity in report
    assert len(report) < 12_000


def test_default_report_remains_concise_at_maximum_findings(tmp_path: Path) -> None:
    scope = base_scope(tmp_path)
    template = make_finding(
        scope,
        axis=SemanticAxis.CONTEXTUAL_SECURITY,
        blocking=False,
        state=FindingState.UNVERIFIED,
    )
    findings = []
    for index in range(128):
        payload = template.model_dump(mode="python")
        payload.update(
            finding_id=f"suspicion-{index:03d}",
            category=FindingCategory.UNSUPPORTED_SUSPICION,
            title="x" * 256,
        )
        findings.append(SemanticFinding(**payload))
    reviewed = artifact(scope, assessment(scope, findings=findings))
    report = render_markdown_report(reviewed)

    assert reviewed.verdict is ReviewVerdict.READY
    assert "additional non-blocking findings" in report
    assert len(report) < 12_000


def test_renderer_escapes_untrusted_markdown_structure(tmp_path: Path) -> None:
    scope = base_scope(tmp_path)
    template = make_finding(
        scope,
        axis=SemanticAxis.CONTEXTUAL_SECURITY,
        blocking=False,
        state=FindingState.UNVERIFIED,
    )
    payload = template.model_dump(mode="python")
    payload.update(
        finding_id="report-injection",
        category=FindingCategory.UNSUPPORTED_SUSPICION,
        title="real\n\n## Fake section\n\nVerdict: **READY**",
    )
    semantic = assessment(scope, findings=[SemanticFinding(**payload)])
    report = render_markdown_report(artifact(scope, semantic))

    assert "\n## Fake section\n" not in report
    assert r"real\n\n\#\# Fake section" in report
    assert report.count("Verdict: **READY**") == 1


def test_artifact_fields_and_collections_are_bounded(tmp_path: Path) -> None:
    scope = base_scope(tmp_path)
    semantic = assessment(scope)
    with pytest.raises(ValidationError, match="at most 256"):
        build_review_artifact(
            *scope,
            semantic,
            verifier_version="v" * 257,
            verifier_commit_or_build="fixture",
        )

    reviewed = artifact(scope, semantic)
    assert len(json.dumps(reviewed.model_dump(mode="json"))) < 200_000


def test_long_valid_check_identifier_uses_bounded_stable_reference(tmp_path: Path) -> None:
    long_id = "check-" + "x" * 512
    scope = base_scope(tmp_path, command=(sys.executable, "-c", "pass"))
    changeset, discovery, plan, evidence = scope
    plan_payload = plan.model_dump(mode="json")
    command = next(check for check in plan_payload["checks"] if check["kind"] == "command")
    command["check_id"] = long_id
    plan_payload["identity"] = hash_payload(
        {key: value for key, value in plan_payload.items() if key != "identity"}
    )
    from pre_pr_verify.verification_models import VerificationPlan

    long_plan = VerificationPlan.model_validate(plan_payload)
    execution = evidence.executions[0]
    request = execution.result.request.model_copy(update={"check_id": long_id})
    result = execution.result.model_copy(update={"request": request})
    long_evidence = build_verification_evidence(
        long_plan, [(execution.snapshot, result)]
    )
    long_scope = changeset, discovery, long_plan, long_evidence
    reviewed = artifact(long_scope, assessment(long_scope))

    summary = next(check for check in reviewed.checks if check.kind.value == "command")
    assert summary.check_id is None
    assert len(summary.check_id_sha256) == 64
    assert long_id not in render_markdown_report(reviewed)


def test_valid_unbounded_1_4_collections_reduce_through_bounded_indexes(
    tmp_path: Path,
) -> None:
    changeset, discovery, _, _ = base_scope(tmp_path)
    additions = [
        PlannerCheckInput(
            check_id=f"bulk-{index:03d}",
            requirement_level=RequirementLevel.REQUIRED,
            selection_reason="Bounded artifact overflow fixture.",
            argv=(sys.executable, "-c", "pass"),
        )
        for index in range(257)
    ]
    plan = build_verification_plan(
        changeset,
        discovery,
        canonical_checks=[],
        trusted_policy_checks=[],
        planner_additions=additions,
    )
    capability = ExecutionCapability(
        structured_argv=True,
        repository_bound_cwd=True,
        git_protection=True,
        source_preservation=True,
        authority_separation=True,
        secret_stripping=True,
        verdict_invariants=True,
        available=[],
        approval_waivable=[],
        approved_gaps=[],
    )
    executions = []
    command_checks = [check for check in plan.checks if check.kind.value == "command"]
    for ordinal, check in enumerate(command_checks):
        manifest_payload = {
            "materialization_ordinal": ordinal,
            "changeset_identity": changeset.identity,
            "discovery_identity": discovery.identity,
            "files": [],
            "complete": True,
            "materialization_failure": None,
        }
        manifest = SnapshotManifest(
            **manifest_payload,
            identity=hash_payload(manifest_payload),
        )
        request = build_execution_request(
            check,
            manifest,
            timeout_seconds=2,
            output_limit_bytes=1024,
            required_capabilities=[CapabilityName.NETWORK_ISOLATION],
        )
        executions.append((manifest, execute_request(request, capability, tmp_path)))
    evidence = build_verification_evidence(plan, executions)
    scope = changeset, discovery, plan, evidence
    reviewed = artifact(scope, assessment(scope))
    report = render_markdown_report(reviewed)

    assert reviewed.verdict is ReviewVerdict.INCONCLUSIVE
    assert reviewed.check_index.count == 260
    assert reviewed.check_index.retained_count == 256
    assert reviewed.check_index.omitted_count == 4
    assert reviewed.evidence_gap_index.count == 257
    assert reviewed.evidence_gap_index.retained_count == 256
    assert reviewed.evidence_gap_index.omitted_count == 1
    assert "additional checks" in report
    assert "additional required gaps" in report
    assert len(report) < 12_000
