from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jj_review.jj import JjClient, UnsupportedStackError


def test_discover_review_stack_walks_linear_history_from_default_head(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    stack = JjClient(repo).discover_review_stack()

    assert stack.selected_revset == "@-"
    assert [revision.subject for revision in stack.revisions] == ["feature 1", "feature 2"]


def test_discover_review_stack_rejects_root_fallback_trunk(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, configure_trunk=False)
    _commit(repo, "feature 1", "feature-1.txt")

    with pytest.raises(
        UnsupportedStackError,
        match=r"`trunk\(\)` resolved to the root commit",
    ):
        JjClient(repo).discover_review_stack()


def test_discover_review_stack_ignores_off_path_reviewable_child(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit(repo, "feature 1", "feature-1.txt")
    feature_1 = _current_parent_commit_id(repo)
    _commit(repo, "feature 2", "feature-2.txt")
    feature_2 = _current_parent_commit_id(repo)
    _new_child(repo, feature_1)
    _commit(repo, "feature side", "feature-side.txt")

    stack = JjClient(repo).discover_review_stack(feature_2)

    assert [revision.subject for revision in stack.revisions] == ["feature 1", "feature 2"]


def test_discover_review_stack_returns_empty_when_head_is_trunk(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    # No commits beyond trunk; the selected revset resolves to trunk itself.
    stack = JjClient(repo).discover_review_stack("main")

    assert stack.revisions == ()
    assert stack.head.subject == "base"


def test_discover_review_stack_fails_with_root_before_trunk(tmp_path: Path) -> None:
    # If a commit's ancestry reaches the root before finding trunk(), the
    # walk should fail with a targeted UnsupportedStackError rather than
    # silently producing a corrupted stack.  We set trunk() to a bookmark
    # that is NOT an ancestor of the selected head.
    repo = _init_repo(tmp_path, configure_trunk=False)
    # Create a unlinked bookmark that lives on @- (the base commit).
    _run(["jj", "bookmark", "create", "main", "-r", "@-"], repo)
    # Create a sibling branch starting from the root commit, not from main.
    _run(["jj", "new", "root()"], repo)
    _run(["jj", "bookmark", "create", "trunk-alias", "-r", "@"], repo)
    _run(
        ["jj", "config", "set", "--repo", 'revset-aliases."trunk()"', "trunk-alias"],
        repo,
    )
    # Now go back and add a commit on the main branch.
    _run(["jj", "new", "main"], repo)
    _commit(repo, "feature on main", "feature.txt")
    head = _current_parent_commit_id(repo)

    with pytest.raises(
        UnsupportedStackError,
        match="stack reached the root commit before `trunk\\(\\)`",
    ):
        JjClient(repo).discover_review_stack(head)


def test_discover_review_stack_rejects_shared_trunk_ancestor_without_merge(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    base = _current_parent_commit_id(repo)

    _commit(repo, "trunk 1", "trunk-1.txt")
    _run(["jj", "bookmark", "move", "main", "--to", "@-"], repo)

    _run(["jj", "new", base], repo)
    _commit(repo, "feature 1", "feature-1.txt")
    head = _current_parent_commit_id(repo)

    with pytest.raises(
        UnsupportedStackError,
        match="stack reached the root commit before `trunk\\(\\)`",
    ):
        JjClient(repo).discover_review_stack(
            head,
            allow_immutable=True,
            allow_trunk_ancestors=True,
        )


def test_discover_review_stack_rejects_immutable_revisions(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit(repo, "feature 1", "feature-1.txt")
    feature_1 = _current_parent_commit_id(repo)
    _commit(repo, "feature 2", "feature-2.txt")
    _run(
        [
            "jj",
            "config",
            "set",
            "--repo",
            'revset-aliases."immutable_heads()"',
            f"builtin_immutable_heads() | {feature_1}",
        ],
        repo,
    )

    with pytest.raises(
        UnsupportedStackError,
        match="immutable commits are not reviewable",
    ):
        JjClient(repo).discover_review_stack()


def _init_repo(tmp_path: Path, *, configure_trunk: bool = True) -> Path:
    repo = tmp_path / "repo"
    _run(["jj", "git", "init", str(repo)], tmp_path)
    _run(["jj", "config", "set", "--repo", "user.name", "Test User"], repo)
    _run(["jj", "config", "set", "--repo", "user.email", "test@example.com"], repo)
    _write_file(repo / "README.md", "base\n")
    _run(["jj", "commit", "-m", "base"], repo)
    if configure_trunk:
        _run(["jj", "bookmark", "create", "main", "-r", "@-"], repo)
        _run(["jj", "config", "set", "--repo", 'revset-aliases."trunk()"', "main"], repo)
    return repo


def _commit(repo: Path, message: str, filename: str) -> None:
    _write_file(repo / filename, f"{message}\n")
    _run(["jj", "commit", "-m", message], repo)


def _current_parent_commit_id(repo: Path) -> str:
    completed = _run(
        [
            "jj",
            "log",
            "--no-graph",
            "-r",
            "@-",
            "-T",
            "commit_id",
        ],
        repo,
    )
    return completed.stdout.strip()


def _new_child(repo: Path, parent_commit_id: str) -> None:
    _run(["jj", "new", parent_commit_id], repo)


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        capture_output=True,
        check=False,
        cwd=cwd,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"{command!r} failed:\nstdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed


def _write_file(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
