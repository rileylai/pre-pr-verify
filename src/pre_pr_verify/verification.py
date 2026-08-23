from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from pre_pr_verify.discovery_models import DiscoveryResult
from pre_pr_verify.errors import PreflightError
from pre_pr_verify.git_capture import _RootedReader, _resolve_repository
from pre_pr_verify.models import ChangeOrigin, ChangeSet, FileKind, RawPath
from pre_pr_verify.verification_models import (
    CapabilityName,
    ChangeSignals,
    CheckKind,
    CheckOrigin,
    PlannedCheck,
    ExecutionRequest,
    FailureKind,
    RequirementLevel,
    SnapshotManifest,
    VerificationPlan,
    hash_payload,
    planned_check_sort_key,
)


@dataclass(frozen=True)
class PlannerCheckInput:
    check_id: str
    requirement_level: RequirementLevel
    selection_reason: str
    argv: tuple[str, ...]
    cwd: str = "."


@dataclass(frozen=True)
class RepositoryCheckInput:
    check_id: str
    requirement_level: RequirementLevel
    selection_reason: str
    argv: tuple[str, ...]
    cwd: str
    source_path: bytes
    source_sha256: str
    source_size: int


@dataclass(frozen=True)
class TrustedPolicyCheckInput:
    check_id: str
    requirement_level: RequirementLevel
    selection_reason: str
    argv: tuple[str, ...]
    policy_label: str
    policy_sha256: str
    cwd: str = "."


_FLOOR = (
    PlannedCheck(
        check_id="scope-capture",
        requirement_level=RequirementLevel.REQUIRED,
        kind=CheckKind.STRUCTURAL_INVARIANT,
        origin=CheckOrigin.DETERMINISTIC_FLOOR,
        selection_reason="The complete pending scope and ChangeSet identity must be valid.",
    ),
    PlannedCheck(
        check_id="source-preservation",
        requirement_level=RequirementLevel.REQUIRED,
        kind=CheckKind.STRUCTURAL_INVARIANT,
        origin=CheckOrigin.DETERMINISTIC_FLOOR,
        selection_reason="Verification must preserve the author HEAD, index, and working tree.",
    ),
    PlannedCheck(
        check_id="result-classification",
        requirement_level=RequirementLevel.REQUIRED,
        kind=CheckKind.STRUCTURAL_INVARIANT,
        origin=CheckOrigin.DETERMINISTIC_FLOOR,
        selection_reason="Every selected command outcome must receive a complete classification.",
    ),
)


def _is_test_path(path: bytes) -> bool:
    parts = [part.lower() for part in path.split(b"/")]
    basename = parts[-1]
    return b"tests" in parts or b"test" in parts or basename.startswith((b"test_", b"test-"))


def _is_documentation_path(path: bytes) -> bool:
    parts = [part.lower() for part in path.split(b"/")]
    return b"docs" in parts or parts[-1].startswith(b"readme") or parts[-1].endswith(b".md")


def extract_change_signals(changeset: ChangeSet) -> ChangeSignals:
    paths = sorted(
        [change.effective.path for change in changeset.changes],
        key=lambda value: value.to_bytes(),
    )
    origins = {
        origin: sum(origin in change.origins for change in changeset.changes)
        for origin in ChangeOrigin
    }
    return ChangeSignals(
        changed_paths=paths,
        changed_path_count=len(paths),
        test_path_count=sum(_is_test_path(path.to_bytes()) for path in paths),
        documentation_path_count=sum(
            _is_documentation_path(path.to_bytes()) for path in paths
        ),
        added_path_count=sum(
            change.head.kind is FileKind.ABSENT
            and change.effective.kind is not FileKind.ABSENT
            for change in changeset.changes
        ),
        deleted_path_count=sum(
            change.effective.kind is FileKind.ABSENT for change in changeset.changes
        ),
        executable_path_count=sum(
            change.effective.mode == "100755" for change in changeset.changes
        ),
        committed_path_count=origins[ChangeOrigin.COMMITTED],
        staged_path_count=origins[ChangeOrigin.STAGED],
        unstaged_path_count=origins[ChangeOrigin.UNSTAGED],
        untracked_path_count=origins[ChangeOrigin.UNTRACKED],
    )


def _planner_check(value: PlannerCheckInput) -> PlannedCheck:
    return PlannedCheck(
        check_id=value.check_id,
        requirement_level=value.requirement_level,
        kind=CheckKind.COMMAND,
        origin=CheckOrigin.MODEL_PROPOSED,
        selection_reason=value.selection_reason,
        argv=list(value.argv),
        cwd=value.cwd,
    )


def _repository_check(value: RepositoryCheckInput) -> PlannedCheck:
    return PlannedCheck(
        check_id=value.check_id,
        requirement_level=value.requirement_level,
        kind=CheckKind.COMMAND,
        origin=CheckOrigin.REPOSITORY_CANONICAL,
        selection_reason=value.selection_reason,
        argv=list(value.argv),
        cwd=value.cwd,
        source_path=RawPath.from_bytes(value.source_path),
        source_sha256=value.source_sha256,
        source_size=value.source_size,
    )


def _trusted_policy_check(value: TrustedPolicyCheckInput) -> PlannedCheck:
    return PlannedCheck(
        check_id=value.check_id,
        requirement_level=value.requirement_level,
        kind=CheckKind.COMMAND,
        origin=CheckOrigin.TRUSTED_POLICY,
        selection_reason=value.selection_reason,
        argv=list(value.argv),
        cwd=value.cwd,
        trusted_policy_label=value.policy_label,
        trusted_policy_sha256=value.policy_sha256,
    )


def discover_canonical_checks(repository: Path | str) -> list[RepositoryCheckInput]:
    root, _runner = _resolve_repository(Path(repository))
    reader = _RootedReader(root, "canonical verification discovery")
    raw = reader.read_file(b"pyproject.toml", limit=1_048_576)
    if raw is None:
        return []
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise PreflightError("pyproject.toml cannot be parsed safely") from error
    section = (
        document.get("tool", {})
        .get("pre-pr-verify", {})
        .get("verification", {})
    )
    configured = section.get("checks")
    if configured is None:
        return []
    if not isinstance(configured, list) or len(configured) > 64:
        raise PreflightError("canonical verification checks must be a bounded list")
    digest = hashlib.sha256(raw).hexdigest()
    result: list[RepositoryCheckInput] = []
    for item in configured:
        if not isinstance(item, dict) or set(item) - {"id", "level", "argv", "cwd", "reason"}:
            raise PreflightError("canonical verification check has unsupported fields")
        try:
            check_id = item["id"]
            level = RequirementLevel(item["level"])
            argv = item["argv"]
        except (KeyError, TypeError, ValueError) as error:
            raise PreflightError("canonical verification check is malformed") from error
        if (
            not isinstance(check_id, str)
            or not isinstance(argv, list)
            or not argv
            or any(not isinstance(value, str) or not value for value in argv)
        ):
            raise PreflightError("canonical verification check is malformed")
        cwd = item.get("cwd", ".")
        reason = item.get(
            "reason", "Declared by repository-native canonical verification guidance."
        )
        if not isinstance(cwd, str) or not isinstance(reason, str) or not reason:
            raise PreflightError("canonical verification check is malformed")
        result.append(
            RepositoryCheckInput(
                check_id=check_id,
                requirement_level=level,
                selection_reason=reason,
                argv=tuple(argv),
                cwd=cwd,
                source_path=b"pyproject.toml",
                source_sha256=digest,
                source_size=len(raw),
            )
        )
    ordered = sorted(result, key=lambda check: check.check_id)
    if len({check.check_id for check in ordered}) != len(ordered):
        raise PreflightError("canonical verification check IDs must be unique")
    return ordered


def build_verification_plan(
    changeset: ChangeSet,
    discovery: DiscoveryResult,
    *,
    canonical_checks: Iterable[RepositoryCheckInput],
    trusted_policy_checks: Iterable[TrustedPolicyCheckInput],
    planner_additions: Iterable[PlannerCheckInput],
) -> VerificationPlan:
    if Path(changeset.repository_root).resolve() != Path(discovery.repository_root).resolve():
        raise PreflightError("ChangeSet and discovery repository roots do not match")
    repository = [_repository_check(value) for value in canonical_checks]
    trusted = [_trusted_policy_check(value) for value in trusted_policy_checks]
    proposed = [_planner_check(value) for value in planner_additions]
    checks = [*_FLOOR, *trusted, *repository]
    protected_ids = {check.check_id for check in checks}
    if len(protected_ids) != len(checks):
        raise PreflightError("protected verification check IDs collide")
    proposed_ids = [check.check_id for check in proposed]
    if len(set(proposed_ids)) != len(proposed_ids):
        raise PreflightError("planner check IDs must be unique")
    collisions = protected_ids.intersection(proposed_ids)
    if collisions:
        raise PreflightError(
            f"planner check ID collides with protected check: {sorted(collisions)[0]}"
        )
    checks.extend(proposed)
    checks.sort(key=planned_check_sort_key)
    signals = extract_change_signals(changeset)
    provisional = VerificationPlan.model_construct(
        contract="verification_plan",
        schema_version="1.0.0",
        changeset_identity=changeset.identity,
        discovery_identity=discovery.identity,
        signals=signals,
        checks=checks,
        identity="",
    )
    return VerificationPlan.model_validate(
        {
            "contract": "verification_plan",
            "schema_version": "1.0.0",
            "changeset_identity": changeset.identity,
            "discovery_identity": discovery.identity,
            "signals": signals,
            "checks": checks,
            "identity": hash_payload(provisional.semantic_payload()),
        }
    )


def build_execution_request(
    check: PlannedCheck,
    snapshot: SnapshotManifest,
    *,
    timeout_seconds: float,
    output_limit_bytes: int,
    required_capabilities: Iterable[CapabilityName],
    nonzero_failure_kind: FailureKind = FailureKind.VERIFICATION,
    snapshot_validation_failure: FailureKind | None = None,
) -> ExecutionRequest:
    if check.kind is not CheckKind.COMMAND or check.argv is None:
        raise PreflightError("only command checks can become execution requests")
    manifest_failure = snapshot.materialization_failure
    if not snapshot.complete and manifest_failure is None:
        # Fail closed even for an object constructed outside normal model
        # validation; an incomplete manifest is never an executable snapshot.
        manifest_failure = FailureKind.CAPABILITY
    if snapshot.complete and manifest_failure is not None:
        # Treat externally constructed inconsistent manifests as unavailable
        # evidence rather than allowing them to produce an executable request.
        manifest_failure = FailureKind.CAPABILITY
    if snapshot_validation_failure is not None and (
        manifest_failure is None
        or snapshot_validation_failure is not manifest_failure
    ):
        raise PreflightError(
            "snapshot validation failure does not match manifest eligibility"
        )
    snapshot_validation_failure = manifest_failure
    return ExecutionRequest(
        check_id=check.check_id,
        snapshot_identity=snapshot.identity,
        requirement_level=check.requirement_level,
        argv=check.argv,
        cwd=check.cwd,
        timeout_seconds=timeout_seconds,
        output_limit_bytes=output_limit_bytes,
        required_capabilities=list(required_capabilities),
        nonzero_failure_kind=nonzero_failure_kind,
        snapshot_validation_failure=snapshot_validation_failure,
    )
