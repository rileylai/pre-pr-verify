from __future__ import annotations

import hashlib
import subprocess
from itertools import combinations
from pathlib import Path

import pytest
from pydantic import ValidationError

from pre_pr_verify.discovery import (
    ProvidedRequirement,
    TrustedSourceSelection,
    discover_review_sources,
)
from pre_pr_verify.discovery_models import RequirementResolutionStatus
from pre_pr_verify.git_capture import capture_changeset
from pre_pr_verify.models import ScopeMode
from pre_pr_verify.semantic import (
    SemanticLimitExceeded,
    bind_semantic_reference,
    build_semantic_assessment,
    collect_semantic_context,
    iter_semantic_sources,
    load_semantic_assessment,
)
from pre_pr_verify.semantic_models import (
    EvidenceReferenceKind,
    FindingCategory,
    FindingSeverity,
    FindingState,
    RequirementComparison,
    RequirementRelation,
    SemanticAssessment,
    SemanticAxis,
    SemanticAxisAssessment,
    SemanticFinding,
    SemanticLimitConcern,
    SemanticLimitGap,
    SemanticStatus,
    MAX_AXIS_RATIONALE_CHARS,
    MAX_COMPARISONS,
    MAX_FINDING_EXPLANATION_CHARS,
    MAX_FINDING_ID_CHARS,
    MAX_FINDINGS,
    MAX_REFERENCES_PER_FINDING,
    hash_payload,
)
from pre_pr_verify.verification import build_verification_plan
from pre_pr_verify.verification_models import build_verification_evidence


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def review_scope(
    tmp_path: Path,
    *,
    explicit_specs: list[ProvidedRequirement] | None = None,
    trusted_requirement: bool = False,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "README.md").write_text("The app must preserve the public API.\n")
    (repo / "AGENTS.md").write_text(
        "Use repository-native tests and preserve the layered architecture.\n"
    )
    trusted_content = "Trusted requirement: preserve compatibility.\n"
    if trusted_requirement:
        (repo / "trusted-policy.txt").write_text(trusted_content)
    (repo / "src").mkdir()
    (repo / "src/app.py").write_text("def api(value): return value\n")
    (repo / "tests").mkdir()
    (repo / "tests/test_app.py").write_text("def test_api(): assert api(1) == 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    (repo / "src/app.py").write_text("def api(value): return str(value)\n")
    changeset = capture_changeset(repo, "main", ScopeMode.PENDING)
    trusted_selection = (
        TrustedSourceSelection(
            path="trusted-policy.txt",
            expected_sha256=hashlib.sha256(trusted_content.encode()).hexdigest(),
        )
        if trusted_requirement
        else None
    )
    discovery = discover_review_sources(
        repo,
        explicit_specs=explicit_specs or [],
        trusted_selection=trusted_selection,
    )
    plan = build_verification_plan(
        changeset,
        discovery,
        canonical_checks=[],
        trusted_policy_checks=[],
        planner_additions=[],
    )
    evidence = build_verification_evidence(plan, [])
    return changeset, discovery, plan, evidence


def missing_requirement_scope(tmp_path: Path):
    repo = tmp_path / "missing-requirement-repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "AGENTS.md").write_text("Use the local architecture.\n")
    (repo / "app.py").write_text("value = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    (repo / "app.py").write_text("value = 2\n")
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
    assert discovery.requirement_resolution.status is RequirementResolutionStatus.MISSING
    return changeset, discovery, plan, evidence


def empty_review_scope(tmp_path: Path):
    repo = tmp_path / "empty-repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "README.md").write_text("No pending change.\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
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
    return changeset, discovery, plan, evidence


def reference(scope, kind: EvidenceReferenceKind, identifier: str, detail: str):
    changeset, discovery, plan, evidence = scope
    return bind_semantic_reference(
        kind,
        identifier,
        detail,
        changeset_identity=changeset.identity,
        discovery_identity=discovery.identity,
        plan_identity=plan.identity,
        evidence_identity=evidence.identity,
    )


def axes(*, spec: SemanticStatus = SemanticStatus.PASS, **overrides):
    values = {
        axis: SemanticAxisAssessment(
            axis=axis,
            status=overrides.get(axis, spec),
            rationale=f"Assessment completed for {axis.value}.",
        )
        for axis in SemanticAxis
    }
    return list(values.values())


def finding(
    scope,
    *,
    finding_id: str,
    axis: SemanticAxis,
    category: FindingCategory,
    state: FindingState = FindingState.CONFIRMED,
    blocking: bool = False,
    kind: EvidenceReferenceKind = EvidenceReferenceKind.CHANGE_PATH,
    identifier: str | None = None,
) -> SemanticFinding:
    changeset, discovery, _, _ = scope
    if (
        category
        in {
            FindingCategory.SPEC_MISMATCH,
            FindingCategory.SPEC_PARTIAL,
            FindingCategory.SPEC_CONTRADICTION,
            FindingCategory.OUT_OF_SCOPE,
        }
        and identifier is None
        and kind is EvidenceReferenceKind.CHANGE_PATH
        and discovery.requirement_resolution.candidate_source_ids
    ):
        kind = EvidenceReferenceKind.DISCOVERY_SOURCE
        identifier = discovery.requirement_resolution.candidate_source_ids[0]
    target = identifier or changeset.changes[0].effective.path.raw_b64
    return SemanticFinding(
        finding_id=finding_id,
        axis=axis,
        category=category,
        state=state,
        severity=FindingSeverity.HIGH,
        blocking=blocking,
        title=finding_id,
        explanation="Concrete semantic evidence supports this assessment.",
        evidence=[
            reference(
                scope,
                kind,
                target,
                "Bound changed behavior evidence.",
            )
        ],
    )


def build(scope, *, axes_value=None, findings=(), comparisons=(), limit_gaps=()):
    changeset, discovery, plan, evidence = scope
    finding_values = list(findings)
    axis_values = list(axes_value or axes())
    owned = {
        axis: sorted(
            item.finding_id for item in finding_values if item.axis is axis
        )
        for axis in SemanticAxis
    }
    axis_values = [
        axis.model_copy(update={"finding_ids": owned[axis.axis]})
        for axis in axis_values
    ]
    return build_semantic_assessment(
        changeset,
        discovery,
        plan,
        evidence,
        axes=axis_values,
        findings=finding_values,
        requirement_comparisons=comparisons,
        limit_gaps=limit_gaps,
    )


def test_clear_spec_match_and_mismatch_are_structured(tmp_path: Path) -> None:
    scope = review_scope(tmp_path)
    passed = build(scope)
    assert passed.axes[0].status is SemanticStatus.PASS
    mismatch = finding(
        scope,
        finding_id="spec-mismatch",
        axis=SemanticAxis.SPEC,
        category=FindingCategory.SPEC_MISMATCH,
        blocking=True,
    )
    assessed = build(
        scope,
        axes_value=axes(spec=SemanticStatus.FAIL),
        findings=[mismatch],
    )
    assert assessed.findings[0].state is FindingState.CONFIRMED
    assert assessed.findings[0].blocking is True


def test_equal_precedence_requirement_judgment_is_recorded(
    tmp_path: Path,
) -> None:
    scope = review_scope(
        tmp_path,
        explicit_specs=[
            ProvidedRequirement("spec-a", "The API accepts integers."),
            ProvidedRequirement("spec-b", "The API returns a string."),
        ],
    )
    _, discovery, _, _ = scope
    source_ids = sorted(discovery.requirement_resolution.candidate_source_ids)
    comparison = RequirementComparison(
        source_ids=source_ids,
        relation=RequirementRelation.COMPLEMENTARY,
        rationale="The equal-precedence candidates were semantically compared.",
    )
    assessed = build(scope, comparisons=[comparison])
    assert (
        assessed.requirement_comparisons[0].relation
        is RequirementRelation.COMPLEMENTARY
    )


def test_complete_contradictory_group_cannot_yield_spec_pass(
    tmp_path: Path,
) -> None:
    scope = review_scope(
        tmp_path,
        explicit_specs=[
            ProvidedRequirement("spec-a", "The API accepts integers."),
            ProvidedRequirement("spec-b", "The API accepts strings."),
        ],
    )
    source_ids = sorted(scope[1].requirement_resolution.candidate_source_ids)
    comparison = RequirementComparison(
        source_ids=source_ids,
        relation=RequirementRelation.CONTRADICTORY,
        rationale="The winning requirements cannot both define the same input contract.",
    )
    with pytest.raises(ValueError, match="contradictory requirements"):
        build(scope, comparisons=[comparison])

    conflict = SemanticFinding(
        finding_id="winning-requirement-conflict",
        axis=SemanticAxis.SPEC,
        category=FindingCategory.SPEC_CONTRADICTION,
        state=FindingState.EVIDENCE_GAP,
        severity=FindingSeverity.HIGH,
        blocking=False,
        title="Winning requirements conflict",
        explanation="Equal-precedence requirements remain semantically contradictory.",
        evidence=[
            reference(
                scope,
                EvidenceReferenceKind.DISCOVERY_SOURCE,
                source_id,
                "Winning requirement participating in the contradiction.",
            )
            for source_id in source_ids
        ],
    )
    gap_axes = axes(spec=SemanticStatus.INCONCLUSIVE)
    gap_axes[0] = gap_axes[0].model_copy(update={"required_evidence_gap": True})
    assessed = build(
        scope,
        axes_value=gap_axes,
        findings=[conflict],
        comparisons=[comparison],
    )
    assert assessed.axes[0].required_evidence_gap is True


def test_missing_requirements_cannot_yield_five_axis_pass(tmp_path: Path) -> None:
    scope = missing_requirement_scope(tmp_path)
    with pytest.raises(ValueError, match="missing requirements"):
        build(scope)

    gap = finding(
        scope,
        finding_id="requirements-unavailable",
        axis=SemanticAxis.SPEC,
        category=FindingCategory.SPEC_PARTIAL,
        state=FindingState.EVIDENCE_GAP,
        blocking=False,
    )
    gap_axes = axes(spec=SemanticStatus.INCONCLUSIVE)
    gap_axes[0] = gap_axes[0].model_copy(update={"required_evidence_gap": True})
    assessed = build(scope, axes_value=gap_axes, findings=[gap])
    assert assessed.axes[0].status is SemanticStatus.INCONCLUSIVE


def test_repository_standard_violation_is_grounded(tmp_path: Path) -> None:
    scope = review_scope(tmp_path)
    standard_id = next(
        source.source_id
        for source in scope[1].sources
        if source.source_type.value == "repository_standard"
    )
    violation = finding(
        scope,
        finding_id="architecture-violation",
        axis=SemanticAxis.STANDARDS,
        category=FindingCategory.STANDARD_VIOLATION,
        blocking=True,
        kind=EvidenceReferenceKind.DISCOVERY_SOURCE,
        identifier=standard_id,
    )
    assessed = build(
        scope,
        axes_value=axes(**{SemanticAxis.STANDARDS: SemanticStatus.FAIL}),
        findings=[violation],
    )
    assert assessed.findings[0].evidence[0].kind is EvidenceReferenceKind.DISCOVERY_SOURCE


def test_trusted_requirement_only_source_is_not_standards_authority(
    tmp_path: Path,
) -> None:
    scope = review_scope(tmp_path, trusted_requirement=True)
    trusted_requirement_id = scope[1].requirement_resolution.candidate_source_ids[0]
    assert trusted_requirement_id not in scope[1].standards_source_ids
    violation = finding(
        scope,
        finding_id="trusted-requirement-is-not-standard",
        axis=SemanticAxis.STANDARDS,
        category=FindingCategory.STANDARD_VIOLATION,
        blocking=True,
        kind=EvidenceReferenceKind.DISCOVERY_SOURCE,
        identifier=trusted_requirement_id,
    )
    with pytest.raises(ValueError, match="canonical Standards source"):
        build(
            scope,
            axes_value=axes(**{SemanticAxis.STANDARDS: SemanticStatus.FAIL}),
            findings=[violation],
        )


def test_standard_violation_changed_path_only_is_rejected(tmp_path: Path) -> None:
    scope = review_scope(tmp_path)
    violation = finding(
        scope,
        finding_id="ungrounded-standard-violation",
        axis=SemanticAxis.STANDARDS,
        category=FindingCategory.STANDARD_VIOLATION,
        blocking=True,
    )
    with pytest.raises(ValueError, match="canonical Standards source"):
        build(
            scope,
            axes_value=axes(**{SemanticAxis.STANDARDS: SemanticStatus.FAIL}),
            findings=[violation],
        )


def test_lower_precedence_spec_source_cannot_ground_blocker(
    tmp_path: Path,
) -> None:
    scope = review_scope(
        tmp_path,
        explicit_specs=[ProvidedRequirement("explicit", "The API returns integers.")],
    )
    _, discovery, _, _ = scope
    lower_id = next(
        source.source_id
        for source in discovery.sources
        if source.requirement_precedence is not None
        and source.source_id
        not in discovery.requirement_resolution.candidate_source_ids
    )
    contradiction = finding(
        scope,
        finding_id="lower-precedence-contradiction",
        axis=SemanticAxis.SPEC,
        category=FindingCategory.SPEC_CONTRADICTION,
        blocking=True,
        kind=EvidenceReferenceKind.DISCOVERY_SOURCE,
        identifier=lower_id,
    )
    with pytest.raises(ValueError, match="winning requirement"):
        build(
            scope,
            axes_value=axes(spec=SemanticStatus.FAIL),
            findings=[contradiction],
        )


def test_missing_meaningful_test_scenario_is_a_test_gap(tmp_path: Path) -> None:
    scope = review_scope(tmp_path)
    gap = finding(
        scope,
        finding_id="missing-boundary-test",
        axis=SemanticAxis.TEST_SUFFICIENCY,
        category=FindingCategory.TEST_GAP,
        blocking=True,
    )
    assessed = build(
        scope,
        axes_value=axes(
            **{SemanticAxis.TEST_SUFFICIENCY: SemanticStatus.FAIL}
        ),
        findings=[gap],
    )
    assert assessed.findings[0].category is FindingCategory.TEST_GAP


def test_grounded_contextual_security_finding(tmp_path: Path) -> None:
    scope = review_scope(tmp_path)
    security = finding(
        scope,
        finding_id="trust-boundary-regression",
        axis=SemanticAxis.CONTEXTUAL_SECURITY,
        category=FindingCategory.CONTEXTUAL_SECURITY,
        blocking=True,
    )
    assessed = build(
        scope,
        axes_value=axes(
            **{SemanticAxis.CONTEXTUAL_SECURITY: SemanticStatus.FAIL}
        ),
        findings=[security],
    )
    assert assessed.findings[0].blocking is True


def test_unsupported_suspicion_remains_unverified_and_nonblocking(
    tmp_path: Path,
) -> None:
    scope = review_scope(tmp_path)
    concern = finding(
        scope,
        finding_id="possible-secret-leak",
        axis=SemanticAxis.CONTEXTUAL_SECURITY,
        category=FindingCategory.UNSUPPORTED_SUSPICION,
        state=FindingState.UNVERIFIED,
        blocking=False,
    )
    assessed = build(scope, findings=[concern])
    assert assessed.findings[0].state is FindingState.UNVERIFIED
    assert assessed.findings[0].blocking is False
    with pytest.raises(ValidationError, match="only confirmed"):
        SemanticFinding.model_validate(
            concern.model_dump(mode="json") | {"blocking": True}
        )


def test_finding_axis_membership_is_complete_and_canonical(tmp_path: Path) -> None:
    scope = review_scope(tmp_path)
    blocker = finding(
        scope,
        finding_id="owned-spec-blocker",
        axis=SemanticAxis.SPEC,
        category=FindingCategory.SPEC_MISMATCH,
        blocking=True,
    )
    changeset, discovery, plan, evidence = scope
    with pytest.raises(ValidationError, match="ownership|orphan"):
        build_semantic_assessment(
            changeset,
            discovery,
            plan,
            evidence,
            axes=axes(spec=SemanticStatus.FAIL),
            findings=[blocker],
        )

    valid = build(
        scope,
        axes_value=axes(spec=SemanticStatus.FAIL),
        findings=[blocker],
    )
    duplicate = valid.model_dump(mode="json")
    duplicate["axes"][1]["finding_ids"] = [blocker.finding_id]
    duplicate["identity"] = hash_payload(
        {key: value for key, value in duplicate.items() if key != "identity"}
    )
    with pytest.raises(ValidationError, match="ownership"):
        load_semantic_assessment(duplicate, *scope)

    wrong_axis = valid.model_dump(mode="json")
    wrong_axis["axes"][0]["finding_ids"] = []
    wrong_axis["axes"][2]["finding_ids"] = [blocker.finding_id]
    wrong_axis["identity"] = hash_payload(
        {key: value for key, value in wrong_axis.items() if key != "identity"}
    )
    with pytest.raises(ValidationError, match="ownership"):
        load_semantic_assessment(wrong_axis, *scope)

    pass_with_blocker = valid.model_dump(mode="json")
    pass_with_blocker["axes"][0]["status"] = "pass"
    pass_with_blocker["identity"] = hash_payload(
        {key: value for key, value in pass_with_blocker.items() if key != "identity"}
    )
    with pytest.raises(ValidationError, match="PASS|blocking"):
        load_semantic_assessment(pass_with_blocker, *scope)


def test_finding_category_must_match_declared_axis(tmp_path: Path) -> None:
    scope = review_scope(tmp_path)
    standard_id = scope[1].standards_source_ids[0]
    with pytest.raises(ValidationError, match="category.*axis"):
        SemanticFinding(
            finding_id="standard-on-impact",
            axis=SemanticAxis.IMPACT,
            category=FindingCategory.STANDARD_VIOLATION,
            state=FindingState.CONFIRMED,
            severity=FindingSeverity.HIGH,
            blocking=True,
            title="Wrong axis",
            explanation="A Standards finding cannot belong to Impact.",
            evidence=[
                reference(
                    scope,
                    EvidenceReferenceKind.DISCOVERY_SOURCE,
                    standard_id,
                    "Canonical Standards source.",
                )
            ],
        )


def test_empty_changeset_is_nothing_to_review(tmp_path: Path) -> None:
    scope = empty_review_scope(tmp_path)
    with pytest.raises(ValueError, match="nothing_to_review"):
        build(scope)


def test_winning_equal_precedence_candidates_require_comparisons(
    tmp_path: Path,
) -> None:
    scope = review_scope(
        tmp_path,
        explicit_specs=[
            ProvidedRequirement("spec-a", "The API accepts integers."),
            ProvidedRequirement("spec-b", "The API returns a string."),
        ],
    )
    with pytest.raises(ValueError, match="omits"):
        build(scope)


def test_contradictory_candidates_cannot_be_spec_pass_without_reconciliation(
    tmp_path: Path,
) -> None:
    scope = review_scope(
        tmp_path,
        explicit_specs=[
            ProvidedRequirement("spec-a", "The API accepts integers."),
            ProvidedRequirement("spec-b", "The API accepts strings."),
        ],
    )
    with pytest.raises(ValueError, match="omits"):
        build(scope, axes_value=axes(spec=SemanticStatus.PASS))


@pytest.mark.parametrize(
    "updates",
    [
        {"field": "requirement_comparisons.fake"},
        {"limit": 1},
        {"observed": 66},
        {"affected_axes": [SemanticAxis.IMPACT]},
        {"input_identity": "f" * 64},
    ],
)
def test_requirement_limit_gap_fields_are_bound_to_winning_candidates(
    tmp_path: Path,
    updates: dict[str, object],
) -> None:
    scope = review_scope(
        tmp_path,
        explicit_specs=[
            ProvidedRequirement("spec-a", "The API accepts integers."),
            ProvidedRequirement("spec-b", "The API returns a string."),
        ],
    )
    source_ids = sorted(scope[1].requirement_resolution.candidate_source_ids)
    comparison = RequirementComparison(
        source_ids=source_ids,
        relation=RequirementRelation.COMPLEMENTARY,
        rationale="The representable winning candidates were reconciled.",
    )
    fake_gap = SemanticLimitGap(
        concern=SemanticLimitConcern.SEMANTIC_COLLECTION,
        field="requirement_comparisons",
        limit=64,
        observed=65,
        affected_axes=[SemanticAxis.SPEC],
        input_identity="a" * 64,
    ).model_copy(update=updates)

    gap_axes = [
        axis.model_copy(
            update={
                "status": SemanticStatus.INCONCLUSIVE,
                "required_evidence_gap": True,
            }
        )
        for axis in axes()
    ]
    with pytest.raises(ValueError, match="requirement comparison limit gap"):
        build(
            scope,
            axes_value=gap_axes,
            comparisons=[comparison],
            limit_gaps=[fake_gap],
        )


def test_fabricated_requirement_overflow_cannot_hide_a_contradiction(
    tmp_path: Path,
) -> None:
    scope = review_scope(
        tmp_path,
        explicit_specs=[
            ProvidedRequirement("spec-a", "The API accepts integers."),
            ProvidedRequirement("spec-b", "The API accepts strings."),
        ],
    )
    fake_gap = SemanticLimitGap(
        concern=SemanticLimitConcern.SEMANTIC_COLLECTION,
        field="requirement_comparisons",
        limit=64,
        observed=65,
        affected_axes=[SemanticAxis.SPEC],
        input_identity="b" * 64,
    )

    gap_axes = [
        axis.model_copy(
            update={
                "status": SemanticStatus.INCONCLUSIVE,
                "required_evidence_gap": True,
            }
        )
        for axis in axes()
    ]
    with pytest.raises(ValueError, match="requirement comparison limit gap"):
        build(
            scope,
            axes_value=gap_axes,
            limit_gaps=[fake_gap],
        )


def test_semantic_text_and_collection_bounds() -> None:
    scope_ref = {
        "kind": "change_path",
        "identifier": "a",
        "detail": "ok",
        "changeset_identity": "a" * 64,
        "discovery_identity": "b" * 64,
        "plan_identity": "c" * 64,
        "evidence_identity": "d" * 64,
    }
    with pytest.raises(ValidationError):
        SemanticAxisAssessment(
            axis=SemanticAxis.SPEC,
            status=SemanticStatus.PASS,
            rationale="x" * 2049,
        )
    with pytest.raises(ValidationError):
        SemanticFinding(
            finding_id="too-long",
            axis=SemanticAxis.SPEC,
            category=FindingCategory.SPEC_PARTIAL,
            state=FindingState.UNVERIFIED,
            severity=FindingSeverity.INFO,
            blocking=False,
            title="title",
            explanation="x" * 4097,
            evidence=[scope_ref],
        )
    with pytest.raises(ValidationError):
        SemanticFinding(
            finding_id="too-many-refs",
            axis=SemanticAxis.IMPACT,
            category=FindingCategory.IMPACT_REGRESSION,
            state=FindingState.UNVERIFIED,
            severity=FindingSeverity.INFO,
            blocking=False,
            title="title",
            explanation="ok",
            evidence=[scope_ref] * 17,
        )

    assert SemanticAxisAssessment(
        axis=SemanticAxis.SPEC,
        status=SemanticStatus.PASS,
        rationale="x" * MAX_AXIS_RATIONALE_CHARS,
    ).rationale == "x" * MAX_AXIS_RATIONALE_CHARS
    assert SemanticFinding(
        finding_id="exact-explanation",
        axis=SemanticAxis.IMPACT,
        category=FindingCategory.IMPACT_REGRESSION,
        state=FindingState.UNVERIFIED,
        severity=FindingSeverity.INFO,
        blocking=False,
        title="title",
        explanation="x" * MAX_FINDING_EXPLANATION_CHARS,
        evidence=[scope_ref],
    ).explanation == "x" * MAX_FINDING_EXPLANATION_CHARS
    exact_detail = scope_ref | {"detail": "d" * 512}
    assert len(
        SemanticFinding(
            finding_id="exact-detail",
            axis=SemanticAxis.IMPACT,
            category=FindingCategory.IMPACT_REGRESSION,
            state=FindingState.UNVERIFIED,
            severity=FindingSeverity.INFO,
            blocking=False,
            title="title",
            explanation="bounded",
            evidence=[exact_detail],
        ).evidence[0].detail
    ) == 512
    with pytest.raises(ValidationError):
        SemanticFinding(
            finding_id="detail-overflow",
            axis=SemanticAxis.IMPACT,
            category=FindingCategory.IMPACT_REGRESSION,
            state=FindingState.UNVERIFIED,
            severity=FindingSeverity.INFO,
            blocking=False,
            title="title",
            explanation="bounded",
            evidence=[scope_ref | {"detail": "d" * 513}],
        )

    exact_finding_id = "f" * MAX_FINDING_ID_CHARS
    assert SemanticFinding(
        finding_id=exact_finding_id,
        axis=SemanticAxis.IMPACT,
        category=FindingCategory.IMPACT_REGRESSION,
        state=FindingState.UNVERIFIED,
        severity=FindingSeverity.INFO,
        blocking=False,
        title="bounded identifier",
        explanation="The finding identifier is exactly at the contract limit.",
        evidence=[scope_ref],
    ).finding_id == exact_finding_id
    with pytest.raises(ValidationError, match="finding_id"):
        SemanticFinding(
            finding_id="f" * (MAX_FINDING_ID_CHARS + 1),
            axis=SemanticAxis.IMPACT,
            category=FindingCategory.IMPACT_REGRESSION,
            state=FindingState.UNVERIFIED,
            severity=FindingSeverity.INFO,
            blocking=False,
            title="oversized identifier",
            explanation="This identifier exceeds the contract limit.",
            evidence=[scope_ref],
        )
    with pytest.raises(ValidationError, match="finding_ids"):
        SemanticAxisAssessment(
            axis=SemanticAxis.IMPACT,
            status=SemanticStatus.INCONCLUSIVE,
            rationale="Oversized finding reference.",
            finding_ids=["f" * (MAX_FINDING_ID_CHARS + 1)],
        )
    with pytest.raises(ValidationError, match="source_ids"):
        RequirementComparison(
            source_ids=["a" * 64, "b" * 65],
            relation=RequirementRelation.COMPLEMENTARY,
            rationale="Source IDs are stable SHA-256 identifiers.",
        )


def test_normal_bounded_assessment_round_trips_through_loader(
    tmp_path: Path,
) -> None:
    scope = review_scope(tmp_path)
    assessment = build(scope)
    assert load_semantic_assessment(assessment.model_dump(mode="json"), *scope) == assessment


def test_excessive_finding_and_comparison_collections_are_rejected(
    tmp_path: Path,
) -> None:
    scope = review_scope(tmp_path)
    findings = [
        finding(
            scope,
            finding_id=f"impact-{index:03d}",
            axis=SemanticAxis.IMPACT,
            category=FindingCategory.IMPACT_REGRESSION,
            state=FindingState.UNVERIFIED,
        )
        for index in range(129)
    ]
    with pytest.raises(SemanticLimitExceeded) as findings_error:
        build(scope, findings=findings)
    assert findings_error.value.gap.concern is SemanticLimitConcern.SEMANTIC_COLLECTION
    assert findings_error.value.gap.field == "findings"
    assert findings_error.value.gap.observed == MAX_FINDINGS + 1

    comparison_scope = review_scope(
        tmp_path / "comparisons",
        explicit_specs=[
            ProvidedRequirement("spec-a", "The API accepts integers."),
            ProvidedRequirement("spec-b", "The API returns strings."),
        ],
    )
    _, discovery, _, _ = comparison_scope
    source_ids = sorted(discovery.requirement_resolution.candidate_source_ids)
    comparison = RequirementComparison(
        source_ids=source_ids,
        relation=RequirementRelation.COMPLEMENTARY,
        rationale="Bounded comparison.",
    )
    base = build(comparison_scope, comparisons=[comparison])
    payload = base.model_dump(mode="json")
    payload["requirement_comparisons"] = [comparison.model_dump(mode="json")] * 65
    payload["identity"] = hash_payload(
        {key: value for key, value in payload.items() if key != "identity"}
    )
    with pytest.raises(SemanticLimitExceeded) as comparison_error:
        load_semantic_assessment(payload, *comparison_scope)
    assert comparison_error.value.gap.field == "requirement_comparisons"


def test_collection_limit_gap_cannot_be_five_axis_pass(tmp_path: Path) -> None:
    scope = review_scope(tmp_path)
    excessive = [
        finding(
            scope,
            finding_id=f"impact-{index:03d}",
            axis=SemanticAxis.IMPACT,
            category=FindingCategory.IMPACT_REGRESSION,
            state=FindingState.UNVERIFIED,
        )
        for index in range(MAX_FINDINGS + 1)
    ]
    with pytest.raises(SemanticLimitExceeded) as error:
        build(scope, findings=excessive)

    with pytest.raises(ValidationError, match="inconclusive evidence-gap axis"):
        build(scope, limit_gaps=[error.value.gap])

    gap_axes = [
        SemanticAxisAssessment(
            axis=axis,
            status=SemanticStatus.INCONCLUSIVE,
            rationale="Semantic collection overflow prevented complete assessment.",
            required_evidence_gap=True,
        )
        for axis in SemanticAxis
    ]
    assessed = build(scope, axes_value=gap_axes, limit_gaps=[error.value.gap])
    assert assessed.limit_gaps == [error.value.gap]
    assert all(axis.status is SemanticStatus.INCONCLUSIVE for axis in assessed.axes)


def test_external_prose_and_reference_overflow_are_structured(
    tmp_path: Path,
) -> None:
    scope = review_scope(tmp_path)
    assessment = build(scope)

    prose_payload = assessment.model_dump(mode="json")
    prose_payload["axes"][0]["rationale"] = "x" * (MAX_AXIS_RATIONALE_CHARS + 1)
    prose_payload["identity"] = hash_payload(
        {key: value for key, value in prose_payload.items() if key != "identity"}
    )
    with pytest.raises(SemanticLimitExceeded) as prose_error:
        load_semantic_assessment(prose_payload, *scope)
    assert prose_error.value.gap.concern is SemanticLimitConcern.PROSE
    assert prose_error.value.gap.field == "axes.0.rationale"

    identifier_payload = assessment.model_dump(mode="json")
    oversized_id = "f" * (MAX_FINDING_ID_CHARS + 1)
    identifier_payload["findings"] = [
        {
            "finding_id": oversized_id,
            "axis": "impact",
            "category": "impact_regression",
            "state": "unverified",
            "severity": "info",
            "blocking": False,
            "title": "Oversized identifier",
            "explanation": "The identifier exceeds the artifact safety bound.",
            "evidence": [
                reference(
                    scope,
                    EvidenceReferenceKind.CHANGE_PATH,
                    scope[0].changes[0].effective.path.raw_b64,
                    "Bound change path.",
                ).model_dump(mode="json")
            ],
        }
    ]
    identifier_payload["axes"][2]["finding_ids"] = [oversized_id]
    identifier_payload["identity"] = hash_payload(
        {key: value for key, value in identifier_payload.items() if key != "identity"}
    )
    with pytest.raises(SemanticLimitExceeded) as identifier_error:
        load_semantic_assessment(identifier_payload, *scope)
    assert identifier_error.value.gap.concern is SemanticLimitConcern.IDENTIFIER

    source_id = scope[1].sources[0].source_id
    reference_payload = assessment.model_dump(mode="json")
    reference_payload["findings"] = [
        {
            "finding_id": "reference-overflow",
            "axis": "impact",
            "category": "impact_regression",
            "state": "unverified",
            "severity": "info",
            "blocking": False,
            "title": "reference overflow",
            "explanation": "The input deliberately exceeds the artifact bound.",
            "evidence": [
                reference(
                    scope,
                    EvidenceReferenceKind.DISCOVERY_SOURCE,
                    f"{source_id}-{index}",
                    "Bounded detail.",
                ).model_dump(mode="json")
                for index in range(MAX_REFERENCES_PER_FINDING + 1)
            ],
        }
    ]
    reference_payload["identity"] = hash_payload(
        {key: value for key, value in reference_payload.items() if key != "identity"}
    )
    with pytest.raises(SemanticLimitExceeded) as reference_error:
        load_semantic_assessment(reference_payload, *scope)
    assert reference_error.value.gap.concern is SemanticLimitConcern.SEMANTIC_COLLECTION
    assert reference_error.value.gap.field == "findings.0.evidence"


def test_reference_limit_accepts_exactly_sixteen_distinct_references() -> None:
    references = [
        {
            "kind": "discovery_source",
            "identifier": f"source-{index:02d}",
            "detail": "Bounded detail.",
            "changeset_identity": "a" * 64,
            "discovery_identity": "b" * 64,
            "plan_identity": "c" * 64,
            "evidence_identity": "d" * 64,
        }
        for index in range(MAX_REFERENCES_PER_FINDING)
    ]
    bounded = SemanticFinding(
        finding_id="bounded-references",
        axis=SemanticAxis.IMPACT,
        category=FindingCategory.IMPACT_REGRESSION,
        state=FindingState.UNVERIFIED,
        severity=FindingSeverity.INFO,
        blocking=False,
        title="bounded references",
        explanation="All references fit the persisted artifact bound.",
        evidence=references,
    )
    assert len(bounded.evidence) == MAX_REFERENCES_PER_FINDING


def test_semantic_identity_and_reference_binding_are_validated(tmp_path: Path) -> None:
    scope = review_scope(tmp_path)
    grounded = finding(
        scope,
        finding_id="bound-finding",
        axis=SemanticAxis.SPEC,
        category=FindingCategory.SPEC_PARTIAL,
    )
    assessment = build(scope, findings=[grounded])
    payload = assessment.model_dump(mode="json")
    payload["identity"] = "0" * 64
    with pytest.raises(ValidationError, match="identity"):
        load_semantic_assessment(payload, *scope)

    scope_mismatch = assessment.model_dump(mode="json")
    scope_mismatch["changeset_identity"] = "e" * 64
    scope_mismatch["identity"] = hash_payload(
        {key: value for key, value in scope_mismatch.items() if key != "identity"}
    )
    with pytest.raises(ValueError, match="not bound|identities do not match"):
        load_semantic_assessment(scope_mismatch, *scope)

    unbound = assessment.model_dump(mode="json")
    unbound["findings"][0]["evidence"][0]["evidence_identity"] = "f" * 64
    unbound["identity"] = hash_payload(
        {key: value for key, value in unbound.items() if key != "identity"}
    )
    with pytest.raises(ValidationError, match="not bound"):
        load_semantic_assessment(unbound, *scope)

    mismatch = assessment.model_dump(mode="json")
    mismatch["findings"] = [
        {
            "finding_id": "bad-reference",
            "axis": "spec",
            "category": "spec_mismatch",
            "state": "confirmed",
            "severity": "high",
            "blocking": True,
            "title": "bad-reference",
            "explanation": "forged",
            "evidence": [
                {
                    **reference(
                        scope,
                        EvidenceReferenceKind.CHANGE_PATH,
                        "not-a-captured-path",
                        "forged",
                    ).model_dump(mode="json"),
                }
            ],
        }
    ]
    mismatch["axes"][0]["finding_ids"] = ["bad-reference"]
    mismatch["axes"][0]["status"] = "fail"
    mismatch["identity"] = hash_payload(
        {key: value for key, value in mismatch.items() if key != "identity"}
    )
    with pytest.raises(ValueError, match="canonical index|does not exist"):
        load_semantic_assessment(mismatch, *scope)


def test_context_search_is_bounded_and_uses_captured_content(tmp_path: Path) -> None:
    scope = review_scope(tmp_path)
    items = collect_semantic_context(scope[0], ["return", "api"])
    assert items
    assert items[0].path.to_bytes() == b"src/app.py"
    assert items[0].matched_terms == ["api", "return"]


def test_persisted_preview_does_not_cap_complete_source_inspection(
    tmp_path: Path,
) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "large-source-repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "large.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    complete_text = "a" * 3_000 + " NEEDLE " + "z" * 3_000
    (repo / "large.txt").write_text(complete_text)
    changeset = capture_changeset(repo, "main", ScopeMode.PENDING)

    sources = list(iter_semantic_sources(changeset))
    assert sources[0].text == complete_text
    assert len(sources[0].text) > 2_048
    preview = collect_semantic_context(changeset, ["NEEDLE"])[0]
    assert "NEEDLE" in preview.excerpt
    assert len(preview.excerpt) <= 2_048
    assert preview.path == sources[0].path
    assert sources[0].content_identity == changeset.changes[0].effective.content_identity


def test_context_selection_overflow_is_explicit_not_truncated(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "context-overflow-repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "one.txt").write_text("base\n")
    (repo / "two.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    (repo / "one.txt").write_text("semantic needle one\n")
    (repo / "two.txt").write_text("semantic needle two\n")
    changeset = capture_changeset(repo, "main", ScopeMode.PENDING)

    with pytest.raises(SemanticLimitExceeded) as error:
        collect_semantic_context(changeset, ["needle"], max_items=1)
    assert error.value.gap.concern is SemanticLimitConcern.SEMANTIC_COLLECTION
    assert error.value.gap.field == "semantic_context.items"

    with pytest.raises(SemanticLimitExceeded) as terms_error:
        collect_semantic_context(changeset, [f"term-{index}" for index in range(65)])
    assert terms_error.value.gap.field == "semantic_context.terms"


def test_context_term_iterable_consumes_at_most_limit_plus_one(
    tmp_path: Path,
) -> None:
    scope = review_scope(tmp_path)
    consumed = 0

    def huge_terms():
        nonlocal consumed
        for index in range(1_000_000):
            consumed += 1
            if consumed > 65:
                raise AssertionError("term iterable was consumed beyond limit + 1")
            yield f"term-{index}"

    with pytest.raises(SemanticLimitExceeded) as error:
        collect_semantic_context(scope[0], huge_terms())
    assert error.value.gap.field == "semantic_context.terms"
    assert error.value.gap.observed == 65
    assert consumed == 65


def test_reference_index_summarizes_more_than_512_paths_without_loss(
    tmp_path: Path,
) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "many-paths-repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "README.md").write_text("Repository requirement.\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    generated = repo / "generated"
    generated.mkdir()
    for index in range(513):
        (generated / f"item-{index:03d}.txt").write_text(f"item {index}\n")
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
    assessment = build_semantic_assessment(
        changeset,
        discovery,
        plan,
        evidence,
        axes=axes(),
    )

    assert assessment.reference_index.changed_paths.count == 513
    assert load_semantic_assessment(
        assessment.model_dump(mode="json"),
        changeset,
        discovery,
        plan,
        evidence,
    ) == assessment


def _comparison_scope(tmp_path: Path, count: int):
    return review_scope(
        tmp_path,
        explicit_specs=[
            ProvidedRequirement(f"spec-{index:02d}", f"Requirement {index}.")
            for index in range(count)
        ],
    )


def test_sixteen_sources_have_complete_single_group_coverage(tmp_path: Path) -> None:
    scope = _comparison_scope(tmp_path, 16)
    source_ids = sorted(scope[1].requirement_resolution.candidate_source_ids)
    group = RequirementComparison(
        source_ids=source_ids,
        relation=RequirementRelation.COMPLEMENTARY,
        rationale="The relation applies to the complete winning requirement group.",
    )
    assessment = build(scope, comparisons=[group])
    assert len(assessment.requirement_comparisons) == 1
    assert len(assessment.requirement_comparisons[0].source_ids) == 16

    hidden = RequirementComparison(
        source_ids=source_ids[:-1],
        relation=RequirementRelation.COMPLEMENTARY,
        rationale="One candidate was deliberately omitted.",
    )
    with pytest.raises(ValueError, match="omits"):
        build(scope, comparisons=[hidden])

    overlapping = RequirementComparison(
        source_ids=source_ids[:2],
        relation=RequirementRelation.CONTRADICTORY,
        rationale="This attempts to reclassify a pair already covered by the group.",
    )
    with pytest.raises(ValueError, match="overlap ambiguously"):
        build(scope, comparisons=[group, overlapping])


def test_seventeen_sources_accept_group_and_pair_decomposition(tmp_path: Path) -> None:
    scope = _comparison_scope(tmp_path, 17)
    source_ids = sorted(scope[1].requirement_resolution.candidate_source_ids)
    comparisons = [
        RequirementComparison(
            source_ids=source_ids[:-1],
            relation=RequirementRelation.COMPLEMENTARY,
            rationale="The first sixteen winning candidates form one complete group.",
        )
    ]
    comparisons.extend(
        RequirementComparison(
            source_ids=sorted((source_id, source_ids[-1])),
            relation=RequirementRelation.COMPLEMENTARY,
            rationale="The final winning candidate is reconciled with the group.",
        )
        for source_id in source_ids[:-1]
    )

    assessment = build(scope, comparisons=comparisons)

    assert len(assessment.requirement_comparisons) == 17
    assert assessment.limit_gaps == []
    assert load_semantic_assessment(
        assessment.model_dump(mode="json"), *scope
    ) == assessment


def test_representable_seventeen_sources_reject_fabricated_limit_gap(
    tmp_path: Path,
) -> None:
    scope = _comparison_scope(tmp_path, 17)
    source_ids = sorted(scope[1].requirement_resolution.candidate_source_ids)
    comparisons = [
        RequirementComparison(
            source_ids=source_ids[:-1],
            relation=RequirementRelation.COMPLEMENTARY,
            rationale="The first sixteen winning candidates form one complete group.",
        )
    ]
    comparisons.extend(
        RequirementComparison(
            source_ids=sorted((source_id, source_ids[-1])),
            relation=RequirementRelation.COMPLEMENTARY,
            rationale="The final winning candidate is reconciled with the group.",
        )
        for source_id in source_ids[:-1]
    )
    fake_gap = SemanticLimitGap(
        concern=SemanticLimitConcern.SEMANTIC_COLLECTION,
        field="requirement_comparisons",
        limit=MAX_COMPARISONS,
        observed=MAX_COMPARISONS + 1,
        affected_axes=[SemanticAxis.SPEC],
        input_identity="c" * 64,
    )
    gap_axes = [
        axis.model_copy(
            update={
                "status": SemanticStatus.INCONCLUSIVE,
                "required_evidence_gap": True,
            }
        )
        for axis in axes()
    ]

    with pytest.raises(ValueError, match="requirement comparison limit gap"):
        build(
            scope,
            axes_value=gap_axes,
            comparisons=comparisons,
            limit_gaps=[fake_gap],
        )


def test_complete_seventeen_source_decomposition_at_record_limit_is_valid(
    tmp_path: Path,
) -> None:
    scope = _comparison_scope(tmp_path, 17)
    source_ids = sorted(scope[1].requirement_resolution.candidate_source_ids)
    groups = [
        tuple(source_ids[:12]),
        (source_ids[11], source_ids[12], source_ids[13], source_ids[14]),
        (source_ids[10], source_ids[14], source_ids[16]),
    ]
    grouped_pairs = {
        tuple(sorted(pair))
        for group in groups
        for pair in combinations(group, 2)
    }
    comparisons = [
        RequirementComparison(
            source_ids=list(group),
            relation=RequirementRelation.COMPLEMENTARY,
            rationale="This group jointly reconciles its winning candidates.",
        )
        for group in groups
    ]
    comparisons.extend(
        RequirementComparison(
            source_ids=list(pair),
            relation=RequirementRelation.COMPLEMENTARY,
            rationale="This remaining winning pair was reconciled.",
        )
        for pair in combinations(source_ids, 2)
        if pair not in grouped_pairs
    )
    assert len(comparisons) == MAX_COMPARISONS

    assessment = build(scope, comparisons=comparisons)

    assert assessment.limit_gaps == []
    assert len(assessment.requirement_comparisons) == MAX_COMPARISONS


def test_real_comparison_record_overflow_retains_boundary_and_gap_binding(
    tmp_path: Path,
) -> None:
    scope = _comparison_scope(tmp_path, 34)
    source_ids = sorted(scope[1].requirement_resolution.candidate_source_ids)
    attempted = [
        RequirementComparison(
            source_ids=list(pair),
            relation=RequirementRelation.COMPLEMENTARY,
            rationale="A bounded comparison record was attempted.",
        )
        for pair in combinations(source_ids, 2)
    ][: MAX_COMPARISONS + 1]

    with pytest.raises(SemanticLimitExceeded) as overflow:
        build(scope, comparisons=attempted)

    assert overflow.value.gap.field == "requirement_comparisons"
    assert overflow.value.gap.limit == MAX_COMPARISONS
    assert overflow.value.gap.observed == MAX_COMPARISONS + 1
    assert overflow.value.gap.affected_axes == [SemanticAxis.SPEC]
    retained = list(overflow.value.values[:MAX_COMPARISONS])
    assert len(retained) == MAX_COMPARISONS
    assert all(isinstance(item, RequirementComparison) for item in retained)

    gap_axes = [
        axis.model_copy(
            update={
                "status": SemanticStatus.INCONCLUSIVE,
                "required_evidence_gap": True,
            }
        )
        if axis.axis is SemanticAxis.SPEC
        else axis
        for axis in axes()
    ]
    assessment = build(
        scope,
        axes_value=gap_axes,
        comparisons=retained,
        limit_gaps=[overflow.value.gap],
    )

    assert len(assessment.requirement_comparisons) == MAX_COMPARISONS
    assert assessment.limit_gaps == [overflow.value.gap]

    forged_payload = assessment.model_dump(mode="json")
    forged_payload["limit_gaps"][0]["input_identity"] = "d" * 64
    forged_payload["identity"] = hash_payload(
        {key: value for key, value in forged_payload.items() if key != "identity"}
    )
    with pytest.raises(ValueError, match="requirement comparison limit gap"):
        load_semantic_assessment(forged_payload, *scope)


def test_exactly_sixty_four_nonoverlapping_comparisons_are_valid(
    tmp_path: Path,
) -> None:
    scope = _comparison_scope(tmp_path, 12)
    source_ids = sorted(scope[1].requirement_resolution.candidate_source_ids)
    trio = tuple(source_ids[:3])
    comparisons = [
        RequirementComparison(
            source_ids=list(trio),
            relation=RequirementRelation.COMPLEMENTARY,
            rationale="This group jointly reconciles three candidates.",
        )
    ]
    trio_pairs = {
        tuple(sorted(pair))
        for pair in ((trio[0], trio[1]), (trio[0], trio[2]), (trio[1], trio[2]))
    }
    for left_index, left in enumerate(source_ids):
        for right in source_ids[left_index + 1 :]:
            if (left, right) in trio_pairs:
                continue
            comparisons.append(
                RequirementComparison(
                    source_ids=[left, right],
                    relation=RequirementRelation.COMPLEMENTARY,
                    rationale="This pair was semantically reconciled.",
                )
            )
    assert len(comparisons) == MAX_COMPARISONS
    assessment = build(scope, comparisons=comparisons)
    assert len(assessment.requirement_comparisons) == MAX_COMPARISONS


def test_exactly_128_findings_are_valid(tmp_path: Path) -> None:
    scope = review_scope(tmp_path)
    findings = [
        finding(
            scope,
            finding_id=f"bounded-{index:03d}",
            axis=SemanticAxis.IMPACT,
            category=FindingCategory.IMPACT_REGRESSION,
            state=FindingState.UNVERIFIED,
        )
        for index in range(MAX_FINDINGS)
    ]
    assessment = build(scope, findings=findings)
    assert len(assessment.findings) == MAX_FINDINGS


def test_limit_gap_collection_overflow_is_itself_structured(tmp_path: Path) -> None:
    scope = review_scope(tmp_path)
    excessive = [
        finding(
            scope,
            finding_id=f"overflow-source-{index:03d}",
            axis=SemanticAxis.IMPACT,
            category=FindingCategory.IMPACT_REGRESSION,
            state=FindingState.UNVERIFIED,
        )
        for index in range(MAX_FINDINGS + 1)
    ]
    with pytest.raises(SemanticLimitExceeded) as base_error:
        build(scope, findings=excessive)
    gaps = [
        base_error.value.gap.model_copy(update={"field": f"findings-{index:02d}"})
        for index in range(17)
    ]
    with pytest.raises(SemanticLimitExceeded) as gaps_error:
        build(scope, limit_gaps=gaps)
    assert gaps_error.value.gap.field == "limit_gaps"
    assert gaps_error.value.gap.observed == 17
