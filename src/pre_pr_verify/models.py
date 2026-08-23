from __future__ import annotations

import base64
import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SCHEMA_VERSION = "1.0.0"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
GIT_OID_PATTERN = r"^[0-9a-f]{40,64}$"


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _display_bytes(value: bytes) -> str:
    escaped: list[str] = []
    short = {9: r"\t", 10: r"\n", 13: r"\r", 92: r"\\"}
    for byte in value:
        if byte in short:
            escaped.append(short[byte])
        elif 32 <= byte <= 126:
            escaped.append(chr(byte))
        else:
            escaped.append(f"\\x{byte:02x}")
    return "".join(escaped)


class RawPath(FrozenModel):
    raw_b64: str
    display: str
    utf8: str | None = None

    @classmethod
    def from_bytes(cls, value: bytes) -> RawPath:
        try:
            utf8 = value.decode("utf-8")
        except UnicodeDecodeError:
            utf8 = None
        return cls(
            raw_b64=base64.b64encode(value).decode("ascii"),
            display=_display_bytes(value),
            utf8=utf8,
        )

    def to_bytes(self) -> bytes:
        return base64.b64decode(self.raw_b64, validate=True)

    @model_validator(mode="after")
    def validate_representations(self) -> RawPath:
        try:
            raw = self.to_bytes()
        except (ValueError, TypeError) as error:
            raise ValueError("raw_b64 is not valid base64") from error
        try:
            canonical_utf8 = raw.decode("utf-8")
        except UnicodeDecodeError:
            canonical_utf8 = None
        if self.display != _display_bytes(raw) or self.utf8 != canonical_utf8:
            raise ValueError("path representations are not canonical")
        return self


class ScopeMode(StrEnum):
    PENDING = "pending"
    COMMITTED_ONLY = "committed-only"


class ChangeOrigin(StrEnum):
    COMMITTED = "committed"
    STAGED = "staged"
    UNSTAGED = "unstaged"
    UNTRACKED = "untracked"


class FileKind(StrEnum):
    ABSENT = "absent"
    REGULAR = "regular"
    SYMLINK = "symlink"
    GITLINK = "gitlink"


class IdentityKind(StrEnum):
    ABSENT = "absent"
    SHA256 = "sha256"
    GIT_OID = "git_oid"


class OmissionReason(StrEnum):
    PER_FILE_LIMIT = "per_file_limit"
    TOTAL_LIMIT = "total_limit"


class ContentLimits(FrozenModel):
    per_file_bytes: int = Field(default=1_048_576, ge=0)
    total_bytes: int = Field(default=10_485_760, ge=0)


class Comparison(FrozenModel):
    requested_base_ref: str = Field(min_length=1)
    resolved_base_commit: str = Field(pattern=GIT_OID_PATTERN)
    merge_base_commit: str = Field(pattern=GIT_OID_PATTERN)
    head_commit: str = Field(pattern=GIT_OID_PATTERN)


class ContentBlob(FrozenModel):
    sha256: str = Field(pattern=SHA256_PATTERN)
    size: int = Field(ge=0)
    data_b64: str

    @model_validator(mode="after")
    def validate_content(self) -> ContentBlob:
        try:
            raw = base64.b64decode(self.data_b64, validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError("content blob is not valid base64") from error
        if len(raw) != self.size:
            raise ValueError("content blob size does not match decoded bytes")
        if hashlib.sha256(raw).hexdigest() != self.sha256:
            raise ValueError("content blob digest does not match decoded bytes")
        return self


class FileState(FrozenModel):
    path: RawPath
    kind: FileKind
    mode: str | None
    size: int = Field(ge=0)
    identity_kind: IdentityKind
    content_identity: str
    binary: bool | None
    content_captured: bool
    omission_reason: OmissionReason | None = None

    @classmethod
    def absent(cls, path: RawPath) -> FileState:
        return cls(
            path=path,
            kind=FileKind.ABSENT,
            mode=None,
            size=0,
            identity_kind=IdentityKind.ABSENT,
            content_identity="absent",
            binary=None,
            content_captured=False,
        )

    @model_validator(mode="after")
    def validate_state(self) -> FileState:
        if self.kind is FileKind.ABSENT:
            if (
                self.mode is not None
                or self.size != 0
                or self.identity_kind is not IdentityKind.ABSENT
                or self.content_identity != "absent"
                or self.binary is not None
                or self.content_captured
                or self.omission_reason is not None
            ):
                raise ValueError("absent file state is inconsistent")
            return self

        if self.mode is None:
            raise ValueError("present file state requires mode")
        if self.kind is FileKind.GITLINK:
            if self.identity_kind is not IdentityKind.GIT_OID:
                raise ValueError("gitlink requires git_oid identity")
            if not __import__("re").fullmatch(GIT_OID_PATTERN[1:-1], self.content_identity):
                raise ValueError("gitlink identity is not a Git object ID")
            if self.content_captured or self.omission_reason is not None:
                raise ValueError("gitlink has no captured content blob")
            return self

        if self.identity_kind is not IdentityKind.SHA256 or not __import__(
            "re"
        ).fullmatch(SHA256_PATTERN[1:-1], self.content_identity):
            raise ValueError("file content requires sha256 identity")
        if self.content_captured and self.omission_reason is not None:
            raise ValueError("captured content cannot have an omission reason")
        if not self.content_captured and self.omission_reason is None:
            raise ValueError("omitted content requires an omission reason")
        return self


class RenameRelation(FrozenModel):
    origin: ChangeOrigin
    old_path: RawPath
    new_path: RawPath
    similarity: int = Field(ge=0, le=100)


class FileChange(FrozenModel):
    origins: list[ChangeOrigin] = Field(min_length=1)
    base: FileState
    head: FileState
    index: FileState | None
    working: FileState | None
    effective: FileState

    @model_validator(mode="after")
    def validate_origins(self) -> FileChange:
        order = list(ChangeOrigin)
        if self.origins != sorted(set(self.origins), key=order.index):
            raise ValueError("change origins must be unique and canonically ordered")
        return self


class ChangeSet(FrozenModel):
    contract: Literal["changeset"] = "changeset"
    schema_version: Literal["1.0.0"] = "1.0.0"
    repository_root: str
    scope: ScopeMode
    comparison: Comparison
    limits: ContentLimits
    empty: bool
    changes: list[FileChange]
    renames: list[RenameRelation]
    contents: list[ContentBlob]
    identity: str = Field(pattern=SHA256_PATTERN)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json",
            exclude={"identity", "repository_root"},
        )

    @model_validator(mode="after")
    def validate_contract(self) -> ChangeSet:
        if self.empty != (not self.changes):
            raise ValueError("empty flag must match the change list")
        if self.identity != _hash_payload(self.semantic_payload()):
            raise ValueError("changeset identity does not match semantic payload")

        blobs = {blob.sha256 for blob in self.contents}
        if len(blobs) != len(self.contents):
            raise ValueError("content blobs must have unique digests")
        for change in self.changes:
            for state in (
                change.base,
                change.head,
                change.index,
                change.working,
                change.effective,
            ):
                if (
                    state is not None
                    and state.content_captured
                    and state.content_identity not in blobs
                ):
                    raise ValueError("captured file state references a missing content blob")
        return self


def _hash_payload(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _change_sort_key(change: FileChange) -> bytes:
    return change.effective.path.to_bytes()


def build_changeset(
    *,
    repository_root: str,
    scope: ScopeMode,
    comparison: Comparison,
    limits: ContentLimits,
    changes: list[FileChange],
    contents: list[ContentBlob],
    renames: list[RenameRelation] | None = None,
) -> ChangeSet:
    ordered_changes = sorted(changes, key=_change_sort_key)
    ordered_contents = sorted(contents, key=lambda blob: blob.sha256)
    ordered_renames = sorted(
        renames or [],
        key=lambda relation: (
            list(ChangeOrigin).index(relation.origin),
            relation.old_path.to_bytes(),
            relation.new_path.to_bytes(),
        ),
    )
    values: dict[str, Any] = {
        "contract": "changeset",
        "schema_version": SCHEMA_VERSION,
        "repository_root": repository_root,
        "scope": scope,
        "comparison": comparison,
        "limits": limits,
        "empty": not ordered_changes,
        "changes": ordered_changes,
        "renames": ordered_renames,
        "contents": ordered_contents,
    }
    provisional = ChangeSet.model_construct(**values, identity="")
    values["identity"] = _hash_payload(provisional.semantic_payload())
    return ChangeSet.model_validate(values)
