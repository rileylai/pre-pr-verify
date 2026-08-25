from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from pre_pr_verify.discovery import discover_review_sources
from pre_pr_verify.errors import PreflightError
from pre_pr_verify.git_capture import capture_changeset
from pre_pr_verify.models import ScopeMode
from pre_pr_verify.verification import (
    PlannerCheckInput,
    TrustedPolicyCheckInput,
    build_verification_plan,
    discover_canonical_checks,
    extract_change_signals,
)
from pre_pr_verify.verification_models import (
    CheckOrigin,
    RequirementLevel,
    VerificationPlan,
    hash_payload,
)


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "README.md").write_text("# Fixture\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    (repo / "src.py").write_text("print('pending')\n")
    (repo / "tests").mkdir()
    (repo / "tests/test_src.py").write_text("def test_pending(): pass\n")
    return repo


def test_required_and_advisory_planning_has_deterministic_signals(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    changeset = capture_changeset(repo, "main", ScopeMode.PENDING)
    discovery = discover_review_sources(repo)

    signals = extract_change_signals(changeset)
    plan = build_verification_plan(
        changeset,
        discovery,
        canonical_checks=[],
        trusted_policy_checks=[],
        planner_additions=[
            PlannerCheckInput(
                check_id="targeted-test",
                requirement_level=RequirementLevel.ADVISORY,
                selection_reason="Changed test and source paths warrant a targeted check.",
                argv=("python", "-c", "print('targeted')"),
            )
        ],
    )

    assert signals.changed_path_count == 2
    assert signals.test_path_count == 1
    assert plan.changeset_identity == changeset.identity
    assert plan.discovery_identity == discovery.identity
    assert [check.requirement_level for check in plan.checks] == [
        RequirementLevel.REQUIRED,
        RequirementLevel.REQUIRED,
        RequirementLevel.REQUIRED,
        RequirementLevel.ADVISORY,
    ]
    assert all(check.selection_reason for check in plan.checks)


def test_repository_native_canonical_command_is_discovered(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    (repo / "pyproject.toml").write_text(
        """
[tool.pre-pr-verify.verification]
checks = [
  { id = "repo-check", level = "required", argv = ["python", "-c", "print('ok')"] },
]
""".strip()
        + "\n"
    )

    checks = discover_canonical_checks(repo)

    assert len(checks) == 1
    assert checks[0].check_id == "repo-check"
    assert checks[0].requirement_level is RequirementLevel.REQUIRED
    assert checks[0].argv == ("python", "-c", "print('ok')")
    assert not hasattr(checks[0], "origin")


def test_no_canonical_command_does_not_invent_language_defaults(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)

    assert discover_canonical_checks(repo) == []


def test_model_proposal_coexists_with_preferred_repository_canonical_check(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    (repo / "pyproject.toml").write_text(
        """
[tool.pre-pr-verify.verification]
checks = [{ id = "repo-check", level = "required", argv = ["python", "-c", "pass"] }]
""".strip()
        + "\n"
    )
    changeset = capture_changeset(repo, "main", ScopeMode.PENDING)
    discovery = discover_review_sources(repo)
    plan = build_verification_plan(
        changeset,
        discovery,
        canonical_checks=discover_canonical_checks(repo),
        trusted_policy_checks=[],
        planner_additions=[
            PlannerCheckInput(
                check_id="targeted-model-check",
                requirement_level=RequirementLevel.ADVISORY,
                selection_reason="Bounded changed-test evidence supports a targeted check.",
                argv=("python", "-c", "pass"),
            )
        ],
    )

    checks = {check.check_id: check for check in plan.checks}
    assert checks["repo-check"].origin is CheckOrigin.REPOSITORY_CANONICAL
    assert checks["targeted-model-check"].origin is CheckOrigin.MODEL_PROPOSED


def test_trusted_required_check_cannot_be_removed_by_planner(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    changeset = capture_changeset(repo, "main", ScopeMode.PENDING)
    discovery = discover_review_sources(repo)
    trusted = TrustedPolicyCheckInput(
        check_id="trusted-policy-check",
        requirement_level=RequirementLevel.REQUIRED,
        selection_reason="Required by digest-pinned trusted policy.",
        argv=("python", "-c", "print('trusted')"),
        policy_label="company verification policy",
        policy_sha256="a" * 64,
    )

    plan = build_verification_plan(
        changeset,
        discovery,
        canonical_checks=[],
        trusted_policy_checks=[trusted],
        planner_additions=[],
    )

    assert any(check.check_id == "trusted-policy-check" for check in plan.checks)
    assert next(
        check for check in plan.checks if check.check_id == "trusted-policy-check"
    ).requirement_level is RequirementLevel.REQUIRED


def test_planner_channel_cannot_claim_trusted_origin(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    changeset = capture_changeset(repo, "main", ScopeMode.PENDING)
    discovery = discover_review_sources(repo)

    plan = build_verification_plan(
        changeset,
        discovery,
        canonical_checks=[],
        trusted_policy_checks=[],
        planner_additions=[
            PlannerCheckInput(
                check_id="model-check",
                requirement_level=RequirementLevel.REQUIRED,
                selection_reason="Proposed targeted verification.",
                argv=("python", "-c", "pass"),
            )
        ],
    )

    model_check = next(check for check in plan.checks if check.check_id == "model-check")
    assert model_check.origin is CheckOrigin.MODEL_PROPOSED


def test_model_duplicate_cannot_replace_repository_canonical_check(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    (repo / "pyproject.toml").write_text(
        """
[tool.pre-pr-verify.verification]
checks = [{ id = "same-id", level = "required", argv = ["python", "-c", "print('repo')"] }]
""".strip()
        + "\n"
    )
    changeset = capture_changeset(repo, "main", ScopeMode.PENDING)
    discovery = discover_review_sources(repo)

    with pytest.raises(PreflightError, match="collides"):
        build_verification_plan(
            changeset,
            discovery,
            canonical_checks=discover_canonical_checks(repo),
            trusted_policy_checks=[],
            planner_additions=[
                PlannerCheckInput(
                    check_id="same-id",
                    requirement_level=RequirementLevel.REQUIRED,
                    selection_reason="Attempted replacement.",
                    argv=("python", "-c", "print('model')"),
                )
            ],
        )


def test_verification_plan_rejects_noncanonical_check_order(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    changeset = capture_changeset(repo, "main", ScopeMode.PENDING)
    discovery = discover_review_sources(repo)
    plan = build_verification_plan(
        changeset,
        discovery,
        canonical_checks=[],
        trusted_policy_checks=[],
        planner_additions=[
            PlannerCheckInput(
                check_id="z-check",
                requirement_level=RequirementLevel.ADVISORY,
                selection_reason="Z check.",
                argv=("python", "-c", "pass"),
            ),
            PlannerCheckInput(
                check_id="a-check",
                requirement_level=RequirementLevel.ADVISORY,
                selection_reason="A check.",
                argv=("python", "-c", "pass"),
            ),
        ],
    )
    payload = plan.model_dump(mode="json")
    payload["checks"][-2:] = reversed(payload["checks"][-2:])
    payload["identity"] = hash_payload(
        {key: value for key, value in payload.items() if key != "identity"}
    )

    with pytest.raises(ValidationError, match="canonical"):
        VerificationPlan.model_validate(payload)


def test_deserialized_floor_cannot_be_command_or_have_provenance(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    changeset = capture_changeset(repo, "main", ScopeMode.PENDING)
    discovery = discover_review_sources(repo)
    plan = build_verification_plan(
        changeset,
        discovery,
        canonical_checks=[],
        trusted_policy_checks=[],
        planner_additions=[],
    )
    payload = plan.model_dump(mode="json")
    payload["checks"][0].update(
        kind="command",
        argv=["python", "-c", "pass"],
        source_path={"raw_b64": "Zm9v", "display": "foo", "utf8": "foo"},
        source_sha256="a" * 64,
        source_size=3,
    )
    payload["identity"] = hash_payload(
        {key: value for key, value in payload.items() if key != "identity"}
    )

    with pytest.raises(ValidationError, match="floor|structural"):
        VerificationPlan.model_validate(payload)
