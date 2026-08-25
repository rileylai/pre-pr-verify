from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import pre_pr_verify.snapshot as snapshot_module
from pre_pr_verify.discovery import discover_review_sources
from pre_pr_verify.git_capture import capture_changeset
from pre_pr_verify.models import ContentLimits, ScopeMode
from pre_pr_verify.snapshot import disposable_git_snapshot
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
    VerificationEvidence,
)
from pre_pr_verify.executor import execute_verification_plan


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
    check_id: str = "git-check",
    second: tuple[str, list[str]] | None = None,
) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    checks = [(check_id, argv)]
    if second is not None:
        second_id, second_argv = second
        checks.append((second_id, second_argv))
    (repo / "pyproject.toml").write_text(
        "[tool.pre-pr-verify.verification]\nchecks = [\n"
        + "\n".join(
            "  { id = "
            + json.dumps(check_id)
            + ", level = \"required\", argv = "
            + json.dumps(check_argv)
            + ", environment_profile = "
            + json.dumps(profile.value)
            + " },"
            for check_id, check_argv in checks
        )
        + "\n]\n"
    )
    (repo / "tracked.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "base")
    return repo


def plan_for(
    repo: Path,
    *,
    limits: ContentLimits | None = None,
):
    changeset = capture_changeset(
        repo,
        "main",
        ScopeMode.PENDING,
        limits=limits,
    )
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


def test_git_profile_runs_indirect_git_dependent_check_and_preserves_layers(
    tmp_path: Path,
) -> None:
    command = [
        sys.executable,
        "-c",
        (
            "import subprocess; from pathlib import Path; "
            "assert Path(subprocess.check_output(['git','rev-parse','--show-toplevel'], text=True).strip()).resolve() == Path.cwd().resolve(); "
            "assert len(subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip()) in (40,64); "
            "assert 'tracked.txt' in subprocess.check_output(['git','ls-files'], text=True); "
            "assert subprocess.check_output(['git','show',':tracked.txt'], text=True) == 'staged\\n'; "
            "assert Path('tracked.txt').read_text() == 'working\\n'"
        ),
    ]
    repo = repository(tmp_path, command)
    (repo / "tracked.txt").write_text("staged\n")
    git(repo, "add", "tracked.txt")
    (repo / "tracked.txt").write_text("working\n")
    head = git(repo, "rev-parse", "HEAD")
    source_git = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    source_index = (source_git / "index").read_bytes()
    source_config = (source_git / "config").read_bytes()
    changeset, discovery, plan = plan_for(repo)

    evidence = execute(repo, changeset, discovery, plan)
    result = evidence.executions[0].result
    assert result.request.environment_profile is EnvironmentProfile.GIT_REPOSITORY
    assert result.status is ExecutionStatus.PASSED
    assert result.failure_kind is None
    assert result.required_evidence_gap is False
    assert evidence.executions[0].snapshot.environment_profile is EnvironmentProfile.GIT_REPOSITORY
    assert git(repo, "rev-parse", "HEAD") == head
    assert (source_git / "index").read_bytes() == source_index
    assert (source_git / "config").read_bytes() == source_config


def test_each_git_check_gets_a_fresh_repository(tmp_path: Path) -> None:
    first = [
        sys.executable,
        "-c",
        "from pathlib import Path; Path('.git/child-marker').write_text('one')",
    ]
    second = [
        sys.executable,
        "-c",
        "from pathlib import Path; assert not Path('.git/child-marker').exists()",
    ]
    repo = repository(tmp_path, first, check_id="a-mutates", second=("b-fresh", second))
    changeset, discovery, plan = plan_for(repo)

    evidence = execute(repo, changeset, discovery, plan)
    assert [item.result.status for item in evidence.executions] == [
        ExecutionStatus.PASSED,
        ExecutionStatus.PASSED,
    ]
    assert len({item.snapshot.identity for item in evidence.executions}) == 2
    assert not (repo / ".git" / "child-marker").exists()


@pytest.mark.parametrize("gap", ["omitted", "gitlink", "budget"])
def test_git_materialization_gap_is_bound_not_run_and_cleaned(
    tmp_path: Path, gap: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "child-ran"
    command = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
    ]
    repo = repository(tmp_path, command)
    limits = None
    if gap == "omitted":
        (repo / "tracked.txt").write_text("too large\n")
        limits = ContentLimits(per_file_bytes=0, total_bytes=0)
    elif gap == "gitlink":
        head = git(repo, "rev-parse", "HEAD")
        git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},submodule")
    elif gap == "budget":
        monkeypatch.setattr(snapshot_module, "MAX_TRACKED_ENTRIES", 0)
    changeset, discovery, plan = plan_for(repo, limits=limits)

    evidence = execute(repo, changeset, discovery, plan)
    result = evidence.executions[0].result
    snapshot = evidence.executions[0].snapshot
    assert result.status is ExecutionStatus.NOT_RUN
    assert result.failure_kind is FailureKind.CAPABILITY
    assert result.required_evidence_gap is True
    assert snapshot.environment_profile is EnvironmentProfile.GIT_REPOSITORY
    assert snapshot.complete is False
    assert snapshot.materialization_failure is FailureKind.CAPABILITY
    assert not marker.exists()
    assert VerificationEvidence.model_validate_json(evidence.model_dump_json()) == evidence


def test_incomplete_git_materialization_context_is_removed_after_use(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path, [sys.executable, "-c", "pass"])
    (repo / "tracked.txt").write_text("omitted\n")
    changeset, discovery, _plan = plan_for(
        repo,
        limits=ContentLimits(per_file_bytes=0, total_bytes=0),
    )

    with disposable_git_snapshot(changeset, discovery) as snapshot:
        temporary_path = snapshot.path
        assert snapshot.manifest.complete is False
        assert not (temporary_path / ".git").exists()
    assert not temporary_path.exists()


def test_complete_git_repository_real_nonzero_is_verification_failure(
    tmp_path: Path,
) -> None:
    command = [sys.executable, "-c", "raise SystemExit(7)"]
    repo = repository(tmp_path, command)
    changeset, discovery, plan = plan_for(repo)

    evidence = execute(repo, changeset, discovery, plan)
    result = evidence.executions[0].result
    assert result.status is ExecutionStatus.FAILED
    assert result.failure_kind is FailureKind.VERIFICATION
    assert result.exit_code == 7
    assert result.required_evidence_gap is False


def test_child_can_mutate_disposable_git_state_without_mutating_source(
    tmp_path: Path,
) -> None:
    command = [
        sys.executable,
        "-c",
        (
            "import subprocess; subprocess.run(['git','update-ref','refs/test/child','HEAD'], check=True); "
            "subprocess.run(['git','config','local.child','yes'], check=True); "
            "subprocess.run(['git','add','tracked.txt'], check=True)"
        ),
    ]
    repo = repository(tmp_path, command)
    source_git = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    before = (git(repo, "rev-parse", "HEAD"), (source_git / "index").read_bytes(), (source_git / "config").read_bytes())
    changeset, discovery, plan = plan_for(repo)

    evidence = execute(repo, changeset, discovery, plan)
    assert evidence.executions[0].result.status is ExecutionStatus.PASSED
    assert before == (
        git(repo, "rev-parse", "HEAD"),
        (source_git / "index").read_bytes(),
        (source_git / "config").read_bytes(),
    )


def test_author_git_config_mutation_retains_pass_and_records_preservation_gap(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path, [sys.executable, "-c", "pass"])
    source_git = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    source_config = source_git / "config"
    before_config = source_config.read_bytes()
    mutation = "[child-source]\n\tmutation = detected\n"
    command = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; "
            f"config = Path({str(source_config)!r}).open('a', encoding='utf-8'); "
            f"config.write({mutation!r}); config.close()"
        ),
    ]
    (repo / "pyproject.toml").write_text(
        "[tool.pre-pr-verify.verification]\nchecks = [\n"
        + "  { id = \"source-config-race\", level = \"required\", argv = "
        + json.dumps(command)
        + ", environment_profile = \"GIT_REPOSITORY\" },\n]\n"
    )
    git(repo, "add", "pyproject.toml")
    git(repo, "commit", "--quiet", "-m", "source config check")
    changeset, discovery, plan = plan_for(repo)

    evidence = execute(repo, changeset, discovery, plan)
    result = evidence.executions[0].result
    assert result.status is ExecutionStatus.PASSED
    assert result.failure_kind is None
    assert source_config.read_bytes() != before_config
    assert b"[child-source]" in source_config.read_bytes()
    assert len(evidence.source_preservation_failures) == 1
    assert evidence.source_preservation_failures[0].check_id == "source-config-race"


def test_post_execution_source_mutation_retains_git_result_and_records_gap(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path, [sys.executable, "-c", "pass"])
    source_file = repo / "tracked.txt"
    command = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(source_file)!r}).write_text('mutated')",
    ]
    (repo / "pyproject.toml").write_text(
        "[tool.pre-pr-verify.verification]\nchecks = [\n"
        + "  { id = \"source-race\", level = \"required\", argv = "
        + json.dumps(command)
        + ", environment_profile = \"GIT_REPOSITORY\" },\n]\n"
    )
    git(repo, "add", "pyproject.toml")
    git(repo, "commit", "--quiet", "-m", "command")
    changeset, discovery, plan = plan_for(repo)

    evidence = execute(repo, changeset, discovery, plan)
    result = evidence.executions[0].result
    assert result.status is ExecutionStatus.PASSED
    assert result.failure_kind is None
    assert len(evidence.source_preservation_failures) == 1
    assert evidence.source_preservation_failures[0].check_id == "source-race"
