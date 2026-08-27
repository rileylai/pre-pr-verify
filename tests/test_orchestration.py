from __future__ import annotations

import hashlib
import io
import os
import stat
import subprocess
import sys
from dataclasses import replace
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


@pytest.fixture
def finalized_review(repository: Path, tmp_path: Path):
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
        axes=tuple(
            SemanticAxisAssessment(
                axis=axis,
                status=SemanticStatus.PASS,
                rationale=f"Reviewed {axis.value} for this report handoff fixture.",
            )
            for axis in SemanticAxis
        ),
    )
    return repository, orchestration.finalize_review(
        changeset,
        discovery,
        plan,
        evidence,
        assessment,
        verifier_version="0.1.8",
        verifier_commit_or_build="report-handoff-fixture",
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


def test_record_setup_answer_only_records_the_transition() -> None:
    setup = PreReviewSetup(interactive=True)

    result = orchestration.record_setup_answer(setup, 1)

    assert result is None
    assert setup.phase is SetupPhase.REQUIREMENTS
    assert setup.scope_selection == "working-changes"


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

    persisted_evidence_path = persisted_path(evidence_path, authorization, changeset)
    reloaded = orchestration.load_completed_execution(
        persisted_evidence_path,
        changeset,
        discovery,
        plan,
        authorization=authorization,
    )

    assert calls == 1
    assert reloaded == evidence
    assert persisted_evidence_path.exists()


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


def test_persist_final_report_round_trips_exact_bytes_and_preserves_review(
    finalized_review, repository: Path
) -> None:
    finalized = replace(
        finalized_review[1],
        report=finalized_review[1].report + "\nUTF-8: 測試 ✓\n",
    )
    report_before = finalized.report
    artifact_before = finalized.artifact.model_dump_json()
    exit_code_before = finalized.exit_code
    expected = report_before.encode("utf-8")

    handoff = orchestration.persist_final_report(
        finalized,
        author_repository=repository,
    )

    assert handoff.path.name == "final-report.md"
    assert handoff.path.is_file()
    assert handoff.path.read_bytes() == expected
    assert handoff.utf8_byte_length == len(expected)
    assert handoff.sha256 == hashlib.sha256(expected).hexdigest()
    assert not handoff.path.resolve().is_relative_to(repository.resolve())
    assert finalized.report == report_before
    assert finalized.artifact.model_dump_json() == artifact_before
    assert finalized.exit_code == exit_code_before
    if os.name == "posix":
        assert stat.S_IMODE(handoff.path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(handoff.path.stat().st_mode) == 0o600


def test_persist_final_report_uses_exclusive_private_creation(
    finalized_review, repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finalized = finalized_review[1]
    opened: list[tuple[object, int, int | None]] = []
    real_open = orchestration.os.open

    def capture_open(path, flags, *args, **kwargs):
        opened.append((path, flags, kwargs.get("dir_fd")))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(orchestration.os, "open", capture_open)
    handoff = orchestration.persist_final_report(
        finalized,
        author_repository=repository,
    )

    creation_flags = next(flags for path, flags, _ in opened if flags & os.O_CREAT)
    assert creation_flags & os.O_WRONLY
    assert creation_flags & os.O_CREAT
    assert creation_flags & os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        assert creation_flags & os.O_NOFOLLOW
    assert handoff.path.is_file()


@pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"),
    reason="secure descriptor-relative directory walking is unavailable",
)
def test_persist_final_report_rejects_intermediate_temp_root_symlink_swap(
    finalized_review, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finalized = finalized_review[1]
    temp_parent = tmp_path / "temp-parent"
    temp_root = temp_parent / "temp-root"
    temp_root.mkdir(parents=True, mode=0o700)
    moved_parent = tmp_path / "temp-parent-moved"
    (repository / "temp-root").mkdir(mode=0o700)
    monkeypatch.setattr(orchestration.tempfile, "gettempdir", lambda: str(temp_root))
    monkeypatch.setattr(
        orchestration.tempfile,
        "mkdtemp",
        lambda **_: pytest.fail("path-based mkdtemp must not anchor the temp root"),
    )
    real_open = orchestration.os.open
    swapped = False

    def swap_parent_before_anchor(path, flags, *args, **kwargs):
        nonlocal swapped
        if (
            not swapped
            and kwargs.get("dir_fd") is not None
            and path == os.fsencode(temp_parent.name)
        ):
            swapped = True
            temp_parent.rename(moved_parent)
            temp_parent.symlink_to(repository, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(orchestration.os, "open", swap_parent_before_anchor)
    with pytest.raises(orchestration.FinalReportHandoffError):
        orchestration.persist_final_report(finalized, author_repository=repository)

    assert swapped
    assert temp_parent.is_symlink()
    assert not (repository / "temp-root" / "final-report.md").exists()


@pytest.mark.skipif(
    os.open not in os.supports_dir_fd
    or os.stat not in os.supports_dir_fd
    or not hasattr(os, "O_DIRECTORY"),
    reason="descriptor-relative directory operations are unavailable",
)
def test_persist_final_report_anchors_creation_and_read_to_directory_fd(
    finalized_review, repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finalized = finalized_review[1]
    opened: list[tuple[object, int, int | None, int]] = []
    created: list[tuple[object, int, int | None]] = []
    real_open = orchestration.os.open
    real_mkdir = orchestration.os.mkdir

    def capture_open(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        opened.append((path, flags, kwargs.get("dir_fd"), descriptor))
        return descriptor

    def capture_mkdir(path, mode, *args, **kwargs):
        created.append((path, mode, kwargs.get("dir_fd")))
        return real_mkdir(path, mode, *args, **kwargs)

    monkeypatch.setattr(orchestration.os, "open", capture_open)
    monkeypatch.setattr(orchestration.os, "mkdir", capture_mkdir)
    handoff = orchestration.persist_final_report(
        finalized,
        author_repository=repository,
    )

    assert len(created) == 1
    directory_name, directory_mode, temp_root_fd = created[0]
    assert directory_mode == 0o700
    assert temp_root_fd is not None
    directory_open = next(
        item
        for item in opened
        if item[0] == directory_name and item[2] == temp_root_fd
    )
    directory_fd = directory_open[3]
    assert directory_open[1] & os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        assert directory_open[1] & os.O_NOFOLLOW

    report_opens = [item for item in opened if item[0] == b"final-report.md"]
    assert len(report_opens) == 2
    assert all(item[2] == directory_fd for item in report_opens)
    assert handoff.path.read_bytes() == finalized.report.encode("utf-8")


def _prepared_temp_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    directory = tmp_path / "prepared-report-root"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(
        orchestration.tempfile,
        "gettempdir",
        lambda **_: str(directory),
    )
    return directory


def test_persist_final_report_does_not_overwrite_preexisting_target(
    finalized_review, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finalized = finalized_review[1]
    temp_root = _prepared_temp_root(tmp_path, monkeypatch)
    original = b"pre-existing report"
    created: list[str] = []
    real_mkdir = orchestration.os.mkdir
    real_open = orchestration.os.open

    def capture_mkdir(path, mode, *args, **kwargs):
        created.append(path)
        return real_mkdir(path, mode, *args, **kwargs)

    planted = False

    def plant_target(path, flags, *args, **kwargs):
        nonlocal planted
        if not planted and path == b"final-report.md" and created:
            target = temp_root / created[0] / "final-report.md"
            target.write_bytes(original)
            planted = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(orchestration.os, "mkdir", capture_mkdir)
    monkeypatch.setattr(orchestration.os, "open", plant_target)

    with pytest.raises(orchestration.FinalReportHandoffError):
        orchestration.persist_final_report(finalized, author_repository=repository)

    assert planted
    target = temp_root / created[0] / "final-report.md"
    assert target.read_bytes() == original
    assert target.parent.exists()


@pytest.mark.skipif(
    os.open not in os.supports_dir_fd
    or os.stat not in os.supports_dir_fd
    or not hasattr(os, "O_DIRECTORY"),
    reason="descriptor-relative directory operations are unavailable",
)
def test_persist_final_report_parent_path_swap_cannot_redirect_creation(
    finalized_review, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finalized = finalized_review[1]
    temp_root = _prepared_temp_root(tmp_path, monkeypatch)
    initial_status = git(repository, "status", "--short")
    initial_head = git(repository, "rev-parse", "HEAD")
    created: list[str] = []
    real_mkdir = orchestration.os.mkdir
    swapped = False
    real_open = orchestration.os.open

    def capture_mkdir(path, mode, *args, **kwargs):
        created.append(path)
        return real_mkdir(path, mode, *args, **kwargs)

    def swap_parent_before_report_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and path == b"final-report.md" and created:
            swapped = True
            directory = temp_root / created[0]
            moved_directory = tmp_path / "moved-report-directory"
            directory.rename(moved_directory)
            directory.symlink_to(repository, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(orchestration.os, "mkdir", capture_mkdir)
    monkeypatch.setattr(orchestration.os, "open", swap_parent_before_report_open)
    with pytest.raises(
        orchestration.FinalReportHandoffError,
        match="temporary directory path changed",
    ):
        orchestration.persist_final_report(
            finalized,
            author_repository=repository,
        )

    assert swapped
    assert (temp_root / created[0]).is_symlink()
    assert not (repository / "final-report.md").exists()
    assert (
        tmp_path / "moved-report-directory" / "final-report.md"
    ).read_bytes() == finalized.report.encode("utf-8")
    assert git(repository, "status", "--short") == initial_status
    assert git(repository, "rev-parse", "HEAD") == initial_head


def test_persist_final_report_leaves_private_residue_after_post_create_fstat_failure(
    finalized_review, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finalized = finalized_review[1]
    temp_root = _prepared_temp_root(tmp_path, monkeypatch)
    created: list[str] = []
    real_mkdir = orchestration.os.mkdir
    real_fstat = orchestration.os.fstat
    calls = 0
    cleanup_calls: list[str] = []

    def capture_mkdir(path, mode, *args, **kwargs):
        created.append(path)
        return real_mkdir(path, mode, *args, **kwargs)

    def fail_report_fstat(descriptor: int):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected report fstat failure")
        return real_fstat(descriptor)

    def forbidden_cleanup(*_args, **_kwargs):
        cleanup_calls.append("cleanup")
        raise AssertionError("failure cleanup must not mutate a pathname")

    monkeypatch.setattr(orchestration.os, "mkdir", capture_mkdir)
    monkeypatch.setattr(orchestration.os, "fstat", fail_report_fstat)
    monkeypatch.setattr(orchestration.os, "unlink", forbidden_cleanup)
    monkeypatch.setattr(orchestration.os, "rmdir", forbidden_cleanup)
    with pytest.raises(orchestration.FinalReportHandoffError):
        orchestration.persist_final_report(
            finalized,
            author_repository=repository,
        )

    assert calls == 2
    assert len(created) == 1
    residue = temp_root / created[0]
    assert residue.is_dir()
    assert (residue / "final-report.md").read_bytes() == b""
    assert cleanup_calls == []


def test_persist_final_report_wraps_utf8_encoding_failure(
    repository: Path,
) -> None:
    finalized = orchestration.FinalizedReview(
        artifact=object(),
        exit_code=0,
        report="\ud800",
    )

    with pytest.raises(orchestration.FinalReportHandoffError) as failure:
        orchestration.persist_final_report(
            finalized,
            author_repository=repository,
        )

    assert isinstance(failure.value.__cause__, UnicodeEncodeError)
    assert finalized.report == "\ud800"


def test_persist_final_report_rejects_temporary_directory_inside_repository(
    finalized_review, repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finalized = finalized_review[1]
    directory = repository / ".report-root"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(orchestration.tempfile, "gettempdir", lambda: str(directory))

    with pytest.raises(
        orchestration.FinalReportHandoffError,
        match="inside the author repository",
    ):
        orchestration.persist_final_report(finalized, author_repository=repository)

    assert not (directory / "final-report.md").exists()


@pytest.mark.skipif(os.name != "posix", reason="hardlink and symlink fixture is POSIX-only")
def test_persist_final_report_cannot_mutate_hardlink_or_follow_symlink(
    finalized_review, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finalized = finalized_review[1]
    protected = repository / "protected.txt"
    original = b"author inode must remain unchanged"
    protected.write_bytes(original)

    for target_kind in ("hardlink", "symlink"):
        directory = tmp_path / f"{target_kind}-root"
        directory.mkdir(mode=0o700)
        created: list[str] = []
        planted = False
        real_mkdir = orchestration.os.mkdir
        real_open = orchestration.os.open

        def capture_mkdir(path, mode, *args, **kwargs):
            created.append(path)
            return real_mkdir(path, mode, *args, **kwargs)

        def plant_target(path, flags, *args, **kwargs):
            nonlocal planted
            if not planted and path == b"final-report.md" and created:
                target = directory / created[0] / "final-report.md"
                if target_kind == "hardlink":
                    os.link(protected, target)
                else:
                    target.symlink_to(protected)
                planted = True
            return real_open(path, flags, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(orchestration.tempfile, "gettempdir", lambda: str(directory))
            patch.setattr(orchestration.os, "mkdir", capture_mkdir)
            patch.setattr(orchestration.os, "open", plant_target)
            with pytest.raises(orchestration.FinalReportHandoffError):
                orchestration.persist_final_report(finalized, author_repository=repository)

        assert planted
        target = directory / created[0] / "final-report.md"
        assert protected.read_bytes() == original
        assert target.is_symlink() if target_kind == "symlink" else target.read_bytes() == original


def test_persist_final_report_failure_does_not_delete_replacement_directory(
    finalized_review, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finalized = finalized_review[1]
    temp_root = _prepared_temp_root(tmp_path, monkeypatch)
    created: list[str] = []
    real_mkdir = orchestration.os.mkdir

    def capture_mkdir(path, mode, *args, **kwargs):
        if kwargs.get("dir_fd") is not None:
            created.append(path)
        return real_mkdir(path, mode, *args, **kwargs)

    def swap_directory_then_fail(_descriptor: int) -> None:
        directory = temp_root / created[0]
        moved_directory = tmp_path / "moved-report-directory"
        directory.rename(moved_directory)
        directory.mkdir(mode=0o700)
        raise OSError("injected report persistence failure")

    cleanup_calls: list[str] = []

    def forbidden_cleanup(*_args, **_kwargs):
        cleanup_calls.append("cleanup")
        raise AssertionError("failure cleanup must not mutate a pathname")

    monkeypatch.setattr(orchestration.os, "mkdir", capture_mkdir)
    monkeypatch.setattr(orchestration.os, "fsync", swap_directory_then_fail)
    monkeypatch.setattr(orchestration.os, "unlink", forbidden_cleanup)
    monkeypatch.setattr(orchestration.os, "rmdir", forbidden_cleanup)

    with pytest.raises(orchestration.FinalReportHandoffError):
        orchestration.persist_final_report(finalized, author_repository=repository)

    assert len(created) == 1
    assert (temp_root / created[0]).is_dir()
    assert (tmp_path / "moved-report-directory" / "final-report.md").read_bytes() == finalized.report.encode("utf-8")
    assert cleanup_calls == []


def test_persist_final_report_failure_does_not_delete_replacement_report_file(
    finalized_review, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finalized = finalized_review[1]
    temp_root = _prepared_temp_root(tmp_path, monkeypatch)
    created: list[str] = []
    real_mkdir = orchestration.os.mkdir

    def capture_mkdir(path, mode, *args, **kwargs):
        if kwargs.get("dir_fd") is not None:
            created.append(path)
        return real_mkdir(path, mode, *args, **kwargs)

    def replace_report_then_fail(_descriptor: int) -> None:
        directory = temp_root / created[0]
        report = directory / "final-report.md"
        moved_report = tmp_path / "moved-report.md"
        report.rename(moved_report)
        report.write_bytes(b"replacement report")
        raise OSError("injected report persistence failure")

    cleanup_calls: list[str] = []

    def forbidden_cleanup(*_args, **_kwargs):
        cleanup_calls.append("cleanup")
        raise AssertionError("failure cleanup must not mutate a pathname")

    monkeypatch.setattr(orchestration.os, "mkdir", capture_mkdir)
    monkeypatch.setattr(orchestration.os, "fsync", replace_report_then_fail)
    monkeypatch.setattr(orchestration.os, "unlink", forbidden_cleanup)
    monkeypatch.setattr(orchestration.os, "rmdir", forbidden_cleanup)

    with pytest.raises(orchestration.FinalReportHandoffError):
        orchestration.persist_final_report(finalized, author_repository=repository)

    assert len(created) == 1
    assert (temp_root / created[0] / "final-report.md").read_bytes() == b"replacement report"
    assert (tmp_path / "moved-report.md").read_bytes() == finalized.report.encode("utf-8")
    assert cleanup_calls == []


def test_persist_final_report_retries_directory_name_collision(
    finalized_review, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finalized = finalized_review[1]
    temp_root = _prepared_temp_root(tmp_path, monkeypatch)
    collision = temp_root / "pre-pr-verify-report-collision"
    collision.mkdir(mode=0o700)
    tokens = iter(("collision", "unique"))
    monkeypatch.setattr(orchestration.secrets, "token_hex", lambda _size: next(tokens))

    handoff = orchestration.persist_final_report(finalized, author_repository=repository)

    assert handoff.path.parent.name == "pre-pr-verify-report-unique"
    assert collision.exists()


def test_persist_final_report_preserves_original_failure_without_cleanup(
    finalized_review, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finalized = finalized_review[1]
    _prepared_temp_root(tmp_path, monkeypatch)

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("original persistence failure")

    cleanup_calls: list[str] = []

    def forbidden_cleanup(*_args, **_kwargs) -> None:
        cleanup_calls.append("cleanup")
        raise AssertionError("failure cleanup must not mutate a pathname")

    monkeypatch.setattr(orchestration.os, "fsync", fail_fsync)
    monkeypatch.setattr(orchestration.os, "unlink", forbidden_cleanup)
    monkeypatch.setattr(orchestration.os, "rmdir", forbidden_cleanup)

    with pytest.raises(orchestration.FinalReportHandoffError) as failure:
        orchestration.persist_final_report(finalized, author_repository=repository)

    assert isinstance(failure.value.__cause__, OSError)
    assert str(failure.value.__cause__) == "original persistence failure"
    assert cleanup_calls == []


@pytest.mark.parametrize("fail_during_write", [False, True])
def test_persist_final_report_closes_all_descriptors(
    finalized_review,
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_during_write: bool,
) -> None:
    finalized = finalized_review[1]
    _prepared_temp_root(tmp_path, monkeypatch)
    opened: list[int] = []
    real_open = orchestration.os.open

    def capture_open(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(orchestration.os, "open", capture_open)
    if fail_during_write:
        monkeypatch.setattr(
            orchestration.os,
            "fsync",
            lambda _fd: (_ for _ in ()).throw(OSError("injected")),
        )

    if fail_during_write:
        with pytest.raises(orchestration.FinalReportHandoffError):
            orchestration.persist_final_report(finalized, author_repository=repository)
    else:
        orchestration.persist_final_report(finalized, author_repository=repository)

    assert opened
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_persist_final_report_failure_leaves_residue_and_never_reexecutes(
    finalized_review, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finalized = finalized_review[1]
    temp_root = _prepared_temp_root(tmp_path, monkeypatch)
    created: list[str] = []
    real_mkdir = orchestration.os.mkdir

    def capture_mkdir(path, mode, *args, **kwargs):
        created.append(path)
        return real_mkdir(path, mode, *args, **kwargs)

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("injected report persistence failure")

    cleanup_calls: list[str] = []

    def forbidden_cleanup(*_args, **_kwargs):
        cleanup_calls.append("cleanup")
        raise AssertionError("failure cleanup must not mutate a pathname")

    monkeypatch.setattr(orchestration.os, "mkdir", capture_mkdir)
    monkeypatch.setattr(orchestration.os, "fsync", fail_fsync)
    monkeypatch.setattr(orchestration.os, "unlink", forbidden_cleanup)
    monkeypatch.setattr(orchestration.os, "rmdir", forbidden_cleanup)
    monkeypatch.setattr(
        orchestration,
        "execute_verification_plan",
        lambda *args, **kwargs: pytest.fail("report handoff must not re-execute verification"),
    )

    with pytest.raises(orchestration.FinalReportHandoffError):
        orchestration.persist_final_report(finalized, author_repository=repository)

    assert created
    assert (temp_root / created[0] / "final-report.md").read_bytes() == finalized.report.encode("utf-8")
    assert cleanup_calls == []


def test_canonical_full_review_lifecycle_is_deterministic(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial_status = git(repository, "status", "--short")
    setup = PreReviewSetup(interactive=True, recommended_scope_number=1)

    scope_step = orchestration.prepare_review(setup)
    assert scope_step.phase is SetupPhase.SCOPE
    assert [choice.value for choice in scope_step.choices] == [
        "working-changes",
        "current-branch",
        "since-commit",
        "custom",
    ]
    assert orchestration.record_setup_answer(setup, 1) is None
    assert setup.phase is SetupPhase.REQUIREMENTS

    options = discover_scope_options(repository)
    resolved = resolve_scope_selection(
        options,
        interactive=True,
        intent=ScopeIntent.WORKING_CHANGES,
    )
    setup.bind_scope(resolved)
    changeset = capture_resolved_scope(resolved)
    discovery = discover_review_sources(
        repository,
        explicit_specs=(
            ProvidedRequirement(
                label="Full lifecycle acceptance criteria",
                content="The canonical lifecycle must be deterministic end to end.",
            ),
        ),
    )
    candidates_by_id = {source.source_id: source for source in discovery.sources}
    setup.set_requirement_candidates(
        tuple(
            RequirementCandidate(
                source_id=source_id,
                label=candidates_by_id[source_id].label,
            )
            for source_id in discovery.requirement_resolution.candidate_source_ids
        )
    )

    requirement_step = orchestration.prepare_review(setup)
    assert requirement_step.phase is SetupPhase.REQUIREMENTS
    assert requirement_step.candidate_count == 1
    assert orchestration.record_setup_answer(setup, 1) is None
    assert setup.phase is SetupPhase.VERIFICATION

    plan = plan_for(changeset, discovery)
    verification_step = orchestration.prepare_review(setup)
    assert verification_step.phase is SetupPhase.VERIFICATION
    assert verification_step.choices[0].value == "authorize"
    assert orchestration.record_setup_answer(setup, 1) is None
    assert setup.phase is SetupPhase.FINAL_CONFIRMATION

    execution_capability = capability()
    authorization = orchestration.authorize_verification_plan(
        setup,
        changeset,
        discovery,
        plan,
        execution_capability,
        timeout_seconds=2,
        output_limit_bytes=4_096,
        required_capabilities=(CapabilityName.OUTPUT_LIMITS,),
    )
    final_step = orchestration.prepare_review(setup)
    assert final_step.phase is SetupPhase.FINAL_CONFIRMATION
    assert final_step.choices[0].value == "yes"
    assert orchestration.record_setup_answer(setup, "yes") is None
    setup.require_ready_to_review(current_scope=resolved)

    executor_calls = 0
    execute = orchestration.execute_verification_plan

    def counted_execute(*args, **kwargs):
        nonlocal executor_calls
        executor_calls += 1
        return execute(*args, **kwargs)

    monkeypatch.setattr(orchestration, "execute_verification_plan", counted_execute)
    evidence_path = tmp_path / "verifier-run" / "verification-evidence.json"
    evidence = orchestration.execute_authorized_plan(
        setup,
        changeset,
        discovery,
        plan,
        execution_capability,
        authorization,
        evidence_path=evidence_path,
    )

    with pytest.raises(RuntimeError, match="presentation failure"):
        raise RuntimeError("presentation failure")

    persisted_evidence_path = persisted_path(evidence_path, authorization, changeset)
    reused_evidence = orchestration.load_completed_execution(
        persisted_evidence_path,
        changeset,
        discovery,
        plan,
        authorization=authorization,
    )
    assert reused_evidence == evidence
    assert executor_calls == 1
    assert len(evidence.executions) == 1
    assert persisted_evidence_path.is_file()

    assessment = build_semantic_assessment(
        changeset,
        discovery,
        plan,
        reused_evidence,
        axes=tuple(
            SemanticAxisAssessment(
                axis=axis,
                status=SemanticStatus.PASS,
                rationale=f"Fixture inspection passed for {axis.value}.",
            )
            for axis in SemanticAxis
        ),
    )
    finalized = orchestration.finalize_review(
        changeset,
        discovery,
        plan,
        reused_evidence,
        assessment,
        verifier_version="0.1.7",
        verifier_commit_or_build="full-lifecycle-fixture",
    )
    emitted = io.StringIO()
    orchestration.emit_final_report(finalized, stream=emitted)

    assert emitted.getvalue() == finalized.report
    for expected in (
        "# PrePR Verify Report",
        "Verdict: **READY**",
        "## Axes",
        "## Semantic Review",
        "### Spec — **PASS**",
        "### Standards — **PASS**",
        "### Impact — **PASS**",
        "### Test Sufficiency — **PASS**",
        "### Contextual Security — **PASS**",
        "## Verification",
        "## Blocking findings",
        "## Non-blocking and unverified findings",
        "## Required evidence gaps",
        "## Artifact references",
        "Canonical audit identities are retained in the machine artifacts.",
    ):
        assert expected in emitted.getvalue()
    for identity in (
        finalized.artifact.identity,
        changeset.identity,
        discovery.identity,
        plan.identity,
        reused_evidence.identity,
        assessment.identity,
    ):
        assert identity not in emitted.getvalue()
    assert capture_resolved_scope(resolved).identity == changeset.identity
    assert git(repository, "status", "--short") == initial_status
    assert not (repository / "verification-evidence.json").exists()
