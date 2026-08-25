from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import pre_pr_verify.snapshot as snapshot_module
from pre_pr_verify.discovery import discover_review_sources
from pre_pr_verify.git_capture import GitRunner, capture_changeset
from pre_pr_verify.models import ContentLimits, ScopeMode
from pre_pr_verify.snapshot import disposable_git_snapshot
from pre_pr_verify.verification_models import (
    EnvironmentProfile,
    FailureKind,
    GitObjectFormat,
)


def git(
    repository: Path,
    *args: str,
    input_data: bytes | None = None,
    env: dict[str, str] | None = None,
) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        env=env,
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        text=input_data is None,
    )
    if input_data is not None:
        return result.stdout.decode("utf-8")
    return result.stdout.strip()


def make_repository(tmp_path: Path, *, object_format: str = "sha1") -> Path:
    repository = tmp_path / "source"
    repository.mkdir()
    args = ["init", "--quiet", "-b", "main"]
    if object_format == "sha256":
        args = ["init", "--quiet", "-b", "main", "--object-format=sha256"]
    git(repository, *args)
    git(repository, "config", "user.name", "Fixture Author")
    git(repository, "config", "user.email", "fixture@example.invalid")
    (repository / "README.md").write_text("fixture\n")
    (repository / "tracked.txt").write_text("base\n")
    (repository / "delete.txt").write_text("delete\n")
    (repository / "mode.sh").write_text("#!/bin/sh\nexit 0\n")
    os.chmod(repository / "mode.sh", 0o755)
    (repository / "target.txt").write_text("target\n")
    (repository / "link").symlink_to("target.txt")
    git(repository, "add", ".")
    git(repository, "commit", "--quiet", "-m", "base")
    return repository


def capture(repository: Path, *, limits: ContentLimits | None = None):
    return capture_changeset(
        repository,
        "HEAD",
        ScopeMode.PENDING,
        limits=limits,
    )


def capture_committed(repository: Path):
    return capture_changeset(repository, "HEAD", ScopeMode.COMMITTED_ONLY)


def materialize(repository: Path, changeset):
    discovery = discover_review_sources(repository)
    return disposable_git_snapshot(changeset, discovery)


def snapshot_git(repository: Path, *args: str) -> str:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(repository / "private-home"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    return git(repository, *args, env=environment)


def semantic_state(repository: Path) -> dict[str, str]:
    return {
        "head": snapshot_git(repository, "rev-parse", "HEAD"),
        "toplevel": snapshot_git(repository, "rev-parse", "--show-toplevel"),
        "files": snapshot_git(repository, "ls-files", "-z").replace("\x00", "\n"),
        "status": snapshot_git(repository, "status", "--porcelain"),
        "diff": snapshot_git(repository, "diff"),
        "cached": snapshot_git(repository, "diff", "--cached"),
    }


def test_clean_head_reconstructs_standalone_repository(tmp_path: Path) -> None:
    source = make_repository(tmp_path)
    changeset = capture(source)
    head = git(source, "rev-parse", "HEAD")

    with materialize(source, changeset) as snapshot:
        assert snapshot.manifest.complete
        assert snapshot.manifest.environment_profile is EnvironmentProfile.GIT_REPOSITORY
        assert snapshot.manifest.object_format is GitObjectFormat.SHA1
        assert snapshot_git(snapshot.path, "rev-parse", "HEAD") == head
        assert snapshot_git(snapshot.path, "status", "--porcelain") == ""
        assert snapshot_git(snapshot.path, "diff") == ""
        assert snapshot_git(snapshot.path, "diff", "--cached") == ""
        assert snapshot_git(snapshot.path, "rev-parse", "--show-toplevel") == str(
            snapshot.path.resolve()
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "staged_only",
        "unstaged_only",
        "staged_and_unstaged",
        "tracked_addition",
        "tracked_deletion",
        "rename",
        "mode",
        "symlink",
        "untracked",
    ],
)
def test_reconstructs_pending_index_and_worktree_semantics(
    tmp_path: Path, mutation: str
) -> None:
    source = make_repository(tmp_path)
    if mutation == "staged_only":
        (source / "tracked.txt").write_text("staged\n")
        git(source, "add", "tracked.txt")
    elif mutation == "unstaged_only":
        (source / "tracked.txt").write_text("working\n")
    elif mutation == "staged_and_unstaged":
        (source / "tracked.txt").write_text("staged\n")
        git(source, "add", "tracked.txt")
        (source / "tracked.txt").write_text("working\n")
    elif mutation == "tracked_addition":
        (source / "added.txt").write_text("added\n")
        git(source, "add", "added.txt")
    elif mutation == "tracked_deletion":
        (source / "delete.txt").unlink()
        git(source, "add", "delete.txt")
    elif mutation == "rename":
        git(source, "mv", "tracked.txt", "renamed.txt")
    elif mutation == "mode":
        os.chmod(source / "mode.sh", 0o644)
    elif mutation == "symlink":
        (source / "link").unlink()
        (source / "link").symlink_to("README.md")
    elif mutation == "untracked":
        (source / "untracked.txt").write_text("untracked\n")
    else:
        raise AssertionError(mutation)

    changeset = capture(source)
    source_semantics = semantic_state(source)
    with materialize(source, changeset) as snapshot:
        assert snapshot.manifest.complete
        assert semantic_state(snapshot.path) == {
            **source_semantics,
            "toplevel": str(snapshot.path.resolve()),
        }


def test_sha256_object_format_is_preserved(tmp_path: Path) -> None:
    source = make_repository(tmp_path, object_format="sha256")
    changeset = capture(source)

    with materialize(source, changeset) as snapshot:
        assert snapshot.manifest.object_format is GitObjectFormat.SHA256
        assert len(snapshot_git(snapshot.path, "rev-parse", "HEAD")) == 64
        assert snapshot_git(snapshot.path, "rev-parse", "--show-object-format") == "sha256"


def test_source_preservation_and_git_authority_isolation(tmp_path: Path) -> None:
    source = make_repository(tmp_path)
    source_git = Path(git(source, "rev-parse", "--absolute-git-dir"))
    source_config = source_git / "config"
    source_index = source_git / "index"
    before = (
        git(source, "rev-parse", "HEAD"),
        source_config.read_bytes(),
        source_index.read_bytes(),
    )
    changeset = capture(source)

    with materialize(source, changeset) as snapshot:
        destination_git = snapshot.path / ".git"
        assert not (destination_git / "objects" / "info" / "alternates").exists()
        assert git(snapshot.path, "rev-parse", "--git-common-dir") == ".git"
        assert not any(
            line.startswith("remote.")
            for line in git(snapshot.path, "config", "--local", "--list").splitlines()
        )
        assert not any((destination_git / "hooks").iterdir())
        source_objects = {
            item.name
            for directory in (source_git / "objects").iterdir()
            if directory.is_dir() and len(directory.name) == 2
            for item in directory.iterdir()
        }
        destination_objects = {
            item.name
            for directory in (destination_git / "objects").iterdir()
            if directory.is_dir() and len(directory.name) == 2
            for item in directory.iterdir()
        }
        assert source_objects & destination_objects
        for name in source_objects & destination_objects:
            source_path = next(
                directory / name
                for directory in (source_git / "objects").iterdir()
                if directory.is_dir() and (directory / name).exists()
            )
            destination_path = next(
                directory / name
                for directory in (destination_git / "objects").iterdir()
                if directory.is_dir() and (directory / name).exists()
            )
            assert source_path.stat().st_ino != destination_path.stat().st_ino
    assert before == (
        git(source, "rev-parse", "HEAD"),
        source_config.read_bytes(),
        source_index.read_bytes(),
    )
    assert GitRunner(source)._environment()["GIT_OPTIONAL_LOCKS"] == "0"


def test_omitted_content_is_a_structured_non_executable_gap(tmp_path: Path) -> None:
    source = make_repository(tmp_path)
    (source / "tracked.txt").write_text("content too large\n")
    changeset = capture(
        source,
        limits=ContentLimits(per_file_bytes=1, total_bytes=1),
    )

    with materialize(source, changeset) as snapshot:
        assert snapshot.manifest.complete is False
        assert snapshot.manifest.environment_profile is EnvironmentProfile.GIT_REPOSITORY
        assert snapshot.manifest.materialization_failure is FailureKind.CAPABILITY
        assert snapshot.manifest.files == []
        assert not (snapshot.path / ".git").exists()


def test_gitlink_is_a_structured_non_executable_gap(tmp_path: Path) -> None:
    source = make_repository(tmp_path)
    head = git(source, "rev-parse", "HEAD")
    git(source, "update-index", "--add", "--cacheinfo", f"160000,{head},submodule")
    changeset = capture(source)

    with materialize(source, changeset) as snapshot:
        assert snapshot.manifest.complete is False
        assert snapshot.manifest.materialization_failure is FailureKind.CAPABILITY
        assert not (snapshot.path / ".git").exists()


@pytest.mark.parametrize(
    ("name", "attribute"),
    [
        ("tracked", "MAX_TRACKED_ENTRIES"),
        ("objects", "MAX_IMPORTED_OBJECTS"),
        ("logical", "MAX_LOGICAL_OBJECT_BYTES"),
        ("materialized", "MAX_MATERIALIZED_BYTES"),
    ],
)
def test_each_materialization_budget_fails_closed(
    tmp_path: Path, name: str, attribute: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_repository(tmp_path)
    (source / "tracked.txt").write_text("changed\n")
    changeset = capture(source)
    monkeypatch.setattr(snapshot_module, attribute, 0)

    with materialize(source, changeset) as snapshot:
        assert snapshot.manifest.complete is False
        assert snapshot.manifest.materialization_failure is FailureKind.CAPABILITY
        assert snapshot.manifest.files == []
        assert not (snapshot.path / ".git").exists()


def test_large_fixture_uses_bounded_batch_materialization(tmp_path: Path) -> None:
    source = tmp_path / "large"
    source.mkdir()
    git(source, "init", "--quiet", "-b", "main")
    git(source, "config", "user.name", "Fixture Author")
    git(source, "config", "user.email", "fixture@example.invalid")
    for index in range(5_001):
        (source / f"file-{index:05d}.txt").write_text(f"payload {index}\n")
    git(source, "add", ".")
    git(source, "commit", "--quiet", "-m", "large")
    changeset = capture_committed(source)

    with materialize(source, changeset) as snapshot:
        assert snapshot.manifest.complete
        assert len(snapshot.manifest.files) == 5_001
        assert snapshot_git(snapshot.path, "ls-files", "-z").count("\x00") == 5_001
