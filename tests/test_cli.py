from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pre_pr_verify import cli


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "PrePR Verify Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "tracked.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    git(repo, "switch", "-c", "feature")
    return repo


def test_capture_prints_valid_empty_changeset(repository: Path, capsys) -> None:
    result = cli.main(
        ["capture", "--repo", str(repository), "--base", "main"]
    )

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["contract"] == "changeset"
    assert output["schema_version"] == "1.1.0"
    assert output["empty"] is True
    assert "verdict" not in output


def test_capture_writes_only_to_explicit_output(repository: Path, tmp_path: Path, capsys) -> None:
    output = tmp_path / "changeset.json"

    result = cli.main(
        [
            "capture",
            "--repo",
            str(repository),
            "--base",
            "main",
            "--scope",
            "committed-only",
            "--output",
            str(output),
        ]
    )

    assert result == 0
    assert capsys.readouterr().out == ""
    assert json.loads(output.read_text())["scope"] == "committed-only"


@pytest.mark.parametrize("relative", [".git/capture.json", ".git/objects/output"])
def test_capture_rejects_output_inside_git_directory(
    repository: Path, relative: str, capsys
) -> None:
    result = cli.main(
        [
            "capture",
            "--repo",
            str(repository),
            "--base",
            "main",
            "--output",
            str(repository / relative),
        ]
    )

    assert result == 3
    assert "protected Git directory" in capsys.readouterr().err


def test_capture_rejects_output_symlink_into_git_directory(
    repository: Path, tmp_path: Path, capsys
) -> None:
    link = tmp_path / "git-link"
    link.symlink_to(repository / ".git", target_is_directory=True)

    result = cli.main(
        [
            "capture",
            "--repo",
            str(repository),
            "--base",
            "main",
            "--output",
            str(link / "capture.json"),
        ]
    )

    assert result == 3
    assert "protected Git directory" in capsys.readouterr().err


def test_invalid_invocation_and_preflight_return_three(tmp_path: Path, capsys) -> None:
    assert cli.main(["capture", "--repo", str(tmp_path)]) == 3
    assert "--base" in capsys.readouterr().err

    assert (
        cli.main(
            ["capture", "--repo", str(tmp_path), "--base", "main"]
        )
        == 3
    )
    error = capsys.readouterr().err
    assert "not a Git" in error
    assert "verdict" not in error


def test_unexpected_internal_error_returns_four(repository: Path, monkeypatch, capsys) -> None:
    def fail(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cli, "capture_changeset", fail)

    assert (
        cli.main(
            ["capture", "--repo", str(repository), "--base", "main"]
        )
        == 4
    )
    assert "internal capture error" in capsys.readouterr().err
