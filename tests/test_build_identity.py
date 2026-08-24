from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from pre_pr_verify.build_identity import installed_core_identity


def test_installed_core_identity_is_reproducible_and_changes_with_content(
    tmp_path: Path,
) -> None:
    source = Path("src/pre_pr_verify")
    copied = tmp_path / "pre_pr_verify"
    shutil.copytree(source, copied)

    first = installed_core_identity(copied)
    assert first == installed_core_identity(copied)
    assert first.startswith("core-sha256:")
    assert not (tmp_path / ".git").exists()

    init = copied / "__init__.py"
    init.write_text(init.read_text() + "\n# identity mutation fixture\n")
    assert installed_core_identity(copied) != first


def test_copied_skill_core_reports_identity_without_git(tmp_path: Path) -> None:
    copied_skill = tmp_path / "installed-skill"
    copied_skill.mkdir()
    shutil.copytree(Path("src/pre_pr_verify"), copied_skill / "pre_pr_verify")
    shutil.copy2("SKILL.md", copied_skill / "SKILL.md")
    shutil.copytree("docs", copied_skill / "docs")

    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(copied_skill)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pre_pr_verify.build_identity import installed_core_identity; "
            "print(installed_core_identity())",
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.stdout.strip().startswith("core-sha256:")
    assert not (copied_skill / ".git").exists()

