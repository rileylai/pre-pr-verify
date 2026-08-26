from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import pre_pr_verify.orchestration as orchestration
from pre_pr_verify.discovery import ProvidedRequirement, discover_review_sources
from pre_pr_verify.errors import PreReviewSetupError, PreflightError
from pre_pr_verify.git_capture import capture_changeset
from pre_pr_verify.models import ScopeMode
from pre_pr_verify.pre_review_setup import (
    PreReviewSetup,
    RequirementCandidate,
    SetupPhase,
)
from pre_pr_verify.scope_intent import (
    ScopeIntent,
    capture_resolved_scope,
    discover_scope_options,
    resolve_scope_selection,
)
from pre_pr_verify.semantic import build_semantic_assessment
from pre_pr_verify.semantic_models import SemanticAxis, SemanticAxisAssessment, SemanticStatus
from pre_pr_verify.verification import PlannerCheckInput, build_verification_plan
from pre_pr_verify.verification_models import (
    CapabilityName,
    EnvironmentProfile,
    ExecutionCapability,
    RequirementLevel,
)


def git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Orchestration Test")
    git(repository, "config", "user.email", "orchestration@example.invalid")
    (repository / "README.md").write_text("The pending change must preserve review contracts.\n")
    (repository / "app.py").write_text("value = 1\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "base")
    (repository / "app.py").write_text("value = 2\n")
    return repository


def review_inputs(repository: Path):
    options = discover_scope_options(repository)
    resolved = resolve_scope_selection(
        options,
        interactive=True,
        intent=ScopeIntent.WORKING_CHANGES,
    )
    changeset = capture_resolved_scope(resolved)
    discovery = discover_review_sources(
        repository,
        explicit_specs=(
            ProvidedRequirement(
                label="Orchestration acceptance criteria",
                content="The review must preserve deterministic orchestration boundaries.",
            ),
        ),
    )
    return resolved, changeset, discovery


def setup_at_final_confirmation(
    resolved, *, verification_answer: int = 1
) -> PreReviewSetup:
    setup = PreReviewSetup(
        interactive=True,
        requirement_candidates=(
            RequirementCandidate(
                source_id="1" * 64,
                label="Orchestration acceptance criteria",
            ),
        ),
        recommended_scope_number=1,
    )
    setup.submit(1)
    setup.bind_scope(resolved)
    setup.submit(1)
    setup.submit(verification_answer)
    assert setup.phase is SetupPhase.FINAL_CONFIRMATION
    return setup


def ready_setup(resolved) -> PreReviewSetup:
    setup = setup_at_final_confirmation(resolved)
    return setup


def capability(
    *, available: tuple[CapabilityName, ...] = (CapabilityName.OUTPUT_LIMITS,)
) -> ExecutionCapability:
    return ExecutionCapability(
        structured_argv=True,
        repository_bound_cwd=True,
        git_protection=True,
        source_preservation=True,
        authority_separation=True,
        secret_stripping=True,
        verdict_invariants=True,
        available=list(available),
        approval_waivable=[],
        approved_gaps=[],
    )


def plan_for(changeset, discovery, profile=EnvironmentProfile.FILESYSTEM_ONLY):
    return build_verification_plan(
        changeset,
        discovery,
        canonical_checks=[],
        trusted_policy_checks=[],
        planner_additions=(
            PlannerCheckInput(
                check_id="orchestration-check",
                requirement_level=RequirementLevel.REQUIRED,
                selection_reason="Focused orchestration fixture check.",
                argv=(sys.executable, "-c", "pass"),
                environment_profile=profile,
            ),
        ),
    )


def authorize(
    setup,
    changeset,
    discovery,
    plan,
    *,
    capability_value: ExecutionCapability | None = None,
    timeout_seconds: float = 2,
    output_limit_bytes: int = 4_096,
    required_capabilities: tuple[CapabilityName, ...] = (
        CapabilityName.OUTPUT_LIMITS,
    ),
    finalize: bool = True,
):
    authorization = orchestration.authorize_verification_plan(
        setup,
        changeset,
        discovery,
        plan,
        capability_value or capability(),
        timeout_seconds=timeout_seconds,
        output_limit_bytes=output_limit_bytes,
        required_capabilities=required_capabilities,
    )
    if finalize:
        setup.submit(1)
    return authorization


def persisted_path(base_path, authorization, changeset):
    return orchestration._execution_evidence_path(
        base_path,
        authorization,
        changeset.repository_root,
    )


def test_prepare_and_record_never_supply_a_human_setup_answer(repository: Path) -> None:
    setup = PreReviewSetup(
        interactive=True,
        requirement_candidates=(
            RequirementCandidate(source_id="1" * 64, label="Criteria"),
        ),
        recommended_scope_number=1,
    )

    step = orchestration.prepare_review(setup)

    assert step.phase is SetupPhase.SCOPE
    assert step.default_number == 1
    assert setup.scope_selection is None
    with pytest.raises(PreReviewSetupError, match="non-empty.*answer"):
        orchestration.record_setup_answer(setup, "")
    with pytest.raises(PreReviewSetupError, match="non-empty.*answer"):
        orchestration.record_setup_answer(setup, None)
    assert setup.phase is SetupPhase.SCOPE
    assert setup.scope_selection is None


def test_incomplete_setup_cannot_authorize_execution(repository: Path) -> None:
    resolved, changeset, discovery = review_inputs(repository)
    setup = PreReviewSetup(
        interactive=True,
        requirement_candidates=(
            RequirementCandidate(source_id="1" * 64, label="Criteria"),
        ),
    )
    plan = plan_for(changeset, discovery)

    with pytest.raises(PreReviewSetupError, match="not complete"):
        authorize(setup, changeset, discovery, plan)


def test_authorization_binds_before_final_confirmation(
    repository: Path, tmp_path: Path
) -> None:
    resolved, changeset, discovery = review_inputs(repository)
    setup = setup_at_final_confirmation(resolved)
    plan_a = plan_for(changeset, discovery)

    authorization = authorize(setup, changeset, discovery, plan_a, finalize=False)

    assert setup.phase is SetupPhase.FINAL_CONFIRMATION
    assert setup.verification_authorization_binding == authorization.binding_identity
    with pytest.raises(PreReviewSetupError, match="not complete"):
        orchestration.execute_authorized_plan(
            setup,
            changeset,
            discovery,
            plan_a,
            capability(),
            authorization,
            evidence_path=tmp_path / "evidence.json",
        )

    plan_b = plan_for(changeset, discovery, EnvironmentProfile.GIT_REPOSITORY)
    with pytest.raises(orchestration.ReauthorizationRequired, match="reauthorization"):
        authorize(setup, changeset, discovery, plan_b)
    assert setup.phase is SetupPhase.VERIFICATION
    assert setup.verification_authorization_binding is None


def test_first_authorization_cannot_be_bound_after_final_confirmation(
    repository: Path,
) -> None:
    resolved, changeset, discovery = review_inputs(repository)
    setup = setup_at_final_confirmation(resolved)

    with pytest.raises(PreReviewSetupError, match="authorization binding"):
        setup.submit(1)

    assert setup.phase is SetupPhase.FINAL_CONFIRMATION
    assert setup.verification_authorization_binding is None


def test_bound_authorization_allows_final_confirmation(repository: Path) -> None:
    resolved, changeset, discovery = review_inputs(repository)
    setup = setup_at_final_confirmation(resolved)
    authorization = authorize(
        setup,
        changeset,
        discovery,
        plan_for(changeset, discovery),
        finalize=False,
    )

    setup.submit(1)

    assert setup.phase is SetupPhase.READY_TO_REVIEW
    assert setup.verification_authorization_binding == authorization.binding_identity


def test_review_without_execution_needs_no_authorization_binding(
    repository: Path,
) -> None:
    resolved, _, _ = review_inputs(repository)
    setup = setup_at_final_confirmation(resolved, verification_answer=2)

    setup.submit(1)

    assert setup.phase is SetupPhase.READY_TO_REVIEW
    assert setup.verification_authorization_binding is None


def test_authorization_is_bound_to_exact_plan_and_profile_change_requires_reauthorization(
    repository: Path, tmp_path: Path
) -> None:
    resolved, changeset, discovery = review_inputs(repository)
    setup = ready_setup(resolved)
    plan_a = plan_for(changeset, discovery)
    authorization = authorize(setup, changeset, discovery, plan_a)
    plan_b = plan_for(changeset, discovery, EnvironmentProfile.GIT_REPOSITORY)

    with pytest.raises(orchestration.ReauthorizationRequired, match="reauthorization"):
        orchestration.execute_authorized_plan(
            setup,
            changeset,
            discovery,
            plan_b,
            capability(),
            authorization,
            evidence_path=tmp_path / "evidence.json",
        )

    assert setup.phase is SetupPhase.VERIFICATION
    assert setup.current_step().phase is SetupPhase.VERIFICATION
    assert setup.verification_selection is None


def test_authorization_helper_cannot_rebind_ready_setup_to_a_new_plan(
    repository: Path,
) -> None:
    resolved, changeset, discovery = review_inputs(repository)
    setup = ready_setup(resolved)
    plan_a = plan_for(changeset, discovery)
    authorize(setup, changeset, discovery, plan_a)
    plan_b = plan_for(changeset, discovery, EnvironmentProfile.GIT_REPOSITORY)

    with pytest.raises(orchestration.ReauthorizationRequired, match="reauthorization"):
        authorize(setup, changeset, discovery, plan_b)

    assert setup.phase is SetupPhase.VERIFICATION
    assert setup.verification_authorization_binding is None


def test_completed_evidence_survives_later_presentation_failure_without_rerun(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved, changeset, discovery = review_inputs(repository)
    setup = ready_setup(resolved)
    plan = plan_for(changeset, discovery)
    authorization = authorize(setup, changeset, discovery, plan)
    evidence_path = tmp_path / "review-run" / "verification-evidence.json"
    calls = 0
    execute = orchestration.execute_verification_plan

    def counted_execute(*args, **kwargs):
        nonlocal calls
        calls += 1
        return execute(*args, **kwargs)

    monkeypatch.setattr(orchestration, "execute_verification_plan", counted_execute)
    evidence = orchestration.execute_authorized_plan(
        setup,
        changeset,
        discovery,
        plan,
        capability(),
        authorization,
        evidence_path=evidence_path,
    )

    with pytest.raises(RuntimeError, match="presentation failure"):
        raise RuntimeError("presentation failure")

    reloaded = orchestration.execute_authorized_plan(
        setup,
        changeset,
        discovery,
        plan,
        capability(),
        authorization,
        evidence_path=evidence_path,
    )

    assert calls == 1
    assert reloaded == evidence
    assert persisted_path(evidence_path, authorization, changeset).exists()


def test_stale_evidence_is_rejected_for_a_different_plan(
    repository: Path, tmp_path: Path
) -> None:
    resolved, changeset, discovery = review_inputs(repository)
    setup = ready_setup(resolved)
    plan_a = plan_for(changeset, discovery)
    authorization = authorize(setup, changeset, discovery, plan_a)
    evidence_path = tmp_path / "evidence.json"
    orchestration.execute_authorized_plan(
        setup,
        changeset,
        discovery,
        plan_a,
        capability(),
        authorization,
        evidence_path=evidence_path,
    )
    persisted_evidence_path = persisted_path(evidence_path, authorization, changeset)
    plan_b = plan_for(changeset, discovery, EnvironmentProfile.GIT_REPOSITORY)

    with pytest.raises(orchestration.EvidenceReuseError, match="plan identity"):
        orchestration.load_completed_execution(
            persisted_evidence_path,
            changeset,
            discovery,
            plan_b,
        )


def test_revised_plan_uses_a_plan_scoped_evidence_path(
    repository: Path, tmp_path: Path
) -> None:
    resolved, changeset, discovery = review_inputs(repository)
    setup = ready_setup(resolved)
    plan_a = plan_for(changeset, discovery)
    authorization_a = authorize(setup, changeset, discovery, plan_a)
    evidence_path = tmp_path / "evidence.json"
    evidence_a = orchestration.execute_authorized_plan(
        setup,
        changeset,
        discovery,
        plan_a,
        capability(),
        authorization_a,
        evidence_path=evidence_path,
    )

    plan_b = plan_for(changeset, discovery, EnvironmentProfile.GIT_REPOSITORY)
    with pytest.raises(orchestration.ReauthorizationRequired, match="reauthorization"):
        orchestration.execute_authorized_plan(
            setup,
            changeset,
            discovery,
            plan_b,
            capability(),
            authorization_a,
            evidence_path=evidence_path,
        )
    setup.submit(1)
    authorization_b = authorize(setup, changeset, discovery, plan_b)

    evidence_b = orchestration.execute_authorized_plan(
        setup,
        changeset,
        discovery,
        plan_b,
        capability(),
        authorization_b,
        evidence_path=evidence_path,
    )
    path_a = persisted_path(evidence_path, authorization_a, changeset)
    path_b = persisted_path(evidence_path, authorization_b, changeset)

    assert evidence_a.plan.identity == plan_a.identity
    assert evidence_b.plan.identity == plan_b.identity
    assert path_a.exists()
    assert path_b.exists()
    assert path_a != path_b
    assert orchestration.load_completed_execution(
        path_a, changeset, discovery, plan_a, authorization=authorization_a
    ).plan.identity == plan_a.identity
    assert orchestration.load_completed_execution(
        path_b, changeset, discovery, plan_b, authorization=authorization_b
    ).plan.identity == plan_b.identity


def test_same_plan_different_capability_and_policy_use_distinct_paths(
    repository: Path, tmp_path: Path
) -> None:
    resolved, changeset, discovery = review_inputs(repository)
    setup = ready_setup(resolved)
    plan = plan_for(changeset, discovery)
    evidence_path = tmp_path / "evidence.json"
    authorization_a = authorize(setup, changeset, discovery, plan)
    orchestration.execute_authorized_plan(
        setup,
        changeset,
        discovery,
        plan,
        capability(),
        authorization_a,
        evidence_path=evidence_path,
    )

    setup.require_verification_reauthorization()
    setup.submit(1)
    authorization_b = authorize(
        setup,
        changeset,
        discovery,
        plan,
        capability_value=capability(available=()),
    )
    orchestration.execute_authorized_plan(
        setup,
        changeset,
        discovery,
        plan,
        capability(available=()),
        authorization_b,
        evidence_path=evidence_path,
    )

    setup.require_verification_reauthorization()
    setup.submit(1)
    authorization_c = authorize(
        setup,
        changeset,
        discovery,
        plan,
        timeout_seconds=3,
    )
    orchestration.execute_authorized_plan(
        setup,
        changeset,
        discovery,
        plan,
        capability(),
        authorization_c,
        evidence_path=evidence_path,
    )

    path_a = persisted_path(evidence_path, authorization_a, changeset)
    path_b = persisted_path(evidence_path, authorization_b, changeset)
    path_c = persisted_path(evidence_path, authorization_c, changeset)
    assert authorization_a.plan_identity == authorization_b.plan_identity
    assert authorization_b.plan_identity == authorization_c.plan_identity
    assert len({path_a, path_b, path_c}) == 3
    assert all(path.exists() for path in (path_a, path_b, path_c))


def test_stale_evidence_is_rejected_for_a_different_changeset(
    repository: Path, tmp_path: Path
) -> None:
    resolved, changeset, discovery = review_inputs(repository)
    setup = ready_setup(resolved)
    plan = plan_for(changeset, discovery)
    authorization = authorize(setup, changeset, discovery, plan)
    evidence_path = tmp_path / "evidence.json"
    orchestration.execute_authorized_plan(
        setup,
        changeset,
        discovery,
        plan,
        capability(),
        authorization,
        evidence_path=evidence_path,
    )
    persisted_evidence_path = persisted_path(evidence_path, authorization, changeset)

    (repository / "app.py").write_text("value = 3\n")
    changed_changeset = capture_changeset(repository, "HEAD", ScopeMode.PENDING)

    with pytest.raises(orchestration.EvidenceReuseError, match="ChangeSet identity"):
        orchestration.load_completed_execution(
            persisted_evidence_path,
            changed_changeset,
            discovery,
            plan,
        )


def test_invalid_persisted_evidence_fails_closed_without_execution(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved, changeset, discovery = review_inputs(repository)
    setup = ready_setup(resolved)
    plan = plan_for(changeset, discovery)
    authorization = authorize(setup, changeset, discovery, plan)
    evidence_path = tmp_path / "evidence.json"
    persisted_evidence_path = persisted_path(evidence_path, authorization, changeset)
    persisted_evidence_path.write_text("not canonical JSON")

    def must_not_execute(*args, **kwargs):
        pytest.fail("invalid persisted evidence triggered a verification rerun")

    monkeypatch.setattr(orchestration, "execute_verification_plan", must_not_execute)
    with pytest.raises(orchestration.EvidenceReuseError, match="loading/validation"):
        orchestration.execute_authorized_plan(
            setup,
            changeset,
            discovery,
            plan,
            capability(),
            authorization,
            evidence_path=evidence_path,
        )


def test_evidence_persistence_rejects_author_repository_paths(
    repository: Path, tmp_path: Path
) -> None:
    resolved, changeset, discovery = review_inputs(repository)
    setup = ready_setup(resolved)
    plan = plan_for(changeset, discovery)
    authorization = authorize(setup, changeset, discovery, plan)
    evidence = orchestration.execute_authorized_plan(
        setup,
        changeset,
        discovery,
        plan,
        capability(),
        authorization,
        evidence_path=tmp_path / "evidence.json",
    )

    with pytest.raises(PreflightError, match="author repository is required"):
        orchestration.persist_verification_evidence(
            tmp_path / "unscoped-evidence.json",
            evidence,
        )
    with pytest.raises(PreflightError, match="outside the author repository"):
        orchestration.persist_verification_evidence(
            repository / "evidence.json",
            evidence,
            author_repository=repository,
        )
    assert not (repository / "evidence.json").exists()


def test_finalization_reloads_semantics_and_returns_canonical_report(
    repository: Path, tmp_path: Path
) -> None:
    resolved, changeset, discovery = review_inputs(repository)
    setup = ready_setup(resolved)
    plan = plan_for(changeset, discovery)
    authorization = authorize(setup, changeset, discovery, plan)
    evidence = orchestration.execute_authorized_plan(
        setup,
        changeset,
        discovery,
        plan,
        capability(),
        authorization,
        evidence_path=tmp_path / "evidence.json",
    )
    assessment = build_semantic_assessment(
        changeset,
        discovery,
        plan,
        evidence,
        axes=(
            SemanticAxisAssessment(
                axis=axis,
                status=SemanticStatus.PASS,
                rationale=f"Reviewed {axis.value} for this orchestration change.",
            )
            for axis in SemanticAxis
        ),
    )

    result = orchestration.finalize_review(
        changeset,
        discovery,
        plan,
        evidence,
        assessment,
        verifier_version="0.1.7",
        verifier_commit_or_build="test-build",
    )

    assert result.exit_code == 0
    assert result.artifact.verdict.value == "READY"
    assert result.report.startswith("# PrePR Verify Report")
    assert "## Semantic Review" in result.report
    assert "### Spec — **PASS**" in result.report
