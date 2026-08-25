from __future__ import annotations

import subprocess
from pathlib import Path

from pre_pr_verify.discovery import ProvidedRequirement, discover_review_sources
from pre_pr_verify.git_capture import capture_changeset
from pre_pr_verify.models import ScopeMode
from pre_pr_verify.verification import (
    PlannerCheckInput,
    build_verification_plan,
    discover_canonical_checks,
)
from pre_pr_verify.verification_models import (
    CheckOrigin,
    EnvironmentProfile,
    RequirementLevel,
)


def git(repository: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def q14_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "learnloop"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Test")
    git(repository, "config", "user.email", "test@example.invalid")

    (repository / "README.md").write_text("# LearnLoop\n")
    (repository / "pyproject.toml").write_text(
        """
[project]
name = "learnloop"
version = "0.1.0"

[dependency-groups]
dev = ["pytest>=8,<9"]

[tool.pytest.ini_options]
testpaths = ["tests"]
""".lstrip()
    )
    (repository / "uv.lock").write_text("# locked fixture\n")
    (repository / "docs").mkdir()
    (repository / "docs/07-evaluation-plan.md").write_text(
        "uv run --no-env-file --frozen pytest -q\n"
    )
    q14 = repository / "tests/evals/parser_note_completeness"
    q14.mkdir(parents=True)
    (q14 / "q14_scoring.py").write_text("def validate_metric_contract(value):\n    return value\n")
    (q14 / "test_q14_scoring.py").write_text(
        "def test_public_validation_helpers_revalidate_existing_model_instances():\n"
        "    assert True\n"
    )
    git(repository, "add", ".")
    git(repository, "commit", "-m", "base")

    (q14 / "q14_scoring.py").write_text(
        "def validate_metric_contract(value):\n    return value\n\n"
        "def validate_metric_registry(value):\n    return value\n"
    )
    (q14 / "test_q14_scoring.py").write_text(
        "def test_public_validation_helpers_revalidate_existing_model_instances():\n"
        "    assert True\n\n"
        "def test_generic_and_named_q14_canonical_helpers_enforce_their_boundaries():\n"
        "    assert True\n"
    )
    return repository


def minimal_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "minimal"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Test")
    git(repository, "config", "user.email", "test@example.invalid")
    (repository / "README.md").write_text("# Minimal\n")
    (repository / "module.txt").write_text("base\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "base")
    (repository / "module.txt").write_text("pending\n")
    return repository


def test_q14_forward_plan_proposes_focused_and_affected_regression_checks(
    tmp_path: Path,
) -> None:
    repository = q14_repository(tmp_path)
    changeset = capture_changeset(repository, "main", ScopeMode.PENDING)
    discovery = discover_review_sources(
        repository,
        explicit_specs=(
            ProvidedRequirement(
                label="Q14 acceptance criteria",
                content="Focused and regression tests must pass.",
            ),
        ),
    )

    assert discover_canonical_checks(repository) == []
    proposals = (
        PlannerCheckInput(
            check_id="q14-focused-tests",
            requirement_level=RequirementLevel.REQUIRED,
            selection_reason=(
                "The acceptance criteria require focused tests and the changed "
                "Q14 test module is present."
            ),
            argv=(
                "uv",
                "run",
                "--no-env-file",
                "--frozen",
                "pytest",
                "-q",
                "tests/evals/parser_note_completeness/test_q14_scoring.py",
            ),
        ),
        PlannerCheckInput(
            check_id="q14-affected-regression",
            requirement_level=RequirementLevel.REQUIRED,
            selection_reason=(
                "The acceptance criteria require regression tests and the "
                "repository documents the affected parser-note-completeness suite."
            ),
            argv=(
                "uv",
                "run",
                "--no-env-file",
                "--frozen",
                "pytest",
                "-q",
                "tests/evals/parser_note_completeness",
            ),
        ),
    )
    plan = build_verification_plan(
        changeset,
        discovery,
        canonical_checks=[],
        trusted_policy_checks=[],
        planner_additions=proposals,
    )

    commands = {
        check.check_id: check
        for check in plan.checks
        if check.kind.value == "command"
    }
    assert set(commands) == {"q14-focused-tests", "q14-affected-regression"}
    assert all(check.origin is CheckOrigin.MODEL_PROPOSED for check in commands.values())
    assert all(
        check.environment_profile is EnvironmentProfile.FILESYSTEM_ONLY
        for check in commands.values()
    )
    assert commands["q14-focused-tests"].argv[-1].endswith("test_q14_scoring.py")
    assert commands["q14-affected-regression"].argv[-1].endswith(
        "parser_note_completeness"
    )


def test_forward_planning_with_insufficient_evidence_keeps_empty_additions(
    tmp_path: Path,
) -> None:
    repository = minimal_repository(tmp_path)
    changeset = capture_changeset(repository, "main", ScopeMode.PENDING)
    discovery = discover_review_sources(repository)

    plan = build_verification_plan(
        changeset,
        discovery,
        canonical_checks=discover_canonical_checks(repository),
        trusted_policy_checks=[],
        planner_additions=(),
    )

    assert [check for check in plan.checks if check.kind.value == "command"] == []
