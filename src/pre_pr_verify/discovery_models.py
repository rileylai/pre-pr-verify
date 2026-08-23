from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from pre_pr_verify.models import FrozenModel, RawPath, SHA256_PATTERN


DISCOVERY_SCHEMA_VERSION = "1.0.0"


class SourceType(StrEnum):
    EXPLICIT_SPEC = "explicit_spec"
    TRUSTED_POLICY_SELECTED = "trusted_policy_selected"
    REPOSITORY_REQUIREMENT = "repository_requirement"
    REPOSITORY_STANDARD = "repository_standard"
    TEST_EVIDENCE = "test_evidence"
    DISCOVERY_CLUE = "discovery_clue"


class SourceTrust(StrEnum):
    INVOCATION = "invocation"
    TRUSTED_SELECTION = "trusted_selection"
    UNTRUSTED_REPOSITORY = "untrusted_repository"


class RequirementPrecedence(StrEnum):
    EXPLICIT = "explicit"
    TRUSTED_POLICY = "trusted_policy"
    REPOSITORY_DOCUMENTATION = "repository_documentation"
    TEST_EVIDENCE = "test_evidence"
    DISCOVERY_CLUE = "discovery_clue"


PRECEDENCE_ORDER = {
    RequirementPrecedence.EXPLICIT: 0,
    RequirementPrecedence.TRUSTED_POLICY: 1,
    RequirementPrecedence.REPOSITORY_DOCUMENTATION: 2,
    RequirementPrecedence.TEST_EVIDENCE: 3,
    RequirementPrecedence.DISCOVERY_CLUE: 4,
}


class RequirementResolutionStatus(StrEnum):
    CANDIDATES = "candidates"
    MISSING = "missing"


class DiscoveryIssueKind(StrEnum):
    UNSAFE_PATH = "unsafe_path"
    SOURCE_TOO_LARGE = "source_too_large"
    UNSUPPORTED_ENCODING = "unsupported_encoding"
    SOURCE_LIMIT_REACHED = "source_limit_reached"


class DiscoverySource(FrozenModel):
    source_id: str = Field(pattern=SHA256_PATTERN)
    source_type: SourceType
    label: str = Field(min_length=1)
    path: RawPath | None
    content_sha256: str = Field(pattern=SHA256_PATTERN)
    size: int = Field(ge=0)
    content_utf8: str
    trust: SourceTrust
    requirement_precedence: RequirementPrecedence | None
    standards_scope: RawPath | None
    security_authority: Literal["none"] = "none"
    execution_authority: Literal["none"] = "none"

    @model_validator(mode="after")
    def validate_source(self) -> DiscoverySource:
        content = self.content_utf8.encode("utf-8")
        if len(content) != self.size:
            raise ValueError("source size does not match UTF-8 content")
        if hashlib.sha256(content).hexdigest() != self.content_sha256:
            raise ValueError("source digest does not match UTF-8 content")
        raw_path = self.path.to_bytes() if self.path is not None else None
        if self.source_id != build_source_id(
            self.source_type,
            self.label,
            raw_path,
            self.content_sha256,
        ):
            raise ValueError("source ID does not match source identity")
        if self.source_type is SourceType.REPOSITORY_STANDARD:
            if self.requirement_precedence is not None or self.standards_scope is None:
                raise ValueError("Standards source applicability is inconsistent")
        elif self.requirement_precedence is None or self.standards_scope is not None:
            raise ValueError("requirement source classification is inconsistent")
        expected_classification = {
            SourceType.EXPLICIT_SPEC: (
                SourceTrust.INVOCATION,
                RequirementPrecedence.EXPLICIT,
                False,
            ),
            SourceType.TRUSTED_POLICY_SELECTED: (
                SourceTrust.TRUSTED_SELECTION,
                RequirementPrecedence.TRUSTED_POLICY,
                True,
            ),
            SourceType.REPOSITORY_REQUIREMENT: (
                SourceTrust.UNTRUSTED_REPOSITORY,
                RequirementPrecedence.REPOSITORY_DOCUMENTATION,
                True,
            ),
            SourceType.REPOSITORY_STANDARD: (
                SourceTrust.UNTRUSTED_REPOSITORY,
                None,
                True,
            ),
            SourceType.TEST_EVIDENCE: (
                SourceTrust.UNTRUSTED_REPOSITORY,
                RequirementPrecedence.TEST_EVIDENCE,
                False,
            ),
            SourceType.DISCOVERY_CLUE: (
                SourceTrust.UNTRUSTED_REPOSITORY,
                RequirementPrecedence.DISCOVERY_CLUE,
                False,
            ),
        }[self.source_type]
        expected_trust, expected_precedence, requires_path = expected_classification
        if (
            self.trust is not expected_trust
            or self.requirement_precedence is not expected_precedence
            or (self.path is not None) is not requires_path
        ):
            raise ValueError("source trust, precedence, or location is inconsistent")
        return self


class DiscoveryIssue(FrozenModel):
    kind: DiscoveryIssueKind
    path: RawPath | None
    detail: str = Field(min_length=1)


class RequirementResolution(FrozenModel):
    status: RequirementResolutionStatus
    precedence: RequirementPrecedence | None
    candidate_source_ids: list[str]

    @model_validator(mode="after")
    def validate_resolution(self) -> RequirementResolution:
        if len(set(self.candidate_source_ids)) != len(self.candidate_source_ids):
            raise ValueError("requirement candidate IDs must be unique")
        if self.status is RequirementResolutionStatus.MISSING:
            if self.precedence is not None or self.candidate_source_ids:
                raise ValueError("missing requirement resolution is inconsistent")
        elif self.precedence is None or not self.candidate_source_ids:
            raise ValueError("resolved requirement requires candidates and precedence")
        return self


class DiscoveryResult(FrozenModel):
    contract: Literal["discovery"] = "discovery"
    schema_version: Literal["1.0.0"] = "1.0.0"
    repository_root: str
    sources: list[DiscoverySource]
    requirement_resolution: RequirementResolution
    standards_source_ids: list[str]
    issues: list[DiscoveryIssue]
    identity: str = Field(pattern=SHA256_PATTERN)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"identity", "repository_root"})

    @model_validator(mode="after")
    def validate_contract(self) -> DiscoveryResult:
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("discovery source IDs must be unique")
        if self.sources != sorted(self.sources, key=source_sort_key):
            raise ValueError("discovery sources are not canonically ordered")
        known = set(source_ids)
        referenced = set(self.requirement_resolution.candidate_source_ids)
        referenced.update(self.standards_source_ids)
        if not referenced <= known:
            raise ValueError("discovery result references an unknown source")
        expected_standards = [
            source.source_id
            for source in self.sources
            if source.source_type is SourceType.REPOSITORY_STANDARD
        ]
        if self.standards_source_ids != expected_standards:
            raise ValueError("Standards source IDs are not canonical")
        requirement_sources = [
            source
            for source in self.sources
            if source.requirement_precedence is not None
        ]
        if requirement_sources:
            winning = min(
                requirement_sources,
                key=lambda source: PRECEDENCE_ORDER[
                    source.requirement_precedence  # type: ignore[index]
                ],
            ).requirement_precedence
            expected_candidates = [
                source
                for source in requirement_sources
                if source.requirement_precedence is winning
            ]
            if self.requirement_resolution.candidate_source_ids != [
                source.source_id for source in expected_candidates
            ]:
                raise ValueError("requirement candidates do not match precedence")
            if (
                self.requirement_resolution.precedence is not winning
                or self.requirement_resolution.status
                is not RequirementResolutionStatus.CANDIDATES
            ):
                raise ValueError("requirement resolution does not match candidates")
        elif (
            self.requirement_resolution.status
            is not RequirementResolutionStatus.MISSING
        ):
            raise ValueError("missing requirement sources require missing resolution")
        if self.identity != hash_payload(self.semantic_payload()):
            raise ValueError("discovery identity does not match semantic payload")
        return self


def hash_payload(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_source_id(
    source_type: SourceType,
    label: str,
    path: bytes | None,
    content_sha256: str,
) -> str:
    digest = hashlib.sha256()
    digest.update(source_type.value.encode("ascii"))
    digest.update(b"\x00")
    digest.update(label.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(path or b"")
    digest.update(b"\x00")
    digest.update(content_sha256.encode("ascii"))
    return digest.hexdigest()


def source_sort_key(source: DiscoverySource) -> tuple[int, str, bytes, str]:
    precedence = source.requirement_precedence
    rank = PRECEDENCE_ORDER[precedence] if precedence is not None else 5
    path = source.path.to_bytes() if source.path is not None else b""
    return rank, source.source_type.value, path, source.label
