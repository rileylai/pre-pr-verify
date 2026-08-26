"""Thin deterministic sequencing helpers for the V1 review lifecycle.

This module binds authorization and persisted execution evidence to the
existing canonical identities. It does not perform semantic review or own a
second setup state machine.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, TextIO

from pre_pr_verify.discovery_models import DiscoveryResult
from pre_pr_verify.errors import (
    EvidenceReuseError,
    ExecutionAuthorizationRequired,
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
        report=render_markdown_report(artifact),
    )


def emit_final_report(
    finalized: FinalizedReview,
    *,
    stream: TextIO = sys.stdout,
) -> None:
    """Emit the already-rendered canonical report without alteration."""

    stream.write(finalized.report)
