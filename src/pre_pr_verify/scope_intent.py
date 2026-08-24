from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Never

from pre_pr_verify.errors import (
    PreflightError,
    ScopeSelectionCancelled,
    ScopeSelectionRequired,
)
from pre_pr_verify.git_capture import (
    _RootedReader,
    _index_entries,
    _working_state,
    GitRunner,
    capture_changeset,
    resolve_repository_and_git_directory,
)
from pre_pr_verify.models import ChangeSet, ScopeMode


class ScopeIntent(StrEnum):
    WORKING_CHANGES = "working-changes"
    CURRENT_BRANCH = "current-branch"
    SINCE_COMMIT = "since-commit"
    CUSTOM = "custom"


class AdvisoryAction(StrEnum):
    CONTINUE_FULL_SCOPE = "continue-full-scope"
    USE_WORKING_CHANGES = "use-working-changes"
    CHOOSE_FEATURE_START = "choose-feature-start-commit"
    CANCEL = "cancel"


@dataclass(frozen=True)
class BaseCandidate:
    ref: str
    resolved_commit: str
    merge_base_commit: str
    ahead_commits: int
    behind_commits: int
    changed_path_count: int


@dataclass(frozen=True)
class RecentCommit:
    commit: str
    inclusive_base_commit: str
    committed_at: int
    subject: str


@dataclass(frozen=True)
class ScopeOptions:
    repository: Path
    head_commit: str
    current_branch: str | None
    working_path_count: int
    staged_path_count: int
    unstaged_path_count: int
    untracked_path_count: int
    base_candidates: tuple[BaseCandidate, ...]
    recent_commits: tuple[RecentCommit, ...]


@dataclass(frozen=True)
class ScopeRecommendation:
    intent: ScopeIntent
    reason: str
    resolved_scope: None = None


@dataclass(frozen=True)
class ResolvedScope:
    repository: Path
    intent: ScopeIntent
    base_ref: str
    expected_head_commit: str
    scope: ScopeMode
    selected_boundary: str
    review_focus: tuple[str, ...]
    materially_different_alternatives: bool


def resolved_scope_identity(resolved: ResolvedScope) -> str:
    """Return the immutable setup identity for the currently resolved scope.

    This reuses the resolver's bounded Git metadata, including the working
    path/status sets, rather than recapturing a ChangeSet.
    """

    runner = GitRunner(resolved.repository)
    metadata = _scope_metadata(
        runner,
        resolved.base_ref,
        resolved.expected_head_commit,
    )
    working_states: list[dict[str, str]] = []
    if resolved.scope is ScopeMode.PENDING and metadata.working.paths:
        _, git_directory = resolve_repository_and_git_directory(resolved.repository)
        repository_reader = _RootedReader(resolved.repository, "scope identity")
        git_reader = _RootedReader(git_directory, "scope identity Git metadata")
        index_entries = _index_entries(runner)
        for path in sorted(metadata.working.paths):
            state = _working_state(
                repository_reader,
                path,
                index_entries.get(path),
                git_reader,
            )
            working_states.append(
                {
                    "path": path.hex(),
                    "kind": state.kind.value,
                    "mode": state.mode or "",
                    "identity_kind": state.identity_kind.value,
                    "content_identity": state.content_identity,
                }
            )
    payload = {
        "repository": str(resolved.repository),
        "intent": resolved.intent.value,
        "base_ref": resolved.base_ref,
        "expected_head_commit": resolved.expected_head_commit,
        "scope": resolved.scope.value,
        "selected_boundary": resolved.selected_boundary,
        "resolved_base_commit": metadata.resolved_base_commit,
        "merge_base_commit": metadata.merge_base_commit,
        "head_commit": metadata.head_commit,
        "commit_count": metadata.commit_count,
        "committed_paths": sorted(path.hex() for path in metadata.committed_paths),
        "staged_paths": sorted(path.hex() for path in metadata.working.staged),
        "unstaged_paths": sorted(path.hex() for path in metadata.working.unstaged),
        "untracked_paths": sorted(path.hex() for path in metadata.working.untracked),
        "working_states": working_states,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class PreviewThresholds:
    max_commits: int = 50
    max_changed_paths: int = 100
    max_line_churn: int = 5_000

    def __post_init__(self) -> None:
        if min(self.max_commits, self.max_changed_paths, self.max_line_churn) < 0:
            raise ValueError("scope preview thresholds must be non-negative")


@dataclass(frozen=True)
class ScopePreview:
    requested_intent: ScopeIntent
    selected_boundary: str
    resolved_base_commit: str
    merge_base_commit: str
    head_commit: str
    commit_count: int
    changed_path_count: int
    approximate_added_lines: int | None
    approximate_deleted_lines: int | None
    line_estimate_complete: bool
    committed_paths: int
    staged_paths: int
    unstaged_paths: int
    untracked_paths: int
    advisory_required: bool
    advisory_reasons: tuple[str, ...]
    advisory_actions: tuple[AdvisoryAction, ...]


@dataclass(frozen=True)
class _WorkingMetadata:
    staged: frozenset[bytes]
    unstaged: frozenset[bytes]
    untracked: frozenset[bytes]

    @property
    def paths(self) -> frozenset[bytes]:
        return self.staged | self.unstaged | self.untracked


@dataclass(frozen=True)
class _ScopeMetadata:
    resolved_base_commit: str
    merge_base_commit: str
    head_commit: str
    commit_count: int
    committed_paths: frozenset[bytes]
    working: _WorkingMetadata


def _resolve_commit(runner: GitRunner, revision: str) -> str:
    if not revision or "\x00" in revision:
        raise PreflightError("base revision must be non-empty and contain no NUL")
    output = runner.run(
        ["rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"]
    )
    try:
        commit = output.strip().decode("ascii", "strict")
    except UnicodeDecodeError as error:
        raise PreflightError("base revision did not resolve to a commit") from error
    if not commit:
        raise PreflightError("base revision did not resolve to a commit")
    return commit


def _count(runner: GitRunner, revision_range: str) -> int:
    raw = runner.run(["rev-list", "--count", revision_range]).strip()
    try:
        return int(raw)
    except ValueError as error:
        raise PreflightError("Git returned an invalid commit count") from error


def _path_set(raw: bytes) -> frozenset[bytes]:
    return frozenset(path for path in raw.split(b"\x00") if path)


def _working_metadata(runner: GitRunner, head: str) -> _WorkingMetadata:
    """Read path/status metadata without opening index or working-tree blobs."""

    staged = _path_set(
        runner.run(
            [
                "diff-index",
                "--cached",
                "--name-only",
                "-z",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                head,
                "--",
            ]
        )
    )
    unstaged = _path_set(
        runner.run(
            [
                "diff-files",
                "--name-only",
                "-z",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                "--",
            ]
        )
    )
    untracked = _path_set(
        runner.run(["ls-files", "--others", "--exclude-standard", "-z", "--"])
    )
    return _WorkingMetadata(
        staged=staged,
        unstaged=unstaged,
        untracked=untracked,
    )


def _committed_paths(
    runner: GitRunner,
    base_commit: str,
    head_commit: str,
) -> frozenset[bytes]:
    return _path_set(
        runner.run(
            [
                "diff-tree",
                "-r",
                "--no-commit-id",
                "--name-only",
                "-z",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                base_commit,
                head_commit,
                "--",
            ]
        )
    )


def _merge_base(runner: GitRunner, base_commit: str, head_commit: str) -> str:
    raw = runner.run(["merge-base", "--all", base_commit, head_commit]).strip()
    merge_bases = raw.decode("ascii", "strict").splitlines()
    if len(merge_bases) != 1:
        raise PreflightError("explicit base and HEAD require exactly one merge base")
    return merge_bases[0]


def _scope_metadata(
    runner: GitRunner,
    base_ref: str,
    expected_head: str,
) -> _ScopeMetadata:
    head = _resolve_commit(runner, "HEAD")
    if head != expected_head:
        raise PreflightError("HEAD changed after scope discovery; restart scope setup")
    base = _resolve_commit(runner, base_ref)
    merge_base = _merge_base(runner, base, head)
    return _ScopeMetadata(
        resolved_base_commit=base,
        merge_base_commit=merge_base,
        head_commit=head,
        commit_count=_count(runner, f"{merge_base}..{head}"),
        committed_paths=_committed_paths(runner, merge_base, head),
        working=_working_metadata(runner, head),
    )


def _resolve_custom_commit(runner: GitRunner, revision: str) -> str:
    """Resolve only a full object ID or exactly one Git ref namespace match."""

    if not revision or "\x00" in revision:
        raise PreflightError("custom ref is invalid")
    if re.fullmatch(r"[0-9a-f]{40,64}", revision):
        try:
            return _resolve_commit(runner, revision)
        except PreflightError as error:
            raise PreflightError("custom SHA must resolve to a commit") from error

    candidates: tuple[str, ...]
    if revision.startswith("refs/"):
        candidates = (revision,)
    else:
        candidates = tuple(
            dict.fromkeys(
                (
                    f"refs/{revision}",
                    f"refs/tags/{revision}",
                    f"refs/heads/{revision}",
                    f"refs/remotes/{revision}",
                    f"refs/remotes/{revision}/HEAD",
                )
            )
        )
    matches = [
        candidate
        for candidate in candidates
        if runner.run(
            ["show-ref", "--verify", "--hash", candidate], check=False
        ).strip()
    ]
    if not matches:
        raise PreflightError("custom ref is invalid")
    if len(matches) != 1:
        raise PreflightError("custom ref is ambiguous")
    try:
        return _resolve_commit(runner, matches[0])
    except PreflightError as error:
        raise PreflightError("custom ref must resolve to a commit") from error


def _candidate_priority(candidate: BaseCandidate) -> tuple[int, int, int, str]:
    short = candidate.ref.rsplit("/", 1)[-1]
    conventional = 0 if short in {"main", "master", "trunk", "develop"} else 1
    return (
        0 if candidate.behind_commits == 0 else 1,
        conventional,
        candidate.ahead_commits,
        candidate.ref,
    )


def discover_scope_options(
    repository: Path | str,
    *,
    max_base_candidates: int = 12,
    max_recent_commits: int = 20,
) -> ScopeOptions:
    """Discover bounded choices without selecting a review boundary."""

    if not 1 <= max_base_candidates <= 50 or not 1 <= max_recent_commits <= 50:
        raise ValueError("scope discovery bounds must be between 1 and 50")
    root, _ = resolve_repository_and_git_directory(repository)
    runner = GitRunner(root)
    head = _resolve_commit(runner, "HEAD")
    current_branch_raw = runner.run(
        ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False
    ).strip()
    current_branch = (
        current_branch_raw.decode("utf-8", "backslashreplace")
        if current_branch_raw
        else None
    )
    current_ref = f"refs/heads/{current_branch}" if current_branch else None

    working = _working_metadata(runner, head)
    references = runner.run(
        [
            "for-each-ref",
            f"--count={max_base_candidates * 4}",
            "--format=%(refname)",
            "refs/heads",
            "refs/remotes",
        ]
    ).splitlines()
    candidates: list[BaseCandidate] = []
    seen_commits: set[str] = set()
    for raw_ref in sorted(references):
        ref = raw_ref.decode("utf-8", "backslashreplace")
        if ref == current_ref or ref.endswith("/HEAD"):
            continue
        try:
            resolved = _resolve_commit(runner, ref)
            if resolved == head or resolved in seen_commits:
                continue
            merge_base = runner.run(["merge-base", resolved, head]).strip().decode(
                "ascii", "strict"
            )
            if not merge_base:
                continue
            candidate = BaseCandidate(
                ref=ref,
                resolved_commit=resolved,
                merge_base_commit=merge_base,
                ahead_commits=_count(runner, f"{merge_base}..{head}"),
                behind_commits=_count(runner, f"{merge_base}..{resolved}"),
                changed_path_count=len(
                    _committed_paths(runner, merge_base, head) | working.paths
                ),
            )
        except (PreflightError, UnicodeDecodeError):
            continue
        candidates.append(candidate)
        seen_commits.add(resolved)
    candidates.sort(key=_candidate_priority)

    log = runner.run(
        [
            "log",
            "--first-parent",
            f"-n{max_recent_commits + 1}",
            "--format=%H%x00%P%x00%ct%x00%s",
            "HEAD",
        ]
    )
    recent: list[RecentCommit] = []
    for record in log.splitlines():
        fields = record.split(b"\x00", 3)
        if len(fields) != 4:
            raise PreflightError("Git returned malformed recent-commit metadata")
        commit_raw, parents_raw, timestamp_raw, subject_raw = fields
        parents = parents_raw.split()
        if not parents:
            continue
        recent.append(
            RecentCommit(
                commit=commit_raw.decode("ascii", "strict"),
                inclusive_base_commit=parents[0].decode("ascii", "strict"),
                committed_at=int(timestamp_raw),
                subject=subject_raw.decode("utf-8", "backslashreplace")[:120],
            )
        )
        if len(recent) == max_recent_commits:
            break

    if _resolve_commit(runner, "HEAD") != head:
        raise PreflightError("HEAD changed during scope discovery; restart scope setup")
    return ScopeOptions(
        repository=root,
        head_commit=head,
        current_branch=current_branch,
        working_path_count=len(working.paths),
        staged_path_count=len(working.staged),
        unstaged_path_count=len(working.unstaged),
        untracked_path_count=len(working.untracked),
        base_candidates=tuple(candidates[:max_base_candidates]),
        recent_commits=tuple(recent),
    )


def recommend_scope(options: ScopeOptions) -> ScopeRecommendation:
    """Return an advisory intent only; never return a resolved boundary."""

    branch = next(
        (candidate for candidate in options.base_candidates if candidate.ahead_commits),
        None,
    )
    thresholds = PreviewThresholds()
    if (
        options.working_path_count
        and branch is not None
        and options.working_path_count < branch.changed_path_count
        and (
            branch.ahead_commits > thresholds.max_commits
            or branch.changed_path_count > thresholds.max_changed_paths
        )
    ):
        return ScopeRecommendation(
            ScopeIntent.WORKING_CHANGES,
            "Working changes are materially narrower than the branch scope.",
        )
    if branch is not None:
        return ScopeRecommendation(
            ScopeIntent.CURRENT_BRANCH,
            "HEAD contains commits beyond at least one discovered base candidate; "
            "the base still requires explicit selection.",
        )
    if options.working_path_count:
        return ScopeRecommendation(
            ScopeIntent.WORKING_CHANGES,
            "Only working-state changes were discovered relative to HEAD.",
        )
    if options.recent_commits:
        return ScopeRecommendation(
            ScopeIntent.SINCE_COMMIT,
            "No branch boundary was evident; choose an inclusive feature-start commit.",
        )
    return ScopeRecommendation(
        ScopeIntent.CUSTOM,
        "No bounded heuristic distinguishes a scope; supply an explicit base/ref.",
    )


def _validate_focus(review_focus: tuple[str, ...]) -> tuple[str, ...]:
    validated: list[str] = []
    for path in review_focus:
        raw = os.fsencode(path)
        parts = raw.split(b"/")
        if (
            not raw
            or raw.startswith(b"/")
            or b"\x00" in raw
            or any(part in (b"", b".", b"..") for part in parts)
            or any(part.lower() == b".git" for part in parts)
        ):
            raise PreflightError("review focus must be a safe repository-relative path")
        validated.append(path)
    return tuple(dict.fromkeys(validated))


def _missing_selection(interactive: bool, detail: str) -> Never:
    if interactive:
        raise ScopeSelectionRequired(f"interactive {detail} selection is required")
    raise PreflightError(f"headless invocation requires an explicit {detail}")


def resolve_scope_selection(
    options: ScopeOptions,
    *,
    interactive: bool,
    intent: ScopeIntent | None = None,
    selected_base: str | None = None,
    selected_commit: str | None = None,
    custom_base: str | None = None,
    cancelled: bool = False,
    review_focus: tuple[str, ...] = (),
) -> ResolvedScope:
    """Materialize an explicit human/config choice into the ChangeSet inputs."""

    if cancelled:
        if not interactive:
            raise PreflightError("headless invocation cannot use interactive cancellation")
        raise ScopeSelectionCancelled("interactive scope setup was cancelled")
    if intent is None:
        _missing_selection(interactive, "scope intent")
    assert intent is not None
    boundary: str
    base_ref: str
    if intent is ScopeIntent.WORKING_CHANGES:
        base_ref = options.head_commit
        boundary = f"HEAD {options.head_commit} -> working state"
    elif intent is ScopeIntent.CURRENT_BRANCH:
        if selected_base is None:
            _missing_selection(interactive, "base candidate")
        candidate = next(
            (item for item in options.base_candidates if item.ref == selected_base),
            None,
        )
        if candidate is None:
            raise PreflightError("selected base is not in the bounded candidate list")
        base_ref = candidate.resolved_commit
        boundary = f"{candidate.ref} at {candidate.resolved_commit}"
    elif intent is ScopeIntent.SINCE_COMMIT:
        if selected_commit is None:
            _missing_selection(interactive, "feature-start commit")
        recent = next(
            (item for item in options.recent_commits if item.commit == selected_commit),
            None,
        )
        if recent is None:
            raise PreflightError("selected commit is not in the bounded recent-commit list")
        base_ref = recent.inclusive_base_commit
        boundary = recent.commit
    elif intent is ScopeIntent.CUSTOM:
        if custom_base is None:
            _missing_selection(interactive, "custom base/ref")
        runner = GitRunner(options.repository)
        base_ref = _resolve_custom_commit(runner, custom_base)
        boundary = f"{custom_base} at {base_ref}"
    else:
        raise PreflightError("unsupported scope intent")
    candidate_counts = sorted(
        {candidate.ahead_commits for candidate in options.base_candidates}
    )
    materially_different_alternatives = bool(
        intent is ScopeIntent.CURRENT_BRANCH
        and len(candidate_counts) > 1
        and candidate_counts[-1] - candidate_counts[0] >= 2
        and candidate_counts[-1] >= max(2, candidate_counts[0] * 2)
    )
    return ResolvedScope(
        repository=options.repository,
        intent=intent,
        base_ref=base_ref,
        expected_head_commit=options.head_commit,
        scope=ScopeMode.PENDING,
        selected_boundary=boundary,
        review_focus=_validate_focus(review_focus),
        materially_different_alternatives=materially_different_alternatives,
    )


def build_scope_preview(
    resolved: ResolvedScope,
    *,
    thresholds: PreviewThresholds | None = None,
) -> ScopePreview:
    """Project advisory metadata without reading source or capturing a ChangeSet."""

    selected_thresholds = thresholds or PreviewThresholds()
    runner = GitRunner(resolved.repository)
    metadata = _scope_metadata(
        runner,
        resolved.base_ref,
        resolved.expected_head_commit,
    )
    changed_paths = metadata.committed_paths | metadata.working.paths
    reasons: list[str] = []
    if metadata.commit_count > selected_thresholds.max_commits:
        reasons.append(f"commit count exceeds {selected_thresholds.max_commits}")
    if len(changed_paths) > selected_thresholds.max_changed_paths:
        reasons.append(
            f"changed-path count exceeds {selected_thresholds.max_changed_paths}"
        )
    if metadata.commit_count and metadata.working.paths:
        reasons.append("selected scope combines committed and working changes")
    if resolved.materially_different_alternatives:
        reasons.append("discovered base candidates imply materially different scopes")
    actions = (
        (
            AdvisoryAction.CONTINUE_FULL_SCOPE,
            AdvisoryAction.USE_WORKING_CHANGES,
            AdvisoryAction.CHOOSE_FEATURE_START,
            AdvisoryAction.CANCEL,
        )
        if reasons
        else ()
    )
    return ScopePreview(
        requested_intent=resolved.intent,
        selected_boundary=resolved.selected_boundary,
        resolved_base_commit=metadata.resolved_base_commit,
        merge_base_commit=metadata.merge_base_commit,
        head_commit=metadata.head_commit,
        commit_count=metadata.commit_count,
        changed_path_count=len(changed_paths),
        approximate_added_lines=None,
        approximate_deleted_lines=None,
        line_estimate_complete=False,
        committed_paths=len(metadata.committed_paths),
        staged_paths=len(metadata.working.staged),
        unstaged_paths=len(metadata.working.unstaged),
        untracked_paths=len(metadata.working.untracked),
        advisory_required=bool(reasons),
        advisory_reasons=tuple(reasons),
        advisory_actions=actions,
    )


def capture_resolved_scope(resolved: ResolvedScope) -> ChangeSet:
    """Capture only after the explicit selection and metadata preview are accepted."""

    changeset = capture_changeset(
        resolved.repository,
        resolved.base_ref,
        resolved.scope,
    )
    if changeset.comparison.head_commit != resolved.expected_head_commit:
        raise PreflightError("HEAD changed after scope discovery; restart scope setup")
    return changeset
