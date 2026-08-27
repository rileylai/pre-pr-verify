from __future__ import annotations

import json
from pathlib import Path

from pre_pr_verify.discovery_models import DiscoveryResult
from pre_pr_verify.models import ChangeSet, LegacyChangeSet
from pre_pr_verify.review_models import LegacyReviewArtifact, ReviewArtifact
from pre_pr_verify.semantic_models import LegacySemanticAssessment, SemanticAssessment
from pre_pr_verify.verification_models import (
    VerificationEvidence,
    VerificationPlan,
)


def render_changeset_schema() -> str:
    return (
        json.dumps(
            ChangeSet.model_json_schema(),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def render_legacy_changeset_schema() -> str:
    return (
        json.dumps(
            LegacyChangeSet.model_json_schema(),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def write_changeset_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_changeset_schema(), encoding="utf-8")


def write_legacy_changeset_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_legacy_changeset_schema(), encoding="utf-8")


def render_discovery_schema() -> str:
    return (
        json.dumps(
            DiscoveryResult.model_json_schema(),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def write_discovery_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_discovery_schema(), encoding="utf-8")


def render_verification_evidence_schema() -> str:
    return (
        json.dumps(
            VerificationEvidence.model_json_schema(),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def write_verification_evidence_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_verification_evidence_schema(), encoding="utf-8")


def render_verification_plan_schema() -> str:
    return (
        json.dumps(
            VerificationPlan.model_json_schema(),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def write_verification_plan_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_verification_plan_schema(), encoding="utf-8")


def render_semantic_assessment_schema() -> str:
    return (
        json.dumps(
            SemanticAssessment.model_json_schema(),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def render_legacy_semantic_assessment_schema() -> str:
    return (
        json.dumps(
            LegacySemanticAssessment.model_json_schema(),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def write_semantic_assessment_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_semantic_assessment_schema(), encoding="utf-8")


def render_review_artifact_schema() -> str:
    return (
        json.dumps(
            ReviewArtifact.model_json_schema(),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def render_legacy_review_artifact_schema() -> str:
    return (
        json.dumps(
            LegacyReviewArtifact.model_json_schema(),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def write_review_artifact_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_review_artifact_schema(), encoding="utf-8")


if __name__ == "__main__":
    write_legacy_changeset_schema(Path("schemas/changeset-1.0.0.schema.json"))
    write_changeset_schema(Path("schemas/changeset-1.1.0.schema.json"))
    write_discovery_schema(Path("schemas/discovery-1.0.0.schema.json"))
    write_verification_plan_schema(Path("schemas/verification-plan-1.1.0.schema.json"))
    write_verification_evidence_schema(
        Path("schemas/verification-evidence-1.1.0.schema.json")
    )
    write_semantic_assessment_schema(
        Path("schemas/semantic-assessment-1.1.0.schema.json")
    )
    Path("schemas/semantic-assessment-1.0.0.schema.json").write_text(
        render_legacy_semantic_assessment_schema(), encoding="utf-8"
    )
    Path("schemas/review-artifact-1.0.0.schema.json").write_text(
        render_legacy_review_artifact_schema(), encoding="utf-8"
    )
    write_review_artifact_schema(Path("schemas/review-artifact-1.1.0.schema.json"))
