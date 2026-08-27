"""Thin deterministic sequencing helpers for the V1 review lifecycle.

This module binds authorization and persisted execution evidence to the
existing canonical identities. It does not perform semantic review or own a
second setup state machine.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, TextIO

from pre_pr_verify.discovery_models import DiscoveryResult
from pre_pr_verify.errors import (
    EvidenceReuseError,
    ExecutionAuthorizationRequired,
    FinalReportHandoffError,
    PreflightError,
    PreReviewSetupError,
    ReauthorizationRequired,
)
from pre_pr_verify.models import ChangeSet
from pre_pr_verify.pre_review_setup import PreReviewSetup, SetupPhase, SetupStep
from pre_pr_verify.review import (
    build_review_artifact,
    load_review_artifact,
    render_markdown_report,
    verdict_exit_code,
)
from pre_pr_verify.review_models import LegacyReviewArtifact, ReviewArtifact
from pre_pr_verify.semantic import load_semantic_assessment
from pre_pr_verify.semantic_models import SemanticAssessment
from pre_pr_verify.verification_models import (
    CapabilityName,
    ExecutionCapability,
    VerificationEvidence,
    VerificationPlan,
    load_verification_evidence,
    hash_payload,
)
from pre_pr_verify.executor import execute_verification_plan


@dataclass(frozen=True)
class VerificationAuthorization:
    """The exact inputs a setup answer authorized for verification execution."""

    plan_identity: str
    capability_identity: str
    timeout_seconds: float
    output_limit_bytes: int
    required_capabilities: tuple[CapabilityName, ...]

    @property
    def execution_policy_identity(self) -> str:
        return _execution_policy_identity(
            self.timeout_seconds,
            self.output_limit_bytes,
            self.required_capabilities,
        )

    @property
    def binding_identity(self) -> str:
        return hash_payload(
            {
                "plan_identity": self.plan_identity,
                "capability_identity": self.capability_identity,
                "execution_policy_identity": self.execution_policy_identity,
            }
        )


@dataclass(frozen=True)
class FinalizedReview:
    """Canonical artifact, exit mapping, and user-facing Markdown report."""

    artifact: ReviewArtifact | LegacyReviewArtifact
    exit_code: int
    report: str


@dataclass(frozen=True)
class FinalReportHandoff:
    """The verified, ephemeral location and digest of a canonical report."""

    path: Path
    utf8_byte_length: int
    sha256: str


_REPORT_DIRECTORY_ATTEMPTS = 16
_REPORT_HANDOFF_SUPPORTS_DIR_FD = all(
    operation in os.supports_dir_fd
    for operation in (os.open, os.mkdir, os.stat)
)


def _require_report_handoff_support() -> None:
    if (
        os.name != "posix"
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or not _REPORT_HANDOFF_SUPPORTS_DIR_FD
    ):
        raise FinalReportHandoffError(
            "canonical report handoff requires descriptor-relative no-follow filesystem support"
        )


def _directory_open_flags() -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _open_anchored_directory(path: Path) -> int:
    """Open an absolute directory by walking every component from filesystem root."""

    path_bytes = os.fsencode(path)
    if not path.is_absolute() or not path_bytes.startswith(b"/"):
        raise FinalReportHandoffError(
            "canonical report handoff temporary root must be an absolute POSIX path"
        )
    current_descriptor: int | None = None
    flags = _directory_open_flags()
    try:
        current_descriptor = os.open(b"/", flags)
        for component in path_bytes.split(b"/"):
            if not component:
                continue
            next_descriptor = os.open(
                component,
                flags,
                dir_fd=current_descriptor,
            )
            try:
                os.close(current_descriptor)
            except OSError:
                try:
                    os.close(next_descriptor)
                except OSError:
                    pass
                raise
            current_descriptor = next_descriptor
        if current_descriptor is None:
            raise FinalReportHandoffError(
                "canonical report handoff could not anchor the temporary root"
            )
        result = current_descriptor
        current_descriptor = None
        return result
    finally:
        if current_descriptor is not None:
            try:
                os.close(current_descriptor)
            except OSError:
                pass


def _create_report_directory(temp_root_descriptor: int) -> str:
    for _ in range(_REPORT_DIRECTORY_ATTEMPTS):
        name = f"pre-pr-verify-report-{secrets.token_hex(16)}"
        try:
            os.mkdir(name, 0o700, dir_fd=temp_root_descriptor)
        except FileExistsError:
            continue
        return name
    raise FinalReportHandoffError(
        "canonical report handoff could not create a unique temporary directory"
    )


def prepare_review(setup: PreReviewSetup) -> SetupStep:
    """Expose the current setup choices without selecting one."""

    return setup.current_step()


def record_setup_answer(
    setup: PreReviewSetup,
    answer: str | int | None,
    *,
    detail: str | None = None,
) -> None:
    """Record one caller-provided setup answer without rendering the next step.

    ``answer`` is intentionally required and has no default. The helper does
    not infer, submit, or accept a recommended answer on behalf of the caller.
    The caller completes prerequisites for the new phase before explicitly
    calling :func:`prepare_review` again.
    """

    if answer is None or (isinstance(answer, str) and not answer.strip()):
        raise PreReviewSetupError(
            "orchestration requires a non-empty externally supplied setup answer"
        )
    setup.submit(answer, detail=detail)


def _capability_identity(capability: ExecutionCapability) -> str:
    return hash_payload(capability.model_dump(mode="json"))


def _execution_policy_identity(
    timeout_seconds: float,
    output_limit_bytes: int,
    required_capabilities: Iterable[CapabilityName],
) -> str:
    values = tuple(sorted(set(required_capabilities), key=lambda item: item.value))
    return hash_payload(
        {
            "timeout_seconds": timeout_seconds,
            "output_limit_bytes": output_limit_bytes,
            "required_capabilities": [value.value for value in values],
        }
    )


def _validate_plan_scope(
    changeset: ChangeSet,
    discovery: DiscoveryResult,
    plan: VerificationPlan,
) -> None:
    if changeset.empty:
        raise PreflightError("empty ChangeSet is nothing_to_review")
    if plan.changeset_identity != changeset.identity:
        raise PreflightError("verification plan is not bound to the ChangeSet")
    if plan.discovery_identity != discovery.identity:
        raise PreflightError("verification plan is not bound to Discovery")


def authorize_verification_plan(
    setup: PreReviewSetup,
    changeset: ChangeSet,
    discovery: DiscoveryResult,
    plan: VerificationPlan,
    capability: ExecutionCapability,
    *,
    timeout_seconds: float,
    output_limit_bytes: int,
    required_capabilities: Iterable[CapabilityName],
) -> VerificationAuthorization:
    """Bind the external execution choice to exact plan inputs.

    Authorization is bound while setup is waiting for final confirmation. The
    final confirmation still gates execution, but it cannot become the first
    point at which a plan is associated with the authorization.
    """

    if (
        setup.phase is SetupPhase.READY_TO_REVIEW
        and setup.verification_authorization_binding is not None
    ):
        setup.require_ready_to_review()
    else:
        setup.require_verification_authorization()
    _validate_plan_scope(changeset, discovery, plan)
    if setup.verification_selection not in {"authorize", "customize-authorization"}:
        raise ExecutionAuthorizationRequired(
            "verification setup did not authorize execution"
        )
    required = tuple(
        sorted(set(required_capabilities), key=lambda item: item.value)
    )
    authorization = VerificationAuthorization(
        plan_identity=plan.identity,
        capability_identity=_capability_identity(capability),
        timeout_seconds=timeout_seconds,
        output_limit_bytes=output_limit_bytes,
        required_capabilities=required,
    )
    existing_binding = setup.verification_authorization_binding
    if existing_binding is not None and existing_binding != authorization.binding_identity:
        setup.require_verification_reauthorization()
        raise ReauthorizationRequired(
            "reauthorization required: verification plan or execution capability changed"
        )
    if existing_binding is None:
        setup.bind_verification_authorization(authorization.binding_identity)
    return authorization


def _require_matching_authorization(
    setup: PreReviewSetup,
    changeset: ChangeSet,
    discovery: DiscoveryResult,
    plan: VerificationPlan,
    capability: ExecutionCapability,
    authorization: VerificationAuthorization,
) -> None:
    setup.require_ready_to_review()
    _validate_plan_scope(changeset, discovery, plan)
    if setup.verification_selection not in {"authorize", "customize-authorization"}:
        raise ExecutionAuthorizationRequired(
            "verification setup did not authorize execution"
        )
    expected_binding = VerificationAuthorization(
        plan_identity=plan.identity,
        capability_identity=_capability_identity(capability),
        timeout_seconds=authorization.timeout_seconds,
        output_limit_bytes=authorization.output_limit_bytes,
        required_capabilities=authorization.required_capabilities,
    ).binding_identity
    if setup.verification_authorization_binding is None:
        raise ExecutionAuthorizationRequired(
            "verification authorization is not bound to setup"
        )
    mismatch = (
        authorization.plan_identity != plan.identity
        or authorization.capability_identity != _capability_identity(capability)
        or setup.verification_authorization_binding != expected_binding
    )
    if mismatch:
        setup.require_verification_reauthorization()
        raise ReauthorizationRequired(
            "reauthorization required: verification plan or execution capability changed"
        )


def persist_verification_evidence(
    path: Path | str,
    evidence: VerificationEvidence,
    *,
    author_repository: Path | str | None = None,
) -> None:
    """Persist evidence only outside the author repository.

    The repository root is required even though this helper is otherwise
    path-based. Without that boundary, a caller could accidentally replace an
    author file while trying to save review-run evidence.
    """

    target = _validated_evidence_path(path, author_repository)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(evidence.model_dump_json(), encoding="utf-8")
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _validated_evidence_path(
    path: Path | str,
    author_repository: Path | str | None,
) -> Path:
    if author_repository is None:
        raise PreflightError(
            "author repository is required before persisting verification evidence"
        )
    target = Path(path).resolve(strict=False)
    repository = Path(author_repository).resolve(strict=True)
    try:
        target.relative_to(repository)
    except ValueError:
        return target
    raise PreflightError(
        "verification evidence must be persisted outside the author repository"
    )


def _execution_evidence_path(
    path: Path | str,
    authorization: VerificationAuthorization,
    author_repository: Path | str,
) -> Path:
    """Derive a stable evidence path from the exact authorization binding."""

    target = _validated_evidence_path(path, author_repository)
    suffix = target.suffix or ".json"
    stem = target.name.removesuffix(target.suffix)
    scoped = target.with_name(f"{stem}-{authorization.binding_identity}{suffix}")
    return _validated_evidence_path(scoped, author_repository)


def load_completed_execution(
    path: Path | str,
    changeset: ChangeSet,
    discovery: DiscoveryResult,
    plan: VerificationPlan,
    *,
    authorization: VerificationAuthorization | None = None,
) -> VerificationEvidence:
    """Load and fail closed unless evidence matches every expected identity."""

    target = Path(path)
    try:
        loaded = load_verification_evidence(target.read_bytes())
    except (OSError, TypeError, ValueError) as error:
        raise EvidenceReuseError(
            "persisted verification evidence failed canonical loading/validation"
        ) from error
    if not isinstance(loaded, VerificationEvidence):
        raise EvidenceReuseError(
            "persisted verification evidence is not the current executable contract"
        )
    if plan.changeset_identity != changeset.identity:
        raise EvidenceReuseError("expected ChangeSet identity does not match the plan")
    if plan.discovery_identity != discovery.identity:
        raise EvidenceReuseError("expected DiscoveryResult identity does not match the plan")
    if loaded.plan.identity != plan.identity:
        raise EvidenceReuseError("persisted evidence has a different plan identity")
    if loaded.plan.changeset_identity != changeset.identity:
        raise EvidenceReuseError("persisted evidence has a different ChangeSet identity")
    if loaded.plan.discovery_identity != discovery.identity:
        raise EvidenceReuseError("persisted evidence has a different DiscoveryResult identity")
    if authorization is not None:
        for execution in loaded.executions:
            if (
                _capability_identity(execution.result.capability)
                != authorization.capability_identity
                or execution.result.request.timeout_seconds
                != authorization.timeout_seconds
                or execution.result.request.output_limit_bytes
                != authorization.output_limit_bytes
                or tuple(execution.result.request.required_capabilities)
                != authorization.required_capabilities
            ):
                raise EvidenceReuseError(
                    "persisted evidence has a different execution capability or policy"
                )
    return loaded


def execute_authorized_plan(
    setup: PreReviewSetup,
    changeset: ChangeSet,
    discovery: DiscoveryResult,
    plan: VerificationPlan,
    capability: ExecutionCapability,
    authorization: VerificationAuthorization,
    *,
    evidence_path: Path | str,
    redaction_values: Iterable[str] = (),
) -> VerificationEvidence:
    """Reuse valid run evidence or execute once for the bound authorization."""

    _require_matching_authorization(
        setup,
        changeset,
        discovery,
        plan,
        capability,
        authorization,
    )
    target = _execution_evidence_path(
        evidence_path,
        authorization,
        changeset.repository_root,
    )
    if target.exists():
        return load_completed_execution(
            target,
            changeset,
            discovery,
            plan,
            authorization=authorization,
        )

    evidence = execute_verification_plan(
        changeset,
        discovery,
        plan,
        capability,
        timeout_seconds=authorization.timeout_seconds,
        output_limit_bytes=authorization.output_limit_bytes,
        required_capabilities=authorization.required_capabilities,
        redaction_values=redaction_values,
    )
    evidence = _validate_evidence_bindings(changeset, discovery, plan, evidence)
    persist_verification_evidence(
        target,
        evidence,
        author_repository=changeset.repository_root,
    )
    return load_completed_execution(
        target,
        changeset,
        discovery,
        plan,
        authorization=authorization,
    )


def _validate_evidence_bindings(
    changeset: ChangeSet,
    discovery: DiscoveryResult,
    plan: VerificationPlan,
    evidence: VerificationEvidence,
) -> VerificationEvidence:
    """Reload the object through the canonical loader before finalization."""

    try:
        reloaded = load_verification_evidence(evidence.model_dump_json())
    except (TypeError, ValueError) as error:
        raise PreflightError("verification evidence failed canonical validation") from error
    if not isinstance(reloaded, VerificationEvidence):
        raise PreflightError("current VerificationEvidence is required for finalization")
    if plan.changeset_identity != changeset.identity:
        raise PreflightError("verification plan is not bound to the ChangeSet")
    if plan.discovery_identity != discovery.identity:
        raise PreflightError("verification plan is not bound to Discovery")
    if reloaded.plan.identity != plan.identity:
        raise PreflightError("verification evidence is not bound to the plan")
    if reloaded.plan.changeset_identity != changeset.identity:
        raise PreflightError("verification evidence is not bound to the ChangeSet")
    if reloaded.plan.discovery_identity != discovery.identity:
        raise PreflightError("verification evidence is not bound to Discovery")
    return reloaded


def finalize_review(
    changeset: ChangeSet,
    discovery: DiscoveryResult,
    plan: VerificationPlan,
    evidence: VerificationEvidence,
    assessment: SemanticAssessment,
    *,
    verifier_version: str,
    verifier_commit_or_build: str,
) -> FinalizedReview:
    """Build, reload, reduce, and render the canonical review artifact."""

    evidence = _validate_evidence_bindings(changeset, discovery, plan, evidence)
    try:
        assessment = load_semantic_assessment(
            assessment.model_dump(mode="json"),
            changeset,
            discovery,
            plan,
            evidence,
        )
    except (TypeError, ValueError) as error:
        raise PreflightError("semantic assessment failed canonical validation") from error
    artifact = build_review_artifact(
        changeset,
        discovery,
        plan,
        evidence,
        assessment,
        verifier_version=verifier_version,
        verifier_commit_or_build=verifier_commit_or_build,
    )
    artifact = load_review_artifact(
        artifact.model_dump_json(),
        changeset,
        discovery,
        plan,
        evidence,
        assessment,
        verifier_version=verifier_version,
        verifier_commit_or_build=verifier_commit_or_build,
    )
    return FinalizedReview(
        artifact=artifact,
        exit_code=verdict_exit_code(artifact.verdict),
        report=render_markdown_report(
            artifact,
            changeset=changeset,
            discovery=discovery,
            plan=plan,
            evidence=evidence,
        ),
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _same_file_identity(path: Path, expected: os.stat_result) -> bool:
    try:
        actual = os.lstat(path)
    except OSError:
        return False
    return actual.st_dev == expected.st_dev and actual.st_ino == expected.st_ino


def _same_stat_identity(actual: os.stat_result, expected: os.stat_result) -> bool:
    return actual.st_dev == expected.st_dev and actual.st_ino == expected.st_ino


def persist_final_report(
    finalized: FinalizedReview,
    *,
    author_repository: Path | str,
) -> FinalReportHandoff:
    """Persist and verify one ephemeral, verifier-owned canonical report.

    The report is created with exclusive descriptor-based filesystem calls in
    a fresh private directory. Successful handoffs intentionally leave the
    temporary report readable for the human-facing receipt.
    """

    try:
        report_bytes = finalized.report.encode("utf-8")
    except UnicodeEncodeError as error:
        raise FinalReportHandoffError(
            "canonical report handoff could not encode the report as UTF-8"
        ) from error
    try:
        repository = Path(author_repository).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise FinalReportHandoffError(
            "canonical report handoff could not resolve the author repository"
        ) from error
    if not repository.is_dir():
        raise FinalReportHandoffError(
            "canonical report handoff requires an author repository directory"
        )
    try:
        temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise FinalReportHandoffError(
            "canonical report handoff could not resolve the temporary directory"
        ) from error
    if _is_relative_to(temporary_root, repository):
        raise FinalReportHandoffError(
            "canonical report handoff temporary directory is inside the author repository"
        )

    _require_report_handoff_support()
    temp_root_descriptor: int | None = None
    directory_name: str | None = None
    report_directory_descriptor: int | None = None
    directory_identity: os.stat_result | None = None
    report_path: Path | None = None
    report_identity: os.stat_result | None = None
    report_descriptor: int | None = None
    try:
        temp_root_descriptor = _open_anchored_directory(temporary_root)
        directory_name = _create_report_directory(temp_root_descriptor)
        report_path = temporary_root / directory_name / "final-report.md"
        created_directory_identity = os.stat(
            directory_name,
            dir_fd=temp_root_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(created_directory_identity.st_mode):
            raise FinalReportHandoffError(
                "canonical report handoff temporary path is not a directory"
            )
        directory_flags = _directory_open_flags()
        report_directory_descriptor = os.open(
            directory_name,
            directory_flags,
            dir_fd=temp_root_descriptor,
        )
        directory_identity = os.fstat(report_directory_descriptor)
        if not stat.S_ISDIR(directory_identity.st_mode):
            raise FinalReportHandoffError(
                "canonical report handoff temporary path is not a directory"
            )
        if os.name == "posix" and stat.S_IMODE(directory_identity.st_mode) != 0o700:
            raise FinalReportHandoffError(
                "canonical report handoff temporary directory is not private"
            )
        if not _same_stat_identity(created_directory_identity, directory_identity):
            raise FinalReportHandoffError(
                "canonical report handoff temporary directory changed during anchoring"
            )

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        report_descriptor = os.open(
            b"final-report.md",
            flags,
            0o600,
            dir_fd=report_directory_descriptor,
        )
        report_identity = os.fstat(report_descriptor)
        if hasattr(os, "fchmod"):
            os.fchmod(report_descriptor, 0o600)
        try:
            with os.fdopen(report_descriptor, "wb", closefd=True) as output:
                report_descriptor = None
                output.write(report_bytes)
                output.flush()
                os.fsync(output.fileno())
        finally:
            if report_descriptor is not None:
                os.close(report_descriptor)
                report_descriptor = None

        report_stat = os.stat(
            b"final-report.md",
            dir_fd=report_directory_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(report_stat.st_mode) or not _same_stat_identity(
            report_stat, report_identity
        ):
            raise FinalReportHandoffError(
                "canonical report handoff created an unexpected report file"
            )
        if os.name == "posix" and stat.S_IMODE(report_stat.st_mode) != 0o600:
            raise FinalReportHandoffError(
                "canonical report handoff report file is not private"
            )

        read_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            read_flags |= os.O_NOFOLLOW
        read_descriptor: int | None = os.open(
            b"final-report.md",
            read_flags,
            dir_fd=report_directory_descriptor,
        )
        try:
            read_identity = os.fstat(read_descriptor)
            if not _same_stat_identity(read_identity, report_identity):
                raise FinalReportHandoffError(
                    "canonical report handoff report changed during verification"
                )
            with os.fdopen(read_descriptor, "rb", closefd=True) as source:
                read_descriptor = None
                persisted_bytes = source.read()
        finally:
            if read_descriptor is not None:
                os.close(read_descriptor)

        if persisted_bytes != report_bytes:
            raise FinalReportHandoffError(
                "canonical report handoff failed byte-for-byte verification"
            )
        if len(persisted_bytes) != len(report_bytes):
            raise FinalReportHandoffError(
                "canonical report handoff failed byte-length verification"
            )
        digest = hashlib.sha256(persisted_bytes).hexdigest()
        if digest != hashlib.sha256(report_bytes).hexdigest():
            raise FinalReportHandoffError(
                "canonical report handoff failed SHA-256 verification"
            )
        if not _same_file_identity(report_path.parent, directory_identity):
            raise FinalReportHandoffError(
                "canonical report handoff temporary directory path changed"
            )
        return FinalReportHandoff(
            path=report_path,
            utf8_byte_length=len(report_bytes),
            sha256=digest,
        )
    except FinalReportHandoffError:
        raise
    except (OSError, RuntimeError, UnicodeError, ValueError, TypeError) as error:
        raise FinalReportHandoffError(
            "canonical report handoff failed during safe persistence"
        ) from error
    finally:
        if report_descriptor is not None:
            try:
                os.close(report_descriptor)
            except OSError:
                pass
        if report_directory_descriptor is not None:
            try:
                os.close(report_directory_descriptor)
            except OSError:
                pass
        if temp_root_descriptor is not None:
            try:
                os.close(temp_root_descriptor)
            except OSError:
                pass


def emit_final_report(
    finalized: FinalizedReview,
    *,
    stream: TextIO = sys.stdout,
) -> None:
    """Emit the already-rendered canonical report without alteration."""

    stream.write(finalized.report)
