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
    EnvironmentProfile,
    PlannedCheck,
    ExecutionRequest,
    FailureKind,
    ProfileProvenance,
    ProfileProvenanceChannel,
    RequirementLevel,
    SnapshotManifest,
    VerificationPlan,
    hash_payload,
    planned_check_sort_key,
    resolve_profile_provenance,
)


@dataclass(frozen=True)
class PlannerCheckInput:
    check_id: str
    requirement_level: RequirementLevel
    selection_reason: str
    argv: tuple[str, ...]
    cwd: str = "."
    environment_profile: EnvironmentProfile = EnvironmentProfile.FILESYSTEM_ONLY
    profile_source_sha256: str | None = None


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
    environment_profile: EnvironmentProfile = EnvironmentProfile.FILESYSTEM_ONLY


@dataclass(frozen=True)
class TrustedPolicyCheckInput:
    check_id: str
    requirement_level: RequirementLevel
    selection_reason: str
    argv: tuple[str, ...]
    policy_label: str
    policy_sha256: str
    cwd: str = "."
    environment_profile: EnvironmentProfile = EnvironmentProfile.FILESYSTEM_ONLY


_CANDIDATE_ORIGIN_ORDER = {
    CheckOrigin.TRUSTED_POLICY: 0,
    CheckOrigin.REPOSITORY_CANONICAL: 1,
    CheckOrigin.MODEL_PROPOSED: 2,
}
_CANDIDATE_ORIGIN_CHANNEL = {
    CheckOrigin.TRUSTED_POLICY: ProfileProvenanceChannel.TRUSTED_POLICY,
    CheckOrigin.REPOSITORY_CANONICAL: ProfileProvenanceChannel.REPOSITORY_DECLARATION,
    CheckOrigin.MODEL_PROPOSED: ProfileProvenanceChannel.MODEL_PROPOSAL,
}


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
    profile = _coerce_environment_profile(value.environment_profile)
    return PlannedCheck(
        check_id=value.check_id,
        requirement_level=value.requirement_level,
        kind=CheckKind.COMMAND,
        origin=CheckOrigin.MODEL_PROPOSED,
        selection_reason=value.selection_reason,
        argv=list(value.argv),
        cwd=value.cwd,
        environment_profile=profile,
        profile_provenance=_profile_entries(
            ProfileProvenanceChannel.MODEL_PROPOSAL,
            profile,
            value.profile_source_sha256 or _planner_profile_digest(value),
        ),
    )


def _repository_check(value: RepositoryCheckInput) -> PlannedCheck:
    profile = _coerce_environment_profile(value.environment_profile)
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
        environment_profile=profile,
        profile_provenance=_profile_entries(
            ProfileProvenanceChannel.REPOSITORY_DECLARATION,
            profile,
            value.source_sha256,
        ),
    )


def _trusted_policy_check(value: TrustedPolicyCheckInput) -> PlannedCheck:
    profile = _coerce_environment_profile(value.environment_profile)
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
        environment_profile=profile,
        profile_provenance=_profile_entries(
            ProfileProvenanceChannel.TRUSTED_POLICY,
            profile,
            value.policy_sha256,
        ),
    )


def _coerce_environment_profile(value: EnvironmentProfile | str) -> EnvironmentProfile:
    try:
        return EnvironmentProfile(value)
    except (TypeError, ValueError) as error:
        raise PreflightError(f"unsupported environment profile: {value!r}") from error


def _profile_entries(
    channel: ProfileProvenanceChannel,
    profile: EnvironmentProfile,
    source_sha256: str,
) -> list[ProfileProvenance]:
    if profile is EnvironmentProfile.FILESYSTEM_ONLY:
        return []
    try:
        return [
            ProfileProvenance(
                channel=channel,
                requested_profile=profile,
                source_sha256=source_sha256,
            )
        ]
    except ValueError as error:
        raise PreflightError(
            f"{channel.value} profile requirement lacks a valid stable digest"
        ) from error


def _planner_profile_digest(value: PlannerCheckInput) -> str:
    return hash_payload(
        {
            "channel": ProfileProvenanceChannel.MODEL_PROPOSAL.value,
            "check_id": value.check_id,
            "requirement_level": value.requirement_level.value,
            "argv": list(value.argv),
            "cwd": value.cwd,
            "environment_profile": _coerce_environment_profile(
                value.environment_profile
            ).value,
            "selection_reason": value.selection_reason,
        }
    )


def _user_profile_entry(
    profile: EnvironmentProfile | str,
    source_sha256: str | None,
) -> list[ProfileProvenance]:
    resolved = _coerce_environment_profile(profile)
    if resolved is EnvironmentProfile.FILESYSTEM_ONLY:
        return []
    digest = source_sha256 or hash_payload(
        {
            "channel": ProfileProvenanceChannel.USER_INVOCATION.value,
            "environment_profile": resolved.value,
        }
    )
    return _profile_entries(
        ProfileProvenanceChannel.USER_INVOCATION,
        resolved,
        digest,
    )


def _execution_definition(check: PlannedCheck) -> tuple[object, ...]:
    return (
        check.requirement_level,
        check.kind,
        tuple(check.argv or []),
        check.cwd,
    )


def _merge_check_candidates(
    candidates: list[PlannedCheck],
    user_profile_entries: list[ProfileProvenance],
) -> PlannedCheck:
    primary = min(
        candidates,
        key=lambda check: _CANDIDATE_ORIGIN_ORDER[check.origin],
    )
    channels = [_CANDIDATE_ORIGIN_CHANNEL[check.origin] for check in candidates]
    if len(channels) != len(set(channels)):
        raise PreflightError(
            f"duplicate profile requirement channel for verification check: {primary.check_id}"
        )
    definition = _execution_definition(primary)
    if any(_execution_definition(check) != definition for check in candidates):
        raise PreflightError(
            f"verification check ID collides with conflicting definition: {primary.check_id}"
        )
    requested = [
        entry
        for check in candidates
        for entry in check.profile_provenance
    ]
    requested.extend(user_profile_entries)
    try:
        profile, provenance = resolve_profile_provenance(requested)
        return PlannedCheck.model_validate(
            {
                **primary.model_dump(mode="json"),
                "environment_profile": profile,
                "profile_provenance": provenance,
            }
        )
    except ValueError as error:
        raise PreflightError(
            f"invalid profile requirements for verification check: {primary.check_id}"
        ) from error


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
        if not isinstance(item, dict) or set(item) - {
            "id",
            "level",
            "argv",
            "cwd",
            "reason",
            "environment_profile",
        }:
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
        try:
            environment_profile = _coerce_environment_profile(
                item.get(
                    "environment_profile",
                    EnvironmentProfile.FILESYSTEM_ONLY.value,
                )
            )
        except PreflightError as error:
            raise PreflightError("canonical verification check is malformed") from error
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
                environment_profile=environment_profile,
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
    minimum_environment_profile: EnvironmentProfile = EnvironmentProfile.FILESYSTEM_ONLY,
    user_invocation_sha256: str | None = None,
) -> VerificationPlan:
    if Path(changeset.repository_root).resolve() != Path(discovery.repository_root).resolve():
        raise PreflightError("ChangeSet and discovery repository roots do not match")
    repository = [_repository_check(value) for value in canonical_checks]
    trusted = [_trusted_policy_check(value) for value in trusted_policy_checks]
    proposed = [_planner_check(value) for value in planner_additions]
    proposed_ids = [check.check_id for check in proposed]
    if len(set(proposed_ids)) != len(proposed_ids):
        raise PreflightError("planner check IDs must be unique")
    user_profile_entries = _user_profile_entry(
        minimum_environment_profile,
        user_invocation_sha256,
    )
    candidates = [*trusted, *repository, *proposed]
    grouped: dict[str, list[PlannedCheck]] = {}
    for check in candidates:
        grouped.setdefault(check.check_id, []).append(check)
    checks = [*_FLOOR]
    checks.extend(
        _merge_check_candidates(group, user_profile_entries)
        for group in grouped.values()
    )
    checks.sort(key=planned_check_sort_key)
    signals = extract_change_signals(changeset)
    provisional = VerificationPlan.model_construct(
        contract="verification_plan",
        schema_version="1.1.0",
        changeset_identity=changeset.identity,
        discovery_identity=discovery.identity,
        signals=signals,
        checks=checks,
        identity="",
    )
    return VerificationPlan.model_validate(
        {
            "contract": "verification_plan",
            "schema_version": "1.1.0",
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
    # A generic post-launch nonzero does not prove that the candidate
    # verification workload ran or that its failure represents a change
    # failure. Trusted callers may provide an explicit attribution policy.
    nonzero_failure_kind: FailureKind = FailureKind.UNCLASSIFIED,
    snapshot_validation_failure: FailureKind | None = None,
) -> ExecutionRequest:
    if check.kind is not CheckKind.COMMAND or check.argv is None:
        raise PreflightError("only command checks can become execution requests")
    if snapshot.environment_profile is not check.environment_profile:
        raise PreflightError(
            "snapshot environment profile does not match planned check"
        )
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
        environment_profile=check.environment_profile,
        nonzero_failure_kind=nonzero_failure_kind,
        snapshot_validation_failure=snapshot_validation_failure,
    )
