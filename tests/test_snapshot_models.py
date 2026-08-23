import pytest
from pydantic import ValidationError

from pre_pr_verify.models import RawPath
from pre_pr_verify.verification_models import (
    SnapshotFile,
    SnapshotKind,
    SnapshotManifest,
    hash_payload,
)


def snapshot_file(*, kind: SnapshotKind, mode: str) -> SnapshotFile:
    return SnapshotFile(
        path=RawPath.from_bytes(b"file"),
        kind=kind,
        mode=mode,
        size=1,
        content_sha256="a" * 64,
    )


def test_snapshot_file_accepts_only_supported_kind_mode_pairs() -> None:
    assert snapshot_file(kind=SnapshotKind.REGULAR, mode="100644").kind is SnapshotKind.REGULAR
    assert snapshot_file(kind=SnapshotKind.REGULAR, mode="100755").mode == "100755"
    assert snapshot_file(kind=SnapshotKind.SYMLINK, mode="120000").kind is SnapshotKind.SYMLINK


@pytest.mark.parametrize(
    ("kind", "mode"),
    [(SnapshotKind.REGULAR, "120000"), (SnapshotKind.SYMLINK, "100644")],
)
def test_snapshot_file_rejects_impossible_kind_mode_pairs(
    kind: SnapshotKind, mode: str
) -> None:
    with pytest.raises(ValidationError, match="snapshot file"):
        snapshot_file(kind=kind, mode=mode)


def test_snapshot_file_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        snapshot_file(kind="gitlink", mode="160000")  # type: ignore[arg-type]


def test_snapshot_manifest_rejects_duplicate_paths() -> None:
    first = snapshot_file(kind=SnapshotKind.REGULAR, mode="100644")
    second = first.model_copy(update={"content_sha256": "b" * 64})
    payload = {
        "materialization_ordinal": 0,
        "changeset_identity": "a" * 64,
        "discovery_identity": "b" * 64,
        "files": [first.model_dump(mode="json"), second.model_dump(mode="json")],
    }
    payload["identity"] = hash_payload(payload)

    with pytest.raises(ValidationError, match="unique"):
        SnapshotManifest.model_validate(payload)


@pytest.mark.parametrize(
    "raw_path",
    [b"../escape", b"/absolute", b"nested/../escape", b".git/config", b"nested/.git/config"],
)
def test_snapshot_manifest_rejects_unbounded_external_paths(raw_path: bytes) -> None:
    file_payload = snapshot_file(
        kind=SnapshotKind.REGULAR, mode="100644"
    ).model_dump(mode="json")
    file_payload["path"] = RawPath.from_bytes(raw_path).model_dump(mode="json")
    payload = {
        "materialization_ordinal": 0,
        "changeset_identity": "a" * 64,
        "discovery_identity": "b" * 64,
        "files": [file_payload],
    }
    payload["identity"] = hash_payload(payload)

    with pytest.raises(ValidationError, match="repository-relative and bounded"):
        SnapshotManifest.model_validate(payload)


def test_incomplete_snapshot_requires_structured_failure() -> None:
    payload = {
        "materialization_ordinal": 0,
        "changeset_identity": "a" * 64,
        "discovery_identity": "b" * 64,
        "files": [],
        "complete": False,
        "materialization_failure": "capability",
    }
    payload["identity"] = hash_payload(payload)

    manifest = SnapshotManifest.model_validate(payload)
    assert manifest.complete is False

    payload["materialization_failure"] = None
    payload["identity"] = hash_payload(payload)
    with pytest.raises(ValidationError, match="incomplete snapshot"):
        SnapshotManifest.model_validate(payload)
