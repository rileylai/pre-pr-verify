from __future__ import annotations

import base64
import errno
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from pre_pr_verify.errors import PreflightError
from pre_pr_verify.git_capture import capture_changeset, source_preservation_fingerprint
from pre_pr_verify.models import ChangeOrigin, ContentLimits, FileKind, ScopeMode


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def write(repo: Path, relative: str, content: bytes) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "PrePR Verify Test")
    git(repo, "config", "user.email", "test@example.invalid")
    write(repo, "shared.txt", b"base\n")
    write(repo, "committed.txt", b"base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    git(repo, "switch", "-c", "feature")
    return repo


def content_for(changeset, state) -> bytes:
    blob = next(
        item for item in changeset.contents if item.sha256 == state.content_identity
    )
    return base64.b64decode(blob.data_b64)


def change_for(changeset, display_path: str):
    return next(
        change
        for change in changeset.changes
        if change.effective.path.display == display_path
    )


def test_pending_capture_preserves_all_origins_and_layers(repository: Path) -> None:
    write(repository, "committed.txt", b"committed\n")
    git(repository, "add", "committed.txt")
    git(repository, "commit", "-m", "committed change")

    write(repository, "staged.txt", b"staged\n")
    git(repository, "add", "staged.txt")
    write(repository, "unstaged.txt", b"unstaged\n")
    write(repository, "untracked.txt", b"untracked\n")

    changeset = capture_changeset(repository, "main", ScopeMode.PENDING)

    assert changeset.empty is False
    assert changeset.comparison.requested_base_ref == "main"
    assert changeset.comparison.merge_base_commit == git(
        repository, "rev-parse", "main"
    )
    assert change_for(changeset, "committed.txt").origins == [
        ChangeOrigin.COMMITTED
    ]
    assert change_for(changeset, "staged.txt").origins == [ChangeOrigin.STAGED]
    assert change_for(changeset, "unstaged.txt").origins == [
        ChangeOrigin.UNTRACKED
    ]
    assert change_for(changeset, "untracked.txt").origins == [
        ChangeOrigin.UNTRACKED
    ]

    committed = change_for(changeset, "committed.txt")
    assert content_for(changeset, committed.base) == b"base\n"
    assert content_for(changeset, committed.head) == b"committed\n"
    assert content_for(changeset, committed.effective) == b"committed\n"

    staged = change_for(changeset, "staged.txt")
    assert staged.head.kind is FileKind.ABSENT
    assert staged.index is not None
    assert content_for(changeset, staged.index) == b"staged\n"


def test_same_path_can_have_staged_and_unstaged_origins(repository: Path) -> None:
    write(repository, "shared.txt", b"staged\n")
    git(repository, "add", "shared.txt")
    write(repository, "shared.txt", b"working\n")

    changeset = capture_changeset(repository, "main", ScopeMode.PENDING)
    change = change_for(changeset, "shared.txt")

    assert change.origins == [ChangeOrigin.STAGED, ChangeOrigin.UNSTAGED]
    assert content_for(changeset, change.head) == b"base\n"
    assert change.index is not None
    assert content_for(changeset, change.index) == b"staged\n"
    assert change.working is not None
    assert content_for(changeset, change.working) == b"working\n"
    assert content_for(changeset, change.effective) == b"working\n"


def test_tracked_unstaged_only_is_captured(repository: Path) -> None:
    write(repository, "shared.txt", b"working only\n")

    changeset = capture_changeset(repository, "main", ScopeMode.PENDING)
    change = change_for(changeset, "shared.txt")

    assert change.origins == [ChangeOrigin.UNSTAGED]
    assert change.index is not None
    assert content_for(changeset, change.index) == b"base\n"
    assert content_for(changeset, change.effective) == b"working only\n"


def test_committed_only_excludes_pending_layers(repository: Path) -> None:
    write(repository, "committed.txt", b"committed\n")
    git(repository, "add", "committed.txt")
    git(repository, "commit", "-m", "committed change")
    write(repository, "shared.txt", b"staged\n")
    git(repository, "add", "shared.txt")
    write(repository, "untracked.txt", b"untracked\n")

    first = capture_changeset(repository, "main", ScopeMode.COMMITTED_ONLY)
    write(repository, "untracked.txt", b"different pending bytes\n")
    second = capture_changeset(repository, "main", ScopeMode.COMMITTED_ONLY)

    assert [change.effective.path.display for change in first.changes] == [
        "committed.txt"
    ]
    assert first.changes[0].index is None
    assert first.changes[0].working is None
    assert first.identity == second.identity


def test_empty_capture_is_valid(repository: Path) -> None:
    changeset = capture_changeset(repository, "main", ScopeMode.PENDING)

    assert changeset.empty is True
    assert changeset.changes == []
    assert len(changeset.identity) == 64


@pytest.mark.parametrize("base", ["missing", "HEAD^{tree}"])
def test_invalid_comparison_is_preflight_failure(repository: Path, base: str) -> None:
    with pytest.raises(PreflightError):
        capture_changeset(repository, base, ScopeMode.PENDING)


def test_non_repository_is_preflight_failure(tmp_path: Path) -> None:
    with pytest.raises(PreflightError):
        capture_changeset(tmp_path, "main", ScopeMode.PENDING)


def test_diverged_base_uses_merge_base(repository: Path) -> None:
    original_base = git(repository, "rev-parse", "main")
    write(repository, "feature.txt", b"feature\n")
    git(repository, "add", "feature.txt")
    git(repository, "commit", "-m", "feature")
    git(repository, "switch", "main")
    write(repository, "main-only.txt", b"main\n")
    git(repository, "add", "main-only.txt")
    git(repository, "commit", "-m", "main moved")
    git(repository, "switch", "feature")

    changeset = capture_changeset(repository, "main", ScopeMode.COMMITTED_ONLY)

    assert changeset.comparison.resolved_base_commit == git(
        repository, "rev-parse", "main"
    )
    assert changeset.comparison.merge_base_commit == original_base
    assert [change.effective.path.display for change in changeset.changes] == [
        "feature.txt"
    ]


def test_delete_rename_and_executable_mode_are_preserved(repository: Path) -> None:
    write(repository, "delete.txt", b"delete me\n")
    write(repository, "rename-old.txt", b"rename me with enough stable content\n")
    write(repository, "script.sh", b"#!/bin/sh\nexit 0\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "fixture files")
    git(repository, "rm", "delete.txt")
    git(repository, "mv", "rename-old.txt", "rename-new.txt")
    os.chmod(repository / "script.sh", 0o755)
    git(repository, "add", "script.sh")

    changeset = capture_changeset(repository, "main", ScopeMode.PENDING)

    deleted = change_for(changeset, "delete.txt")
    assert deleted.effective.kind is FileKind.ABSENT
    renamed = change_for(changeset, "rename-new.txt")
    assert renamed.base.path.display == "rename-old.txt"
    assert renamed.effective.path.display == "rename-new.txt"
    assert [(item.old_path.display, item.new_path.display) for item in changeset.renames] == [
        ("rename-old.txt", "rename-new.txt")
    ]
    executable = change_for(changeset, "script.sh")
    assert executable.head.mode == "100644"
    assert executable.effective.mode == "100755"


def test_unstaged_exact_rename_is_detected_without_git_filters(repository: Path) -> None:
    os.rename(repository / "shared.txt", repository / "renamed-shared.txt")

    changeset = capture_changeset(repository, "main")
    renamed = change_for(changeset, "renamed-shared.txt")

    assert renamed.origins == [ChangeOrigin.UNSTAGED]
    assert renamed.base.path.display == "shared.txt"
    assert renamed.effective.path.display == "renamed-shared.txt"
    assert [(item.origin, item.old_path.display, item.new_path.display) for item in changeset.renames] == [
        (ChangeOrigin.UNSTAGED, "shared.txt", "renamed-shared.txt")
    ]


def test_working_copy_is_not_inferred_as_rename(repository: Path) -> None:
    write(repository, "copied.txt", (repository / "shared.txt").read_bytes())

    changeset = capture_changeset(repository, "main")

    copied = change_for(changeset, "copied.txt")
    assert copied.origins == [ChangeOrigin.UNTRACKED]
    assert changeset.renames == []


def test_rename_chain_across_committed_and_staged_layers(repository: Path) -> None:
    git(repository, "mv", "shared.txt", "committed-name.txt")
    git(repository, "commit", "-m", "committed rename")
    git(repository, "mv", "committed-name.txt", "staged-name.txt")

    changeset = capture_changeset(repository, "main")
    renamed = change_for(changeset, "staged-name.txt")

    assert renamed.origins == [ChangeOrigin.COMMITTED, ChangeOrigin.STAGED]
    assert renamed.base.path.display == "shared.txt"
    assert renamed.head.path.display == "committed-name.txt"
    assert renamed.index is not None
    assert renamed.index.path.display == "staged-name.txt"
    assert renamed.effective.path.display == "staged-name.txt"


def test_symlink_is_hashed_without_following_target(repository: Path, tmp_path: Path) -> None:
    secret = tmp_path / "outside-secret.txt"
    secret.write_bytes(b"must not be captured")
    os.symlink(os.fsencode(secret), os.fsencode(repository / "outside-link"))

    changeset = capture_changeset(repository, "main", ScopeMode.PENDING)
    state = change_for(changeset, "outside-link").effective

    assert state.kind is FileKind.SYMLINK
    assert state.content_identity == hashlib.sha256(os.fsencode(secret)).hexdigest()
    assert content_for(changeset, state) == os.fsencode(secret)
    assert all(
        base64.b64decode(blob.data_b64) != b"must not be captured"
        for blob in changeset.contents
    )


def test_gitlink_records_commit_without_traversal(repository: Path) -> None:
    target_oid = git(repository, "rev-parse", "main")
    git(
        repository,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{target_oid},vendor/dependency",
    )
    git(repository, "commit", "-m", "add gitlink")

    changeset = capture_changeset(repository, "main", ScopeMode.COMMITTED_ONLY)
    state = change_for(changeset, "vendor/dependency").effective

    assert state.kind is FileKind.GITLINK
    assert state.content_identity == target_oid
    assert state.content_captured is False


def test_unstaged_submodule_records_working_head_without_content_traversal(
    tmp_path: Path,
) -> None:
    dependency = tmp_path / "dependency"
    dependency.mkdir()
    git(dependency, "init", "-b", "main")
    git(dependency, "config", "user.name", "PrePR Verify Test")
    git(dependency, "config", "user.email", "test@example.invalid")
    write(dependency, "version.txt", b"one\n")
    git(dependency, "add", ".")
    git(dependency, "commit", "-m", "version one")

    repo = tmp_path / "superproject"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "PrePR Verify Test")
    git(repo, "config", "user.email", "test@example.invalid")
    subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(dependency),
            "deps/example",
        ],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    git(repo, "commit", "-m", "add submodule")
    git(repo, "switch", "-c", "feature")

    write(dependency, "version.txt", b"two\n")
    git(dependency, "add", ".")
    git(dependency, "commit", "-m", "version two")
    second_oid = git(dependency, "rev-parse", "HEAD")
    git(repo / "deps/example", "fetch", "origin")
    git(repo / "deps/example", "checkout", second_oid)

    changeset = capture_changeset(repo, "main")
    state = change_for(changeset, "deps/example").effective

    assert state.kind is FileKind.GITLINK
    assert state.content_identity == second_oid
    assert change_for(changeset, "deps/example").origins == [
        ChangeOrigin.UNSTAGED
    ]
    assert all(b"version.txt" not in change.effective.path.to_bytes() for change in changeset.changes)


def test_submodule_gitdir_cannot_escape_allowed_metadata_roots(
    repository: Path, tmp_path: Path
) -> None:
    target_oid = git(repository, "rev-parse", "main")
    git(
        repository,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{target_oid},vendor/dependency",
    )
    (repository / "vendor/dependency").mkdir(parents=True)
    outside = tmp_path / "outside-gitdir"
    outside.mkdir()
    (outside / "HEAD").write_text(target_oid + "\n")
    (repository / "vendor/dependency/.git").write_text(f"gitdir: {outside}\n")

    with pytest.raises(PreflightError, match="escapes"):
        capture_changeset(repository, "main")


def test_submodule_gitdir_intermediate_symlink_is_not_followed(
    repository: Path, tmp_path: Path
) -> None:
    target_oid = git(repository, "rev-parse", "main")
    git(
        repository,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{target_oid},vendor/dependency",
    )
    (repository / "vendor/dependency").mkdir(parents=True)
    outside = tmp_path / "outside-module"
    outside.mkdir()
    (outside / "HEAD").write_text(target_oid + "\n")
    modules = repository / ".git/modules"
    modules.mkdir()
    (modules / "escape").symlink_to(outside, target_is_directory=True)
    (repository / "vendor/dependency/.git").write_text(
        "gitdir: ../../.git/modules/escape\n"
    )

    with pytest.raises(PreflightError, match="Git metadata path"):
        capture_changeset(repository, "main")


def test_binary_and_content_limits_are_deterministic(repository: Path) -> None:
    write(repository, "a.bin", b"a\x00aa")
    write(repository, "b.txt", b"bbbb")
    write(repository, "c.txt", b"ccccc")
    limits = ContentLimits(per_file_bytes=4, total_bytes=6)

    first = capture_changeset(repository, "main", limits=limits)
    second = capture_changeset(repository, "main", limits=limits)

    a_state = change_for(first, "a.bin").effective
    b_state = change_for(first, "b.txt").effective
    c_state = change_for(first, "c.txt").effective
    assert a_state.binary is True and a_state.content_captured is True
    assert b_state.content_captured is False
    assert b_state.omission_reason.value == "total_limit"
    assert c_state.content_captured is False
    assert c_state.omission_reason.value == "per_file_limit"
    assert first.identity == second.identity


def test_ignored_files_are_excluded(repository: Path) -> None:
    write(repository, ".gitignore", b"ignored.txt\n")
    git(repository, "add", ".gitignore")
    git(repository, "commit", "-m", "ignore fixture")
    write(repository, "ignored.txt", b"ignored\n")
    write(repository, "visible.txt", b"visible\n")

    changeset = capture_changeset(repository, "main")
    paths = [change.effective.path.display for change in changeset.changes]

    assert "ignored.txt" not in paths
    assert "visible.txt" in paths


def test_explicit_include_can_capture_ignored_file_but_stays_repo_bounded(
    repository: Path,
) -> None:
    write(repository, ".gitignore", b"ignored.txt\n")
    git(repository, "add", ".gitignore")
    git(repository, "commit", "-m", "ignore fixture")
    write(repository, "ignored.txt", b"explicit\n")

    changeset = capture_changeset(
        repository,
        "main",
        explicit_includes=[b"ignored.txt", b"ignored.txt"],
    )

    included = change_for(changeset, "ignored.txt")
    assert included.origins == [ChangeOrigin.UNTRACKED]
    assert content_for(changeset, included.effective) == b"explicit\n"
    assert [item.display for item in changeset.explicit_includes] == ["ignored.txt"]

    for unsafe in (b"../outside", b".git/config", b"nested/../../outside"):
        with pytest.raises(PreflightError):
            capture_changeset(repository, "main", explicit_includes=[unsafe])


def test_intermediate_symlink_escape_is_rejected(
    repository: Path, tmp_path: Path
) -> None:
    write(repository, "nested/secret.txt", b"inside\n")
    git(repository, "add", "nested/secret.txt")
    git(repository, "commit", "-m", "tracked nested file")

    outside = tmp_path / "outside"
    outside.mkdir()
    write(outside, "secret.txt", b"outside secret\n")
    (repository / "nested" / "secret.txt").unlink()
    (repository / "nested").rmdir()
    (repository / "nested").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PreflightError, match="repository path"):
        capture_changeset(repository, "main")


def test_explicit_include_intermediate_symlink_escape_is_rejected(
    repository: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside-explicit"
    outside.mkdir()
    write(outside, "secret.txt", b"outside explicit secret\n")
    (repository / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PreflightError, match="repository path"):
        capture_changeset(
            repository,
            "main",
            explicit_includes=[b"link/secret.txt"],
        )


def test_index_symlink_is_not_followed(repository: Path, tmp_path: Path) -> None:
    index = repository / ".git" / "index"
    secret = tmp_path / "secret-index"
    secret.write_bytes(index.read_bytes())
    index.unlink()
    index.symlink_to(secret)

    with pytest.raises(PreflightError, match="index"):
        capture_changeset(repository, "main")


def test_hostile_non_utf8_filename_is_reversible(repository: Path) -> None:
    raw_path = b"line\n\xff\tname.txt"
    try:
        descriptor = os.open(
            os.path.join(os.fsencode(repository), raw_path),
            os.O_WRONLY | os.O_CREAT,
            0o644,
        )
    except OSError as error:
        if error.errno not in (errno.EILSEQ, errno.EPERM):
            raise
        pytest.skip(f"managed macOS filesystem rejects non-UTF-8 names: {error}")
    try:
        os.write(descriptor, b"hostile path\n")
    finally:
        os.close(descriptor)

    changeset = capture_changeset(repository, "main")
    change = next(
        item for item in changeset.changes if item.effective.path.to_bytes() == raw_path
    )

    assert change.effective.path.utf8 is None
    assert "\\n" in change.effective.path.display
    assert "\\xff" in change.effective.path.display


@pytest.mark.parametrize("raw_path", [b"tab\tname.txt", b"line\nname.txt"])
def test_real_git_hostile_utf8_filename_is_reversible(
    repository: Path, raw_path: bytes
) -> None:
    descriptor = os.open(
        os.path.join(os.fsencode(repository), raw_path),
        os.O_WRONLY | os.O_CREAT,
        0o644,
    )
    try:
        os.write(descriptor, b"hostile utf8 path\n")
    finally:
        os.close(descriptor)

    changeset = capture_changeset(repository, "main")
    change = next(
        item for item in changeset.changes if item.effective.path.to_bytes() == raw_path
    )

    assert change.effective.path.utf8 == raw_path.decode("utf-8")
    assert (
        "\\t" in change.effective.path.display
        or "\\n" in change.effective.path.display
    )


def test_git_external_diff_textconv_and_fsmonitor_do_not_execute(
    repository: Path,
) -> None:
    sentinel = repository / "sentinel"
    extension = repository / "extension.sh"
    extension.write_text(f"#!/bin/sh\ntouch '{sentinel}'\nexit 0\n")
    extension.chmod(0o755)
    write(
        repository,
        ".gitattributes",
        b"*.special diff=evil\n*.filtered filter=evil\n",
    )
    write(repository, "file.special", b"base\n")
    write(repository, "file.filtered", b"base\n")
    git(
        repository,
        "add",
        ".gitattributes",
        "file.special",
        "file.filtered",
        "extension.sh",
    )
    git(repository, "commit", "-m", "dangerous extensions")
    git(repository, "config", "diff.external", str(extension))
    git(repository, "config", "diff.evil.textconv", str(extension))
    git(repository, "config", "core.fsmonitor", str(extension))
    git(repository, "config", "filter.evil.clean", str(extension))
    git(repository, "config", "filter.evil.required", "true")
    write(repository, "file.special", b"changed\n")
    write(repository, "file.filtered", b"changed\n")

    changeset = capture_changeset(repository, "main")

    assert changeset.empty is False
    assert not sentinel.exists()


def test_capture_retries_once_after_race(repository: Path) -> None:
    write(repository, "shared.txt", b"first\n")

    def mutate(attempt: int) -> None:
        if attempt == 0:
            write(repository, "shared.txt", b"second\n")

    changeset = capture_changeset(repository, "main", after_capture=mutate)

    assert content_for(changeset, change_for(changeset, "shared.txt").effective) == b"second\n"


def test_capture_retries_when_index_changes(repository: Path) -> None:
    write(repository, "shared.txt", b"staged after first scan\n")

    def mutate(attempt: int) -> None:
        if attempt == 0:
            git(repository, "add", "shared.txt")

    changeset = capture_changeset(repository, "main", after_capture=mutate)

    assert change_for(changeset, "shared.txt").origins == [ChangeOrigin.STAGED]


def test_capture_retries_when_head_changes(repository: Path) -> None:
    write(repository, "committed-race.txt", b"commit after first scan\n")
    git(repository, "add", "committed-race.txt")

    def mutate(attempt: int) -> None:
        if attempt == 0:
            git(repository, "commit", "-m", "race commit")

    changeset = capture_changeset(repository, "main", after_capture=mutate)

    assert change_for(changeset, "committed-race.txt").origins == [
        ChangeOrigin.COMMITTED
    ]


def test_capture_retries_when_porcelain_state_changes(repository: Path) -> None:
    def mutate(attempt: int) -> None:
        if attempt == 0:
            write(repository, "appeared.txt", b"appeared after first scan\n")

    changeset = capture_changeset(repository, "main", after_capture=mutate)

    assert change_for(changeset, "appeared.txt").origins == [ChangeOrigin.UNTRACKED]


def test_capture_fails_when_repository_remains_unstable(repository: Path) -> None:
    write(repository, "shared.txt", b"first\n")

    def mutate(attempt: int) -> None:
        write(repository, "shared.txt", f"changed-{attempt}\n".encode())

    with pytest.raises(PreflightError, match="unstable"):
        capture_changeset(repository, "main", after_capture=mutate)


def test_capture_preserves_head_index_and_working_state(repository: Path) -> None:
    write(repository, "shared.txt", b"staged\n")
    git(repository, "add", "shared.txt")
    write(repository, "shared.txt", b"working\n")
    write(repository, "untracked.txt", b"untracked\n")
    before_head = git(repository, "rev-parse", "HEAD")
    before_status = subprocess.run(
        ["git", "status", "--porcelain=v2", "-z", "--untracked-files=all"],
        cwd=repository,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    before_index = (repository / ".git" / "index").read_bytes()

    capture_changeset(repository, "main")

    assert git(repository, "rev-parse", "HEAD") == before_head
    assert (repository / ".git" / "index").read_bytes() == before_index
    assert (
        subprocess.run(
            ["git", "status", "--porcelain=v2", "-z", "--untracked-files=all"],
            cwd=repository,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
        == before_status
    )


def test_linked_worktree_config_is_bound_to_source_preservation(
    tmp_path: Path,
) -> None:
    main = tmp_path / "main"
    linked = tmp_path / "linked"
    main.mkdir()
    git(main, "init", "-b", "main")
    git(main, "config", "user.name", "PrePR Verify Test")
    git(main, "config", "user.email", "test@example.invalid")
    write(main, "tracked.txt", b"base\n")
    git(main, "add", ".")
    git(main, "commit", "-m", "base")
    git(main, "worktree", "add", "-b", "linked", str(linked), "HEAD")
    git(linked, "config", "extensions.worktreeConfig", "true")
    git(linked, "config", "--worktree", "prepr-verify.baseline", "yes")

    before = source_preservation_fingerprint(linked)
    worktree_config = Path(git(linked, "rev-parse", "--git-path", "config.worktree"))
    worktree_config.write_bytes(
        worktree_config.read_bytes() + b"[prepr-verify-change]\n\tvalue = detected\n"
    )

    assert source_preservation_fingerprint(linked) != before


def test_unrelated_histories_have_no_merge_base(tmp_path: Path) -> None:
    repo = tmp_path / "unrelated"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "PrePR Verify Test")
    git(repo, "config", "user.email", "test@example.invalid")
    write(repo, "main.txt", b"main\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "main")
    git(repo, "switch", "--orphan", "feature")
    if (repo / "main.txt").exists():
        (repo / "main.txt").unlink()
    write(repo, "feature.txt", b"feature\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "feature")

    with pytest.raises(PreflightError, match="merge base"):
        capture_changeset(repo, "main")
