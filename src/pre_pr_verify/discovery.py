from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, cast

from pre_pr_verify.discovery_models import (
    DISCOVERY_SCHEMA_VERSION,
    PRECEDENCE_ORDER,
    DiscoveryIssue,
    DiscoveryIssueKind,
    DiscoveryResult,
    DiscoverySource,
    RequirementPrecedence,
    RequirementResolution,
    RequirementResolutionStatus,
    SourceTrust,
    SourceType,
    build_source_id,
    hash_payload,
    source_sort_key,
)
from pre_pr_verify.errors import PreflightError
from pre_pr_verify.git_capture import (
    _RootedReader,
    _resolve_repository,
    _validate_raw_path,
)
from pre_pr_verify.models import RawPath


MAX_SOURCE_BYTES = 1_048_576
MAX_TOTAL_BYTES = 4_194_304
MAX_REPOSITORY_SOURCES = 256

_ROOT_REQUIREMENT_NAMES = {
    b"readme.md",
    b"requirements.md",
    b"spec.md",
    b"prd.md",
}
_STANDARD_NAMES = {
    b"agents.md",
    b"contributing.md",
    b"code_style.md",
    b"style_guide.md",
}


@dataclass(frozen=True)
class ProvidedRequirement:
    label: str
    content: str
    precedence: RequirementPrecedence = RequirementPrecedence.EXPLICIT


@dataclass(frozen=True)
class TrustedSourceSelection:
    path: bytes | str
    expected_sha256: str


def _path_key(path: bytes | None) -> bytes:
    return path if path is not None else b""


def _build_source(
    *,
    source_type: SourceType,
    label: str,
    content: str,
    trust: SourceTrust,
    precedence: RequirementPrecedence | None,
    path: bytes | None = None,
    standards_scope: bytes | None = None,
) -> DiscoverySource:
    if not label:
        raise PreflightError("discovery source label must not be empty")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_SOURCE_BYTES:
        raise PreflightError("provided discovery source exceeds the size limit")
    content_sha256 = hashlib.sha256(encoded).hexdigest()
    return DiscoverySource(
        source_id=build_source_id(source_type, label, path, content_sha256),
        source_type=source_type,
        label=label,
        path=RawPath.from_bytes(path) if path is not None else None,
        content_sha256=content_sha256,
        size=len(encoded),
        content_utf8=content,
        trust=trust,
        requirement_precedence=precedence,
        standards_scope=(
            RawPath.from_bytes(standards_scope)
            if standards_scope is not None
            else None
        ),
    )


def _classify_repository_path(
    path: bytes,
) -> tuple[SourceType, RequirementPrecedence | None, bytes | None] | None:
    components = path.split(b"/")
    basename = components[-1].lower()
    if basename in _STANDARD_NAMES:
        scope = b"/".join(components[:-1]) or b"."
        return SourceType.REPOSITORY_STANDARD, None, scope
    if len(components) == 1 and basename in _ROOT_REQUIREMENT_NAMES:
        return (
            SourceType.REPOSITORY_REQUIREMENT,
            RequirementPrecedence.REPOSITORY_DOCUMENTATION,
            None,
        )
    if (
        len(components) >= 2
        and components[0].lower() == b"docs"
        and basename.endswith(b".md")
    ):
        return (
            SourceType.REPOSITORY_REQUIREMENT,
            RequirementPrecedence.REPOSITORY_DOCUMENTATION,
            None,
        )
    return None


def _read_repository_source(
    reader: _RootedReader,
    path: bytes,
) -> tuple[str | None, DiscoveryIssue | None]:
    try:
        raw = reader.read_file(path, limit=MAX_SOURCE_BYTES)
    except PreflightError as error:
        kind = (
            DiscoveryIssueKind.SOURCE_TOO_LARGE
            if "exceeds safe size" in str(error)
            else DiscoveryIssueKind.UNSAFE_PATH
        )
        return None, DiscoveryIssue(
            kind=kind,
            path=RawPath.from_bytes(path),
            detail="repository source could not be read within the discovery boundary",
        )
    if raw is None:
        return None, DiscoveryIssue(
            kind=DiscoveryIssueKind.UNSAFE_PATH,
            path=RawPath.from_bytes(path),
            detail="repository source disappeared during discovery",
        )
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, DiscoveryIssue(
            kind=DiscoveryIssueKind.UNSUPPORTED_ENCODING,
            path=RawPath.from_bytes(path),
            detail="repository source is not valid UTF-8",
        )


def _provided_source_type(precedence: RequirementPrecedence) -> SourceType:
    if precedence is RequirementPrecedence.EXPLICIT:
        return SourceType.EXPLICIT_SPEC
    if precedence is RequirementPrecedence.TEST_EVIDENCE:
        return SourceType.TEST_EVIDENCE
    if precedence is RequirementPrecedence.DISCOVERY_CLUE:
        return SourceType.DISCOVERY_CLUE
    raise PreflightError("provided evidence uses a reserved requirement precedence")


def _resolve_requirements(sources: list[DiscoverySource]) -> RequirementResolution:
    candidates = [
        source for source in sources if source.requirement_precedence is not None
    ]
    if not candidates:
        return RequirementResolution(
            status=RequirementResolutionStatus.MISSING,
            precedence=None,
            candidate_source_ids=[],
        )
    winning_precedence = min(
        (
            cast(RequirementPrecedence, source.requirement_precedence)
            for source in candidates
        ),
        key=lambda value: PRECEDENCE_ORDER[value],
    )
    winners = [
        source
        for source in candidates
        if source.requirement_precedence is winning_precedence
    ]
    source_ids = [source.source_id for source in winners]
    return RequirementResolution(
        status=RequirementResolutionStatus.CANDIDATES,
        precedence=winning_precedence,
        candidate_source_ids=source_ids,
    )


def discover_review_sources(
    repository: Path | str,
    *,
    explicit_specs: Iterable[ProvidedRequirement] = (),
    trusted_selection: TrustedSourceSelection | None = None,
    additional_evidence: Iterable[ProvidedRequirement] = (),
) -> DiscoveryResult:
    root, runner = _resolve_repository(Path(repository))
    reader = _RootedReader(root, "repository discovery")
    sources: list[DiscoverySource] = []
    issues: list[DiscoveryIssue] = []

    for provided in explicit_specs:
        if provided.precedence is not RequirementPrecedence.EXPLICIT:
            raise PreflightError("explicit spec must use explicit precedence")
        sources.append(
            _build_source(
                source_type=SourceType.EXPLICIT_SPEC,
                label=provided.label,
                content=provided.content,
                trust=SourceTrust.INVOCATION,
                precedence=RequirementPrecedence.EXPLICIT,
            )
        )

    if trusted_selection is not None:
        raw_path = (
            trusted_selection.path
            if isinstance(trusted_selection.path, bytes)
            else os.fsencode(trusted_selection.path)
        )
        _validate_raw_path(raw_path)
        content, issue = _read_repository_source(reader, raw_path)
        if issue is not None or content is None:
            raise PreflightError("trusted selected source cannot be read safely")
        actual_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if actual_digest != trusted_selection.expected_sha256:
            raise PreflightError("trusted selected source digest does not match")
        sources.append(
            _build_source(
                source_type=SourceType.TRUSTED_POLICY_SELECTED,
                label=f"trusted selection: {RawPath.from_bytes(raw_path).display}",
                path=raw_path,
                content=content,
                trust=SourceTrust.TRUSTED_SELECTION,
                precedence=RequirementPrecedence.TRUSTED_POLICY,
            )
        )

    listed = runner.run(["ls-files", "-co", "--exclude-standard", "-z"])
    repository_paths = sorted(set(path for path in listed.split(b"\x00") if path))
    discovered_count = 0
    total_bytes = sum(source.size for source in sources)
    for path in repository_paths:
        _validate_raw_path(path)
        classification = _classify_repository_path(path)
        if classification is None:
            continue
        if discovered_count >= MAX_REPOSITORY_SOURCES:
            issues.append(
                DiscoveryIssue(
                    kind=DiscoveryIssueKind.SOURCE_LIMIT_REACHED,
                    path=None,
                    detail="repository discovery source-count limit was reached",
                )
            )
            break
        content, issue = _read_repository_source(reader, path)
        if issue is not None:
            issues.append(issue)
            continue
        assert content is not None
        encoded_size = len(content.encode("utf-8"))
        if total_bytes + encoded_size > MAX_TOTAL_BYTES:
            issues.append(
                DiscoveryIssue(
                    kind=DiscoveryIssueKind.SOURCE_LIMIT_REACHED,
                    path=RawPath.from_bytes(path),
                    detail="repository discovery total-content limit was reached",
                )
            )
            break
        source_type, precedence, standards_scope = classification
        sources.append(
            _build_source(
                source_type=source_type,
                label=RawPath.from_bytes(path).display,
                path=path,
                content=content,
                trust=SourceTrust.UNTRUSTED_REPOSITORY,
                precedence=precedence,
                standards_scope=standards_scope,
            )
        )
        discovered_count += 1
        total_bytes += encoded_size

    for provided in additional_evidence:
        source_type = _provided_source_type(provided.precedence)
        sources.append(
            _build_source(
                source_type=source_type,
                label=provided.label,
                content=provided.content,
                trust=SourceTrust.UNTRUSTED_REPOSITORY,
                precedence=provided.precedence,
            )
        )

    ordered_sources = sorted(sources, key=source_sort_key)
    ordered_issues = sorted(
        issues,
        key=lambda issue: (
            issue.kind.value,
            _path_key(issue.path.to_bytes() if issue.path is not None else None),
            issue.detail,
        ),
    )
    resolution = _resolve_requirements(ordered_sources)
    standards_ids = [
        source.source_id
        for source in ordered_sources
        if source.source_type is SourceType.REPOSITORY_STANDARD
    ]
    values: dict[str, Any] = {
        "contract": "discovery",
        "schema_version": DISCOVERY_SCHEMA_VERSION,
        "repository_root": str(root),
        "sources": ordered_sources,
        "requirement_resolution": resolution,
        "standards_source_ids": standards_ids,
        "issues": ordered_issues,
    }
    provisional = DiscoveryResult.model_construct(**values, identity="")
    values["identity"] = hash_payload(provisional.semantic_payload())
    return DiscoveryResult.model_validate(values)
