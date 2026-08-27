from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from pre_pr_verify.discovery import ProvidedRequirement, discover_review_sources
from pre_pr_verify.executor import execute_request
from pre_pr_verify.git_capture import capture_changeset
from pre_pr_verify.models import ScopeMode
from pre_pr_verify.review import (
    _REPORT_RATIONALE_LIMIT,
    _report_text,
    build_review_artifact,
    load_review_artifact,
    render_markdown_report,
    verdict_exit_code,
)
from pre_pr_verify.review_models import AxisStatus, ReviewVerdict, hash_payload
from pre_pr_verify.semantic import (
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
    SemanticLimitGap,
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
    VerificationPlan,
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
            "environment_profile": "FILESYSTEM_ONLY",
            "object_format": None,
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
    rationales: dict[SemanticAxis, str] | None = None,
) -> list[SemanticAxisAssessment]:
    statuses = statuses or {}
    gaps = gaps or set()
    rationales = rationales or {}
    return [
        SemanticAxisAssessment(
            axis=axis,
            status=statuses.get(axis, SemanticStatus.PASS),
            rationale=rationales.get(axis, f"Completed {axis.value} assessment."),
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


def assessment(scope, *, axes=None, findings=(), comparisons=(), limit_gaps=()):
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
        reviewed_requirement_sources=canonical_winning_requirement_set(discovery),
        findings=findings,
        requirement_comparisons=comparisons,
        limit_gaps=limit_gaps,
    )


def artifact(scope, semantic):
    return build_review_artifact(
        *scope,
        semantic,
        verifier_version="0.1.0",
        verifier_commit_or_build="fixture-build",
    )


def bound_report(scope, reviewed):
    changeset, discovery, plan, evidence = scope
    return render_markdown_report(
        reviewed,
        changeset=changeset,
        discovery=discovery,
        plan=plan,
        evidence=evidence,
    )


def scope_with_check_id(scope, check_id: str):
    changeset, discovery, plan, evidence = scope
    plan_payload = plan.model_dump(mode="json")
    command = next(check for check in plan_payload["checks"] if check["kind"] == "command")
    command["check_id"] = check_id
    plan_payload["identity"] = hash_payload(
        {key: value for key, value in plan_payload.items() if key != "identity"}
    )
    updated_plan = VerificationPlan.model_validate(plan_payload)
    if evidence.executions:
        execution = evidence.executions[0]
        request = execution.result.request.model_copy(update={"check_id": check_id})
        result = execution.result.model_copy(update={"request": request})
        updated_evidence = build_verification_evidence(
            updated_plan, [(execution.snapshot, result)]
        )
    else:
        updated_evidence = build_verification_evidence(updated_plan, [])
    return changeset, discovery, updated_plan, updated_evidence


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


def test_passed_checks_do_not_hide_semantic_blocker_or_its_rationale(tmp_path: Path) -> None:
    scope = base_scope(tmp_path, command=(sys.executable, "-c", "pass"))
    finding = make_finding(scope, axis=SemanticAxis.IMPACT, blocking=True)
    finding = finding.model_copy(
        update={
            "title": "Empty fallback returns an invalid value",
            "explanation": "The changed fallback returns zero even though the public contract requires a positive value.",
        }
    )
    rationale = (
        "Reviewed the changed fallback and its caller contract; empty input returns "
        "zero, so the implementation violates the positive-value invariant."
    )
    reviewed = artifact(
        scope,
        assessment(
            scope,
            axes=semantic_axes(
                statuses={SemanticAxis.IMPACT: SemanticStatus.FAIL},
                rationales={SemanticAxis.IMPACT: rationale},
            ),
            findings=[finding],
        ),
    )
    report = bound_report(scope, reviewed)

    assert reviewed.verdict is ReviewVerdict.NEEDS_CHANGES
    assert "targeted-check` (required): passed" in report
    assert "## Semantic Review" in report
    assert rationale in report
    assert "Empty fallback returns an invalid value" in report
    assert "positive value" in report


def test_passed_checks_do_not_erase_concrete_missing_test_gap(tmp_path: Path) -> None:
    scope = base_scope(tmp_path, command=(sys.executable, "-c", "pass"))
    finding = make_finding(
        scope,
        axis=SemanticAxis.TEST_SUFFICIENCY,
        blocking=False,
        state=FindingState.EVIDENCE_GAP,
    ).model_copy(
        update={
            "title": "Fallback error path lacks regression coverage",
            "explanation": "The new fallback behavior has no test for malformed input.",
        }
    )
    reviewed = artifact(
        scope,
        assessment(
            scope,
            axes=semantic_axes(
                statuses={SemanticAxis.TEST_SUFFICIENCY: SemanticStatus.INCONCLUSIVE},
                gaps={SemanticAxis.TEST_SUFFICIENCY},
                rationales={
                    SemanticAxis.TEST_SUFFICIENCY: (
                        "Reviewed the new fallback and existing tests; the malformed-input "
                        "branch is not exercised by the current suite."
                    )
                },
            ),
            findings=[finding],
        ),
    )
    report = render_markdown_report(reviewed)

    assert reviewed.verdict is ReviewVerdict.INCONCLUSIVE
    assert next(axis for axis in reviewed.axes if axis.axis is SemanticAxis.TEST_SUFFICIENCY).status is AxisStatus.INCONCLUSIVE
    assert "targeted-check` (required): passed" in report
    assert "Fallback error path lacks regression coverage" in report
    assert "malformed-input branch" in report


def test_clean_review_reports_rationale_for_every_axis(tmp_path: Path) -> None:
    scope = base_scope(tmp_path, command=(sys.executable, "-c", "pass"))
    rationales = {
        axis: f"Reviewed the changed paths, relevant callers, boundaries, and repository evidence for {axis.value.replace('_', ' ')}."
        for axis in SemanticAxis
    }
    reviewed = artifact(scope, assessment(scope, axes=semantic_axes(rationales=rationales)))
    report = bound_report(scope, reviewed)

    assert "## Semantic Review" in report
    for rationale in rationales.values():
        assert rationale in report


def test_rationale_is_bound_to_semantic_assessment_and_legacy_artifact_loads(
    tmp_path: Path,
) -> None:
    scope = base_scope(tmp_path)
    semantic = assessment(
        scope,
        axes=semantic_axes(
            rationales={SemanticAxis.SPEC: "Reviewed the public result contract and changed implementation."}
        ),
    )
    reviewed = artifact(scope, semantic)
    payload = reviewed.model_dump(mode="json")
    payload["semantic_summaries"][0]["rationale"] = "Forged conclusion."
    payload["identity"] = hash_payload({key: value for key, value in payload.items() if key != "identity"})

    with pytest.raises(ValidationError, match="contradicts canonical reduction"):
        load_artifact(payload, scope, semantic)

    legacy_payload = reviewed.model_dump(mode="json")
    legacy_payload.pop("semantic_summaries")
    legacy_payload["schema_version"] = "1.0.0"
    legacy_payload["identity"] = hash_payload(
        {key: value for key, value in legacy_payload.items() if key != "identity"}
    )
    legacy = load_artifact(legacy_payload, scope, semantic)

    assert legacy.schema_version == "1.0.0"
    assert "semantic_summaries" not in legacy.model_dump(mode="json")


def test_semantic_rationale_is_bounded_and_markdown_safe(tmp_path: Path) -> None:
    scope = base_scope(tmp_path)
    rationale = "unsafe\n\n## Fake heading\n\nVerdict: **READY**" + ("x" * 2_000)
    reviewed = artifact(
        scope,
        assessment(scope, axes=semantic_axes(rationales={SemanticAxis.SPEC: rationale})),
    )
    report = render_markdown_report(reviewed)

    assert "\n## Fake heading\n" not in report
    assert "detail-sha256:" not in report
    assert r"unsafe\n\n\#\# Fake heading" in report
    assert report.count("Verdict: **READY**") == 1
    reason = next(
        line.removeprefix("- Reason: ")
        for line in report.splitlines()
        if line.startswith("- Reason: unsafe")
    )
    assert len(reason) <= _REPORT_RATIONALE_LIMIT
    assert reason.endswith("...")


def test_semantic_rationale_preserves_short_and_mid_length_text(tmp_path: Path) -> None:
    scope = base_scope(tmp_path)
    short = "Reviewed the changed implementation and its caller contract."
    mid_length = "Reviewed changed paths and callers for compatibility evidence " * 5
    assert len(mid_length) > 240
    assert len(mid_length) < _REPORT_RATIONALE_LIMIT
    reviewed = artifact(
        scope,
        assessment(
            scope,
            axes=semantic_axes(
                rationales={SemanticAxis.SPEC: short, SemanticAxis.STANDARDS: mid_length}
            ),
        ),
    )

    report = render_markdown_report(reviewed)

    assert f"- Reason: {short}" in report
    assert f"- Reason: {mid_length}" in report
    assert f"- Reason: {mid_length}..." not in report


def test_semantic_rationale_truncates_at_the_nearest_sentence_boundary(
    tmp_path: Path,
) -> None:
    scope = base_scope(tmp_path)
    first = "The implementation preserves the binding and legacy loader behavior."
    second = "The focused regression tests cover the exact boundary and reducer handoff."
    rationale = f"sentence-aware: {first} {second} " + ("additional context " * 80)
    reviewed = artifact(
        scope,
        assessment(scope, axes=semantic_axes(rationales={SemanticAxis.SPEC: rationale})),
    )

    report = render_markdown_report(reviewed)

    reason = next(
        line.removeprefix("- Reason: ")
        for line in report.splitlines()
        if line.startswith("- Reason: sentence-aware:")
    )
    assert reason == f"sentence-aware: {first} {second}..."


def test_semantic_rationale_uses_a_word_boundary_when_no_sentence_fits(
    tmp_path: Path,
) -> None:
    scope = base_scope(tmp_path)
    rationale = "word-fallback: " + ("reviewed compatibility evidence " * 60)
    reviewed = artifact(
        scope,
        assessment(scope, axes=semantic_axes(rationales={SemanticAxis.SPEC: rationale})),
    )

    report = render_markdown_report(reviewed)

    reason = next(
        line.removeprefix("- Reason: ")
        for line in report.splitlines()
        if line.startswith("- Reason: word-fallback:")
    )
    prefix = reason.removesuffix("...")
    assert reason.endswith("...")
    assert len(reason) <= _REPORT_RATIONALE_LIMIT
    assert rationale.startswith(prefix)
    assert rationale[len(prefix)].isspace()


def test_generic_report_text_keeps_its_existing_bound() -> None:
    value = "generic-field-" + ("x" * 500)

    rendered = _report_text(value)

    assert rendered == value[:237] + "..."
    assert len(rendered) == 240


def test_human_report_resolves_bound_references_without_opaque_identities(
    tmp_path: Path,
) -> None:
    scope = base_scope(tmp_path, command=(sys.executable, "-c", "pass"))
    changeset, discovery, plan, evidence = scope
    source = next(source for source in discovery.sources if source.path is not None)
    changed_path = changeset.changes[0].effective.path
    command = next(check for check in plan.checks if check.kind.value == "command")
    execution = evidence.executions[0]
    references = [
        bind_semantic_reference(
            kind,
            identifier,
            "Bound fixture evidence.",
            changeset_identity=changeset.identity,
            discovery_identity=discovery.identity,
            plan_identity=plan.identity,
            evidence_identity=evidence.identity,
        )
        for kind, identifier in (
            (EvidenceReferenceKind.CHANGE_PATH, changed_path.raw_b64),
            (EvidenceReferenceKind.DISCOVERY_SOURCE, source.source_id),
            (EvidenceReferenceKind.PLAN_CHECK, command.check_id),
            (EvidenceReferenceKind.EXECUTION, str(execution.ordinal)),
        )
    ]
    references.sort(key=lambda reference: (reference.kind.value, reference.identifier))
    finding = make_finding(
        scope,
        axis=SemanticAxis.CONTEXTUAL_SECURITY,
        blocking=False,
        state=FindingState.UNVERIFIED,
    ).model_copy(update={"evidence": references})
    reviewed = artifact(scope, assessment(scope, findings=[finding]))
    before = reviewed.model_dump_json()
    report = bound_report(scope, reviewed)

    source_label = source.path.utf8 or source.path.display
    changed_label = changed_path.utf8 or changed_path.display
    assert f"source: {source_label}" in report
    assert f"path: {changed_label}" in report
    assert f"check: {command.check_id}" in report
    assert f"execution: {command.check_id} — passed" in report
    assert command.check_id in plan.model_dump_json()
    assert command.check_id in evidence.model_dump_json()
    assert command.check_id in reviewed.model_dump_json()
    assert f"discovery_source:{source.source_id}" not in report
    assert f"change_path:{changed_path.raw_b64}" not in report
    assert all(
        identity not in report
        for identity in (
            reviewed.identity,
            changeset.identity,
            discovery.identity,
            plan.identity,
            evidence.identity,
            reviewed.bindings.semantic_assessment_identity,
        )
    )
    assert reviewed.model_dump_json() == before


def test_report_context_must_match_canonical_bindings(tmp_path: Path) -> None:
    scope = base_scope(tmp_path)
    reviewed = artifact(scope, assessment(scope))
    other_scope = base_scope(tmp_path / "other", requirement_text="A different requirement.")

    with pytest.raises(ValueError, match="not bound"):
        render_markdown_report(
            reviewed,
            changeset=other_scope[0],
            discovery=other_scope[1],
            plan=other_scope[2],
            evidence=other_scope[3],
        )


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


def test_explicit_required_verification_failure_needs_changes(tmp_path: Path) -> None:
    scope = base_scope(
        tmp_path,
        command=(sys.executable, "-c", "raise SystemExit(7)"),
        nonzero_failure_kind=FailureKind.VERIFICATION,
    )
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
    report = bound_report(scope, reviewed)

    assert reviewed.verdict is ReviewVerdict.INCONCLUSIVE
    assert check.outcome.value == "failed"
    assert check.failure_kind is FailureKind.UNCLASSIFIED
    assert check.required_evidence_gap is True
    assert "targeted-check` (required): failed/unclassified" in report
    assert "Required check at execution 0 did not produce reliable evidence." in report
    assert "verification.0" not in report


def test_confirmed_blocker_preserves_required_unclassified_verification_gap(
    tmp_path: Path,
) -> None:
    scope = base_scope(
        tmp_path,
        command=(sys.executable, "-c", "raise SystemExit(2)"),
        nonzero_failure_kind=FailureKind.UNCLASSIFIED,
    )
    blocker = make_finding(scope, axis=SemanticAxis.IMPACT, blocking=True)
    reviewed = artifact(
        scope,
        assessment(
            scope,
            axes=semantic_axes(statuses={SemanticAxis.IMPACT: SemanticStatus.FAIL}),
            findings=[blocker],
        ),
    )
    check = next(item for item in reviewed.checks if item.kind.value == "command")

    assert reviewed.verdict is ReviewVerdict.NEEDS_CHANGES
    assert any(
        finding.finding_id == blocker.finding_id for finding in reviewed.findings
    )
    assert check.failure_kind is FailureKind.UNCLASSIFIED
    assert check.required_evidence_gap is True
    assert any(gap.kind.value == "verification" for gap in reviewed.evidence_gaps)


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
    report = bound_report(scope, reviewed)

    assert reviewed.verdict is ReviewVerdict.INCONCLUSIVE
    assert all(axis.status is AxisStatus.INCONCLUSIVE for axis in reviewed.axes)
    check = next(item for item in reviewed.checks if item.kind.value == "command")
    assert check.outcome.value == "passed"
    assert any(gap.kind.value == "source_preservation" for gap in reviewed.evidence_gaps)
    assert "source-preservation.0" not in report
    assert "source preservation: targeted-check — source preservation failure" in report


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
    report = bound_report(scope, reviewed)
    assert "Semantic assessment did not establish readiness for impact." in report
    assert "semantic.impact" not in report


def test_required_gap_report_omits_canonical_gap_identifiers(tmp_path: Path) -> None:
    scope = base_scope(tmp_path)
    limit_gap = SemanticLimitGap(
        concern=SemanticLimitConcern.SEMANTIC_COLLECTION,
        field="semantic_context.items",
        limit=128,
        observed=129,
        affected_axes=[SemanticAxis.SPEC],
        input_identity="d" * 64,
    )
    semantic = assessment(
        scope,
        axes=semantic_axes(
            statuses={SemanticAxis.SPEC: SemanticStatus.INCONCLUSIVE},
            gaps={SemanticAxis.SPEC},
        ),
        limit_gaps=[limit_gap],
    )
    reviewed = artifact(scope, semantic)
    report = bound_report(scope, reviewed)
    gap = next(
        gap for gap in reviewed.evidence_gaps if gap.gap_id.startswith("semantic-limit.")
    )

    assert (
        "Semantic input exceeded the canonical semantic\\_context.items bound. — "
        "semantic input limit"
    ) in report
    assert gap.gap_id not in report
    assert limit_gap.input_identity not in report
    assert "semantic-limit" not in report
    persisted = reviewed.model_dump_json()
    assert gap.gap_id in persisted
    assert limit_gap.input_identity in persisted


def test_renderer_is_faithful_concise_and_does_not_duplicate_output(tmp_path: Path) -> None:
    marker = "FULL-STDOUT-SHOULD-NOT-APPEAR"
    scope = base_scope(tmp_path, command=(sys.executable, "-c", f"print({marker!r})"))
    reviewed = artifact(scope, assessment(scope))
    before = reviewed.model_dump_json()
    report = bound_report(scope, reviewed)

    assert f"Verdict: **{reviewed.verdict.value}**" in report
    assert all(f"{axis.axis.value}: **{axis.status.value.upper()}**" in report for axis in reviewed.axes)
    assert marker not in report
    assert scope[3].executions[0].result.stdout.sha256 not in report
    assert reviewed.identity not in report
    assert reviewed.model_dump_json() == before
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


def test_long_valid_check_identifier_does_not_expose_hash_reference(tmp_path: Path) -> None:
    long_id = "check-" + "x" * 512
    scope = base_scope(tmp_path, command=(sys.executable, "-c", "pass"))
    long_scope = scope_with_check_id(scope, long_id)
    long_plan = long_scope[2]
    long_evidence = long_scope[3]
    reviewed = artifact(long_scope, assessment(long_scope))
    before = (
        long_plan.model_dump_json(),
        long_evidence.model_dump_json(),
        reviewed.model_dump_json(),
    )
    report = bound_report(long_scope, reviewed)

    summary = next(check for check in reviewed.checks if check.kind.value == "command")
    assert summary.check_id is None
    assert len(summary.check_id_sha256) == 64
    assert long_id not in report
    verification = report.split("## Blocking findings", 1)[0]
    assert long_id not in verification
    execution_lines = [line for line in verification.splitlines() if "[execution: " in line]
    assert len(execution_lines) == 1
    assert "[execution: check-" in execution_lines[0]
    assert execution_lines[0].endswith("...]")
    assert len(execution_lines[0]) < 600
    assert long_id in long_plan.model_dump_json()
    assert long_id in long_evidence.model_dump_json()
    assert long_plan.model_dump_json() == before[0]
    assert long_evidence.model_dump_json() == before[1]
    assert reviewed.model_dump_json() == before[2]


def test_bound_oversized_check_id_is_bounded_in_blocking_verification_report(
    tmp_path: Path,
) -> None:
    long_id = "check-" + "x" * 512
    scope = base_scope(
        tmp_path,
        command=(sys.executable, "-c", "raise SystemExit(1)"),
    )
    long_scope = scope_with_check_id(scope, long_id)
    reviewed = artifact(long_scope, assessment(long_scope))
    report = bound_report(long_scope, reviewed)

    blocking = report.split("## Blocking findings", 1)[1].split(
        "## Non-blocking and unverified findings", 1
    )[0]
    assert reviewed.verdict is ReviewVerdict.NEEDS_CHANGES
    assert "required verification failed" in blocking
    assert long_id not in blocking
    assert "execution: check-" in blocking
    assert "..." in blocking


def test_bound_oversized_check_id_is_bounded_in_required_gap_report(
    tmp_path: Path,
) -> None:
    long_id = "check-" + "x" * 512
    scope = base_scope(
        tmp_path,
        command=(sys.executable, "-c", "pass"),
        missing_capability=True,
    )
    long_scope = scope_with_check_id(scope, long_id)
    reviewed = artifact(long_scope, assessment(long_scope))
    report = bound_report(long_scope, reviewed)

    gaps = report.split("## Required evidence gaps", 1)[1]
    assert reviewed.verdict is ReviewVerdict.INCONCLUSIVE
    assert "Required check at execution 0 did not produce reliable evidence." in gaps
    assert long_id not in gaps
    assert "execution: check-" in gaps
    assert "check: check-" in gaps
    assert "verification.0" not in gaps
    assert "plan-check-sha256" not in gaps


def test_bound_oversized_check_id_is_bounded_in_source_preservation_gap(
    tmp_path: Path,
) -> None:
    long_id = "check-" + "x" * 512
    initial_scope = base_scope(tmp_path, command=(sys.executable, "-c", "pass"))
    changeset, discovery, plan, evidence = scope_with_check_id(initial_scope, long_id)
    execution = evidence.executions[0]
    preservation = SourcePreservationFailure(
        ordinal=execution.ordinal,
        check_id=long_id,
        snapshot_identity=execution.snapshot.identity,
        reason="Fixture source-preservation failure.",
    )
    evidence = build_verification_evidence(
        plan,
        [(execution.snapshot, execution.result)],
        source_preservation_failures=[preservation],
    )
    scope = changeset, discovery, plan, evidence
    reviewed = artifact(scope, assessment(scope))
    report = bound_report(scope, reviewed)

    gaps = report.split("## Required evidence gaps", 1)[1]
    assert long_id not in gaps
    assert "source preservation: check-" in gaps
    assert "..." in gaps
    assert "source-preservation.0" not in gaps


def test_bound_resolved_source_label_is_markdown_safe(tmp_path: Path) -> None:
    initial_scope = base_scope(tmp_path)
    changeset = initial_scope[0]
    unsafe_label = "unsafe\n\n## Fake heading\n\nVerdict: **NEEDS_CHANGES**"
    discovery = discover_review_sources(
        Path(changeset.repository_root),
        explicit_specs=(
            ProvidedRequirement(
                label=unsafe_label,
                content="The public result must remain an integer.",
            ),
        ),
    )
    plan = build_verification_plan(
        changeset,
        discovery,
        canonical_checks=[],
        trusted_policy_checks=[],
        planner_additions=[],
    )
    evidence = build_verification_evidence(plan, [])
    scope = changeset, discovery, plan, evidence
    finding = make_finding(
        scope,
        axis=SemanticAxis.SPEC,
        blocking=False,
        state=FindingState.UNVERIFIED,
    )
    reviewed = artifact(scope, assessment(scope, findings=[finding]))
    report = bound_report(scope, reviewed)

    assert "\n## Fake heading\n" not in report
    assert r"unsafe\n\n\#\# Fake heading" in report
    assert report.count("Verdict: **READY**") == 1


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
            "environment_profile": "FILESYSTEM_ONLY",
            "object_format": None,
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
