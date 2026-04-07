from __future__ import annotations

from pathlib import Path

import pytest

from jj_review.cache import ReviewStateStore, resolve_state_path
from jj_review.jj import JjClient

from .submit_command_helpers import (
    commit as _commit,
)
from .submit_command_helpers import (
    configure_submit_environment as _configure_submit_environment,
)
from .submit_command_helpers import (
    init_repo as _init_repo,
)
from .submit_command_helpers import (
    issue_comments as _issue_comments,
)
from .submit_command_helpers import (
    read_remote_ref as _read_remote_ref,
)
from .submit_command_helpers import (
    remote_refs as _remote_refs,
)
from .submit_command_helpers import (
    run as _run,
)
from .submit_command_helpers import (
    run_main as _main,
)


def test_cleanup_apply_prunes_unlinked_state_for_stale_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id
    assert _main(repo, config_path, "unlink", change_id) == 0
    capsys.readouterr()
    _run(["jj", "abandon", change_id], repo)

    exit_code = _main(repo, config_path, "cleanup", "--apply")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "remove saved jj-review data" in captured.out
    assert change_id not in ReviewStateStore.for_repo(repo).load().changes

def test_cleanup_restack_previews_and_applies_survivor_rebase_after_merged_ancestor(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    bottom_change_id = stack.revisions[0].change_id
    top_change_id = stack.revisions[1].change_id
    trunk_commit_id = stack.trunk.commit_id
    fake_repo.pull_requests[1].state = "closed"
    fake_repo.pull_requests[1].merged_at = "2026-03-16T12:00:00Z"

    preview_exit_code = _main(repo, config_path, "cleanup", "--restack", top_change_id)
    preview = capsys.readouterr()

    assert preview_exit_code == 0
    assert "Planned restack actions:" in preview.out
    assert f"rebase {top_change_id[:8]} onto trunk()" in preview.out

    apply_exit_code = _main(
        repo,
        config_path,
        "cleanup",
        "--restack",
        "--apply",
        top_change_id,
    )
    applied = capsys.readouterr()
    rewritten_top = JjClient(repo).resolve_revision(top_change_id)

    assert apply_exit_code == 0
    assert "Applied restack actions:" in applied.out
    assert rewritten_top.only_parent_commit_id() == trunk_commit_id
    assert JjClient(repo).resolve_revision(bottom_change_id).commit_id != rewritten_top.commit_id

def test_cleanup_reports_stale_cache_and_remote_branch_without_applying(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    bookmark = initial_state.changes[change_id].bookmark
    assert bookmark is not None

    _run(["jj", "abandon", change_id], repo)
    _run(["jj", "bookmark", "delete", bookmark], repo)

    exit_code = _main(repo, config_path, "cleanup")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Planned cleanup actions:" in captured.out
    assert "[planned] tracking:" in captured.out
    assert f"[planned] remote branch: delete remote branch {bookmark}@origin" in (
        captured.out
    )
    assert "cleanup --apply" in captured.out
    assert change_id in state_store.load().changes
    assert f"refs/heads/{bookmark}" in _remote_refs(fake_repo.git_dir)

def test_cleanup_apply_removes_stale_cache_and_remote_branch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    bookmark = initial_state.changes[change_id].bookmark
    assert bookmark is not None

    _run(["jj", "abandon", change_id], repo)
    _run(["jj", "bookmark", "delete", bookmark], repo)

    exit_code = _main(repo, config_path, "cleanup", "--apply")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Applied cleanup actions:" in captured.out
    assert f"[applied] remote branch: delete remote branch {bookmark}@origin" in (
        captured.out
    )
    assert change_id not in state_store.load().changes
    assert f"refs/heads/{bookmark}" not in _remote_refs(fake_repo.git_dir)


def test_cleanup_plans_local_bookmark_forget_before_remote_delete_when_safe(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    bookmark = state_store.load().changes[change_id].bookmark
    assert bookmark is not None

    _run(["jj", "bookmark", "set", bookmark, "-r", change_id], repo)
    monkeypatch.setattr(
        "jj_review.commands.cleanup._stale_change_reasons",
        lambda **kwargs: {
            change_id: "local change is no longer reviewable"
            for change_id in kwargs["change_ids"]
        },
    )

    exit_code = _main(repo, config_path, "cleanup")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "[planned] local bookmark: forget local bookmark" in captured.out
    assert f"{bookmark} (local change is no longer reviewable)" in captured.out
    assert f"[planned] remote branch: delete remote branch {bookmark}@origin" in (
        captured.out
    )
    assert "[blocked] remote branch" not in captured.out
    assert bookmark in _run(["jj", "bookmark", "list", bookmark], repo).stdout
    assert f"refs/heads/{bookmark}" in _remote_refs(fake_repo.git_dir)


def test_cleanup_apply_forgets_local_bookmark_before_deleting_remote_branch_when_safe(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    bookmark = state_store.load().changes[change_id].bookmark
    assert bookmark is not None

    _run(["jj", "bookmark", "set", bookmark, "-r", change_id], repo)
    monkeypatch.setattr(
        "jj_review.commands.cleanup._stale_change_reasons",
        lambda **kwargs: {
            change_id: "local change is no longer reviewable"
            for change_id in kwargs["change_ids"]
        },
    )

    exit_code = _main(repo, config_path, "cleanup", "--apply")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"[applied] local bookmark: forget local bookmark {bookmark}" in captured.out
    assert f"[applied] remote branch: delete remote branch {bookmark}@origin" in (
        captured.out
    )
    assert change_id not in state_store.load().changes
    assert bookmark not in _run(["jj", "bookmark", "list", bookmark], repo).stdout
    assert f"refs/heads/{bookmark}" not in _remote_refs(fake_repo.git_dir)


def test_cleanup_apply_batches_remote_delete_local_forget_and_fetch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_ids = tuple(revision.change_id for revision in stack.revisions)
    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    bookmarks = tuple(
        state.changes[change_id].bookmark for change_id in change_ids
    )
    assert all(bookmark is not None for bookmark in bookmarks)

    for change_id, bookmark in zip(change_ids, bookmarks, strict=True):
        assert bookmark is not None
        _run(["jj", "bookmark", "set", bookmark, "-r", change_id], repo)

    monkeypatch.setattr(
        "jj_review.commands.cleanup._stale_change_reasons",
        lambda **kwargs: {
            change_id: "local change is no longer reviewable"
            for change_id in kwargs["change_ids"]
        },
    )

    original_delete_remote_bookmarks = JjClient.delete_remote_bookmarks
    original_forget_bookmarks = JjClient.forget_bookmarks
    original_fetch_remote = JjClient.fetch_remote
    calls: list[tuple[str, object]] = []

    def tracking_delete_remote_bookmarks(
        self,
        *,
        remote: str,
        deletions,
        fetch: bool = True,
    ) -> None:
        calls.append(("delete_remote_bookmarks", (remote, tuple(deletions), fetch)))
        return original_delete_remote_bookmarks(
            self,
            remote=remote,
            deletions=deletions,
            fetch=fetch,
        )

    def tracking_forget_bookmarks(self, bookmarks) -> None:
        calls.append(("forget_bookmarks", tuple(bookmarks)))
        return original_forget_bookmarks(self, bookmarks)

    def tracking_fetch_remote(self, *, remote: str, branches=None) -> None:
        calls.append(("fetch_remote", (remote, branches)))
        return original_fetch_remote(self, remote=remote, branches=branches)

    monkeypatch.setattr(
        "jj_review.commands.cleanup.JjClient.delete_remote_bookmarks",
        tracking_delete_remote_bookmarks,
    )
    monkeypatch.setattr(
        "jj_review.commands.cleanup.JjClient.forget_bookmarks",
        tracking_forget_bookmarks,
    )
    monkeypatch.setattr(
        "jj_review.commands.cleanup.JjClient.fetch_remote",
        tracking_fetch_remote,
    )

    exit_code = _main(repo, config_path, "cleanup", "--apply")
    capsys.readouterr()

    assert exit_code == 0
    assert calls == [
        (
            "delete_remote_bookmarks",
            (
                "origin",
                tuple(
                    (bookmark, state.changes[change_id].last_submitted_commit_id)
                    for change_id, bookmark in zip(change_ids, bookmarks, strict=True)
                    if bookmark is not None
                ),
                False,
            ),
        ),
        (
            "forget_bookmarks",
            tuple(bookmark for bookmark in bookmarks if bookmark is not None),
        ),
        ("fetch_remote", ("origin", None)),
    ]


def test_cleanup_apply_keeps_remote_branch_when_target_changes_mid_delete(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    bookmark = initial_state.changes[change_id].bookmark
    assert bookmark is not None

    _run(["jj", "abandon", change_id], repo)
    _run(["jj", "bookmark", "delete", bookmark], repo)

    original_delete_remote_bookmarks = JjClient.delete_remote_bookmarks

    def delete_remote_bookmarks_with_race(
        self,
        *,
        remote: str,
        deletions,
        fetch: bool = True,
    ) -> None:
        bookmark, _expected_remote_target = tuple(deletions)[0]
        _run(
            [
                "git",
                "--git-dir",
                str(fake_repo.git_dir),
                "update-ref",
                f"refs/heads/{bookmark}",
                _read_remote_ref(fake_repo.git_dir, "main"),
            ],
            fake_repo.git_dir.parent,
        )
        original_delete_remote_bookmarks(
            self,
            remote=remote,
            deletions=deletions,
            fetch=fetch,
        )

    monkeypatch.setattr(
        "jj_review.commands.cleanup.JjClient.delete_remote_bookmarks",
        delete_remote_bookmarks_with_race,
    )

    exit_code = _main(repo, config_path, "cleanup", "--apply")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert change_id in state_store.load().changes
    assert _read_remote_ref(fake_repo.git_dir, bookmark) == _read_remote_ref(
        fake_repo.git_dir, "main"
    )
    assert "force-with-lease" in captured.err

def test_cleanup_apply_deletes_managed_stack_comment_for_closed_pull_request(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    fake_repo.pull_requests[2].state = "closed"

    exit_code = _main(repo, config_path, "cleanup", "--apply")
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "[applied] stack summary comment: delete stack summary comment #1 from PR #2" in (
        captured.out
    )
    assert refreshed_state.changes[change_id].pr_number == 2
    assert refreshed_state.changes[change_id].stack_comment_id is None
    assert _issue_comments(fake_repo, 2) == []


def test_cleanup_apply_deletes_discovered_stack_comment_when_cache_id_is_missing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    fake_repo.pull_requests[2].state = "closed"
    state_store.save(
        initial_state.model_copy(
            update={
                "changes": {
                    **initial_state.changes,
                    change_id: initial_state.changes[change_id].model_copy(
                        update={"stack_comment_id": None}
                    ),
                }
            }
        )
    )

    exit_code = _main(repo, config_path, "cleanup", "--apply")
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "[applied] stack summary comment: delete stack summary comment #1 from PR #2" in (
        captured.out
    )
    assert refreshed_state.changes[change_id].stack_comment_id is None
    assert _issue_comments(fake_repo, 2) == []

def test_cleanup_apply_writes_and_deletes_intent_file_on_success(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """cleanup --apply deletes its intent file on success."""
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    bookmark = ReviewStateStore.for_repo(repo).load().changes[change_id].bookmark
    assert bookmark is not None

    # Abandon the change and delete the bookmark to make it stale
    _run(["jj", "abandon", change_id], repo)
    _run(["jj", "bookmark", "delete", bookmark], repo)

    exit_code = _main(repo, config_path, "cleanup", "--apply")
    capsys.readouterr()

    assert exit_code == 0
    state_dir = resolve_state_path(repo).parent
    intent_files = list(state_dir.glob("incomplete-*.toml"))
    assert intent_files == [], f"Expected no intent files after success, found: {intent_files}"

def test_cleanup_apply_leaves_intent_file_on_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """cleanup --apply leaves its intent file behind when it fails mid-way."""
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    bookmark = ReviewStateStore.for_repo(repo).load().changes[change_id].bookmark
    assert bookmark is not None

    _run(["jj", "abandon", change_id], repo)
    _run(["jj", "bookmark", "delete", bookmark], repo)

    def failing_delete_remote_bookmarks(self, *, remote, deletions, fetch=True):
        raise RuntimeError("Simulated failure during cleanup apply")

    monkeypatch.setattr(
        "jj_review.commands.cleanup.JjClient.delete_remote_bookmarks",
        failing_delete_remote_bookmarks,
    )

    with pytest.raises(RuntimeError, match="Simulated failure"):
        _main(repo, config_path, "cleanup", "--apply")
    capsys.readouterr()

    state_dir = resolve_state_path(repo).parent
    intent_files = list(state_dir.glob("incomplete-*.toml"))
    assert len(intent_files) == 1, f"Expected 1 intent file after failure, found: {intent_files}"

    import tomllib
    with intent_files[0].open("rb") as f:
        data = tomllib.load(f)
    assert data["kind"] == "cleanup-apply"
