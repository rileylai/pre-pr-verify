from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import tempfile
import zlib
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
    _TreeEntry,
    _kind_from_mode,
    _tree_entries,
    _validate_raw_path,
    capture_changeset,
    source_preservation_fingerprint,
)
from pre_pr_verify.models import ChangeSet, FileKind, FileState, RawPath
from pre_pr_verify.verification_models import (
    FailureKind,
    EnvironmentProfile,
    GitObjectFormat,
    SnapshotFile,
    SnapshotManifest,
    SnapshotKind,
    VerificationPlan,
    hash_payload,
)


MAX_TRACKED_ENTRIES = 100_000
MAX_IMPORTED_OBJECTS = 250_000
MAX_LOGICAL_OBJECT_BYTES = 1 << 30
MAX_MATERIALIZED_BYTES = 1 << 30


class _MaterializationGap(Exception):
    def __init__(
        self,
        failure: FailureKind,
        message: str,
        object_format: GitObjectFormat | None,
    ) -> None:
        super().__init__(message)
        self.failure = failure
        self.object_format = object_format


@dataclass(frozen=True)
class _GitIndexEntry:
    mode: str
    kind: FileKind
    oid: str


@dataclass(frozen=True)
class _PreparedGitObject:
    kind: str
    data: bytes


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


def _git_snapshot_materialization_gap(
    changeset: ChangeSet,
    discovery: DiscoveryResult,
    *,
    materialization_ordinal: int,
    failure: FailureKind,
    object_format: GitObjectFormat | None,
) -> SnapshotManifest:
    provisional = SnapshotManifest.model_construct(
        materialization_ordinal=materialization_ordinal,
        changeset_identity=changeset.identity,
        discovery_identity=discovery.identity,
        environment_profile=EnvironmentProfile.GIT_REPOSITORY,
        object_format=object_format,
        files=[],
        complete=False,
        materialization_failure=failure,
        identity="",
    )
    return SnapshotManifest.model_validate(
        {
            "materialization_ordinal": materialization_ordinal,
            "changeset_identity": changeset.identity,
            "discovery_identity": discovery.identity,
            "environment_profile": EnvironmentProfile.GIT_REPOSITORY,
            "object_format": object_format,
            "files": [],
            "complete": False,
            "materialization_failure": failure,
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


def _git_object_id(kind: str, data: bytes, object_format: GitObjectFormat) -> str:
    payload = f"{kind} {len(data)}\0".encode("ascii") + data
    return hashlib.new(object_format.value, payload).hexdigest()


def _source_object_format(
    runner: GitRunner,
) -> GitObjectFormat:
    try:
        value = runner.run(["rev-parse", "--show-object-format"]).strip().decode(
            "ascii", "strict"
        )
    except (UnicodeDecodeError, PreflightError) as error:
        raise _MaterializationGap(
            FailureKind.CAPABILITY,
            "source Git object format is unavailable",
            None,
        ) from error
    try:
        return GitObjectFormat(value)
    except ValueError as error:
        raise _MaterializationGap(
            FailureKind.CAPABILITY,
            f"unsupported Git object format: {value!r}",
            None,
        ) from error


def _head_tree(
    runner: GitRunner,
    head: str,
    object_format: GitObjectFormat,
) -> tuple[dict[bytes, _TreeEntry], set[str]]:
    """Read HEAD's tree index and the exact tree/blob closure in one listing."""

    try:
        root_tree = runner.run(
            ["rev-parse", "--verify", "--end-of-options", f"{head}^{{tree}}"]
        ).strip().decode("ascii", "strict")
        output = runner.run(["ls-tree", "-r", "-t", "-z", "--full-tree", head])
    except (UnicodeDecodeError, PreflightError) as error:
        raise _MaterializationGap(
            FailureKind.CONFIGURATION,
            "reviewed HEAD tree cannot be read",
            object_format,
        ) from error
    entries: dict[bytes, _TreeEntry] = {}
    object_ids = {root_tree}
    for record in output.split(b"\x00"):
        if not record:
            continue
        try:
            header, path = record.split(b"\t", 1)
            mode_raw, type_raw, oid_raw = header.split(b" ", 2)
            mode = mode_raw.decode("ascii", "strict")
            object_type = type_raw.decode("ascii", "strict")
            oid = oid_raw.decode("ascii", "strict")
            _validate_raw_path(path)
            if object_type == "tree":
                object_ids.add(oid)
                continue
            kind = _kind_from_mode(mode)
            if kind is FileKind.GITLINK:
                raise _MaterializationGap(
                    FailureKind.CAPABILITY,
                    "Gitlink entries are not materializable in v0.1.8",
                    object_format,
                )
            if object_type != "blob":
                raise ValueError("non-blob HEAD tree entry")
            entries[path] = _TreeEntry(mode, kind, oid)
            object_ids.add(oid)
        except _MaterializationGap:
            raise
        except (UnicodeDecodeError, ValueError) as error:
            raise _MaterializationGap(
                FailureKind.CONFIGURATION,
                "reviewed HEAD tree listing is malformed",
                object_format,
            ) from error
    return entries, object_ids


def _read_head_objects(
    runner: GitRunner,
    head: str,
    object_ids: set[str],
    object_format: GitObjectFormat,
) -> dict[str, _PreparedGitObject]:
    requested = [head, *sorted(object_ids)]
    try:
        responses = runner.batch_cat_file(requested)
    except PreflightError as error:
        raise _MaterializationGap(
            FailureKind.CONFIGURATION,
            "reviewed Git object closure cannot be read",
            object_format,
        ) from error
    objects: dict[str, _PreparedGitObject] = {}
    for object_id in requested:
        try:
            kind, data = responses[object_id]
        except KeyError as error:
            raise _MaterializationGap(
                FailureKind.CONFIGURATION,
                "Git object closure is incomplete",
                object_format,
            ) from error
        if kind not in {"commit", "tree", "blob"}:
            raise _MaterializationGap(
                FailureKind.CONFIGURATION,
                "Git object closure contains an unsupported object type",
                object_format,
            )
        objects[object_id] = _PreparedGitObject(kind, data)
    if objects[head].kind != "commit":
        raise _MaterializationGap(
            FailureKind.CONFIGURATION,
            "reviewed HEAD is not a commit object",
            object_format,
        )
    return objects


def _state_payload(
    state: FileState,
    contents: dict[str, bytes],
    source_payloads: dict[str, bytes],
    object_format: GitObjectFormat,
) -> bytes | None:
    if state.kind is FileKind.ABSENT:
        return None
    if state.kind is FileKind.GITLINK:
        raise _MaterializationGap(
            FailureKind.CAPABILITY,
            "Gitlink entries are not materializable in v0.1.8",
            object_format,
        )
    if state.mode not in {"100644", "100755", "120000"}:
        raise _MaterializationGap(
            FailureKind.CONFIGURATION,
            "captured Git file mode is unsupported",
            object_format,
        )
    if state.content_captured:
        try:
            return contents[state.content_identity]
        except KeyError as error:
            raise _MaterializationGap(
                FailureKind.CONFIGURATION,
                "captured content blob is unavailable",
                object_format,
            ) from error
    try:
        return source_payloads[state.content_identity]
    except KeyError as error:
        raise _MaterializationGap(
            FailureKind.CAPABILITY,
            "required captured content was omitted",
            object_format,
        ) from error


def _check_git_budget(
    label: str,
    actual: int,
    limit: int,
    object_format: GitObjectFormat,
) -> None:
    if actual > limit:
        raise _MaterializationGap(
            FailureKind.CAPABILITY,
            f"Git materialization budget exceeded: {label}={actual} > {limit}",
            object_format,
        )


def _destination_environment(private_root: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(private_root / "home"),
        "TMPDIR": str(private_root / "tmp"),
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "GIT_OPTIONAL_LOCKS": "0",
    }


def _run_destination_git(
    repository: Path,
    private_root: Path,
    args: list[str],
    *,
    input_data: bytes | None = None,
) -> None:
    result = subprocess.run(
        ["git", "--no-pager", *args],
        cwd=repository,
        env=_destination_environment(private_root),
        stdin=None if input_data is not None else subprocess.DEVNULL,
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "backslashreplace").strip()
        raise PreflightError(message or f"destination Git command failed: {args[0]}")


def _write_loose_object(
    git_directory: Path,
    kind: str,
    data: bytes,
    object_format: GitObjectFormat,
) -> str:
    object_id = _git_object_id(kind, data, object_format)
    object_directory = git_directory / "objects" / object_id[:2]
    object_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    object_path = object_directory / object_id[2:]
    if not object_path.exists():
        compressed = zlib.compress(f"{kind} {len(data)}\0".encode("ascii") + data)
        temporary = object_directory / f".{object_id[2:]}.tmp-{os.getpid()}"
        try:
            descriptor = os.open(
                os.fsencode(temporary),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                offset = 0
                while offset < len(compressed):
                    offset += os.write(descriptor, compressed[offset:])
            finally:
                os.close(descriptor)
            os.replace(temporary, object_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return object_id


def _create_git_repository(
    private_root: Path,
    object_format: GitObjectFormat,
    head: str,
    objects: dict[str, _PreparedGitObject],
    index_entries: dict[bytes, _GitIndexEntry],
    worktree_entries: dict[bytes, _MaterializedEntry],
) -> tuple[Path, list[SnapshotFile]]:
    template = private_root / "template"
    (template / "hooks").mkdir(mode=0o700, parents=True)
    (private_root / "home").mkdir(mode=0o700)
    (private_root / "tmp").mkdir(mode=0o700)
    repository = private_root / "repository"
    repository.mkdir(mode=0o700)
    try:
        _run_destination_git(
            private_root,
            private_root,
            [
                "init",
                "--quiet",
                f"--template={template}",
                f"--object-format={object_format.value}",
                str(repository),
            ],
        )
        git_directory = repository / ".git"
        for object_id, object_value in objects.items():
            actual = _write_loose_object(
                git_directory,
                object_value.kind,
                object_value.data,
                object_format,
            )
            if actual != object_id:
                raise PreflightError("Git object identity changed during materialization")
        _run_destination_git(
            repository,
            private_root,
            ["update-ref", "--no-deref", "HEAD", head],
        )
        if index_entries:
            index_data = b"".join(
                mode.encode("ascii")
                + b" "
                + entry.oid.encode("ascii")
                + b"\t"
                + path
                + b"\x00"
                for path, entry in sorted(index_entries.items())
                for mode in [entry.mode]
            )
            _run_destination_git(
                repository,
                private_root,
                ["update-index", "--add", "-z", "--index-info"],
                input_data=index_data,
            )
        files = [
            _write_entry(repository, entry)
            for entry in sorted(worktree_entries.values(), key=lambda item: item.path)
        ]
        return repository, files
    except OSError as error:
        raise PreflightError("standalone Git repository could not be written") from error


def _prepare_git_materialization(
    changeset: ChangeSet,
    runner: GitRunner,
    private_root: Path,
) -> tuple[Path, GitObjectFormat, list[SnapshotFile]]:
    object_format = _source_object_format(runner)
    try:
        head = runner.run(
            ["rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"]
        ).strip().decode("ascii", "strict")
    except (UnicodeDecodeError, PreflightError) as error:
        raise _MaterializationGap(
            FailureKind.CONFIGURATION,
            "reviewed HEAD cannot be read",
            object_format,
        ) from error
    if head != changeset.comparison.head_commit:
        raise PreflightError("source repository HEAD no longer matches the ChangeSet")

    head_records, tree_object_ids = _head_tree(runner, head, object_format)
    head_objects = _read_head_objects(
        runner,
        head,
        tree_object_ids,
        object_format,
    )
    source_payloads = {
        hashlib.sha256(value.data).hexdigest(): value.data
        for object_id, value in head_objects.items()
        if value.kind == "blob" and object_id in tree_object_ids
    }
    source_blob_oids = {
        hashlib.sha256(value.data).hexdigest(): object_id
        for object_id, value in head_objects.items()
        if value.kind == "blob" and object_id in tree_object_ids
    }
    contents = _content_map(changeset)

    worktree_entries: dict[bytes, _MaterializedEntry] = {}
    index_entries: dict[bytes, _GitIndexEntry] = {}
    objects = dict(head_objects)
    for path, record in head_records.items():
        mode, kind, object_id = record.mode, record.kind, record.oid
        payload = head_objects[object_id].data
        worktree_entries[path] = _MaterializedEntry(path, kind, mode, payload)
        index_entries[path] = _GitIndexEntry(mode, kind, object_id)

    for change in changeset.changes:
        if change.head.kind is not FileKind.ABSENT:
            worktree_entries.pop(change.head.path.to_bytes(), None)
        effective_payload = _state_payload(
            change.effective,
            contents,
            source_payloads,
            object_format,
        )
        if effective_payload is not None:
            assert change.effective.mode is not None
            worktree_entries[change.effective.path.to_bytes()] = _MaterializedEntry(
                change.effective.path.to_bytes(),
                change.effective.kind,
                change.effective.mode,
                effective_payload,
            )

        if change.index is None:
            continue
        if change.head.kind is not FileKind.ABSENT:
            index_entries.pop(change.head.path.to_bytes(), None)
        index_payload = _state_payload(
            change.index,
            contents,
            source_payloads,
            object_format,
        )
        if index_payload is None:
            continue
        assert change.index.mode is not None
        digest = hashlib.sha256(index_payload).hexdigest()
        index_object_id = source_blob_oids.get(digest)
        if index_object_id is None:
            index_object_id = _git_object_id("blob", index_payload, object_format)
            objects.setdefault(
                index_object_id,
                _PreparedGitObject("blob", index_payload),
            )
        index_entries[change.index.path.to_bytes()] = _GitIndexEntry(
            change.index.mode,
            change.index.kind,
            index_object_id,
        )

    _check_git_budget(
        "tracked_entries",
        len(index_entries),
        MAX_TRACKED_ENTRIES,
        object_format,
    )
    logical_object_bytes = sum(len(value.data) for value in objects.values())
    _check_git_budget(
        "imported_objects",
        len(objects),
        MAX_IMPORTED_OBJECTS,
        object_format,
    )
    _check_git_budget(
        "logical_object_bytes",
        logical_object_bytes,
        MAX_LOGICAL_OBJECT_BYTES,
        object_format,
    )
    materialized_bytes = sum(len(entry.data) for entry in worktree_entries.values())
    _check_git_budget(
        "materialized_bytes",
        materialized_bytes,
        MAX_MATERIALIZED_BYTES,
        object_format,
    )
    repository, files = _create_git_repository(
        private_root,
        object_format,
        head,
        objects,
        index_entries,
        worktree_entries,
    )
    return repository, object_format, files


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
    source_fingerprint = source_preservation_fingerprint(root)
    recaptured = capture_changeset(
        root,
        changeset.comparison.requested_base_ref,
        changeset.scope,
        limits=changeset.limits,
        explicit_includes=[item.to_bytes() for item in changeset.explicit_includes],
    )
    if (
        recaptured.identity != changeset.identity
        or source_preservation_fingerprint(root) != source_fingerprint
    ):
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
        if (
            after.identity != changeset.identity
            or source_preservation_fingerprint(root) != source_fingerprint
        ):
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
            if (
                final.identity != changeset.identity
                or source_preservation_fingerprint(root) != source_fingerprint
            ):
                raise PreflightError(
                    "verification did not preserve the source repository"
                )
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


@contextmanager
def disposable_git_snapshot(
    changeset: ChangeSet,
    discovery: DiscoveryResult,
    *,
    plan: VerificationPlan | None = None,
    materialization_ordinal: int = 0,
) -> Iterator[DisposableSnapshot]:
    """Materialize one independent, bounded Git repository for later execution."""

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
    source_fingerprint = source_preservation_fingerprint(root)
    recaptured = capture_changeset(
        root,
        changeset.comparison.requested_base_ref,
        changeset.scope,
        limits=changeset.limits,
        explicit_includes=[item.to_bytes() for item in changeset.explicit_includes],
    )
    if (
        recaptured.identity != changeset.identity
        or source_preservation_fingerprint(root) != source_fingerprint
    ):
        raise PreflightError("source repository no longer matches the intended ChangeSet")

    temporary = Path(tempfile.mkdtemp(prefix="pre-pr-verify-git-snapshot-"))
    try:
        object_format: GitObjectFormat | None = None
        try:
            repository, object_format, files = _prepare_git_materialization(
                changeset,
                runner,
                temporary,
            )
            _validate_discovery(repository, discovery)
            if plan is not None:
                _validate_plan_sources(repository, plan)
            ordered_files = sorted(files, key=lambda item: item.path.to_bytes())
            provisional = SnapshotManifest.model_construct(
                materialization_ordinal=materialization_ordinal,
                changeset_identity=changeset.identity,
                discovery_identity=discovery.identity,
                environment_profile=EnvironmentProfile.GIT_REPOSITORY,
                object_format=object_format,
                files=ordered_files,
                identity="",
            )
            manifest = SnapshotManifest.model_validate(
                {
                    "changeset_identity": changeset.identity,
                    "discovery_identity": discovery.identity,
                    "materialization_ordinal": materialization_ordinal,
                    "environment_profile": EnvironmentProfile.GIT_REPOSITORY,
                    "object_format": object_format,
                    "files": ordered_files,
                    "identity": hash_payload(
                        provisional.model_dump(mode="json", exclude={"identity"})
                    ),
                }
            )
        except _MaterializationGap as gap:
            object_format = gap.object_format
            shutil.rmtree(temporary, ignore_errors=True)
            temporary.mkdir(mode=0o700)
            repository = temporary
            manifest = _git_snapshot_materialization_gap(
                changeset,
                discovery,
                materialization_ordinal=materialization_ordinal,
                failure=gap.failure,
                object_format=object_format,
            )
        except PreflightError as error:
            shutil.rmtree(temporary, ignore_errors=True)
            temporary.mkdir(mode=0o700)
            repository = temporary
            manifest = _git_snapshot_materialization_gap(
                changeset,
                discovery,
                materialization_ordinal=materialization_ordinal,
                failure=FailureKind.CONFIGURATION,
                object_format=object_format,
            )

        after = capture_changeset(
            root,
            changeset.comparison.requested_base_ref,
            changeset.scope,
            limits=changeset.limits,
            explicit_includes=[item.to_bytes() for item in changeset.explicit_includes],
        )
        if (
            after.identity != changeset.identity
            or source_preservation_fingerprint(root) != source_fingerprint
        ):
            raise PreflightError("source repository changed while creating the snapshot")
        try:
            yield DisposableSnapshot(repository, manifest)
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
            if (
                final.identity != changeset.identity
                or source_preservation_fingerprint(root) != source_fingerprint
            ):
                raise PreflightError(
                    "verification did not preserve the source repository"
                )
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
