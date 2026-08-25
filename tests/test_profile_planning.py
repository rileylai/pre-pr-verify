from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pre_pr_verify.discovery import discover_review_sources
from pre_pr_verify.errors import PreflightError
from pre_pr_verify.git_capture import capture_changeset
from pre_pr_verify.models import ScopeMode
from pre_pr_verify.verification import (
    PlannerCheckInput,
    RepositoryCheckInput,
    TrustedPolicyCheckInput,
    build_verification_plan,
    discover_canonical_checks,
)
from pre_pr_verify.verification_models import (
    CheckOrigin,
    EnvironmentProfile,
    ProfileProvenanceChannel,
)


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


@pytest.fixture
def review_context(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "README.md").write_text("# Fixture\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    (repo / "changed.py").write_text("value = 2\n")
    return (
        repo,
        capture_changeset(repo, "main", ScopeMode.PENDING),
        discover_review_sources(repo),
    )


def repo_check(
    check_id: str,
    profile: EnvironmentProfile = EnvironmentProfile.FILESYSTEM_ONLY,
) -> RepositoryCheckInput:
    return RepositoryCheckInput(
        check_id=check_id,
        requirement_level="required",
        selection_reason="Repository canonical check.",
        argv=("python", "-c", "pass"),
        cwd=".",
        source_path=b"pyproject.toml",
        source_sha256=("a" if check_id == "a-check" else "b") * 64,
        source_size=128,
        environment_profile=profile,
    )


def trusted_check(
    check_id: str,
    profile: EnvironmentProfile = EnvironmentProfile.FILESYSTEM_ONLY,
) -> TrustedPolicyCheckInput:
    return TrustedPolicyCheckInput(
        check_id=check_id,
        requirement_level="required",
        selection_reason="Trusted policy check.",
        argv=("python", "-c", "pass"),
        policy_label="trusted policy",
        policy_sha256=("c" if check_id == "a-check" else "d") * 64,
        environment_profile=profile,
    )


def model_check(
    check_id: str,
    profile: EnvironmentProfile = EnvironmentProfile.FILESYSTEM_ONLY,
) -> PlannerCheckInput:
    return PlannerCheckInput(
        check_id=check_id,
        requirement_level="required",
        selection_reason="Model-proposed check.",
        argv=("python", "-c", "pass"),
        environment_profile=profile,
        profile_source_sha256=("e" if check_id == "a-check" else "f") * 64,
    )


def plan_for(
    context,
    *,
    canonical=(),
    trusted=(),
    proposed=(),
    minimum=EnvironmentProfile.FILESYSTEM_ONLY,
):
    _repo, changeset, discovery = context
    return build_verification_plan(
        changeset,
        discovery,
        canonical_checks=canonical,
        trusted_policy_checks=trusted,
        planner_additions=proposed,
        minimum_environment_profile=minimum,
    )


def planned_command(plan, check_id: str = "a-check"):
    return next(check for check in plan.checks if check.check_id == check_id)


def test_repository_declaration_parses_profile_without_granting_authority(
    review_context,
) -> None:
    repo, _changeset, _discovery = review_context
    (repo / "pyproject.toml").write_text(
        """
[tool.pre-pr-verify.verification]
checks = [
  { id = "repo-check", level = "required", argv = ["python", "-c", "pass"], environment_profile = "GIT_REPOSITORY" },
]
""".strip()
        + "\n"
    )

    discovered = discover_canonical_checks(repo)
    assert discovered[0].environment_profile is EnvironmentProfile.GIT_REPOSITORY

    plan = plan_for(review_context, canonical=discovered)
    check = planned_command(plan, "repo-check")
    assert check.origin is CheckOrigin.REPOSITORY_CANONICAL
    assert check.profile_provenance[0].channel is ProfileProvenanceChannel.REPOSITORY_DECLARATION
    assert not hasattr(check, "required_capabilities")


def test_invalid_repository_profile_is_rejected(review_context) -> None:
    repo, _changeset, _discovery = review_context
    (repo / "pyproject.toml").write_text(
        """
[tool.pre-pr-verify.verification]
checks = [{ id = "repo-check", level = "required", argv = ["python", "-c", "pass"], environment_profile = "UNKNOWN" }]
""".strip()
        + "\n"
    )

    with pytest.raises(PreflightError, match="canonical verification check"):
        discover_canonical_checks(repo)


def test_all_independent_git_raisers_are_retained_canonically(review_context) -> None:
    plan = plan_for(
        review_context,
        canonical=[repo_check("a-check", EnvironmentProfile.GIT_REPOSITORY)],
        trusted=[trusted_check("a-check", EnvironmentProfile.GIT_REPOSITORY)],
        proposed=[model_check("a-check", EnvironmentProfile.GIT_REPOSITORY)],
    )

    check = planned_command(plan)
    assert check.environment_profile is EnvironmentProfile.GIT_REPOSITORY
    assert [entry.channel for entry in check.profile_provenance] == [
        ProfileProvenanceChannel.REPOSITORY_DECLARATION,
        ProfileProvenanceChannel.TRUSTED_POLICY,
        ProfileProvenanceChannel.MODEL_PROPOSAL,
    ]


def test_lower_requirements_cannot_downgrade_git(review_context) -> None:
    plan = plan_for(
        review_context,
        canonical=[repo_check("a-check", EnvironmentProfile.GIT_REPOSITORY)],
        trusted=[trusted_check("a-check")],
        proposed=[model_check("a-check")],
    )

    check = planned_command(plan)
    assert check.environment_profile is EnvironmentProfile.GIT_REPOSITORY
    assert [entry.channel for entry in check.profile_provenance] == [
        ProfileProvenanceChannel.REPOSITORY_DECLARATION,
    ]


def test_irrelevant_filesystem_requirements_are_not_provenance_raisers(
    review_context,
) -> None:
    plan = plan_for(
        review_context,
        canonical=[repo_check("a-check")],
        trusted=[trusted_check("a-check")],
        proposed=[model_check("a-check")],
    )

    check = planned_command(plan)
    assert check.environment_profile is EnvironmentProfile.FILESYSTEM_ONLY
    assert check.profile_provenance == []


def test_duplicate_channel_requirements_fail(review_context) -> None:
    first = trusted_check("a-check", EnvironmentProfile.GIT_REPOSITORY)
    second = trusted_check("a-check", EnvironmentProfile.GIT_REPOSITORY)

    with pytest.raises(PreflightError, match="profile requirement"):
        plan_for(review_context, trusted=[first, second])


def test_review_profile_requirement_order_is_deterministic(review_context) -> None:
    first = plan_for(
        review_context,
        canonical=[
            repo_check("b-check", EnvironmentProfile.GIT_REPOSITORY),
            repo_check("a-check", EnvironmentProfile.GIT_REPOSITORY),
        ],
        trusted=[trusted_check("b-check"), trusted_check("a-check")],
        proposed=[model_check("b-check"), model_check("a-check")],
    )
    second = plan_for(
        review_context,
        canonical=[
            repo_check("a-check", EnvironmentProfile.GIT_REPOSITORY),
            repo_check("b-check", EnvironmentProfile.GIT_REPOSITORY),
        ],
        trusted=[trusted_check("a-check"), trusted_check("b-check")],
        proposed=[model_check("a-check"), model_check("b-check")],
    )

    assert first.identity == second.identity
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_adding_or_removing_a_git_raiser_changes_plan_identity(review_context) -> None:
    base = plan_for(review_context, canonical=[repo_check("a-check")])
    raised = plan_for(
        review_context,
        canonical=[repo_check("a-check")],
        trusted=[trusted_check("a-check", EnvironmentProfile.GIT_REPOSITORY)],
    )
    removed = plan_for(review_context, canonical=[repo_check("a-check")])

    assert base.identity != raised.identity
    assert raised.identity != removed.identity
    assert planned_command(raised).environment_profile is EnvironmentProfile.GIT_REPOSITORY
    assert planned_command(removed).environment_profile is EnvironmentProfile.FILESYSTEM_ONLY


def test_review_level_git_floor_raises_every_command(review_context) -> None:
    plan = plan_for(
        review_context,
        proposed=[model_check("a-check"), model_check("b-check")],
        minimum=EnvironmentProfile.GIT_REPOSITORY,
    )

    commands = [check for check in plan.checks if check.kind.value == "command"]
    assert commands
    assert all(
        check.environment_profile is EnvironmentProfile.GIT_REPOSITORY
        for check in commands
    )
    assert all(
        any(
            entry.channel is ProfileProvenanceChannel.USER_INVOCATION
            for entry in check.profile_provenance
        )
        for check in commands
    )


def test_filesystem_floor_does_not_lower_stronger_check(review_context) -> None:
    plan = plan_for(
        review_context,
        canonical=[repo_check("a-check", EnvironmentProfile.GIT_REPOSITORY)],
        minimum=EnvironmentProfile.FILESYSTEM_ONLY,
    )

    assert planned_command(plan).environment_profile is EnvironmentProfile.GIT_REPOSITORY


def test_default_planning_remains_filesystem_only(review_context) -> None:
    plan = plan_for(review_context, proposed=[model_check("a-check")])

    assert all(
        check.environment_profile is EnvironmentProfile.FILESYSTEM_ONLY
        for check in plan.checks
    )
    assert all(not check.profile_provenance for check in plan.checks)
