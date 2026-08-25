from __future__ import annotations

import pytest
from pydantic import ValidationError

from pre_pr_verify.errors import PreflightError
from pre_pr_verify.verification_models import (
    CapabilityName,
    ChangeSignals,
    CheckKind,
    CheckOrigin,
    EnvironmentProfile,
    ExecutionCapability,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    FailureKind,
    GitObjectFormat,
    LegacyPlannedCheck,
    LegacyVerificationEvidence,
    LegacyVerificationPlan,
    OutputEvidence,
    PlannedCheck,
    ProfileProvenance,
    ProfileProvenanceChannel,
    RequirementLevel,
    SnapshotManifest,
    VerificationEvidence,
    VerificationPlan,
    build_verification_evidence,
    derive_execution_decision,
    hash_payload,
    load_verification_evidence,
    load_verification_plan,
    resolve_profile_provenance,
    planned_check_sort_key,
)


def signals() -> ChangeSignals:
    return ChangeSignals(
        changed_paths=[],
        changed_path_count=0,
        test_path_count=0,
        documentation_path_count=0,
        added_path_count=0,
        deleted_path_count=0,
        executable_path_count=0,
        committed_path_count=0,
        staged_path_count=0,
        unstaged_path_count=0,
        untracked_path_count=0,
    )


def profile_entry(
    channel: ProfileProvenanceChannel = ProfileProvenanceChannel.MODEL_PROPOSAL,
    profile: EnvironmentProfile = EnvironmentProfile.GIT_REPOSITORY,
    digest: str = "a" * 64,
) -> ProfileProvenance:
    return ProfileProvenance(
        channel=channel,
        requested_profile=profile,
        source_sha256=digest,
    )


def command_check(
    profile: EnvironmentProfile = EnvironmentProfile.FILESYSTEM_ONLY,
    provenance: list[ProfileProvenance] | None = None,
) -> PlannedCheck:
    return PlannedCheck(
        check_id="command",
        requirement_level=RequirementLevel.REQUIRED,
        kind=CheckKind.COMMAND,
        origin=CheckOrigin.MODEL_PROPOSED,
        selection_reason="Contract fixture.",
        argv=["python", "-c", "pass"],
        environment_profile=profile,
        profile_provenance=provenance or [],
    )


def plan(
    profile: EnvironmentProfile = EnvironmentProfile.FILESYSTEM_ONLY,
    provenance: list[ProfileProvenance] | None = None,
    include_command: bool = True,
) -> VerificationPlan:
    checks = [
        PlannedCheck(
            check_id=check_id,
            requirement_level=RequirementLevel.REQUIRED,
            kind=CheckKind.STRUCTURAL_INVARIANT,
            origin=CheckOrigin.DETERMINISTIC_FLOOR,
            selection_reason="Contract floor.",
        )
        for check_id in (
            "scope-capture",
            "source-preservation",
            "result-classification",
        )
    ]
    if include_command:
        checks.append(command_check(profile, provenance))
    checks.sort(key=planned_check_sort_key)
    provisional = VerificationPlan.model_construct(
        changeset_identity="a" * 64,
        discovery_identity="b" * 64,
        signals=signals(),
        checks=checks,
        identity="",
    )
    return VerificationPlan.model_validate(
        {
            "changeset_identity": "a" * 64,
            "discovery_identity": "b" * 64,
            "signals": signals(),
            "checks": checks,
            "identity": hash_payload(provisional.semantic_payload()),
        }
    )


def snapshot(
    profile: EnvironmentProfile = EnvironmentProfile.FILESYSTEM_ONLY,
    object_format: GitObjectFormat | None = None,
) -> SnapshotManifest:
    payload = {
        "materialization_ordinal": 0,
        "changeset_identity": "a" * 64,
        "discovery_identity": "b" * 64,
        "environment_profile": profile.value,
        "object_format": object_format.value if object_format is not None else None,
        "files": [],
        "complete": True,
        "materialization_failure": None,
    }
    return SnapshotManifest(**payload, identity=hash_payload(payload))


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


def result_for(
    manifest: SnapshotManifest,
    profile: EnvironmentProfile,
) -> ExecutionResult:
    request = ExecutionRequest(
        check_id="command",
        snapshot_identity=manifest.identity,
        requirement_level=RequirementLevel.REQUIRED,
        argv=["python", "-c", "pass"],
        timeout_seconds=1,
        output_limit_bytes=1024,
        required_capabilities=[CapabilityName.OUTPUT_LIMITS],
        environment_profile=profile,
    )
    available = capability()
    return ExecutionResult(
        request=request,
        capability=available,
        decision=derive_execution_decision(request, available),
        status=ExecutionStatus.PASSED,
        failure_kind=None,
        exit_code=0,
        duration_ms=1,
        stdout=OutputEvidence(
            excerpt="",
            sha256="e" * 64,
            total_bytes=0,
            truncated=False,
            redacted=False,
        ),
        stderr=OutputEvidence(
            excerpt="",
            sha256="e" * 64,
            total_bytes=0,
            truncated=False,
            redacted=False,
        ),
        required_evidence_gap=False,
    )


def test_profile_provenance_is_bounded_and_monotonic() -> None:
    lower = profile_entry(
        ProfileProvenanceChannel.REPOSITORY_DECLARATION,
        EnvironmentProfile.FILESYSTEM_ONLY,
    )
    higher = profile_entry(
        ProfileProvenanceChannel.TRUSTED_POLICY,
        EnvironmentProfile.GIT_REPOSITORY,
    )

    resolved, retained = resolve_profile_provenance([higher, lower])

    assert resolved is EnvironmentProfile.GIT_REPOSITORY
    assert retained == [higher]
    assert command_check(
        EnvironmentProfile.GIT_REPOSITORY,
        [higher],
    ).profile_provenance == [higher]

    with pytest.raises(ValidationError, match="profile provenance"):
        command_check(
            EnvironmentProfile.GIT_REPOSITORY,
            [profile_entry(ProfileProvenanceChannel.MODEL_PROPOSAL), higher],
        )


def test_duplicate_profile_provenance_channel_is_rejected() -> None:
    first = profile_entry(ProfileProvenanceChannel.MODEL_PROPOSAL)
    second = profile_entry(ProfileProvenanceChannel.MODEL_PROPOSAL, digest="b" * 64)

    with pytest.raises(ValidationError, match="unique"):
        command_check(EnvironmentProfile.GIT_REPOSITORY, [first, second])


def test_planned_profile_tampering_fails_without_identity_recomputation() -> None:
    value = plan()
    payload = value.model_dump(mode="json")
    payload["checks"][-1]["environment_profile"] = EnvironmentProfile.GIT_REPOSITORY.value

    with pytest.raises(ValidationError):
        VerificationPlan.model_validate(payload)


def test_profile_provenance_tampering_fails_without_identity_recomputation() -> None:
    value = plan(
        EnvironmentProfile.GIT_REPOSITORY,
        [profile_entry()],
    )
    payload = value.model_dump(mode="json")
    payload["checks"][-1]["profile_provenance"][0]["source_sha256"] = "b" * 64

    with pytest.raises(ValidationError, match="identity"):
        VerificationPlan.model_validate(payload)


def test_request_profile_mismatch_against_planned_check_is_rejected() -> None:
    planned = plan()
    git_snapshot = snapshot(
        EnvironmentProfile.GIT_REPOSITORY,
        GitObjectFormat.SHA1,
    )
    result = result_for(git_snapshot, EnvironmentProfile.GIT_REPOSITORY)

    with pytest.raises(ValidationError, match="planned command"):
        build_verification_evidence(planned, [(git_snapshot, result)])


def test_snapshot_profile_mismatch_against_request_is_rejected() -> None:
    planned = plan(
        EnvironmentProfile.GIT_REPOSITORY,
        [profile_entry()],
    )
    filesystem_snapshot = snapshot()
    result = result_for(filesystem_snapshot, EnvironmentProfile.GIT_REPOSITORY)

    with pytest.raises(ValidationError, match="snapshot profile"):
        build_verification_evidence(planned, [(filesystem_snapshot, result)])


def test_git_object_format_participates_in_snapshot_identity() -> None:
    sha1 = snapshot(EnvironmentProfile.GIT_REPOSITORY, GitObjectFormat.SHA1)
    sha256 = snapshot(EnvironmentProfile.GIT_REPOSITORY, GitObjectFormat.SHA256)

    assert sha1.identity != sha256.identity


def test_filesystem_only_cannot_masquerade_as_git_repository() -> None:
    with pytest.raises(ValidationError, match="filesystem-only"):
        snapshot(EnvironmentProfile.FILESYSTEM_ONLY, GitObjectFormat.SHA1)
    with pytest.raises(ValidationError, match="requires object format"):
        snapshot(EnvironmentProfile.GIT_REPOSITORY)


def test_new_contracts_round_trip_with_explicit_filesystem_profile() -> None:
    value = plan(include_command=False)
    evidence = build_verification_evidence(value, [])

    assert value.model_dump(mode="json")["checks"][0]["environment_profile"] == (
        EnvironmentProfile.FILESYSTEM_ONLY.value
    )
    assert VerificationPlan.model_validate_json(value.model_dump_json()) == value
    assert VerificationEvidence.model_validate_json(evidence.model_dump_json()) == evidence


def legacy_plan() -> LegacyVerificationPlan:
    checks = [
        LegacyPlannedCheck(
            check_id=check_id,
            requirement_level=RequirementLevel.REQUIRED,
            kind=CheckKind.STRUCTURAL_INVARIANT,
            origin=CheckOrigin.DETERMINISTIC_FLOOR,
            selection_reason="Legacy floor.",
        )
        for check_id in (
            "scope-capture",
            "source-preservation",
            "result-classification",
        )
    ]
    checks.sort(key=planned_check_sort_key)
    provisional = LegacyVerificationPlan.model_construct(
        changeset_identity="a" * 64,
        discovery_identity="b" * 64,
        signals=signals(),
        checks=checks,
        identity="",
    )
    payload = {
        "contract": "verification_plan",
        "schema_version": "1.0.0",
        "changeset_identity": "a" * 64,
        "discovery_identity": "b" * 64,
        "signals": signals().model_dump(mode="json"),
        "checks": [check.model_dump(mode="json") for check in checks],
    }
    payload["identity"] = hash_payload(payload)
    return LegacyVerificationPlan.model_validate(payload)


def test_legacy_plan_and_evidence_load_without_migration() -> None:
    old_plan = legacy_plan()
    plan_payload = old_plan.model_dump(mode="json")
    loaded_plan = load_verification_plan(plan_payload)

    assert isinstance(loaded_plan, LegacyVerificationPlan)
    assert loaded_plan.identity == old_plan.identity
    assert "environment_profile" not in loaded_plan.model_dump(mode="json")["checks"][0]

    provisional = LegacyVerificationEvidence.model_construct(
        plan=old_plan,
        executions=[],
        source_preservation_failures=[],
        identity="",
    )
    evidence_payload = {
        **provisional.semantic_payload(),
        "identity": hash_payload(provisional.semantic_payload()),
    }
    old_evidence = LegacyVerificationEvidence.model_validate(evidence_payload)
    loaded_evidence = load_verification_evidence(evidence_payload)

    assert isinstance(loaded_evidence, LegacyVerificationEvidence)
    assert loaded_evidence.identity == old_evidence.identity
    assert loaded_evidence.schema_version == "1.0.0"
    assert "environment_profile" not in loaded_evidence.model_dump(mode="json")["plan"]["checks"][0]


@pytest.mark.parametrize("loader", [load_verification_plan, load_verification_evidence])
def test_unknown_verification_versions_fail_closed(loader) -> None:
    with pytest.raises(ValueError, match="unsupported .*schema version"):
        loader({"schema_version": "9.9.9"})


def test_request_profile_mismatch_is_rejected_before_materialization() -> None:
    filesystem = snapshot()
    git_check = command_check(
        EnvironmentProfile.GIT_REPOSITORY,
        [profile_entry()],
    )

    from pre_pr_verify.verification import build_execution_request

    with pytest.raises(PreflightError, match="environment profile"):
        build_execution_request(
            git_check,
            filesystem,
            timeout_seconds=1,
            output_limit_bytes=1024,
            required_capabilities=[CapabilityName.OUTPUT_LIMITS],
        )
