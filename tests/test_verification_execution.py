from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from pre_pr_verify.discovery import discover_review_sources
from pre_pr_verify.errors import PreflightError
from pre_pr_verify.executor import (
    decide_execution,
    execute_request,
    execute_verification_plan,
)
from pre_pr_verify.git_capture import capture_changeset
from pre_pr_verify.models import ContentLimits, ScopeMode
from pre_pr_verify.snapshot import disposable_snapshot
from pre_pr_verify.verification import (
    build_execution_request,
    build_verification_plan,
    discover_canonical_checks,
)
from pre_pr_verify.verification_models import (
    CapabilityName,
    CheckKind,
    CheckOrigin,
    DecisionKind,
    ExecutionCapability,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    FailureKind,
    PlannedCheck,
    RequirementLevel,
    SnapshotManifest,
    VerificationEvidence,
    build_verification_evidence,
    hash_payload,
)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "README.md").write_text("# Fixture\n")
    (repo / "tracked.txt").write_text("base\n")
    (repo / "delete.txt").write_text("remove\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    git(repo, "switch", "-c", "feature")
    (repo / "committed.txt").write_text("committed pending state\n")
    git(repo, "add", "committed.txt")
    git(repo, "commit", "-m", "feature commit")
    (repo / "staged.txt").write_text("staged pending state\n")
    git(repo, "add", "staged.txt")
    (repo / "tracked.txt").write_text("pending\n")
    (repo / "delete.txt").unlink()
    (repo / "new.sh").write_text("#!/bin/sh\necho new\n")
    os.chmod(repo / "new.sh", 0o755)
    return repo


def capability(**overrides: object) -> ExecutionCapability:
    values: dict[str, object] = {
        "structured_argv": True,
        "repository_bound_cwd": True,
        "git_protection": True,
        "source_preservation": True,
        "authority_separation": True,
        "secret_stripping": True,
        "verdict_invariants": True,
        "available": {CapabilityName.OUTPUT_LIMITS},
        "approval_waivable": set(),
        "approved_gaps": set(),
    }
    values.update(overrides)
    return ExecutionCapability(**values)


def request(*argv: str, **overrides: object) -> ExecutionRequest:
    values: dict[str, object] = {
        "check_id": "check",
        "snapshot_identity": "0" * 64,
        "requirement_level": RequirementLevel.REQUIRED,
        "argv": argv,
        "cwd": ".",
        "timeout_seconds": 2.0,
        "output_limit_bytes": 1024,
        "required_capabilities": {CapabilityName.OUTPUT_LIMITS},
        "nonzero_failure_kind": FailureKind.VERIFICATION,
    }
    values.update(overrides)
    return ExecutionRequest(**values)


def test_execution_permission_and_capability_decisions() -> None:
    executable = decide_execution(request(sys.executable, "-c", "pass"), capability())
    cannot = decide_execution(
        request(sys.executable, "-c", "pass"),
        capability(secret_stripping=False),
    )
    approval = decide_execution(
        request(
            sys.executable,
            "-c",
            "pass",
            required_capabilities={CapabilityName.NETWORK_ISOLATION},
        ),
        capability(approval_waivable={CapabilityName.NETWORK_ISOLATION}),
    )

    assert executable.kind is DecisionKind.EXECUTABLE
    assert cannot.kind is DecisionKind.CANNOT_SAFELY_EXECUTE
    assert approval.kind is DecisionKind.REQUIRES_APPROVAL


def test_required_nonwaivable_safety_gap_never_starts_process(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    execution_request = request(
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
    )

    result = execute_request(
        execution_request,
        capability(source_preservation=False),
        tmp_path,
    )

    assert result.status is ExecutionStatus.NOT_RUN
    assert result.failure_kind is FailureKind.CAPABILITY
    assert result.required_evidence_gap is True
    assert not marker.exists()


def test_approval_waivable_gap_executes_only_after_specific_approval() -> None:
    execution_request = request(
        sys.executable,
        "-c",
        "pass",
        required_capabilities={CapabilityName.NETWORK_ISOLATION},
    )
    approved = capability(
        approval_waivable={CapabilityName.NETWORK_ISOLATION},
        approved_gaps={CapabilityName.NETWORK_ISOLATION},
    )

    decision = decide_execution(execution_request, approved)

    assert decision.kind is DecisionKind.EXECUTABLE
    assert decision.accepted_risks == [CapabilityName.NETWORK_ISOLATION]


def test_timeout_nonzero_and_infrastructure_are_distinct(tmp_path: Path) -> None:
    timed_out = execute_request(
        request(
            sys.executable,
            "-c",
            "import time; time.sleep(2)",
            timeout_seconds=0.05,
        ),
        capability(),
        tmp_path,
    )
    failed = execute_request(
        request(sys.executable, "-c", "raise SystemExit(7)"),
        capability(),
        tmp_path,
    )
    infrastructure = execute_request(
        request("definitely-not-a-real-pre-pr-verify-command"),
        capability(),
        tmp_path,
    )

    assert (timed_out.status, timed_out.failure_kind) == (
        ExecutionStatus.TIMED_OUT,
        FailureKind.INFRASTRUCTURE,
    )
    assert (failed.status, failed.failure_kind, failed.exit_code) == (
        ExecutionStatus.FAILED,
        FailureKind.VERIFICATION,
        7,
    )
    assert (infrastructure.status, infrastructure.failure_kind) == (
        ExecutionStatus.ERRORED,
        FailureKind.CONFIGURATION,
    )


def test_timeout_kills_descendant_holding_output_pipes(tmp_path: Path) -> None:
    descendant = (
        "import pathlib, sys, time; sys.stderr.write('held\\n'); sys.stderr.flush(); "
        "time.sleep(0.4); "
        "pathlib.Path('descendant-survived').write_text('bad')"
    )
    parent = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {descendant!r}])"
    )
    started = __import__("time").monotonic()
    result = execute_request(
        request(sys.executable, "-c", parent, timeout_seconds=0.15),
        capability(),
        tmp_path,
    )
    elapsed = __import__("time").monotonic() - started

    assert result.status is ExecutionStatus.TIMED_OUT
    assert result.failure_kind is FailureKind.INFRASTRUCTURE
    assert elapsed < 1.5
    __import__("time").sleep(0.6)
    assert not (tmp_path / "descendant-survived").exists()


def test_timeout_kills_descendant_after_it_closes_inherited_pipes(tmp_path: Path) -> None:
    descendant = (
        "import pathlib, os, time; os.close(1); os.close(2); time.sleep(0.4); "
        "pathlib.Path('closed-pipe-survived').write_text('bad')"
    )
    parent = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {descendant!r}])"
    )

    result = execute_request(
        request(sys.executable, "-c", parent, timeout_seconds=0.15),
        capability(),
        tmp_path,
    )

    assert result.status is ExecutionStatus.TIMED_OUT
    assert result.failure_kind is FailureKind.INFRASTRUCTURE
    __import__("time").sleep(0.6)
    assert not (tmp_path / "closed-pipe-survived").exists()


def test_post_popen_setup_failure_reaps_started_process(
    tmp_path: Path, monkeypatch
) -> None:
    def fail_set_blocking(*args: object, **kwargs: object) -> None:
        raise OSError("injected setup failure")

    monkeypatch.setattr(os, "set_blocking", fail_set_blocking)
    result = execute_request(
        request(
            sys.executable,
            "-c",
            "import time; time.sleep(.4); from pathlib import Path; Path('setup-failure-survived').write_text('bad')",
        ),
        capability(),
        tmp_path,
    )

    assert result.status is ExecutionStatus.ERRORED
    assert result.failure_kind is FailureKind.INFRASTRUCTURE
    __import__("time").sleep(0.6)
    assert not (tmp_path / "setup-failure-survived").exists()


def test_host_process_error_is_infrastructure_failure(
    tmp_path: Path, monkeypatch
) -> None:
    def fail_to_spawn(*args, **kwargs):
        raise OSError("host process service unavailable")

    monkeypatch.setattr(subprocess, "Popen", fail_to_spawn)

    result = execute_request(
        request(sys.executable, "-c", "pass"), capability(), tmp_path
    )

    assert result.status is ExecutionStatus.ERRORED
    assert result.failure_kind is FailureKind.INFRASTRUCTURE
    assert result.required_evidence_gap is True


def test_permission_configuration_and_unclassified_required_failures_are_gaps(
    tmp_path: Path,
) -> None:
    denied = tmp_path / "denied"
    denied.write_text("not executable")
    denied.chmod(0o600)
    permission = execute_request(request(str(denied)), capability(), tmp_path)
    unclassified = execute_request(
        request(
            sys.executable,
            "-c",
            "raise SystemExit(2)",
            nonzero_failure_kind=FailureKind.UNCLASSIFIED,
        ),
        capability(),
        tmp_path,
    )

    assert permission.failure_kind is FailureKind.PERMISSION
    assert permission.required_evidence_gap is True
    assert unclassified.failure_kind is FailureKind.UNCLASSIFIED
    assert unclassified.required_evidence_gap is True


def test_missing_cwd_is_configuration_error(tmp_path: Path) -> None:
    result = execute_request(
        request(sys.executable, "-c", "pass", cwd="missing-directory"),
        capability(),
        tmp_path,
    )

    assert result.decision.kind is DecisionKind.CANNOT_SAFELY_EXECUTE
    assert result.status is ExecutionStatus.NOT_RUN
    assert result.failure_kind is FailureKind.CONFIGURATION
    assert result.required_evidence_gap is True


def test_cwd_validation_is_classified_without_starting_process(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "nested" / "safe").mkdir(parents=True)
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    (tmp_path / "loop").symlink_to("loop")
    (tmp_path / "loop-a").symlink_to("loop-b")
    (tmp_path / "loop-b").symlink_to("loop-a")

    invalid = [
        ("missing", FailureKind.CONFIGURATION),
        ("missing/child", FailureKind.CONFIGURATION),
        ("escape", FailureKind.PERMISSION),
        ("loop", FailureKind.PERMISSION),
        ("loop-a", FailureKind.PERMISSION),
        (".git", FailureKind.PERMISSION),
        ("..", FailureKind.PERMISSION),
    ]
    results = [
        execute_request(
            request(
                sys.executable,
                "-c",
                "from pathlib import Path; Path('cwd-invalid-ran').write_text('bad')",
                cwd=cwd,
            ),
            capability(),
            tmp_path,
        )
        for cwd, _ in invalid
    ]

    for (cwd, expected_failure), result in zip(invalid, results):
        assert result.status is ExecutionStatus.NOT_RUN
        assert result.decision.kind is DecisionKind.CANNOT_SAFELY_EXECUTE
        assert result.failure_kind is expected_failure, cwd
        assert result.required_evidence_gap is True
    assert not (tmp_path / "cwd-invalid-ran").exists()

    result = execute_request(
        request(sys.executable, "-c", "pass", cwd="nested/safe"),
        capability(),
        tmp_path,
    )

    assert result.status is ExecutionStatus.PASSED


def test_execution_result_rejects_decision_capability_contradiction(
    tmp_path: Path,
) -> None:
    result = execute_request(
        request(sys.executable, "-c", "pass"), capability(), tmp_path
    )
    payload = result.model_dump(mode="json")
    payload["decision"] = {
        "kind": "requires_approval",
        "missing_capabilities": ["network_isolation"],
        "accepted_risks": [],
        "blocked_failure_kind": "permission",
        "reasons": ["fabricated approval requirement"],
    }

    with pytest.raises(ValidationError, match="decision"):
        ExecutionResult.model_validate(payload)


def test_not_run_failure_kind_is_bound_to_structured_decision_cause(
    tmp_path: Path,
) -> None:
    blocked = execute_request(
        request(
            sys.executable,
            "-c",
            "pass",
            required_capabilities={CapabilityName.NETWORK_ISOLATION},
        ),
        capability(),
        tmp_path,
    )
    assert blocked.status is ExecutionStatus.NOT_RUN
    assert blocked.failure_kind is FailureKind.CAPABILITY
    payload = blocked.model_dump(mode="json")
    payload["failure_kind"] = "permission"

    with pytest.raises(ValidationError, match="not-run|failure"):
        ExecutionResult.model_validate(payload)

    approval = execute_request(
        request(
            sys.executable,
            "-c",
            "pass",
            required_capabilities={CapabilityName.NETWORK_ISOLATION},
        ),
        capability(approval_waivable={CapabilityName.NETWORK_ISOLATION}),
        tmp_path,
    )
    assert approval.failure_kind is FailureKind.PERMISSION
    approval_payload = approval.model_dump(mode="json")
    approval_payload["failure_kind"] = "capability"
    with pytest.raises(ValidationError, match="not-run|failure"):
        ExecutionResult.model_validate(approval_payload)

    missing = execute_request(
        request(sys.executable, "-c", "pass", cwd="missing"),
        capability(),
        tmp_path,
    )
    assert missing.failure_kind is FailureKind.CONFIGURATION
    missing_payload = missing.model_dump(mode="json")
    missing_payload["failure_kind"] = "permission"
    with pytest.raises(ValidationError, match="not-run|failure"):
        ExecutionResult.model_validate(missing_payload)


def test_execution_result_rejects_executed_nonexecutable_and_zero_failed(
    tmp_path: Path,
) -> None:
    blocked_request = request(
        sys.executable,
        "-c",
        "pass",
        required_capabilities={CapabilityName.NETWORK_ISOLATION},
    )
    blocked = execute_request(
        blocked_request,
        capability(approval_waivable={CapabilityName.NETWORK_ISOLATION}),
        tmp_path,
    )
    executed_payload = blocked.model_dump(mode="json")
    executed_payload.update(
        status="passed",
        failure_kind=None,
        exit_code=0,
        required_evidence_gap=False,
    )
    with pytest.raises(ValidationError, match="non-executable"):
        ExecutionResult.model_validate(executed_payload)

    passed = execute_request(
        request(sys.executable, "-c", "pass"), capability(), tmp_path
    )
    failed_payload = passed.model_dump(mode="json")
    failed_payload.update(
        status="failed",
        failure_kind="verification",
        exit_code=0,
        required_evidence_gap=False,
    )
    with pytest.raises(ValidationError, match="non-zero"):
        ExecutionResult.model_validate(failed_payload)


def test_environment_secrets_are_stripped_and_output_is_redacted_and_truncated(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PRE_PR_VERIFY_TEST_SECRET", "super-secret-value")
    code = (
        "import os, sys; "
        "print(os.getenv('PRE_PR_VERIFY_TEST_SECRET', 'stripped')); "
        "print('token=visible-secret'); "
        "print('stderr-token=visible-secret', file=sys.stderr); "
        "print('x' * 5000)"
    )
    redactions = (value for value in ("visible-secret",))
    result = execute_request(
        request(sys.executable, "-c", code, output_limit_bytes=96),
        capability(),
        tmp_path,
        redaction_values=redactions,
    )

    assert "super-secret-value" not in result.stdout.excerpt
    assert "visible-secret" not in result.stdout.excerpt
    assert "visible-secret" not in result.stderr.excerpt
    assert "[REDACTED]" in result.stderr.excerpt
    assert "[REDACTED]" in result.stdout.excerpt
    assert result.stdout.truncated is True
    assert len(result.stdout.sha256) == 64


def test_redaction_does_not_expose_secret_fragments_at_truncation_boundary(
    tmp_path: Path,
) -> None:
    secret = "boundary-secret-value"
    code = (
        "import sys; "
        f"sys.stdout.write('out-'+{secret!r}+'-tail'); "
        f"sys.stderr.write('err-'+{secret!r}+'-tail')"
    )
    result = execute_request(
        request(sys.executable, "-c", code, output_limit_bytes=10),
        capability(),
        tmp_path,
        redaction_values=(secret,),
    )

    for output in (result.stdout, result.stderr):
        assert len(output.excerpt.encode()) <= 10
        assert output.redacted is True
        assert output.truncated is True
        assert secret not in output.excerpt
        assert secret[:8] not in output.excerpt
        assert secret[-8:] not in output.excerpt


def test_overlapping_redaction_patterns_wait_for_longest_value_across_chunks(
    tmp_path: Path,
) -> None:
    short = "TOKEN"
    long = "TOKEN-SENSITIVE"
    code = (
        "import os, sys, time; "
        "os.write(1, b'out:TOKEN'); os.write(2, b'err:TOKEN'); "
        "time.sleep(0.05); "
        "os.write(1, b'-SENSITIVE:tail'); os.write(2, b'-SENSITIVE:tail')"
    )
    result = execute_request(
        request(sys.executable, "-c", code, output_limit_bytes=64),
        capability(),
        tmp_path,
        redaction_values=(short, long),
    )

    for output in (result.stdout, result.stderr):
        assert output.redacted is True
        assert long not in output.excerpt
        assert "SENSITIVE" not in output.excerpt
        assert "[REDACTED]" in output.excerpt


def test_offset_overlapping_redaction_patterns_redact_union_across_chunks(
    tmp_path: Path,
) -> None:
    first = "user:password"
    second = "password:SUPERSECRET"
    code = (
        "import os, time; "
        "os.write(1, b'out:user:password'); os.write(2, b'err:user:password'); "
        "time.sleep(0.05); "
        "os.write(1, b':SUPERSECRET:tail'); os.write(2, b':SUPERSECRET:tail')"
    )
    result = execute_request(
        request(sys.executable, "-c", code, output_limit_bytes=96),
        capability(),
        tmp_path,
        redaction_values=(first, second),
    )

    for output in (result.stdout, result.stderr):
        assert output.redacted is True
        assert first not in output.excerpt
        assert second not in output.excerpt
        assert "SUPERSECRET" not in output.excerpt


@pytest.mark.parametrize("output_limit", [1, 3, 8, 32])
def test_repeated_self_overlapping_redaction_is_safe_at_every_limit(
    tmp_path: Path, output_limit: int
) -> None:
    code = (
        "import os, time; "
        "os.write(1, b'b'); os.write(2, b'b'); "
        "time.sleep(0.03); "
        "os.write(1, b'bbaa'); os.write(2, b'bbaa')"
    )
    result = execute_request(
        request(sys.executable, "-c", code, output_limit_bytes=output_limit),
        capability(),
        tmp_path,
        redaction_values=("bb",),
    )

    for output in (result.stdout, result.stderr):
        assert output.redacted is True
        assert len(output.excerpt.encode()) <= output_limit
        # Every b in bbbbaa belongs to an overlapping occurrence of bb.  No
        # single-character prefix/suffix may survive truncation.
        assert "b" not in output.excerpt


@pytest.mark.parametrize("output_limit", [2, 5, 12, 32])
def test_offset_overlapping_literals_are_unioned_at_all_boundaries(
    tmp_path: Path, output_limit: int
) -> None:
    code = (
        "import os; "
        "os.write(1, b'xxabcdefghYY'); os.write(2, b'xxabcdefghYY')"
    )
    result = execute_request(
        request(sys.executable, "-c", code, output_limit_bytes=output_limit),
        capability(),
        tmp_path,
        redaction_values=("abc", "cdefgh"),
    )

    for output in (result.stdout, result.stderr):
        assert output.redacted is True
        assert len(output.excerpt.encode()) <= output_limit
        assert "abc" not in output.excerpt
        assert "cdefgh" not in output.excerpt
        # The overlapping union starts at the only c in the protected span;
        # retaining it would expose a prefix/suffix at a truncation boundary.
        assert "c" not in output.excerpt


def test_same_start_overlapping_literals_are_redacted_across_chunks(
    tmp_path: Path,
) -> None:
    code = (
        "import os, time; "
        "os.write(1, b'xxabcdef'); os.write(2, b'xxabcdef'); "
        "time.sleep(0.03); "
        "os.write(1, b'yy'); os.write(2, b'yy')"
    )
    result = execute_request(
        request(sys.executable, "-c", code, output_limit_bytes=32),
        capability(),
        tmp_path,
        redaction_values=("abc", "abcdef"),
    )

    for output in (result.stdout, result.stderr):
        assert output.redacted is True
        assert "abcdef" not in output.excerpt
        assert "abc" not in output.excerpt


@pytest.mark.parametrize("protected", ["REDACTED", "ACT", "[REDACTED]"])
def test_redaction_marker_collision_fails_closed_on_both_streams(
    tmp_path: Path, protected: str
) -> None:
    code = (
        "import sys; "
        f"print('stdout-{protected}'); "
        f"print('stderr-{protected}', file=sys.stderr)"
    )
    result = execute_request(
        request(sys.executable, "-c", code, output_limit_bytes=96),
        capability(),
        tmp_path,
        redaction_values=(protected,),
    )

    for output in (result.stdout, result.stderr):
        assert output.excerpt == ""
        assert output.redacted is True
        assert output.truncated is True


def test_oversized_redaction_pattern_fails_closed_without_output(
    tmp_path: Path,
) -> None:
    secret = "x" * 5000
    result = execute_request(
        request(
            sys.executable,
            "-c",
            f"print('prefix-{secret}-suffix')",
            output_limit_bytes=64,
        ),
        capability(),
        tmp_path,
        redaction_values=(secret,),
    )

    assert result.stdout.excerpt == ""
    assert result.stdout.redacted is True
    assert result.stdout.truncated is True
    assert secret[:16] not in result.stdout.excerpt


def test_incomplete_identity_valid_snapshot_cannot_start_command(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    changeset = capture_changeset(repo, "main", ScopeMode.PENDING)
    discovery = discover_review_sources(repo)
    with disposable_snapshot(changeset, discovery) as snapshot:
        payload = snapshot.manifest.model_dump(mode="json")
        payload.update(
            complete=False,
            materialization_failure="capability",
            files=[],
        )
        payload["identity"] = hash_payload({key: value for key, value in payload.items() if key != "identity"})
        incomplete = SnapshotManifest.model_validate(payload)
        marker = tmp_path / "must-not-start"
        check = PlannedCheck(
            check_id="incomplete-marker",
            requirement_level=RequirementLevel.REQUIRED,
            kind=CheckKind.COMMAND,
            origin=CheckOrigin.MODEL_PROPOSED,
            selection_reason="Adversarial incomplete-snapshot execution guard.",
            argv=[
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('started')",
            ],
        )
        execution_request = build_execution_request(
            check,
            incomplete,
            timeout_seconds=2,
            output_limit_bytes=1024,
            required_capabilities=[CapabilityName.OUTPUT_LIMITS],
        )
        result = execute_request(
            execution_request,
            capability(),
            snapshot.path,
        )

    assert execution_request.snapshot_validation_failure is FailureKind.CAPABILITY
    assert result.status is ExecutionStatus.NOT_RUN
    assert result.failure_kind is FailureKind.CAPABILITY
    assert result.required_evidence_gap is True
    assert not marker.exists()


def test_disposable_snapshot_is_complete_bound_and_preserves_source(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    before = (
        git(repo, "rev-parse", "HEAD"),
        hashlib.sha256((Path(git(repo, "rev-parse", "--git-path", "index"))).read_bytes()).hexdigest()
        if Path(git(repo, "rev-parse", "--git-path", "index")).is_absolute()
        else hashlib.sha256((repo / git(repo, "rev-parse", "--git-path", "index")).read_bytes()).hexdigest(),
        subprocess.run(
            ["git", "status", "--porcelain=v1", "-z"],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout,
    )
    changeset = capture_changeset(repo, "main", ScopeMode.PENDING)
    discovery = discover_review_sources(repo)

    with disposable_snapshot(changeset, discovery) as snapshot:
        assert (snapshot.path / "tracked.txt").read_text() == "pending\n"
        assert (snapshot.path / "committed.txt").read_text() == "committed pending state\n"
        assert (snapshot.path / "staged.txt").read_text() == "staged pending state\n"
        assert not (snapshot.path / "delete.txt").exists()
        assert (snapshot.path / "new.sh").read_text().startswith("#!/bin/sh")
        assert os.access(snapshot.path / "new.sh", os.X_OK)
        assert not (snapshot.path / ".git").exists()
        assert snapshot.manifest.changeset_identity == changeset.identity
        assert snapshot.manifest.discovery_identity == discovery.identity
        (snapshot.path / "verification-write.txt").write_text("snapshot only\n")

    index_path = Path(git(repo, "rev-parse", "--git-path", "index"))
    if not index_path.is_absolute():
        index_path = repo / index_path
    after = (
        git(repo, "rev-parse", "HEAD"),
        hashlib.sha256(index_path.read_bytes()).hexdigest(),
        subprocess.run(
            ["git", "status", "--porcelain=v1", "-z"],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout,
    )
    assert before == after
    assert not (repo / "verification-write.txt").exists()


def test_explicit_ignored_include_survives_snapshot_recapture_and_executes(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    (repo / ".gitignore").write_text("ignored-verification.txt\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "ignore verification fixture")
    (repo / "ignored-verification.txt").write_text("explicit evidence\n")
    changeset = capture_changeset(
        repo,
        "main",
        ScopeMode.PENDING,
        explicit_includes=[b"ignored-verification.txt"],
    )
    discovery = discover_review_sources(repo)

    with disposable_snapshot(changeset, discovery) as snapshot:
        assert (
            snapshot.path / "ignored-verification.txt"
        ).read_text() == "explicit evidence\n"
        execution = execute_request(
            request(
                sys.executable,
                "-c",
                "from pathlib import Path; assert Path('ignored-verification.txt').read_text() == 'explicit evidence\\n'",
                snapshot_identity=snapshot.manifest.identity,
            ),
            capability(),
            snapshot.path,
        )

    assert execution.status is ExecutionStatus.PASSED


@pytest.mark.parametrize("content_limit", [0, 1])
def test_omitted_effective_content_becomes_structured_required_evidence_gap(
    tmp_path: Path, content_limit: int
) -> None:
    repo = repository(tmp_path)
    (repo / "pyproject.toml").write_text(
        """
[tool.pre-pr-verify.verification]
checks = [{ id = "canonical", level = "required", argv = ["python", "-c", "raise SystemExit(99)"] }]
""".strip()
        + "\n"
    )
    changeset = capture_changeset(
        repo,
        "main",
        ScopeMode.PENDING,
        limits=ContentLimits(
            per_file_bytes=content_limit, total_bytes=content_limit
        ),
    )
    assert changeset.empty is False
    discovery = discover_review_sources(repo)
    plan = build_verification_plan(
        changeset,
        discovery,
        canonical_checks=discover_canonical_checks(repo),
        trusted_policy_checks=[],
        planner_additions=[],
    )

    evidence = execute_verification_plan(
        changeset,
        discovery,
        plan,
        capability(),
        timeout_seconds=1,
        output_limit_bytes=1024,
        required_capabilities=[CapabilityName.OUTPUT_LIMITS],
    )

    result = evidence.executions[0].result
    assert result.status is ExecutionStatus.NOT_RUN
    assert result.failure_kind is FailureKind.CAPABILITY
    assert result.required_evidence_gap is True
    assert evidence.executions[0].snapshot.complete is False
    assert evidence.executions[0].snapshot.materialization_failure is FailureKind.CAPABILITY

    malicious = evidence.model_dump(mode="json")
    malicious["source_preservation_failures"] = [
        {
            "ordinal": 0,
            "check_id": result.request.check_id,
            "snapshot_identity": evidence.executions[0].snapshot.identity,
            "failure_kind": "capability",
            "required_evidence_gap": True,
            "reason": "forged post-execution signal",
        }
    ]
    malicious["identity"] = hash_payload(
        {key: value for key, value in malicious.items() if key != "identity"}
    )
    with pytest.raises(ValidationError, match="not-run execution"):
        VerificationEvidence.model_validate(malicious)


def test_snapshot_rejects_discovery_from_another_repository_moment(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    old_discovery = discover_review_sources(repo)
    outside = tmp_path / "outside-requirement.md"
    outside.write_text("# Changed after discovery\n")
    (repo / "README.md").unlink()
    (repo / "README.md").symlink_to(outside)
    changeset = capture_changeset(repo, "main", ScopeMode.PENDING)

    with pytest.raises(PreflightError, match="discovery evidence"):
        with disposable_snapshot(changeset, old_discovery):
            pass


def test_snapshot_rejects_canonical_guidance_from_another_moment(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    (repo / "pyproject.toml").write_text(
        """
[tool.pre-pr-verify.verification]
checks = [{ id = "canonical", level = "required", argv = ["python", "-c", "print('old')"] }]
""".strip()
        + "\n"
    )
    stale_checks = discover_canonical_checks(repo)
    outside = tmp_path / "outside-pyproject.toml"
    outside.write_text(
        """
[tool.pre-pr-verify.verification]
checks = [{ id = "canonical", level = "required", argv = ["python", "-c", "print('new')"] }]
""".strip()
        + "\n"
    )
    (repo / "pyproject.toml").unlink()
    (repo / "pyproject.toml").symlink_to(outside)
    changeset = capture_changeset(repo, "main", ScopeMode.PENDING)
    discovery = discover_review_sources(repo)
    plan = build_verification_plan(
        changeset,
        discovery,
        canonical_checks=stale_checks,
        trusted_policy_checks=[],
        planner_additions=[],
    )

    with pytest.raises(PreflightError, match="verification guidance"):
        with disposable_snapshot(changeset, discovery, plan=plan):
            pass


def test_bound_plan_snapshot_request_and_result_form_canonical_evidence(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    (repo / "pyproject.toml").write_text(
        """
[tool.pre-pr-verify.verification]
checks = [{ id = "canonical", level = "required", argv = ["python", "-c", "print('verified')"] }]
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
        planner_additions=[],
    )

    with disposable_snapshot(changeset, discovery, plan=plan) as snapshot:
        check = next(item for item in plan.checks if item.check_id == "canonical")
        execution_request = build_execution_request(
            check,
            snapshot.manifest,
            timeout_seconds=2,
            output_limit_bytes=1024,
            required_capabilities=[CapabilityName.OUTPUT_LIMITS],
        )
        result = execute_request(
            execution_request, capability(), snapshot.path
        )
        evidence = build_verification_evidence(plan, [(snapshot.manifest, result)])

    assert result.status is ExecutionStatus.PASSED
    assert result.request.snapshot_identity == evidence.executions[0].snapshot.identity
    assert VerificationEvidence.model_validate_json(evidence.model_dump_json()) == evidence


def test_each_planned_command_runs_in_a_fresh_pristine_snapshot(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    (repo / "pyproject.toml").write_text(
        """
[tool.pre-pr-verify.verification]
checks = [
  { id = "a-mutates", level = "required", argv = ["python", "-c", "from pathlib import Path; Path('tracked.txt').write_text('mutated'); Path('marker').write_text('created')"] },
  { id = "b-observes-pristine", level = "required", argv = ["python", "-c", "from pathlib import Path; assert Path('tracked.txt').read_text().startswith('pending'); assert not Path('marker').exists()"] },
]
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
        planner_additions=[],
    )

    evidence = execute_verification_plan(
        changeset,
        discovery,
        plan,
        capability(),
        timeout_seconds=2,
        output_limit_bytes=1024,
        required_capabilities=[CapabilityName.OUTPUT_LIMITS],
    )

    assert [item.result.status for item in evidence.executions] == [
        ExecutionStatus.PASSED,
        ExecutionStatus.PASSED,
    ]
    assert [item.snapshot.materialization_ordinal for item in evidence.executions] == [
        0,
        1,
    ]
    assert len({item.snapshot.identity for item in evidence.executions}) == 2
    shared_payload = evidence.model_dump(mode="json")
    shared_payload["executions"][1]["snapshot"] = shared_payload["executions"][0][
        "snapshot"
    ]
    shared_payload["executions"][1]["result"]["request"]["snapshot_identity"] = (
        shared_payload["executions"][0]["snapshot"]["identity"]
    )
    with pytest.raises(ValidationError, match="ordinal"):
        VerificationEvidence.model_validate(shared_payload)
    assert not (repo / "marker").exists()
    assert (repo / "tracked.txt").read_text() == "pending\n"


def test_post_execution_source_change_preserves_result_and_records_gap(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    (repo / "pyproject.toml").write_text(
        """
[tool.pre-pr-verify.verification]
checks = [{ id = "source-race", level = "required", argv = ["python", "-c", "pass"] }]
""".strip()
        + "\n"
    )
    changeset = capture_changeset(repo, "main", ScopeMode.PENDING)
    discovery = discover_review_sources(repo)
    planned = discover_canonical_checks(repo)[0]
    planned = replace(
        planned,
        argv=(
            sys.executable,
            "-c",
            (
                "import time; time.sleep(0.1); "
                f"from pathlib import Path; Path({str(repo / 'tracked.txt')!r}).write_text('changed-after-start'); "
                "print('child-completed')"
            ),
        ),
    )
    plan = build_verification_plan(
        changeset,
        discovery,
        canonical_checks=[planned],
        trusted_policy_checks=[],
        planner_additions=[],
    )

    evidence = execute_verification_plan(
        changeset,
        discovery,
        plan,
        capability(),
        timeout_seconds=2,
        output_limit_bytes=1024,
        required_capabilities=[CapabilityName.OUTPUT_LIMITS],
    )

    result = evidence.executions[0].result
    assert result.status is ExecutionStatus.PASSED
    assert result.stdout.excerpt == "child-completed\n"
    assert len(evidence.source_preservation_failures) == 1
    failure = evidence.source_preservation_failures[0]
    assert failure.check_id == "source-race"
    assert failure.ordinal == 0
    assert failure.failure_kind is FailureKind.CAPABILITY
    assert failure.required_evidence_gap is True
    assert VerificationEvidence.model_validate_json(evidence.model_dump_json()) == evidence
