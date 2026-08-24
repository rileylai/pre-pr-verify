from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import pre_pr_verify.git_capture as git_capture
import pre_pr_verify.scope_intent as scope_intent
from pre_pr_verify.errors import (
    PreflightError,
    ScopeSelectionCancelled,
    ScopeSelectionRequired,
)
from pre_pr_verify.models import ChangeOrigin, ScopeMode
from pre_pr_verify.pre_review_setup import PreReviewSetup, RequirementCandidate
from pre_pr_verify.scope_intent import (
    AdvisoryAction,
    PreviewThresholds,
    ScopeIntent,
    build_scope_preview,
    capture_resolved_scope,
    discover_scope_options,
    recommend_scope,
    resolve_scope_selection,
)


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


def write(repo: Path, relative: str, content: str) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Scope Resolver Test")
    git(repo, "config", "user.email", "scope@example.invalid")
    write(repo, "app.py", "value = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    git(repo, "switch", "-c", "feature")
    return repo


def commit(repo: Path, relative: str, content: str, message: str) -> str:
    write(repo, relative, content)
    git(repo, "add", relative)
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def test_working_scope_selection_uses_head_and_only_working_layers(
    repository: Path,
) -> None:
    write(repository, "app.py", "value = 2\n")
    git(repository, "add", "app.py")
    write(repository, "app.py", "value = 3\n")
    write(repository, "new.py", "created = True\n")

    options = discover_scope_options(repository)
    resolved = resolve_scope_selection(
        options,
        interactive=True,
        intent=ScopeIntent.WORKING_CHANGES,
    )
    preview = build_scope_preview(resolved)
    changeset = capture_resolved_scope(resolved)

    assert resolved.base_ref == options.head_commit
    assert resolved.scope is ScopeMode.PENDING
    assert changeset.comparison.merge_base_commit == options.head_commit
    assert preview.commit_count == 0
    assert preview.staged_paths == 1
    assert preview.unstaged_paths == 1
    assert preview.untracked_paths == 1
    assert all(ChangeOrigin.COMMITTED not in change.origins for change in changeset.changes)


def test_branch_scope_requires_explicit_base_selection(repository: Path) -> None:
    commit(repository, "feature.py", "feature = 1\n", "feature")
    options = discover_scope_options(repository)

    with pytest.raises(ScopeSelectionRequired, match="base candidate"):
        resolve_scope_selection(
            options,
            interactive=True,
            intent=ScopeIntent.CURRENT_BRANCH,
        )

    candidate = next(item for item in options.base_candidates if item.ref == "refs/heads/main")
    resolved = resolve_scope_selection(
        options,
        interactive=True,
        intent=ScopeIntent.CURRENT_BRANCH,
        selected_base=candidate.ref,
    )
    preview = build_scope_preview(resolved)
    changeset = capture_resolved_scope(resolved)

    assert resolved.base_ref == candidate.resolved_commit
    assert changeset.comparison.requested_base_ref == candidate.resolved_commit
    assert preview.commit_count == 1


def test_recent_commit_selection_includes_the_chosen_feature_start(
    repository: Path,
) -> None:
    feature_start = commit(repository, "one.py", "one = 1\n", "feature start")
    commit(repository, "two.py", "two = 2\n", "feature follow-up")
    options = discover_scope_options(repository)
    recent = next(item for item in options.recent_commits if item.commit == feature_start)

    resolved = resolve_scope_selection(
        options,
        interactive=True,
        intent=ScopeIntent.SINCE_COMMIT,
        selected_commit=feature_start,
    )
    preview = build_scope_preview(resolved)

    assert resolved.base_ref == recent.inclusive_base_commit
    assert resolved.selected_boundary == feature_start
    assert preview.commit_count == 2


def test_recommendation_never_materializes_a_scope(repository: Path) -> None:
    commit(repository, "feature.py", "feature = 1\n", "feature")
    options = discover_scope_options(repository)
    recommendation = recommend_scope(options)

    assert recommendation.intent is ScopeIntent.CURRENT_BRANCH
    assert recommendation.resolved_scope is None
    with pytest.raises(ScopeSelectionRequired, match="intent"):
        resolve_scope_selection(options, interactive=True)


def test_large_scope_preview_is_advisory_only(repository: Path) -> None:
    for index in range(3):
        commit(repository, f"file-{index}.txt", f"line {index}\n", f"change {index}")
    options = discover_scope_options(repository)
    candidate = next(item for item in options.base_candidates if item.ref == "refs/heads/main")
    resolved = resolve_scope_selection(
        options,
        interactive=True,
        intent=ScopeIntent.CURRENT_BRANCH,
        selected_base=candidate.ref,
    )

    preview = build_scope_preview(
        resolved,
        thresholds=PreviewThresholds(max_commits=2, max_changed_paths=2, max_line_churn=2),
    )
    changeset = capture_resolved_scope(resolved)

    assert preview.advisory_required is True
    assert "commit count exceeds 2" in preview.advisory_reasons
    assert preview.advisory_actions == (
        AdvisoryAction.CONTINUE_FULL_SCOPE,
        AdvisoryAction.USE_WORKING_CHANGES,
        AdvisoryAction.CHOOSE_FEATURE_START,
        AdvisoryAction.CANCEL,
    )
    assert changeset.scope is ScopeMode.PENDING


def test_materially_different_base_candidates_trigger_advisory(
    repository: Path,
) -> None:
    for index in range(5):
        commit(repository, f"step-{index}.txt", f"step {index}\n", f"step {index}")
    git(repository, "branch", "near-base")
    commit(repository, "last.txt", "last\n", "last")
    options = discover_scope_options(repository)
    candidate = next(item for item in options.base_candidates if item.ref == "refs/heads/main")
    resolved = resolve_scope_selection(
        options,
        interactive=True,
        intent=ScopeIntent.CURRENT_BRANCH,
        selected_base=candidate.ref,
    )

    preview = build_scope_preview(resolved)

    assert preview.advisory_required is True
    assert (
        "discovered base candidates imply materially different scopes"
        in preview.advisory_reasons
    )


def test_interactive_cancellation_stops_before_capture(repository: Path) -> None:
    options = discover_scope_options(repository)

    with pytest.raises(ScopeSelectionCancelled, match="cancelled"):
        resolve_scope_selection(options, interactive=True, cancelled=True)


def test_head_movement_after_selection_requires_setup_restart(repository: Path) -> None:
    options = discover_scope_options(repository)
    resolved = resolve_scope_selection(
        options,
        interactive=True,
        intent=ScopeIntent.WORKING_CHANGES,
    )
    commit(repository, "moved.py", "moved = True\n", "move HEAD")

    with pytest.raises(PreflightError, match="HEAD changed.*restart"):
        build_scope_preview(resolved)


def ready_setup(resolved) -> PreReviewSetup:
    setup = PreReviewSetup(
        interactive=True,
        requirement_candidates=(
            RequirementCandidate("a" * 64, "Repository requirement"),
        ),
        recommended_scope_number=1,
    )
    setup.submit("1")
    setup.bind_scope(resolved)
    setup.submit("1")
    setup.submit("1")
    setup.submit("yes")
    return setup


def test_ready_setup_accepts_an_unchanged_resolved_scope(repository: Path) -> None:
    options = discover_scope_options(repository)
    resolved = resolve_scope_selection(
        options,
        interactive=True,
        intent=ScopeIntent.WORKING_CHANGES,
    )
    setup = ready_setup(resolved)

    setup.require_ready_to_review()


def test_ready_setup_rejects_working_scope_mutation(repository: Path) -> None:
    options = discover_scope_options(repository)
    resolved = resolve_scope_selection(
        options,
        interactive=True,
        intent=ScopeIntent.WORKING_CHANGES,
    )
    setup = ready_setup(resolved)
    write(repository, "new.py", "new = True\n")

    with pytest.raises(PreflightError, match="stale.*restart"):
        setup.require_ready_to_review()


def test_ready_setup_rejects_working_content_mutation(repository: Path) -> None:
    write(repository, "app.py", "value = 2\n")
    options = discover_scope_options(repository)
    resolved = resolve_scope_selection(
        options,
        interactive=True,
        intent=ScopeIntent.WORKING_CHANGES,
    )
    setup = ready_setup(resolved)
    write(repository, "app.py", "value = 3\n")

    with pytest.raises(PreflightError, match="stale.*restart"):
        setup.require_ready_to_review()


def test_ready_setup_rejects_head_movement(repository: Path) -> None:
    options = discover_scope_options(repository)
    resolved = resolve_scope_selection(
        options,
        interactive=True,
        intent=ScopeIntent.WORKING_CHANGES,
    )
    setup = ready_setup(resolved)
    commit(repository, "moved.py", "moved = True\n", "move HEAD")

    with pytest.raises(PreflightError, match="stale.*restart"):
        setup.require_ready_to_review()


def test_headless_missing_scope_fails_closed(repository: Path) -> None:
    options = discover_scope_options(repository)

    with pytest.raises(PreflightError, match="headless.*explicit scope"):
        resolve_scope_selection(options, interactive=False)
    with pytest.raises(PreflightError, match="headless.*base"):
        resolve_scope_selection(
            options,
            interactive=False,
            intent=ScopeIntent.CURRENT_BRANCH,
        )


def test_review_focus_does_not_narrow_canonical_readiness_scope(
    repository: Path,
) -> None:
    write(repository, "focused.py", "focused = True\n")
    write(repository, "adjacent.py", "adjacent = True\n")
    options = discover_scope_options(repository)
    resolved = resolve_scope_selection(
        options,
        interactive=True,
        intent=ScopeIntent.WORKING_CHANGES,
        review_focus=("focused.py",),
    )

    preview = build_scope_preview(resolved)
    changeset = capture_resolved_scope(resolved)

    assert resolved.review_focus == ("focused.py",)
    assert preview.changed_path_count == 2
    assert {change.effective.path.display for change in changeset.changes} == {
        "focused.py",
        "adjacent.py",
    }


@pytest.mark.parametrize("kind", ["branch", "tag", "sha"])
def test_custom_scope_accepts_one_unambiguous_commit(
    repository: Path,
    kind: str,
) -> None:
    git(repository, "branch", "stable-branch")
    git(repository, "tag", "stable-tag")
    values = {
        "branch": "stable-branch",
        "tag": "stable-tag",
        "sha": git(repository, "rev-parse", "HEAD"),
    }
    options = discover_scope_options(repository)

    resolved = resolve_scope_selection(
        options,
        interactive=True,
        intent=ScopeIntent.CUSTOM,
        custom_base=values[kind],
    )

    assert resolved.base_ref == git(repository, "rev-parse", "HEAD")


def test_custom_scope_rejects_invalid_ambiguous_and_non_commit_refs(
    repository: Path,
) -> None:
    git(repository, "branch", "shared")
    git(repository, "tag", "shared")
    write(repository, "blob.txt", "not a commit\n")
    blob = git(repository, "hash-object", "-w", "blob.txt")
    git(repository, "tag", "blob-tag", blob)
    options = discover_scope_options(repository)

    for value, message in (
        ("missing", "invalid"),
        ("shared", "ambiguous"),
        ("blob-tag", "commit"),
    ):
        with pytest.raises(PreflightError, match=message):
            resolve_scope_selection(
                options,
                interactive=True,
                intent=ScopeIntent.CUSTOM,
                custom_base=value,
            )


def test_preselection_discovery_reads_metadata_not_source_content(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write(repository, "app.py", "working source must not be read\n")
    write(repository, "untracked.py", "untracked source must not be read\n")

    def fail_read(*args, **kwargs):
        raise AssertionError("pre-selection discovery read repository content")

    monkeypatch.setattr(git_capture._RootedReader, "read_file", fail_read)
    monkeypatch.setattr(scope_intent, "capture_changeset", fail_read)

    options = discover_scope_options(repository)
    resolved = resolve_scope_selection(
        options,
        interactive=True,
        intent=ScopeIntent.WORKING_CHANGES,
    )
    preview = build_scope_preview(resolved)

    assert options.working_path_count == 2
    assert options.unstaged_path_count == 1
    assert options.untracked_path_count == 1
    assert preview.changed_path_count == 2
    assert preview.approximate_added_lines is None
    assert preview.approximate_deleted_lines is None
    assert preview.line_estimate_complete is False


def test_representative_narrow_scope_recommends_working_changes(
    repository: Path,
) -> None:
    for batch in range(21):
        for index in range(batch * 8, batch * 8 + 8):
            write(repository, f"committed-{index:03d}.txt", f"feature {index}\n")
        git(repository, "add", ".")
        git(repository, "commit", "-m", f"feature {batch:02d}")
    write(repository, "app.py", "working = True\n")
    write(repository, "working-only.py", "working_only = True\n")

    options = discover_scope_options(repository)
    main = next(item for item in options.base_candidates if item.ref == "refs/heads/main")
    recommendation = recommend_scope(options)

    assert options.working_path_count == 2
    assert main.ahead_commits == 21
    assert main.changed_path_count >= 170
    assert recommendation.intent is ScopeIntent.WORKING_CHANGES
    assert recommendation.reason == (
        "Working changes are materially narrower than the branch scope."
    )
    assert recommendation.resolved_scope is None
