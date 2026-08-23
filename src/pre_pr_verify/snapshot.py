from __future__ import annotations

import base64
import hashlib
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal, cast

from pre_pr_verify.discovery_models import DiscoveryResult, SourceType
from pre_pr_verify.errors import PreflightError
from pre_pr_verify.git_capture import (
    GitRunner,
    _RootedReader,
    _resolve_repository,
    _tree_entries,
    _validate_raw_path,
    capture_changeset,
)
from pre_pr_verify.models import ChangeSet, FileKind, FileState, RawPath
from pre_pr_verify.verification_models import (
    FailureKind,
    SnapshotFile,
    SnapshotManifest,
    SnapshotKind,
    VerificationPlan,
    hash_payload,
)


@dataclass(frozen=True)
class DisposableSnapshot:
    path: Path
    manifest: SnapshotManifest


@dataclass(frozen=True)
class _MaterializedEntry:
    path: bytes
    kind: FileKind
    mode: str
    data: bytes


def _snapshot_materialization_gap(
    changeset: ChangeSet,
    discovery: DiscoveryResult,
    *,
    materialization_ordinal: int,
) -> SnapshotManifest:
    """Describe a post-capture snapshot gap without claiming executable state."""

    provisional = SnapshotManifest.model_construct(
        materialization_ordinal=materialization_ordinal,
        changeset_identity=changeset.identity,
        discovery_identity=discovery.identity,
        files=[],
        complete=False,
        materialization_failure=FailureKind.CAPABILITY,
        identity="",
    )
    return SnapshotManifest.model_validate(
        {
            "materialization_ordinal": materialization_ordinal,
            "changeset_identity": changeset.identity,
            "discovery_identity": discovery.identity,
            "files": [],
            "complete": False,
            "materialization_failure": FailureKind.CAPABILITY,
            "identity": hash_payload(
                provisional.model_dump(mode="json", exclude={"identity"})
            ),
        }
    )


def _content_map(changeset: ChangeSet) -> dict[str, bytes]:
    return {
        blob.sha256: base64.b64decode(blob.data_b64, validate=True)
        for blob in changeset.contents
    }


def _state_entry(
    state: FileState, contents: dict[str, bytes]
) -> _MaterializedEntry:
    if state.kind in {FileKind.ABSENT, FileKind.GITLINK}:
        raise PreflightError("unsupported effective snapshot state")
    if not state.content_captured:
        raise PreflightError("effective snapshot content is unavailable")
    try:
        data = contents[state.content_identity]
    except KeyError as error:
        raise PreflightError("effective snapshot content blob is missing") from error
    assert state.mode is not None
    return _MaterializedEntry(state.path.to_bytes(), state.kind, state.mode, data)


def _snapshot_entries(changeset: ChangeSet, runner: GitRunner) -> list[_MaterializedEntry]:
    head_entries = _tree_entries(runner, changeset.comparison.head_commit)
    values: dict[bytes, _MaterializedEntry] = {}
    for path, entry in head_entries.items():
        if entry.kind is FileKind.GITLINK:
            raise PreflightError("V1 cannot materialize a complete submodule snapshot")
        data = runner.run(["cat-file", "blob", entry.oid])
        values[path] = _MaterializedEntry(path, entry.kind, entry.mode, data)

    contents = _content_map(changeset)
    for change in changeset.changes:
        head_path = change.head.path.to_bytes()
        values.pop(head_path, None)
        if change.effective.kind is not FileKind.ABSENT:
            materialized = _state_entry(change.effective, contents)
            values[materialized.path] = materialized
    return [values[path] for path in sorted(values)]


def _ensure_parent(root: Path, path: bytes) -> Path:
    _validate_raw_path(path)
    target = Path(os.fsdecode(os.fsencode(root) + b"/" + path))
    current = root
    for component in path.split(b"/")[:-1]:
        current = Path(os.fsdecode(os.fsencode(current) + b"/" + component))
        if current.is_symlink():
            raise PreflightError("snapshot path crosses a symlink")
        current.mkdir(exist_ok=True)
    return target


def _write_entry(root: Path, entry: _MaterializedEntry) -> SnapshotFile:
    target = _ensure_parent(root, entry.path)
    if entry.kind is FileKind.SYMLINK:
        os.symlink(entry.data, os.fsencode(target))
    else:
        descriptor = os.open(
            os.fsencode(target),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o700 if entry.mode == "100755" else 0o600,
        )
        try:
            offset = 0
            while offset < len(entry.data):
                offset += os.write(descriptor, entry.data[offset:])
        finally:
            os.close(descriptor)
        os.chmod(target, 0o755 if entry.mode == "100755" else 0o644)
    if entry.mode not in {"100644", "100755", "120000"}:
        raise PreflightError("unsupported snapshot file mode")
    mode = cast(Literal["100644", "100755", "120000"], entry.mode)
    kind = (
        SnapshotKind.SYMLINK
        if entry.kind is FileKind.SYMLINK
        else SnapshotKind.REGULAR
    )
    return SnapshotFile(
        path=RawPath.from_bytes(entry.path),
        mode=mode,
        kind=kind,
        size=len(entry.data),
        content_sha256=hashlib.sha256(entry.data).hexdigest(),
    )


def _validate_discovery(snapshot: Path, discovery: DiscoveryResult) -> None:
    reader = _RootedReader(snapshot, "snapshot discovery evidence")
    for source in discovery.sources:
        if source.path is None or source.source_type in {
            SourceType.EXPLICIT_SPEC,
            SourceType.TEST_EVIDENCE,
            SourceType.DISCOVERY_CLUE,
        }:
            continue
        raw_path = source.path.to_bytes()
        _validate_raw_path(raw_path)
        try:
            data = reader.read_file(raw_path, limit=source.size)
        except PreflightError as error:
            raise PreflightError(
                "discovery evidence is not present in the executable snapshot"
            ) from error
        if data is None or hashlib.sha256(data).hexdigest() != source.content_sha256:
            raise PreflightError(
                "discovery evidence does not match the executable snapshot"
            )


def _validate_plan_sources(snapshot: Path, plan: VerificationPlan) -> None:
    reader = _RootedReader(snapshot, "snapshot verification guidance")
    for check in plan.checks:
        if check.source_path is None:
            continue
        assert check.source_size is not None
        try:
            data = reader.read_file(
                check.source_path.to_bytes(), limit=check.source_size
            )
        except PreflightError as error:
            raise PreflightError(
                "verification guidance is not present in the executable snapshot"
            ) from error
        if data is None or hashlib.sha256(data).hexdigest() != check.source_sha256:
            raise PreflightError(
                "verification guidance does not match the executable snapshot"
            )


@contextmanager
def disposable_snapshot(
    changeset: ChangeSet,
    discovery: DiscoveryResult,
    *,
    plan: VerificationPlan | None = None,
    materialization_ordinal: int = 0,
) -> Iterator[DisposableSnapshot]:
    if materialization_ordinal < 0:
        raise PreflightError("snapshot materialization ordinal must be non-negative")
    root, runner = _resolve_repository(Path(changeset.repository_root))
    if root != Path(discovery.repository_root).resolve():
        raise PreflightError("ChangeSet and discovery repository roots do not match")
    if plan is not None and (
        plan.changeset_identity != changeset.identity
        or plan.discovery_identity != discovery.identity
    ):
        raise PreflightError("verification plan does not match captured evidence")
    recaptured = capture_changeset(
        root,
        changeset.comparison.requested_base_ref,
        changeset.scope,
        limits=changeset.limits,
        explicit_includes=[item.to_bytes() for item in changeset.explicit_includes],
    )
    if recaptured.identity != changeset.identity:
        raise PreflightError("source repository no longer matches the intended ChangeSet")

    temporary = Path(tempfile.mkdtemp(prefix="pre-pr-verify-snapshot-"))
    try:
        files = [_write_entry(temporary, entry) for entry in _snapshot_entries(changeset, runner)]
        _validate_discovery(temporary, discovery)
        if plan is not None:
            _validate_plan_sources(temporary, plan)
        ordered_files = sorted(files, key=lambda item: item.path.to_bytes())
        provisional = SnapshotManifest.model_construct(
            materialization_ordinal=materialization_ordinal,
            changeset_identity=changeset.identity,
            discovery_identity=discovery.identity,
            files=ordered_files,
            identity="",
        )
        manifest = SnapshotManifest.model_validate(
            {
                "changeset_identity": changeset.identity,
                "discovery_identity": discovery.identity,
                "materialization_ordinal": materialization_ordinal,
                "files": ordered_files,
                "identity": hash_payload(
                    provisional.model_dump(mode="json", exclude={"identity"})
                ),
            }
        )
        after = capture_changeset(
            root,
            changeset.comparison.requested_base_ref,
            changeset.scope,
            limits=changeset.limits,
            explicit_includes=[
                item.to_bytes() for item in changeset.explicit_includes
            ],
        )
        if after.identity != changeset.identity:
            raise PreflightError("source repository changed while creating the snapshot")
        try:
            yield DisposableSnapshot(temporary, manifest)
        finally:
            final = capture_changeset(
                root,
                changeset.comparison.requested_base_ref,
                changeset.scope,
                limits=changeset.limits,
                explicit_includes=[
                    item.to_bytes() for item in changeset.explicit_includes
                ],
            )
            if final.identity != changeset.identity:
                raise PreflightError(
                    "verification did not preserve the source repository"
                )
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
