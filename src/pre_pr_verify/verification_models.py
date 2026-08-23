from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from pre_pr_verify.models import FrozenModel, RawPath, SHA256_PATTERN


VERIFICATION_SCHEMA_VERSION = "1.0.0"


class RequirementLevel(StrEnum):
    REQUIRED = "required"
    ADVISORY = "advisory"


class CheckOrigin(StrEnum):
    DETERMINISTIC_FLOOR = "deterministic_floor"
    REPOSITORY_CANONICAL = "repository_canonical"
    TRUSTED_POLICY = "trusted_policy"
    MODEL_PROPOSED = "model_proposed"


class CheckKind(StrEnum):
    STRUCTURAL_INVARIANT = "structural_invariant"
    COMMAND = "command"


class SnapshotKind(StrEnum):
    REGULAR = "regular"
    SYMLINK = "symlink"


class CapabilityName(StrEnum):
    OUTPUT_LIMITS = "output_limits"
    NETWORK_ISOLATION = "network_isolation"
    RESOURCE_LIMITS = "resource_limits"
    PROCESS_ISOLATION = "process_isolation"


class DecisionKind(StrEnum):
    EXECUTABLE = "executable"
    REQUIRES_APPROVAL = "requires_approval"
    CANNOT_SAFELY_EXECUTE = "cannot_safely_execute"


class ExecutionStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"
    TIMED_OUT = "timed_out"
    ERRORED = "errored"
    CANCELLED = "cancelled"


class FailureKind(StrEnum):
    VERIFICATION = "verification"
    INFRASTRUCTURE = "infrastructure"
    PERMISSION = "permission"
    CONFIGURATION = "configuration"
    CAPABILITY = "capability"
    UNCLASSIFIED = "unclassified"


class ChangeSignals(FrozenModel):
    changed_paths: list[RawPath]
    changed_path_count: int = Field(ge=0)
    test_path_count: int = Field(ge=0)
    documentation_path_count: int = Field(ge=0)
    added_path_count: int = Field(ge=0)
    deleted_path_count: int = Field(ge=0)
    executable_path_count: int = Field(ge=0)
    committed_path_count: int = Field(ge=0)
    staged_path_count: int = Field(ge=0)
    unstaged_path_count: int = Field(ge=0)
    untracked_path_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> ChangeSignals:
        if self.changed_paths != sorted(
            self.changed_paths, key=lambda value: value.to_bytes()
        ):
            raise ValueError("change signal paths are not canonical")
        if self.changed_path_count != len(self.changed_paths):
            raise ValueError("changed path count does not match paths")
        return self


class PlannedCheck(FrozenModel):
    check_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    requirement_level: RequirementLevel
    kind: CheckKind
    origin: CheckOrigin
    selection_reason: str = Field(min_length=1)
    argv: list[str] | None = None
    cwd: str = "."
    source_path: RawPath | None = None
    source_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    source_size: int | None = Field(default=None, ge=0)
    trusted_policy_label: str | None = Field(default=None, min_length=1)
    trusted_policy_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_check(self) -> PlannedCheck:
        if self.kind is CheckKind.COMMAND:
            if not self.argv or any(not value or "\x00" in value for value in self.argv):
                raise ValueError("command check requires structured non-empty argv")
        elif self.argv is not None:
            raise ValueError("structural invariant cannot have argv")
        if self.kind is CheckKind.STRUCTURAL_INVARIANT and (
            self.origin is not CheckOrigin.DETERMINISTIC_FLOOR
            or self.requirement_level is not RequirementLevel.REQUIRED
        ):
            raise ValueError("structural invariant has invalid origin or requirement")
        if self.kind is CheckKind.COMMAND and self.origin is CheckOrigin.DETERMINISTIC_FLOOR:
            raise ValueError("deterministic floor cannot be a command")
        repository_source = (self.source_path, self.source_sha256, self.source_size)
        if any(value is not None for value in repository_source) != all(
            value is not None for value in repository_source
        ):
            raise ValueError("check source path, digest, and size must be paired")
        trusted_source = (self.trusted_policy_label, self.trusted_policy_sha256)
        if any(value is not None for value in trusted_source) != all(
            value is not None for value in trusted_source
        ):
            raise ValueError("trusted-policy label and digest must be paired")
        if self.origin is CheckOrigin.REPOSITORY_CANONICAL:
            if self.source_path is None or self.trusted_policy_label is not None:
                raise ValueError("repository check requires repository provenance")
        elif self.origin is CheckOrigin.TRUSTED_POLICY:
            if self.trusted_policy_label is None or self.source_path is not None:
                raise ValueError("trusted-policy check requires trusted provenance")
        elif self.source_path is not None or self.trusted_policy_label is not None:
            raise ValueError("check origin cannot carry protected provenance")
        if self.cwd.startswith("/") or any(
            part in ("", "..") for part in self.cwd.split("/")
        ):
            raise ValueError("check cwd must be repository-relative")
        if any(part.lower() == ".git" for part in self.cwd.split("/")):
            raise ValueError("check cwd cannot enter .git")
        return self


class VerificationPlan(FrozenModel):
    contract: Literal["verification_plan"] = "verification_plan"
    schema_version: Literal["1.0.0"] = "1.0.0"
    changeset_identity: str = Field(pattern=SHA256_PATTERN)
    discovery_identity: str = Field(pattern=SHA256_PATTERN)
    signals: ChangeSignals
    checks: list[PlannedCheck]
    identity: str = Field(pattern=SHA256_PATTERN)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"identity"})

    @model_validator(mode="after")
    def validate_plan(self) -> VerificationPlan:
        identifiers = [check.check_id for check in self.checks]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("planned check IDs must be unique")
        if self.checks != sorted(self.checks, key=planned_check_sort_key):
            raise ValueError("planned checks are not canonically ordered")
        floor = [
            check for check in self.checks if check.origin is CheckOrigin.DETERMINISTIC_FLOOR
        ]
        required_floor = {
            "scope-capture",
            "source-preservation",
            "result-classification",
        }
        if {check.check_id for check in floor} != required_floor or any(
            check.requirement_level is not RequirementLevel.REQUIRED
            or check.kind is not CheckKind.STRUCTURAL_INVARIANT
            or check.argv is not None
            or check.source_path is not None
            or check.trusted_policy_label is not None
            for check in floor
        ):
            raise ValueError("deterministic policy floor is incomplete")
        if self.identity != hash_payload(self.semantic_payload()):
            raise ValueError("verification plan identity does not match payload")
        return self


class ExecutionRequest(FrozenModel):
    check_id: str = Field(min_length=1)
    snapshot_identity: str = Field(pattern=SHA256_PATTERN)
    requirement_level: RequirementLevel
    argv: list[str] = Field(min_length=1)
    cwd: str = "."
    timeout_seconds: float = Field(gt=0, le=3600)
    output_limit_bytes: int = Field(gt=0, le=16_777_216)
    required_capabilities: list[CapabilityName]
    nonzero_failure_kind: FailureKind = FailureKind.VERIFICATION
    cwd_validation_failure: FailureKind | None = None
    snapshot_validation_failure: FailureKind | None = None

    @model_validator(mode="after")
    def validate_request(self) -> ExecutionRequest:
        if any(not value or "\x00" in value for value in self.argv):
            raise ValueError("execution argv must be structured and non-empty")
        # Runtime rooted no-follow validation classifies unsafe paths as
        # NOT_RUN evidence; rejecting them here would lose that evidence.
        ordered = sorted(set(self.required_capabilities), key=lambda item: item.value)
        if self.required_capabilities != ordered:
            object.__setattr__(self, "required_capabilities", ordered)
        if self.nonzero_failure_kind in {
            FailureKind.INFRASTRUCTURE,
            FailureKind.PERMISSION,
            FailureKind.CAPABILITY,
        }:
            raise ValueError("nonzero classification cannot claim a host failure")
        if self.cwd_validation_failure not in {None, FailureKind.CONFIGURATION, FailureKind.PERMISSION}:
            raise ValueError("cwd validation failure must be configuration or permission")
        if self.snapshot_validation_failure not in {
            None,
            FailureKind.CAPABILITY,
            FailureKind.CONFIGURATION,
            FailureKind.PERMISSION,
        }:
            raise ValueError(
                "snapshot validation failure must be capability, configuration, or permission"
            )
        return self


class ExecutionCapability(FrozenModel):
    structured_argv: bool
    repository_bound_cwd: bool
    git_protection: bool
    source_preservation: bool
    authority_separation: bool
    secret_stripping: bool
    verdict_invariants: bool
    available: list[CapabilityName]
    approval_waivable: list[CapabilityName]
    approved_gaps: list[CapabilityName]

    @model_validator(mode="after")
    def validate_capability(self) -> ExecutionCapability:
        for field in ("available", "approval_waivable", "approved_gaps"):
            values = getattr(self, field)
            ordered = sorted(set(values), key=lambda item: item.value)
            object.__setattr__(self, field, ordered)
        if not set(self.approved_gaps) <= set(self.approval_waivable):
            raise ValueError("approved gaps must be policy-waivable")
        return self


class ExecutionDecision(FrozenModel):
    kind: DecisionKind
    missing_capabilities: list[CapabilityName]
    accepted_risks: list[CapabilityName]
    blocked_failure_kind: FailureKind | None
    reasons: list[str]

    @model_validator(mode="after")
    def validate_decision(self) -> ExecutionDecision:
        for field in ("missing_capabilities", "accepted_risks"):
            values = getattr(self, field)
            if values != sorted(set(values), key=lambda item: item.value):
                raise ValueError("execution decision capabilities are not canonical")
        if not self.reasons:
            raise ValueError("execution decision requires a reason")
        if self.kind is DecisionKind.EXECUTABLE:
            if self.blocked_failure_kind is not None:
                raise ValueError("executable decision cannot carry a blocked failure")
        elif self.kind is DecisionKind.REQUIRES_APPROVAL:
            if self.blocked_failure_kind is not FailureKind.PERMISSION:
                raise ValueError("approval decision must classify as permission")
        elif self.blocked_failure_kind not in {
            FailureKind.CAPABILITY,
            FailureKind.CONFIGURATION,
            FailureKind.PERMISSION,
        }:
            raise ValueError("cannot-execute decision requires a structured cause")
        return self


_NON_WAIVABLE_INVARIANTS = (
    "structured_argv",
    "repository_bound_cwd",
    "git_protection",
    "source_preservation",
    "authority_separation",
    "secret_stripping",
    "verdict_invariants",
)


def derive_execution_decision(
    request: ExecutionRequest, capability: ExecutionCapability
) -> ExecutionDecision:
    if request.cwd_validation_failure is not None:
        return ExecutionDecision(
            kind=DecisionKind.CANNOT_SAFELY_EXECUTE,
            missing_capabilities=[],
            accepted_risks=[],
            blocked_failure_kind=request.cwd_validation_failure,
            reasons=[
                f"cwd validation failed: {request.cwd_validation_failure.value}"
            ],
        )
    if request.snapshot_validation_failure is not None:
        return ExecutionDecision(
            kind=DecisionKind.CANNOT_SAFELY_EXECUTE,
            missing_capabilities=[],
            accepted_risks=[],
            blocked_failure_kind=request.snapshot_validation_failure,
            reasons=[
                "snapshot materialization did not establish complete required evidence"
            ],
        )
    failed_invariants = [
        name for name in _NON_WAIVABLE_INVARIANTS if not getattr(capability, name)
    ]
    missing = sorted(
        set(request.required_capabilities) - set(capability.available),
        key=lambda item: item.value,
    )
    if failed_invariants:
        return ExecutionDecision(
            kind=DecisionKind.CANNOT_SAFELY_EXECUTE,
            missing_capabilities=missing,
            accepted_risks=[],
            blocked_failure_kind=FailureKind.CAPABILITY,
            reasons=[
                f"non-waivable invariant unavailable: {name}"
                for name in failed_invariants
            ],
        )
    nonwaivable = [
        item for item in missing if item not in capability.approval_waivable
    ]
    if nonwaivable:
        return ExecutionDecision(
            kind=DecisionKind.CANNOT_SAFELY_EXECUTE,
            missing_capabilities=missing,
            accepted_risks=[],
            blocked_failure_kind=FailureKind.CAPABILITY,
            reasons=[
                f"required capability unavailable: {item.value}"
                for item in nonwaivable
            ],
        )
    unapproved = [item for item in missing if item not in capability.approved_gaps]
    if unapproved:
        return ExecutionDecision(
            kind=DecisionKind.REQUIRES_APPROVAL,
            missing_capabilities=missing,
            accepted_risks=[],
            blocked_failure_kind=FailureKind.PERMISSION,
            reasons=[
                f"explicit approval required for capability gap: {item.value}"
                for item in unapproved
            ],
        )
    return ExecutionDecision(
        kind=DecisionKind.EXECUTABLE,
        missing_capabilities=missing,
        accepted_risks=missing,
        blocked_failure_kind=None,
        reasons=(
            [f"approved capability gap accepted: {item.value}" for item in missing]
            or ["all required execution capabilities are available"]
        ),
    )


class OutputEvidence(FrozenModel):
    excerpt: str
    sha256: str = Field(pattern=SHA256_PATTERN)
    total_bytes: int = Field(ge=0)
    truncated: bool
    redacted: bool


class ExecutionResult(FrozenModel):
    request: ExecutionRequest
    capability: ExecutionCapability
    decision: ExecutionDecision
    status: ExecutionStatus
    failure_kind: FailureKind | None
    exit_code: int | None
    duration_ms: int = Field(ge=0)
    stdout: OutputEvidence
    stderr: OutputEvidence
    required_evidence_gap: bool

    @model_validator(mode="after")
    def validate_result(self) -> ExecutionResult:
        expected_decision = derive_execution_decision(self.request, self.capability)
        if self.decision != expected_decision:
            raise ValueError("execution decision does not match request and capability")
        if self.decision.kind is DecisionKind.EXECUTABLE:
            if self.status is ExecutionStatus.NOT_RUN:
                raise ValueError("executable decision cannot produce not-run status")
        elif self.status is not ExecutionStatus.NOT_RUN:
            raise ValueError("non-executable decision cannot have an executed outcome")
        if self.status is ExecutionStatus.PASSED:
            if self.failure_kind is not None or self.exit_code != 0:
                raise ValueError("passed result cannot have a failure")
        elif self.failure_kind is None:
            raise ValueError("non-passing result requires a failure kind")
        elif self.status is ExecutionStatus.FAILED:
            if self.exit_code in (None, 0):
                raise ValueError("failed result requires a non-zero exit code")
            if self.failure_kind is not self.request.nonzero_failure_kind:
                raise ValueError("failed result does not match nonzero classification")
        elif self.status is ExecutionStatus.NOT_RUN:
            expected_kinds = (
                {self.decision.blocked_failure_kind}
                if self.decision.blocked_failure_kind is not None
                else set()
            )
            if self.exit_code is not None or self.failure_kind not in expected_kinds:
                raise ValueError("not-run result is inconsistent with its decision")
        elif self.status is ExecutionStatus.TIMED_OUT:
            if self.exit_code is not None or self.failure_kind is not FailureKind.INFRASTRUCTURE:
                raise ValueError("timed-out result must be an infrastructure failure")
        elif self.status is ExecutionStatus.ERRORED:
            if self.exit_code is not None or self.failure_kind not in {
                FailureKind.INFRASTRUCTURE,
                FailureKind.PERMISSION,
                FailureKind.CONFIGURATION,
            }:
                raise ValueError("errored result has an invalid failure classification")
        elif self.status is ExecutionStatus.CANCELLED and (
            self.exit_code is not None
            or self.failure_kind is not FailureKind.INFRASTRUCTURE
        ):
            raise ValueError("cancelled result must be an infrastructure failure")
        expected_gap = (
            self.request.requirement_level is RequirementLevel.REQUIRED
            and not (
                self.status is ExecutionStatus.PASSED
                or (
                    self.status is ExecutionStatus.FAILED
                    and self.failure_kind is FailureKind.VERIFICATION
                )
            )
        )
        if self.required_evidence_gap != expected_gap:
            raise ValueError("required evidence-gap classification is inconsistent")
        return self


class SnapshotFile(FrozenModel):
    path: RawPath
    mode: Literal["100644", "100755", "120000"]
    kind: SnapshotKind
    size: int = Field(ge=0)
    content_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_snapshot_file(self) -> SnapshotFile:
        raw_path = self.path.to_bytes()
        if (
            not raw_path
            or raw_path.startswith(b"/")
            or b"\x00" in raw_path
            or any(component in (b"", b".", b"..") for component in raw_path.split(b"/"))
            or any(component.lower() == b".git" for component in raw_path.split(b"/"))
        ):
            raise ValueError("snapshot file path must be repository-relative and bounded")
        if self.kind is SnapshotKind.SYMLINK and self.mode != "120000":
            raise ValueError("symlink snapshot file requires symlink mode")
        if self.kind is SnapshotKind.REGULAR and self.mode == "120000":
            raise ValueError("regular snapshot file cannot have symlink mode")
        return self


class SnapshotManifest(FrozenModel):
    materialization_ordinal: int = Field(ge=0)
    changeset_identity: str = Field(pattern=SHA256_PATTERN)
    discovery_identity: str = Field(pattern=SHA256_PATTERN)
    files: list[SnapshotFile]
    complete: bool = True
    materialization_failure: FailureKind | None = None
    identity: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_manifest(self) -> SnapshotManifest:
        if self.complete and self.materialization_failure is not None:
            raise ValueError("complete snapshot cannot carry a materialization failure")
        if not self.complete and self.materialization_failure not in {
            FailureKind.CAPABILITY,
            FailureKind.CONFIGURATION,
            FailureKind.PERMISSION,
        }:
            raise ValueError("incomplete snapshot requires a structured materialization failure")
        if not self.complete and self.files:
            raise ValueError("incomplete snapshot cannot expose a partial executable tree")
        paths = [item.path.raw_b64 for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("snapshot file paths must be unique")
        if self.files != sorted(self.files, key=lambda item: item.path.to_bytes()):
            raise ValueError("snapshot files are not canonical")
        payload = self.model_dump(mode="json", exclude={"identity"})
        if self.identity != hash_payload(payload):
            raise ValueError("snapshot identity does not match manifest")
        return self


class CommandExecutionEvidence(FrozenModel):
    ordinal: int = Field(ge=0)
    snapshot: SnapshotManifest
    result: ExecutionResult

    @model_validator(mode="after")
    def validate_binding(self) -> CommandExecutionEvidence:
        if self.ordinal != self.snapshot.materialization_ordinal:
            raise ValueError("execution ordinal does not match snapshot materialization")
        if self.result.request.snapshot_identity != self.snapshot.identity:
            raise ValueError("execution result does not match its pristine snapshot")
        request_failure = self.result.request.snapshot_validation_failure
        if self.snapshot.complete != (request_failure is None):
            raise ValueError("snapshot completeness does not match execution request")
        if not self.snapshot.complete and request_failure is not self.snapshot.materialization_failure:
            raise ValueError("snapshot materialization failure is not bound to the request")
        return self


class SourcePreservationFailure(FrozenModel):
    ordinal: int = Field(ge=0)
    check_id: str = Field(min_length=1)
    snapshot_identity: str = Field(pattern=SHA256_PATTERN)
    failure_kind: Literal[FailureKind.CAPABILITY] = FailureKind.CAPABILITY
    required_evidence_gap: Literal[True] = True
    reason: str = Field(min_length=1)


class VerificationEvidence(FrozenModel):
    contract: Literal["verification_evidence"] = "verification_evidence"
    schema_version: Literal["1.0.0"] = "1.0.0"
    plan: VerificationPlan
    executions: list[CommandExecutionEvidence]
    source_preservation_failures: list[SourcePreservationFailure] = Field(
        default_factory=list
    )
    identity: str = Field(pattern=SHA256_PATTERN)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"identity"})

    @model_validator(mode="after")
    def validate_evidence(self) -> VerificationEvidence:
        planned_command_ids = [
            check.check_id
            for check in self.plan.checks
            if check.kind is CheckKind.COMMAND
        ]
        actual = [item.result.request.check_id for item in self.executions]
        if actual != planned_command_ids:
            raise ValueError("execution results do not cover every command check")
        if [item.ordinal for item in self.executions] != list(
            range(len(self.executions))
        ):
            raise ValueError("command execution ordinals are not canonical")
        for item in self.executions:
            if (
                item.snapshot.changeset_identity != self.plan.changeset_identity
                or item.snapshot.discovery_identity != self.plan.discovery_identity
            ):
                raise ValueError("command snapshot does not match verification plan")
        planned_command_by_id = {
            check.check_id: check
            for check in self.plan.checks
            if check.kind is CheckKind.COMMAND
        }
        for item in self.executions:
            result = item.result
            planned = planned_command_by_id[result.request.check_id]
            if (
                result.request.argv != planned.argv
                or result.request.cwd != planned.cwd
                or result.request.requirement_level is not planned.requirement_level
            ):
                raise ValueError("execution request does not match planned command")
        failure_ordinals = [item.ordinal for item in self.source_preservation_failures]
        if failure_ordinals != sorted(set(failure_ordinals)):
            raise ValueError("source-preservation failures are not canonically ordered")
        executions_by_ordinal = {item.ordinal: item for item in self.executions}
        for failure in self.source_preservation_failures:
            execution = executions_by_ordinal.get(failure.ordinal)
            if execution is None:
                raise ValueError("source-preservation failure has no execution")
            if execution.result.status is ExecutionStatus.NOT_RUN:
                raise ValueError(
                    "source-preservation failure cannot bind to a not-run execution"
                )
            if failure.check_id != execution.result.request.check_id:
                raise ValueError("source-preservation failure check does not match execution")
            if failure.snapshot_identity != execution.snapshot.identity:
                raise ValueError("source-preservation failure snapshot is not bound")
            if (
                execution.snapshot.changeset_identity != self.plan.changeset_identity
                or execution.snapshot.discovery_identity != self.plan.discovery_identity
            ):
                raise ValueError("source-preservation failure does not match the plan")
        if self.identity != hash_payload(self.semantic_payload()):
            raise ValueError("verification evidence identity does not match payload")
        return self


def build_verification_evidence(
    plan: VerificationPlan,
    executions: list[tuple[SnapshotManifest, ExecutionResult]],
    *,
    source_preservation_failures: list[SourcePreservationFailure] | None = None,
) -> VerificationEvidence:
    ordered_executions = [
        CommandExecutionEvidence(ordinal=index, snapshot=snapshot, result=result)
        for index, (snapshot, result) in enumerate(executions)
    ]
    provisional = VerificationEvidence.model_construct(
        contract="verification_evidence",
        schema_version="1.0.0",
        plan=plan,
        executions=ordered_executions,
        source_preservation_failures=source_preservation_failures or [],
        identity="",
    )
    return VerificationEvidence(
        plan=plan,
        executions=ordered_executions,
        source_preservation_failures=source_preservation_failures or [],
        identity=hash_payload(provisional.semantic_payload()),
    )


_FLOOR_ORDER = {
    "scope-capture": 0,
    "source-preservation": 1,
    "result-classification": 2,
}
_ORIGIN_ORDER = {
    CheckOrigin.DETERMINISTIC_FLOOR: 0,
    CheckOrigin.TRUSTED_POLICY: 1,
    CheckOrigin.REPOSITORY_CANONICAL: 2,
    CheckOrigin.MODEL_PROPOSED: 3,
}


def planned_check_sort_key(check: PlannedCheck) -> tuple[int, int, str]:
    return (
        _ORIGIN_ORDER[check.origin],
        _FLOOR_ORDER.get(check.check_id, 0),
        check.check_id,
    )


def hash_payload(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
