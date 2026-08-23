from __future__ import annotations

import base64
import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from pre_pr_verify.errors import InternalCaptureError, PreflightError
from pre_pr_verify.models import (
    ChangeOrigin,
    ChangeSet,
    Comparison,
    ContentBlob,
    ContentLimits,
    FileChange,
    FileKind,
    FileState,
    IdentityKind,
    OmissionReason,
    RawPath,
    RenameRelation,
    ScopeMode,
    build_changeset,
)


_LAYERS = ("base", "head", "index", "working")
_ORIGIN_TRANSITIONS = {
    ChangeOrigin.COMMITTED: ("base", "head"),
    ChangeOrigin.STAGED: ("head", "index"),
    ChangeOrigin.UNSTAGED: ("index", "working"),
    ChangeOrigin.UNTRACKED: ("index", "working"),
}


@dataclass(frozen=True)
class _TreeEntry:
    mode: str
    kind: FileKind
    oid: str


@dataclass(frozen=True)
class _Event:
    origin: ChangeOrigin
    status: str
    old_path: bytes
    new_path: bytes
    similarity: int | None = None


@dataclass(frozen=True)
class _RawState:
    path: bytes
    kind: FileKind
    mode: str | None
    identity_kind: IdentityKind
    content_identity: str
    data: bytes | None
    binary: bool | None

    @property
    def size(self) -> int:
        return len(self.data) if self.data is not None else 0


class GitRunner:
    """Run a fixed set of read-only Git commands without repository extensions."""

    def __init__(self, repository: Path):
        self.repository = repository

    def run(self, args: Sequence[str], *, check: bool = True) -> bytes:
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
        }
        command = [
            "git",
            "--no-pager",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "core.filemode=true",
            *args,
        ]
        result = subprocess.run(
            command,
            cwd=self.repository,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and result.returncode != 0:
            message = result.stderr.decode("utf-8", "backslashreplace").strip()
            raise PreflightError(message or f"Git command failed: {args[0]}")
        return result.stdout


class _DisjointSet:
    def __init__(self) -> None:
        self.parent: dict[tuple[str, bytes], tuple[str, bytes]] = {}

    def add(self, item: tuple[str, bytes]) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: tuple[str, bytes]) -> tuple[str, bytes]:
        self.add(item)
        parent = self.parent[item]
        if parent != item:
            self.parent[item] = self.find(parent)
        return self.parent[item]

    def union(self, left: tuple[str, bytes], right: tuple[str, bytes]) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            if left_root < right_root:
                self.parent[right_root] = left_root
            else:
                self.parent[left_root] = right_root


class _RootedPathMissing(PreflightError):
    """A rooted traversal found a missing intermediate component."""


class _RootedReader:
    """Read beneath one trusted root without following any path component."""

    def __init__(self, root: Path, label: str):
        self.root = root
        self.label = label

    @staticmethod
    def _components(path: bytes) -> list[bytes]:
        if not path or path.startswith(b"/") or b"\x00" in path:
            raise PreflightError("rooted path is not a valid relative path")
        components = path.split(b"/")
        if any(component in (b"", b".", b"..") for component in components):
            raise PreflightError("rooted path contains an unsafe component")
        return components

    def _open_parent(self, path: bytes) -> tuple[int, bytes]:
        components = self._components(path)
        flags = os.O_RDONLY | os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(os.fsencode(self.root), flags)
            for component in components[:-1]:
                try:
                    next_descriptor = os.open(component, flags, dir_fd=descriptor)
                finally:
                    os.close(descriptor)
                descriptor = next_descriptor
        except FileNotFoundError as error:
            raise _RootedPathMissing(
                f"{self.label} path is missing"
            ) from error
        except OSError as error:
            raise PreflightError(
                f"{self.label} path escapes or crosses an unsafe directory"
            ) from error
        return descriptor, components[-1]

    def stat(self, path: bytes) -> os.stat_result | None:
        descriptor, name = self._open_parent(path)
        try:
            try:
                return os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return None
            except OSError as error:
                raise PreflightError(
                    f"{self.label} path cannot be inspected safely"
                ) from error
        finally:
            os.close(descriptor)

    def readlink(self, path: bytes) -> bytes:
        descriptor, name = self._open_parent(path)
        try:
            try:
                target = os.readlink(name, dir_fd=descriptor)
            except OSError as error:
                raise PreflightError(
                    f"{self.label} symlink cannot be read safely"
                ) from error
        finally:
            os.close(descriptor)
        return target if isinstance(target, bytes) else os.fsencode(target)

    def read_file(self, path: bytes, *, limit: int | None = None) -> bytes | None:
        descriptor, name = self._open_parent(path)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            try:
                file_descriptor = os.open(name, flags, dir_fd=descriptor)
            except FileNotFoundError:
                return None
            except OSError as error:
                raise PreflightError(
                    f"{self.label} file cannot be opened safely"
                ) from error
        finally:
            os.close(descriptor)
        try:
            opened = os.fstat(file_descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise PreflightError(f"{self.label} file must be regular")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(file_descriptor, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if limit is not None and total > limit:
                    raise PreflightError(f"{self.label} file exceeds safe size")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(file_descriptor)

    def digest_file(self, path: bytes) -> str | None:
        descriptor, name = self._open_parent(path)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            try:
                file_descriptor = os.open(name, flags, dir_fd=descriptor)
            except FileNotFoundError:
                return None
            except OSError as error:
                raise PreflightError(
                    f"{self.label} file cannot be opened safely"
                ) from error
        finally:
            os.close(descriptor)
        try:
            opened = os.fstat(file_descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise PreflightError(f"{self.label} file must be regular")
            digest = hashlib.sha256()
            while True:
                chunk = os.read(file_descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            return digest.hexdigest()
        finally:
            os.close(file_descriptor)


def _validate_raw_path(path: bytes) -> None:
    if not path or path.startswith(b"/") or b"\x00" in path:
        raise PreflightError("repository path is not a valid relative Git path")
    components = path.split(b"/")
    if any(component in (b"", b".", b"..") for component in components):
        raise PreflightError("repository path contains an unsafe component")
    if any(component.lower() == b".git" for component in components):
        raise PreflightError(".git paths are protected")


def _resolve_repository(requested: Path) -> tuple[Path, GitRunner]:
    repository = requested.resolve()
    if not repository.is_dir():
        raise PreflightError("repository path is not a directory")
    runner = GitRunner(repository)
    try:
        inside = runner.run(["rev-parse", "--is-inside-work-tree"])
        root_raw = runner.run(["rev-parse", "--show-toplevel"])
    except PreflightError as error:
        raise PreflightError("not a Git working-tree repository") from error
    if inside.strip() != b"true":
        raise PreflightError("not a Git working-tree repository")
    root = Path(os.fsdecode(root_raw.rstrip(b"\n"))).resolve()
    return root, GitRunner(root)


def resolve_repository_and_git_directory(repository: Path | str) -> tuple[Path, Path]:
    root, runner = _resolve_repository(Path(repository))
    git_directory_raw = runner.run(["rev-parse", "--absolute-git-dir"]).rstrip(b"\n")
    return root, Path(os.fsdecode(git_directory_raw)).resolve()


def _resolve_commit(runner: GitRunner, revision: str) -> str:
    if not revision or "\x00" in revision:
        raise PreflightError("revision must be non-empty and contain no NUL")
    output = runner.run(
        ["rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"]
    )
    oid = output.strip().decode("ascii", "strict")
    if not oid:
        raise PreflightError(f"invalid commit revision: {revision}")
    return oid


def _tree_entries(runner: GitRunner, revision: str) -> dict[bytes, _TreeEntry]:
    output = runner.run(["ls-tree", "-r", "-z", "--full-tree", revision])
    entries: dict[bytes, _TreeEntry] = {}
    for record in output.split(b"\x00"):
        if not record:
            continue
        header, path = record.split(b"\t", 1)
        mode_raw, type_raw, oid_raw = header.split(b" ", 2)
        _validate_raw_path(path)
        mode = mode_raw.decode("ascii")
        kind = _kind_from_mode(mode)
        entries[path] = _TreeEntry(mode, kind, oid_raw.decode("ascii"))
    return entries


def _index_entries(runner: GitRunner) -> dict[bytes, _TreeEntry]:
    output = runner.run(["ls-files", "--stage", "-z"])
    entries: dict[bytes, _TreeEntry] = {}
    for record in output.split(b"\x00"):
        if not record:
            continue
        header, path = record.split(b"\t", 1)
        mode_raw, oid_raw, stage_raw = header.split(b" ", 2)
        if stage_raw != b"0":
            raise PreflightError("unmerged index entries are not a stable review scope")
        _validate_raw_path(path)
        mode = mode_raw.decode("ascii")
        entries[path] = _TreeEntry(
            mode,
            _kind_from_mode(mode),
            oid_raw.decode("ascii"),
        )
    return entries


def _kind_from_mode(mode: str) -> FileKind:
    if mode == "120000":
        return FileKind.SYMLINK
    if mode == "160000":
        return FileKind.GITLINK
    if mode in ("100644", "100755"):
        return FileKind.REGULAR
    raise PreflightError(f"unsupported Git file mode: {mode}")


def _parse_name_status(output: bytes, origin: ChangeOrigin) -> list[_Event]:
    tokens = output.split(b"\x00")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    events: list[_Event] = []
    index = 0
    while index < len(tokens):
        status_token = tokens[index]
        index += 1
        if b"\t" in status_token:
            status_token, first_path = status_token.split(b"\t", 1)
            tokens.insert(index, first_path)
        status = status_token.decode("ascii")
        code = status[:1]
        if code in ("R", "C"):
            if index + 1 >= len(tokens):
                raise PreflightError("malformed Git rename output")
            old_path, new_path = tokens[index], tokens[index + 1]
            index += 2
            if code == "C":
                # Copy inference is deliberately disabled; treat an unexpected copy as add.
                old_path = new_path
                status = "A"
            similarity = int(status[1:]) if code == "R" and status[1:] else 0
        else:
            if index >= len(tokens):
                raise PreflightError("malformed Git name-status output")
            old_path = new_path = tokens[index]
            index += 1
            similarity = None
        _validate_raw_path(old_path)
        _validate_raw_path(new_path)
        events.append(_Event(origin, status, old_path, new_path, similarity))
    return events


def _diff_events(
    runner: GitRunner,
    origin: ChangeOrigin,
    *comparison: str,
) -> list[_Event]:
    output = runner.run(
        [
            "diff",
            "--name-status",
            "-z",
            "-M50%",
            "-l1000",
            "--diff-algorithm=myers",
            "--no-ext-diff",
            *comparison,
        ]
    )
    return _parse_name_status(output, origin)


def _untracked_events(runner: GitRunner) -> list[_Event]:
    output = runner.run(["ls-files", "--others", "--exclude-standard", "-z"])
    events: list[_Event] = []
    for path in output.split(b"\x00"):
        if not path:
            continue
        _validate_raw_path(path)
        events.append(_Event(ChangeOrigin.UNTRACKED, "A", path, path))
    return events


def _explicit_include_events(
    reader: _RootedReader,
    includes: tuple[bytes, ...],
    index_entries: dict[bytes, _TreeEntry],
    existing_events: list[_Event],
) -> list[_Event]:
    existing_paths = {
        path for event in existing_events for path in (event.old_path, event.new_path)
    }
    events: list[_Event] = []
    for path in includes:
        _validate_raw_path(path)
        if path in existing_paths or path in index_entries:
            continue
        metadata = reader.stat(path)
        if metadata is None:
            raise PreflightError("explicitly included path does not exist")
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
            raise PreflightError("explicit include must name a file or symlink")
        events.append(_Event(ChangeOrigin.UNTRACKED, "A", path, path))
    return events


def _blob_bytes(
    runner: GitRunner,
    entry: _TreeEntry,
    cache: dict[str, bytes],
) -> bytes | None:
    if entry.kind is FileKind.GITLINK:
        return None
    if entry.oid not in cache:
        cache[entry.oid] = runner.run(["cat-file", "blob", entry.oid])
    return cache[entry.oid]


def _raw_tree_state(
    runner: GitRunner,
    path: bytes,
    entries: dict[bytes, _TreeEntry],
    blob_cache: dict[str, bytes],
) -> _RawState:
    entry = entries.get(path)
    if entry is None:
        return _raw_absent(path)
    data = _blob_bytes(runner, entry, blob_cache)
    if entry.kind is FileKind.GITLINK:
        return _RawState(
            path,
            entry.kind,
            entry.mode,
            IdentityKind.GIT_OID,
            entry.oid,
            None,
            None,
        )
    assert data is not None
    return _RawState(
        path,
        entry.kind,
        entry.mode,
        IdentityKind.SHA256,
        hashlib.sha256(data).hexdigest(),
        data,
        _is_binary(data) if entry.kind is FileKind.REGULAR else False,
    )


def _raw_absent(path: bytes) -> _RawState:
    return _RawState(
        path,
        FileKind.ABSENT,
        None,
        IdentityKind.ABSENT,
        "absent",
        None,
        None,
    )


def _working_state(
    reader: _RootedReader,
    path: bytes,
    index_entry: _TreeEntry | None,
    git_reader: _RootedReader,
) -> _RawState:
    _validate_raw_path(path)
    metadata = reader.stat(path)
    if metadata is None:
        return _raw_absent(path)

    if stat.S_ISLNK(metadata.st_mode):
        data = reader.readlink(path)
        return _RawState(
            path,
            FileKind.SYMLINK,
            "120000",
            IdentityKind.SHA256,
            hashlib.sha256(data).hexdigest(),
            data,
            False,
        )

    if index_entry is not None and index_entry.kind is FileKind.GITLINK:
        if not stat.S_ISDIR(metadata.st_mode):
            raise PreflightError("gitlink working path is not a directory")
        working_oid = _submodule_head(reader, path, git_reader)
        return _RawState(
            path,
            FileKind.GITLINK,
            "160000",
            IdentityKind.GIT_OID,
            working_oid or index_entry.oid,
            None,
            None,
        )

    if not stat.S_ISREG(metadata.st_mode):
        raise PreflightError("unsupported working-tree file type")
    regular_data = reader.read_file(path)
    if regular_data is None:
        return _raw_absent(path)
    opened = reader.stat(path)
    if opened is None or not stat.S_ISREG(opened.st_mode):
        raise PreflightError("working-tree file changed type during capture")
    mode = "100755" if opened.st_mode & 0o111 else "100644"
    return _RawState(
        path,
        FileKind.REGULAR,
        mode,
        IdentityKind.SHA256,
        hashlib.sha256(regular_data).hexdigest(),
        regular_data,
        _is_binary(regular_data),
    )


def _required_metadata(
    reader: _RootedReader, path: bytes, limit: int
) -> bytes:
    data = reader.read_file(path, limit=limit)
    if data is None:
        raise PreflightError("required Git metadata is missing")
    return data


def _submodule_head(
    repository_reader: _RootedReader,
    worktree_path: bytes,
    git_reader: _RootedReader,
) -> str | None:
    marker = worktree_path + b"/.git"
    marker_metadata = repository_reader.stat(marker)
    if marker_metadata is None:
        return None
    if stat.S_ISLNK(marker_metadata.st_mode):
        raise PreflightError("submodule .git marker must not be a symlink")
    if stat.S_ISDIR(marker_metadata.st_mode):
        metadata_reader = repository_reader
        metadata_prefix = marker + b"/"
    elif stat.S_ISREG(marker_metadata.st_mode):
        marker_data = _required_metadata(repository_reader, marker, 4096)
        prefix = b"gitdir: "
        if not marker_data.startswith(prefix):
            raise PreflightError("submodule .git file is malformed")
        raw_target = marker_data[len(prefix) :].strip()
        if not raw_target or b"\x00" in raw_target:
            raise PreflightError("submodule gitdir target is invalid")
        if os.path.isabs(raw_target):
            target = os.path.normpath(raw_target)
        else:
            target = os.path.normpath(
                os.path.join(
                    os.fsencode(repository_reader.root),
                    worktree_path,
                    raw_target,
                )
            )
        git_root = os.fsencode(git_reader.root)
        try:
            common = os.path.commonpath((git_root, target))
        except ValueError as error:
            raise PreflightError("submodule gitdir target is invalid") from error
        if common != git_root or target == git_root:
            raise PreflightError("submodule gitdir escapes allowed Git metadata roots")
        metadata_reader = git_reader
        metadata_prefix = (
            os.path.relpath(target, git_root).replace(os.sep.encode(), b"/") + b"/"
        )
    else:
        raise PreflightError("submodule .git marker has an unsupported type")

    head = _required_metadata(metadata_reader, metadata_prefix + b"HEAD", 4096).strip()
    if head.startswith(b"ref: "):
        reference = head[5:]
        if (
            not reference.startswith(b"refs/")
            or b"\x00" in reference
            or any(part in (b"", b".", b"..") for part in reference.split(b"/"))
        ):
            raise PreflightError("submodule HEAD reference is unsafe")
        head_data = metadata_reader.read_file(
            metadata_prefix + reference, limit=4096
        )
        if head_data is not None:
            head = head_data.strip()
        else:
            packed = _required_metadata(
                metadata_reader, metadata_prefix + b"packed-refs", 10_485_760
            )
            matches = [
                line.split(b" ", 1)[0]
                for line in packed.splitlines()
                if not line.startswith((b"#", b"^"))
                and line.endswith(b" " + reference)
            ]
            if len(matches) != 1:
                raise PreflightError("submodule HEAD reference cannot be resolved")
            head = matches[0]
    try:
        oid = head.decode("ascii")
    except UnicodeDecodeError as error:
        raise PreflightError("submodule HEAD is not an object ID") from error
    if len(oid) not in (40, 64) or any(character not in "0123456789abcdef" for character in oid):
        raise PreflightError("submodule HEAD is not an object ID")
    return oid


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _metadata_fingerprint(runner: GitRunner, git_reader: _RootedReader) -> tuple[str, str]:
    head = _resolve_commit(runner, "HEAD")
    index_path_raw = runner.run(["rev-parse", "--git-path", "index"]).rstrip(b"\n")
    if not os.path.isabs(index_path_raw):
        index_path_raw = os.path.normpath(
            os.path.join(os.fsencode(runner.repository), index_path_raw)
        )
    git_root = os.fsencode(git_reader.root)
    if os.path.commonpath((git_root, index_path_raw)) != git_root:
        raise PreflightError("Git index escapes the Git metadata root")
    index_relative = os.path.relpath(index_path_raw, git_root).replace(
        os.sep.encode(), b"/"
    )
    try:
        index_digest = git_reader.digest_file(index_relative)
    except PreflightError as error:
        raise PreflightError("Git index cannot be read safely") from error
    if index_digest is None:
        index_digest = hashlib.sha256(b"").hexdigest()
    return head, index_digest


def _same_state(left: _RawState, right: _RawState) -> bool:
    return (
        left.kind,
        left.mode,
        left.identity_kind,
        left.content_identity,
    ) == (
        right.kind,
        right.mode,
        right.identity_kind,
        right.content_identity,
    )


def _working_tree_scan(
    repository_reader: _RootedReader,
    runner: GitRunner,
    index_entries: dict[bytes, _TreeEntry],
    blob_cache: dict[str, bytes],
    explicit_includes: tuple[bytes, ...],
    git_reader: _RootedReader,
) -> tuple[list[_Event], dict[bytes, _RawState], str, str]:
    states: dict[bytes, _RawState] = {}
    events: list[_Event] = []
    deleted: list[tuple[bytes, _RawState]] = []
    for path in sorted(index_entries):
        index_state = _raw_tree_state(runner, path, index_entries, blob_cache)
        working = _working_state(
            repository_reader, path, index_entries[path], git_reader
        )
        states[path] = working
        if not _same_state(index_state, working):
            status = "D" if working.kind is FileKind.ABSENT else "M"
            events.append(_Event(ChangeOrigin.UNSTAGED, status, path, path))
            if status == "D":
                deleted.append((path, index_state))

    untracked = _untracked_events(runner)
    explicit_events = _explicit_include_events(
        repository_reader,
        explicit_includes,
        index_entries,
        events + untracked,
    )
    untracked.extend(explicit_events)
    for event in untracked:
        states[event.new_path] = _working_state(
            repository_reader,
            event.new_path,
            None,
            git_reader,
        )

    # Working-tree rename detection is deliberately limited to an unambiguous
    # exact-content match. Git conversion filters are never invoked.
    deleted_by_identity: dict[tuple[FileKind, str], list[bytes]] = {}
    for path, state in deleted:
        deleted_by_identity.setdefault((state.kind, state.content_identity), []).append(path)
    added_by_identity: dict[tuple[FileKind, str], list[bytes]] = {}
    for event in untracked:
        state = states[event.new_path]
        added_by_identity.setdefault((state.kind, state.content_identity), []).append(
            event.new_path
        )

    renamed_old: set[bytes] = set()
    renamed_new: set[bytes] = set()
    rename_events: list[_Event] = []
    for identity in sorted(set(deleted_by_identity) & set(added_by_identity), key=str):
        old_paths = deleted_by_identity[identity]
        new_paths = added_by_identity[identity]
        if len(old_paths) == 1 and len(new_paths) == 1:
            old_path, new_path = old_paths[0], new_paths[0]
            renamed_old.add(old_path)
            renamed_new.add(new_path)
            rename_events.append(
                _Event(ChangeOrigin.UNSTAGED, "R100", old_path, new_path, 100)
            )

    events = [
        event
        for event in events
        if not (event.status == "D" and event.old_path in renamed_old)
    ]
    events.extend(
        event for event in untracked if event.new_path not in renamed_new
    )
    events.extend(rename_events)

    status_records = [
        b"\x00".join(
            (
                event.origin.value.encode("ascii"),
                event.status.encode("ascii"),
                event.old_path,
                event.new_path,
                str(event.similarity if event.similarity is not None else -1).encode(
                    "ascii"
                ),
            )
        )
        for event in sorted(
            events,
            key=lambda item: (
                item.origin.value,
                item.status,
                item.old_path,
                item.new_path,
                item.similarity or -1,
            ),
        )
    ]
    status_fingerprint = hashlib.sha256(b"\xff".join(status_records)).hexdigest()
    content_records = []
    for path, state in sorted(states.items()):
        content_records.append(
            b"\x00".join(
                (
                    path,
                    state.kind.value.encode("ascii"),
                    (state.mode or "-").encode("ascii"),
                    state.identity_kind.value.encode("ascii"),
                    state.content_identity.encode("ascii"),
                )
            )
        )
    content_fingerprint = hashlib.sha256(b"\xff".join(content_records)).hexdigest()
    return events, states, status_fingerprint, content_fingerprint


def _build_groups(events: list[_Event], scope: ScopeMode) -> list[tuple[dict[str, bytes], list[ChangeOrigin]]]:
    dsu = _DisjointSet()
    paths = {path for event in events for path in (event.old_path, event.new_path)}
    rename_sides: dict[tuple[str, str], tuple[set[bytes], set[bytes]]] = {}
    for origin, transition in _ORIGIN_TRANSITIONS.items():
        relevant = [event for event in events if event.origin is origin and event.status.startswith("R")]
        old_paths, new_paths = rename_sides.setdefault(transition, (set(), set()))
        old_paths.update(event.old_path for event in relevant)
        new_paths.update(event.new_path for event in relevant)

    active_layers = _LAYERS[:2] if scope is ScopeMode.COMMITTED_ONLY else _LAYERS
    for path in paths:
        for layer in active_layers:
            dsu.add((layer, path))
    for event in events:
        left_layer, right_layer = _ORIGIN_TRANSITIONS[event.origin]
        dsu.union((left_layer, event.old_path), (right_layer, event.new_path))
    for left_layer, right_layer in zip(active_layers, active_layers[1:]):
        renamed_old, renamed_new = rename_sides[(left_layer, right_layer)]
        for path in paths:
            if path in renamed_old or path in renamed_new:
                continue
            dsu.union((left_layer, path), (right_layer, path))

    origins_by_root: dict[tuple[str, bytes], set[ChangeOrigin]] = {}
    for event in events:
        left_layer, _ = _ORIGIN_TRANSITIONS[event.origin]
        root = dsu.find((left_layer, event.old_path))
        origins_by_root.setdefault(root, set()).add(event.origin)

    nodes_by_root: dict[tuple[str, bytes], list[tuple[str, bytes]]] = {}
    for node in dsu.parent:
        nodes_by_root.setdefault(dsu.find(node), []).append(node)

    groups = []
    for root, origins in origins_by_root.items():
        by_layer: dict[str, bytes] = {}
        for layer, path in nodes_by_root[root]:
            if layer in by_layer and by_layer[layer] != path:
                raise InternalCaptureError("logical change has multiple paths in one layer")
            by_layer[layer] = path
        ordered_origins = sorted(origins, key=list(ChangeOrigin).index)
        groups.append((by_layer, ordered_origins))
    groups.sort(key=lambda item: item[0][active_layers[-1]])
    return groups


def _allocate_content(
    raw_changes: list[tuple[dict[str, _RawState | None], list[ChangeOrigin]]],
    limits: ContentLimits,
) -> tuple[dict[str, ContentBlob | OmissionReason], list[ContentBlob]]:
    candidates: list[tuple[bytes, int, _RawState]] = []
    for states, _ in raw_changes:
        for layer_index, layer in enumerate((*_LAYERS, "effective")):
            state = states.get(layer)
            if (
                state is not None
                and state.data is not None
                and state.kind is not FileKind.ABSENT
            ):
                candidates.append((state.path, layer_index, state))
    decisions: dict[str, ContentBlob | OmissionReason] = {}
    used = 0
    for _, _, state in sorted(candidates, key=lambda item: (item[0], item[1])):
        digest = state.content_identity
        if digest in decisions:
            continue
        assert state.data is not None
        if len(state.data) > limits.per_file_bytes:
            decisions[digest] = OmissionReason.PER_FILE_LIMIT
        elif used + len(state.data) > limits.total_bytes:
            decisions[digest] = OmissionReason.TOTAL_LIMIT
        else:
            blob = ContentBlob(
                sha256=digest,
                size=len(state.data),
                data_b64=base64.b64encode(state.data).decode("ascii"),
            )
            decisions[digest] = blob
            used += len(state.data)
    blobs = [decision for decision in decisions.values() if isinstance(decision, ContentBlob)]
    return decisions, blobs


def _file_state(
    raw: _RawState,
    decisions: dict[str, ContentBlob | OmissionReason],
) -> FileState:
    path = RawPath.from_bytes(raw.path)
    if raw.kind is FileKind.ABSENT:
        return FileState.absent(path)
    if raw.kind is FileKind.GITLINK:
        return FileState(
            path=path,
            kind=raw.kind,
            mode=raw.mode,
            size=0,
            identity_kind=raw.identity_kind,
            content_identity=raw.content_identity,
            binary=None,
            content_captured=False,
        )
    decision = decisions[raw.content_identity]
    captured = isinstance(decision, ContentBlob)
    omission_reason = None if captured else decision
    assert omission_reason is None or isinstance(omission_reason, OmissionReason)
    return FileState(
        path=path,
        kind=raw.kind,
        mode=raw.mode,
        size=raw.size,
        identity_kind=raw.identity_kind,
        content_identity=raw.content_identity,
        binary=raw.binary,
        content_captured=captured,
        omission_reason=omission_reason,
    )


def _capture_once(
    root: Path,
    runner: GitRunner,
    base_ref: str,
    scope: ScopeMode,
    limits: ContentLimits,
    after_capture: Callable[[int], None] | None,
    attempt: int,
    explicit_includes: tuple[bytes, ...],
) -> ChangeSet:
    resolved_base = _resolve_commit(runner, base_ref)
    head = _resolve_commit(runner, "HEAD")
    try:
        merge_base_raw = runner.run(
            ["merge-base", "--all", resolved_base, head]
        ).strip()
    except PreflightError as error:
        raise PreflightError("explicit base and HEAD have no merge base") from error
    if not merge_base_raw:
        raise PreflightError("explicit base and HEAD have no merge base")
    merge_bases = merge_base_raw.decode("ascii").splitlines()
    if len(merge_bases) != 1:
        raise PreflightError("comparison scope has multiple merge bases")
    merge_base = merge_bases[0]
    git_directory_raw = runner.run(["rev-parse", "--absolute-git-dir"]).rstrip(b"\n")
    git_directory = Path(os.fsdecode(git_directory_raw)).resolve()
    repository_reader = _RootedReader(root, "repository")
    git_reader = _RootedReader(git_directory, "Git metadata")
    before_metadata = _metadata_fingerprint(runner, git_reader)
    if before_metadata[0] != head:
        raise _UnstableCapture("HEAD changed while capture was starting")

    base_entries = _tree_entries(runner, merge_base)
    head_entries = _tree_entries(runner, head)
    events = _diff_events(
        runner,
        ChangeOrigin.COMMITTED,
        merge_base,
        head,
    )
    index_entries: dict[bytes, _TreeEntry] = {}
    working_states: dict[bytes, _RawState] = {}
    working_status_fingerprint = hashlib.sha256(b"").hexdigest()
    working_content_fingerprint = hashlib.sha256(b"").hexdigest()
    blob_cache: dict[str, bytes] = {}
    if scope is ScopeMode.PENDING:
        index_entries = _index_entries(runner)
        events.extend(
            _diff_events(runner, ChangeOrigin.STAGED, "--cached", head)
        )
        (
            working_events,
            working_states,
            working_status_fingerprint,
            working_content_fingerprint,
        ) = _working_tree_scan(
            repository_reader,
            runner,
            index_entries,
            blob_cache,
            explicit_includes,
            git_reader,
        )
        events.extend(working_events)

    groups = _build_groups(events, scope)
    raw_changes: list[tuple[dict[str, _RawState | None], list[ChangeOrigin]]] = []
    for paths, origins in groups:
        base_path = paths["base"]
        head_path = paths["head"]
        states: dict[str, _RawState | None] = {
            "base": _raw_tree_state(runner, base_path, base_entries, blob_cache),
            "head": _raw_tree_state(runner, head_path, head_entries, blob_cache),
            "index": None,
            "working": None,
        }
        if scope is ScopeMode.PENDING:
            index_path = paths["index"]
            working_path = paths["working"]
            states["index"] = _raw_tree_state(
                runner, index_path, index_entries, blob_cache
            )
            working = working_states.get(working_path)
            if working is None:
                working = _working_state(
                    repository_reader,
                    working_path,
                    index_entries.get(working_path),
                    git_reader,
                )
            states["working"] = working
            states["effective"] = working
        else:
            states["effective"] = states["head"]
        raw_changes.append((states, origins))

    if after_capture is not None:
        after_capture(attempt)
    after_metadata = _metadata_fingerprint(runner, git_reader)
    after_working_status_fingerprint = working_status_fingerprint
    after_working_content_fingerprint = working_content_fingerprint
    if scope is ScopeMode.PENDING:
        (
            _,
            _,
            after_working_status_fingerprint,
            after_working_content_fingerprint,
        ) = _working_tree_scan(
            repository_reader,
            runner,
            index_entries,
            blob_cache,
            explicit_includes,
            git_reader,
        )
    final_metadata = _metadata_fingerprint(runner, git_reader)
    if (
        before_metadata != after_metadata
        or before_metadata != final_metadata
        or working_status_fingerprint != after_working_status_fingerprint
        or working_content_fingerprint != after_working_content_fingerprint
    ):
        raise _UnstableCapture("repository changed during capture")

    decisions, blobs = _allocate_content(raw_changes, limits)
    changes: list[FileChange] = []
    for states, origins in raw_changes:
        changes.append(
            FileChange(
                origins=origins,
                base=_file_state(states["base"], decisions),  # type: ignore[arg-type]
                head=_file_state(states["head"], decisions),  # type: ignore[arg-type]
                index=(
                    _file_state(states["index"], decisions)  # type: ignore[arg-type]
                    if states["index"] is not None
                    else None
                ),
                working=(
                    _file_state(states["working"], decisions)  # type: ignore[arg-type]
                    if states["working"] is not None
                    else None
                ),
                effective=_file_state(states["effective"], decisions),  # type: ignore[arg-type]
            )
        )

    renames = [
        RenameRelation(
            origin=event.origin,
            old_path=RawPath.from_bytes(event.old_path),
            new_path=RawPath.from_bytes(event.new_path),
            similarity=event.similarity or 0,
        )
        for event in events
        if event.status.startswith("R")
    ]
    return build_changeset(
        repository_root=str(root),
        scope=scope,
        comparison=Comparison(
            requested_base_ref=base_ref,
            resolved_base_commit=resolved_base,
            merge_base_commit=merge_base,
            head_commit=head,
        ),
        limits=limits,
        changes=changes,
        renames=renames,
        contents=blobs,
        explicit_includes=[RawPath.from_bytes(path) for path in explicit_includes],
    )


class _UnstableCapture(Exception):
    pass


def capture_changeset(
    repository: Path | str,
    base_ref: str,
    scope: ScopeMode = ScopeMode.PENDING,
    *,
    limits: ContentLimits | None = None,
    runner_factory: Callable[[Path], GitRunner] = GitRunner,
    after_capture: Callable[[int], None] | None = None,
    explicit_includes: Iterable[bytes | str] = (),
) -> ChangeSet:
    root, initial_runner = _resolve_repository(Path(repository))
    runner = initial_runner if runner_factory is GitRunner else runner_factory(root)
    selected_limits = limits or ContentLimits()
    normalized_includes = tuple(
        sorted(
            {
                value if isinstance(value, bytes) else os.fsencode(value)
                for value in explicit_includes
            }
        )
    )
    if scope is ScopeMode.COMMITTED_ONLY and normalized_includes:
        raise PreflightError("explicit includes are not part of committed-only scope")
    for include in normalized_includes:
        _validate_raw_path(include)
    for attempt in range(2):
        try:
            return _capture_once(
                root,
                runner,
                base_ref,
                scope,
                selected_limits,
                after_capture,
                attempt,
                normalized_includes,
            )
        except _UnstableCapture:
            if attempt == 1:
                raise PreflightError("repository remained unstable after one retry")
    raise InternalCaptureError("capture retry loop exited unexpectedly")
