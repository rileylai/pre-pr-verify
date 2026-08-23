from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from typing import NoReturn, Sequence

from pre_pr_verify.errors import PreflightError
from pre_pr_verify.git_capture import (
    capture_changeset,
    resolve_repository_and_git_directory,
)
from pre_pr_verify.models import ScopeMode


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise PreflightError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="pre-pr-verify")
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_Parser,
    )
    capture = commands.add_parser("capture")
    capture.add_argument("--repo", required=True)
    capture.add_argument("--base", required=True)
    capture.add_argument(
        "--scope",
        choices=[mode.value for mode in ScopeMode],
        default=ScopeMode.PENDING.value,
    )
    capture.add_argument("--output")
    return parser


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _write_explicit_output(repository: str, requested: str, payload: str) -> None:
    _, git_directory = resolve_repository_and_git_directory(repository)
    output = Path(requested).expanduser()
    parent = output.parent.resolve()
    resolved_output = (parent / output.name).resolve()
    if _is_relative_to(resolved_output, git_directory):
        raise PreflightError("output path is inside the protected Git directory")
    if not parent.is_dir():
        raise PreflightError("output parent directory does not exist")

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, output)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        if arguments.command != "capture":
            raise PreflightError("unsupported command")
        changeset = capture_changeset(
            arguments.repo,
            arguments.base,
            ScopeMode(arguments.scope),
        )
        payload = changeset.model_dump_json(indent=2) + "\n"
        if arguments.output:
            _write_explicit_output(arguments.repo, arguments.output, payload)
        else:
            sys.stdout.write(payload)
        return 0
    except PreflightError as error:
        sys.stderr.write(f"preflight error: {error}\n")
        return 3
    except Exception:
        sys.stderr.write("internal capture error\n")
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
