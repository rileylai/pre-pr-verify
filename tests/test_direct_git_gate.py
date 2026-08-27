from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import pre_pr_verify.executor as executor_module
from pre_pr_verify.discovery import discover_review_sources
from pre_pr_verify.executor import (
    _classify_direct_git_request,
    execute_verification_plan,
)
from pre_pr_verify.git_capture import capture_changeset
from pre_pr_verify.models import ScopeMode
from pre_pr_verify.verification import (
    build_verification_plan,
    discover_canonical_checks,
)
from pre_pr_verify.verification_models import (
    CapabilityName,
    EnvironmentProfile,
    ExecutionCapability,
    ExecutionStatus,
    FailureKind,
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


def capability() -> ExecutionCapability:
    return ExecutionCapability(
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


def repository(
    tmp_path: Path,
    argv: list[str],
    *,
    profile: EnvironmentProfile = EnvironmentProfile.GIT_REPOSITORY,
    level: str = "required",
) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "pyproject.toml").write_text(
        "[tool.pre-pr-verify.verification]\nchecks = [\n"
        "  { id = \"direct-git\", level = "
        + json.dumps(level)
        + ", argv = "
        + json.dumps(argv)
        + ", environment_profile = "
        + json.dumps(profile.value)
        + " },\n]\n"
    )
    (repo / "tracked.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "base")
    return repo


def plan_for(repo: Path):
    changeset = capture_changeset(repo, "main", ScopeMode.PENDING)
    discovery = discover_review_sources(repo)
    plan = build_verification_plan(
        changeset,
        discovery,
        canonical_checks=discover_canonical_checks(repo),
        trusted_policy_checks=[],
        planner_additions=[],
    )
    return changeset, discovery, plan


def execute(repo: Path, changeset, discovery, plan):
    return execute_verification_plan(
        changeset,
        discovery,
        plan,
        capability(),
        timeout_seconds=2,
        output_limit_bytes=4096,
        required_capabilities=[CapabilityName.OUTPUT_LIMITS],
    )


@pytest.mark.parametrize(
    ("argv", "classification"),
    [
        (["git", "rev-parse", "HEAD"], "supported_bounded_git"),
        (["/usr/bin/git", "rev-parse", "--show-toplevel"], "supported_bounded_git"),
        (["git", "ls-files"], "supported_bounded_git"),
        (["git", "ls-files", "tracked.txt"], "supported_bounded_git"),
        (["git", "ls-files", "--"], "supported_bounded_git"),
        (["git", "ls-files", "--", "-literal"], "supported_bounded_git"),
        (["git", "status"], "supported_bounded_git"),
        (["git", "status", "--porcelain"], "supported_bounded_git"),
        (["git", "status", "--porcelain=v1"], "supported_bounded_git"),
        (["git", "status", "--short"], "supported_bounded_git"),
        (["git", "diff"], "supported_bounded_git"),
        (["git", "diff", "--cached"], "supported_bounded_git"),
        (["git", "diff", "--", "tracked.txt"], "supported_bounded_git"),
        (["git", "diff", "--cached", "--", "tracked.txt"], "supported_bounded_git"),
        (["git", "log", "-1"], "unsupported_bounded_git"),
        (["git", "describe", "--tags"], "unsupported_bounded_git"),
        (["git", "status", "--branch"], "unsupported_bounded_git"),
        (["git", "ls-files", "--stage"], "unsupported_bounded_git"),
        (["python", "-c", "import subprocess; subprocess.run(['git', 'log'])"], "not_direct_git"),
        (["git", "-C", "/tmp/other", "status"], "prohibited_profile_override"),
        (["git", "-C/tmp/other", "status"], "prohibited_profile_override"),
        (["git", "--git-dir=/tmp/other/.git", "status"], "prohibited_profile_override"),
        (["git", "--work-tree", "/tmp/other", "status"], "prohibited_profile_override"),
        (["git", "--namespace=other", "status"], "prohibited_profile_override"),
        (["git", "-c", "core.hooksPath=/tmp", "status"], "prohibited_profile_override"),
        (["git", "--config-env=foo=BAR", "status"], "prohibited_profile_override"),
    ],
)
def test_direct_git_classifier_is_tiny_and_conservative(
    argv: list[str], classification: str
) -> None:
    assert _classify_direct_git_request(argv) == classification


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "rev-parse", "HEAD"],
        ["git", "rev-parse", "--show-toplevel"],
        ["git", "ls-files"],
        ["git", "ls-files", "tracked.txt"],
        ["git", "ls-files", "--"],
        ["git", "ls-files", "--", "tracked.txt"],
        ["git", "status"],
        ["git", "status", "--porcelain"],
        ["git", "status", "--porcelain=v1"],
        ["git", "status", "--short"],
        ["git", "diff"],
        ["git", "diff", "--cached"],
        ["git", "diff", "--", "tracked.txt"],
        ["git", "diff", "--cached", "--", "tracked.txt"],
    ],
)
def test_supported_direct_git_forms_execute_in_git_profile(
    tmp_path: Path, argv: list[str]
) -> None:
    repo = repository(tmp_path, argv)
    changeset, discovery, plan = plan_for(repo)

    evidence = execute(repo, changeset, discovery, plan)
    result = evidence.executions[0].result

    assert result.status is ExecutionStatus.PASSED
    assert result.failure_kind is None
    assert result.request.environment_profile is EnvironmentProfile.GIT_REPOSITORY
    assert evidence.executions[0].snapshot.complete is True
    assert evidence.executions[0].snapshot.environment_profile is EnvironmentProfile.GIT_REPOSITORY


def test_unsupported_direct_git_is_not_run_before_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "child-ran"
    repo = repository(tmp_path, ["git", "log", "-1"])
    changeset, discovery, plan = plan_for(repo)

    def materialization_must_not_start(*args, **kwargs):
        pytest.fail("unsupported direct Git was materialized")

    monkeypatch.setattr(
        executor_module, "disposable_git_snapshot", materialization_must_not_start
    )
    # The marker is intentionally never used by the command: NOT_RUN is the
    # evidence that the rejected direct request did not spawn a child.
    assert not marker.exists()
    evidence = execute(repo, changeset, discovery, plan)
    result = evidence.executions[0].result
    snapshot = evidence.executions[0].snapshot

    assert result.status is ExecutionStatus.NOT_RUN
    assert result.failure_kind is FailureKind.CAPABILITY
    assert result.required_evidence_gap is True
    assert result.request.snapshot_validation_failure is FailureKind.CAPABILITY
    assert snapshot.complete is False
    assert snapshot.materialization_failure is FailureKind.CAPABILITY
    assert snapshot.environment_profile is EnvironmentProfile.GIT_REPOSITORY
    assert not marker.exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "-C", "/tmp/other", "status"],
        ["git", "--git-dir=/tmp/other/.git", "status"],
        ["git", "--work-tree", "/tmp/other", "status"],
        ["git", "--namespace=other", "status"],
        ["git", "-c", "core.hooksPath=/tmp", "status"],
        ["git", "--config-env=foo=BAR", "status"],
    ],
)
def test_direct_git_configuration_escape_is_not_run(
    tmp_path: Path, argv: list[str]
) -> None:
    repo = repository(tmp_path, argv)
    changeset, discovery, plan = plan_for(repo)

    evidence = execute(repo, changeset, discovery, plan)
    result = evidence.executions[0].result

    assert result.status is ExecutionStatus.NOT_RUN
    assert result.failure_kind is FailureKind.CONFIGURATION
    assert result.required_evidence_gap is True


def test_indirect_git_nonzero_is_unclassified_without_explicit_attribution(
    tmp_path: Path,
) -> None:
    command = [
        sys.executable,
        "-c",
        (
            "import subprocess; "
            "subprocess.run(['git', 'describe', '--tags'], check=False); "
            "raise SystemExit(7)"
        ),
    ]
    repo = repository(tmp_path, command)
    changeset, discovery, plan = plan_for(repo)

    evidence = execute(repo, changeset, discovery, plan)
    result = evidence.executions[0].result

    assert result.status is ExecutionStatus.FAILED
    assert result.failure_kind is FailureKind.UNCLASSIFIED
    assert result.exit_code == 7
    assert result.required_evidence_gap is True


def test_filesystem_only_direct_git_nonzero_is_unclassified_gap(tmp_path: Path) -> None:
    repo = repository(
        tmp_path,
        ["git", "status"],
        profile=EnvironmentProfile.FILESYSTEM_ONLY,
    )
    changeset, discovery, plan = plan_for(repo)

    evidence = execute(repo, changeset, discovery, plan)
    result = evidence.executions[0].result

    assert result.request.environment_profile is EnvironmentProfile.FILESYSTEM_ONLY
    assert evidence.executions[0].snapshot.environment_profile is EnvironmentProfile.FILESYSTEM_ONLY
    assert evidence.executions[0].snapshot.complete is True
    assert result.status is ExecutionStatus.FAILED
    assert result.failure_kind is FailureKind.UNCLASSIFIED
    assert result.required_evidence_gap is True


def test_advisory_unsupported_direct_git_is_not_run_without_required_gap(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path, ["git", "log"], level="advisory")
    changeset, discovery, plan = plan_for(repo)

    evidence = execute(repo, changeset, discovery, plan)
    result = evidence.executions[0].result

    assert plan.checks[-1].requirement_level is RequirementLevel.ADVISORY
    assert result.status is ExecutionStatus.NOT_RUN
    assert result.failure_kind is FailureKind.CAPABILITY
    assert result.required_evidence_gap is False


def test_direct_gate_result_round_trips_with_bound_gap(tmp_path: Path) -> None:
    repo = repository(tmp_path, ["git", "show", "HEAD"])
    changeset, discovery, plan = plan_for(repo)

    evidence = execute(repo, changeset, discovery, plan)

    assert evidence.model_validate_json(evidence.model_dump_json()) == evidence
