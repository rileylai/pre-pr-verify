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


def test_bare_invocation_requires_a_mandatory_senior_inspection_gate() -> None:
    skill = Path("SKILL.md").read_text()
    runbook = Path("docs/09_v1_skill_runbook.md").read_text()
    combined = f"{skill}\n{runbook}"

    assert "bare `$pre-pr-verify` invocation" in combined
    assert "no extra prompt" in combined
    inspection_start = runbook.index("### 5. Mandatory senior semantic inspection gate")
    assessment_start = runbook.index("### 6. Semantic assessment construction")
    inspection = runbook[inspection_start:assessment_start]

    assert inspection_start < assessment_start
    assert "verification complete → inspection gate" in inspection
    assert "never take the shortcut" in inspection
    assert "Do not construct `SemanticAssessment`" in inspection
    inspection_lower = inspection.casefold()
    for required_dimension in (
        "implementation logic",
        "edge/error behavior",
        "contracts and compatibility",
        "Impact",
        "Test Sufficiency",
        "Contextual Security",
    ):
        assert required_dimension.casefold() in inspection_lower
    assert "changed implementation was inspected" in inspection
    assert "relevant surrounding context was inspected" in inspection
    assert "each materially relevant review dimension" in inspection
    assert "concrete findings are recorded when supported" in inspection


def test_full_review_requires_user_visible_canonical_report_handoff() -> None:
    skill = Path("SKILL.md").read_text()
    runbook = Path("docs/09_v1_skill_runbook.md").read_text()

    skill_flow = skill[skill.index("## V1 flow") : skill.index("## Pre-review setup interaction")]
    finalization = runbook[runbook.index("### 7. Deterministic finalization") : runbook.index("### 8. Emit the canonical report")]
    delivery = runbook[runbook.index("### 8. Emit the canonical report") :]
    skill_flow_text = " ".join(skill_flow.split())
    finalization_text = " ".join(finalization.split())
    delivery_text = " ".join(delivery.split())

    assert "`finalized.report` is the canonical Markdown report" in skill_flow_text
    assert "stdout success alone is not review completion" in skill_flow_text
    assert "`finalized.report` verbatim" in skill_flow_text
    assert "actual assistant final response shown to the user" in skill_flow_text
    assert "user, beginning `# PrePR Verify Report`" in skill_flow_text

    assert "writes `finalized.report` exactly to stdout by" in delivery_text
    assert "stdout success alone is not review completion" in delivery_text
    assert "actual assistant final response shown to the user" in delivery_text
    assert "exact verbatim `finalized.report`" in delivery_text
    assert "transport layer, not a renderer" in delivery_text
    assert "Do not end with `report emitted above`, `canonical report above`, `review complete`" in delivery_text
    assert "path-only message" in delivery_text
    assert "summary-only message" in delivery_text
    assert "user-visible canonical report handoff succeeds" in delivery_text

    assert "Once stdout emission succeeds, END REVIEW" not in skill_flow_text
    assert "Once stdout emission succeeds, END REVIEW" not in delivery_text

    assert finalization_text.index("finalize_review(") < finalization_text.index(
        "emit_final_report(finalized)"
    )
    assert finalization_text.index("emit_final_report(finalized)") < finalization_text.index(
        "transport finalized.report verbatim"
    )
    assert finalization_text.index("transport finalized.report verbatim") < finalization_text.index(
        "END REVIEW only after"
    )
    assert delivery_text.index("emit_final_report(finalized)") < delivery_text.index(
        "actual assistant final response shown to"
    )
    assert delivery_text.index("actual assistant final response shown to") < delivery_text.index(
        "`END REVIEW` occurs only after"
    )


def test_execution_retry_recovery_is_single_path_and_fail_closed() -> None:
    skill = Path("SKILL.md").read_text()
    runbook = Path("docs/09_v1_skill_runbook.md").read_text()
    execution_start = runbook.index("### 4. Authorized execution")
    execution_end = runbook.index("### 5. Mandatory senior semantic inspection gate")
    execution = runbook[execution_start:execution_end]
    first_start = execution.index("#### First-attempt API")
    recovery_start = execution.index("#### Recovery after possible launch")
    first_attempt = " ".join(execution[first_start:recovery_start].split())
    recovery = " ".join(execution[recovery_start:].split())
    execution_text = " ".join(execution.split())

    assert "exactly one verifier-owned `review_run_dir`" in execution_text
    assert "derive exactly one" in execution_text
    assert "before the first execution attempt" in execution_text
    assert "keep them immutable" in execution_text
    assert "`execute_authorized_plan(...)` is the first-attempt API" in first_attempt
    first_attempt_code_start = first_attempt.index("```python")
    first_attempt_code_end = first_attempt.index(
        "```", first_attempt_code_start + len("```python")
    )
    first_attempt_code = first_attempt[
        first_attempt_code_start:first_attempt_code_end
    ]
    assert "execute_authorized_plan(" in first_attempt_code
    assert "Call it exactly once" in first_attempt
    assert "`load_completed_execution(...)`" not in first_attempt

    recovery_code_start = recovery.index("```python")
    recovery_code_end = recovery.index("```", recovery_code_start + len("```python"))
    recovery_code = recovery[recovery_code_start:recovery_code_end]
    assert "load_completed_execution(" in recovery_code
    assert "execute_authorized_plan(" not in recovery_code
    assert "Do not call `execute_authorized_plan(...)` again" in recovery
    assert "same original `review_run_dir`" in recovery
    assert "same original authorization-scoped evidence target" in recovery
    assert "never allocate a new run directory or evidence namespace" in recovery
    assert "zero additional command launches" in recovery
    assert "absent, incomplete, invalid, stale, or unreadable" in recovery
    assert "outcome remains `UNKNOWN`" in recovery
    assert "fail closed" in recovery
    assert "do not retry" in recovery
    assert "genuinely new explicit authorization flow" in recovery
    assert "prior execution outcome is unknown" in recovery
    assert "it will reuse evidence" not in execution_text

    skill_text = " ".join(skill.split())
    assert "allocate exactly one verifier-owned" in skill_text
    assert "`execute_authorized_plan(...)` once for first attempt" in skill_text
    assert "use only `load_completed_execution(...)` on target" in skill_text
    assert "Absent/invalid evidence is `UNKNOWN`: fail closed; no retry" in skill_text
    assert "New execution requires new explicit authorization telling user" in skill_text


def test_spec_limit_gap_does_not_stop_independent_semantic_axes() -> None:
    runbook = Path("docs/09_v1_skill_runbook.md").read_text()
    assert "Spec `INCONCLUSIVE`" in runbook
    assert "must not stop" in runbook
    assert "Impact" in runbook
    assert "Test Sufficiency" in runbook
