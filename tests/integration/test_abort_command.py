from __future__ import annotations

import os
from pathlib import Path

from jj_review.cache import ReviewStateStore
from jj_review.intent import write_new_intent
from jj_review.jj import JjClient
from jj_review.models.intent import AbortIntent, CleanupRestackIntent, SubmitIntent

from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo,
)
from .submit_command_helpers import (
    configure_submit_environment,
    read_remote_ref,
    remote_refs,
    run_main,
)


def test_abort_reports_nothing_when_no_intent_file_exists(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    exit_code = run_main(repo, config_path, "abort")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Nothing to abort" in captured.out


def test_abort_dry_run_shows_planned_actions_without_mutating(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    bookmark = initial_state.changes[change_id].bookmark
    assert bookmark is not None
    initial_remote_target = read_remote_ref(fake_repo.git_dir, bookmark)

    # Inject a stale submit intent to simulate an interrupted submit.
    from jj_review.models.intent import SubmitIntent
    intent = SubmitIntent(
        kind="submit",
        pid=99999999,  # dead PID — simulates an interrupted operation
        label=f"submit on {change_id[:8]}",
        display_revset=change_id[:8],
        head_change_id=change_id,
        ordered_change_ids=(change_id,),
        bookmarks={change_id: bookmark},
        bases={},
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_new_intent(state_store.state_dir, intent)

    exit_code = run_main(repo, config_path, "abort", "--dry-run")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Planned abort actions" in captured.out
    assert "close PR" in captured.out
    assert "delete remote branch" in captured.out
    assert "forget local bookmark" in captured.out
    # Nothing was actually mutated.
    assert state_store.load() == initial_state
    assert read_remote_ref(fake_repo.git_dir, bookmark) == initial_remote_target
    assert fake_repo.pull_requests[1].state == "open"
    # Intent file still present after dry-run.
    assert state_store.list_intents()


def test_abort_retracts_submitted_change_and_clears_state(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    bookmark = state_store.load().changes[change_id].bookmark
    assert bookmark is not None

    # Inject a submit intent referencing the live change.
    from jj_review.models.intent import SubmitIntent
    intent = SubmitIntent(
        kind="submit",
        pid=99999999,  # dead PID — simulates an interrupted operation
        label=f"submit on {change_id[:8]}",
        display_revset=change_id[:8],
        head_change_id=change_id,
        ordered_change_ids=(change_id,),
        bookmarks={change_id: bookmark},
        bases={},
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_new_intent(state_store.state_dir, intent)

    exit_code = run_main(repo, config_path, "abort")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Applied abort actions" in captured.out
    assert "close PR" in captured.out
    assert "delete remote branch" in captured.out
    assert "forget local bookmark" in captured.out

    # PR was closed on GitHub.
    assert fake_repo.pull_requests[1].state == "closed"

    # Remote branch was deleted.
    assert bookmark not in remote_refs(fake_repo.git_dir)

    # Saved state was cleared.
    refreshed = state_store.load()
    assert change_id not in refreshed.changes

    # Intent file was removed.
    assert not state_store.list_intents()


def test_abort_removes_cleanup_restack_intent_with_note(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.trunk.change_id

    state_store = ReviewStateStore.for_repo(repo)
    state_store.require_writable()
    intent = CleanupRestackIntent(
        kind="cleanup-restack",
        pid=99999999,  # dead PID — simulates an interrupted operation
        label="cleanup --restack on @-",
        display_revset="@-",
        ordered_change_ids=(change_id,),
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_new_intent(state_store.state_dir, intent)

    exit_code = run_main(repo, config_path, "abort")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Applied abort actions" in captured.out
    assert "removed intent file" in captured.out
    assert "restack" in captured.out  # note about manual inspection
    assert not state_store.list_intents()


def test_abort_reports_stale_when_all_intents_have_gone_change_ids(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    state_store = ReviewStateStore.for_repo(repo)
    state_store.require_writable()
    # Use a change_id that doesn't exist in this repo.
    from jj_review.models.intent import SubmitIntent
    intent = SubmitIntent(
        kind="submit",
        pid=99999999,  # dead PID
        label="submit on @-",
        display_revset="@-",
        head_change_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ordered_change_ids=("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",),
        bookmarks={"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": "review/feat-aaaa"},
        bases={},
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_new_intent(state_store.state_dir, intent)

    exit_code = run_main(repo, config_path, "abort")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "stale" in captured.out
    assert "cleanup" in captured.out


def test_abort_skips_live_pid_intent_and_warns(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    bookmark = state_store.load().changes[change_id].bookmark
    assert bookmark is not None

    # Inject an intent with the current (live) PID to simulate a still-running submit.
    intent = SubmitIntent(
        kind="submit",
        pid=os.getpid(),
        label=f"submit on {change_id[:8]}",
        display_revset=change_id[:8],
        head_change_id=change_id,
        ordered_change_ids=(change_id,),
        bookmarks={change_id: bookmark},
        bases={},
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_new_intent(state_store.state_dir, intent)

    exit_code = run_main(repo, config_path, "abort")
    captured = capsys.readouterr()

    # Should warn and exit 1 without retracting anything.
    assert exit_code == 1
    assert "still in progress" in captured.out
    # PR untouched, intent file still present.
    assert fake_repo.pull_requests[1].state == "open"
    assert state_store.list_intents()


def test_abort_bails_when_another_abort_is_running(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    state_store = ReviewStateStore.for_repo(repo)
    state_store.require_writable()

    # Simulate a concurrent abort by writing an AbortIntent with a live PID.
    abort_lock = AbortIntent(
        kind="abort",
        pid=os.getpid(),
        label="abort",
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_new_intent(state_store.state_dir, abort_lock)

    exit_code = run_main(repo, config_path, "abort")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "already in progress" in captured.out


def test_abort_cleans_up_stale_abort_lock_and_proceeds(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    bookmark = state_store.load().changes[change_id].bookmark
    assert bookmark is not None

    # Write a dead-PID AbortIntent (leftover from a previous crashed abort).
    stale_lock = AbortIntent(
        kind="abort",
        pid=99999999,
        label="abort",
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_new_intent(state_store.state_dir, stale_lock)

    # Also write the real interrupted submit intent.
    intent = SubmitIntent(
        kind="submit",
        pid=99999999,  # dead PID — simulates an interrupted operation
        label=f"submit on {change_id[:8]}",
        display_revset=change_id[:8],
        head_change_id=change_id,
        ordered_change_ids=(change_id,),
        bookmarks={change_id: bookmark},
        bases={},
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_new_intent(state_store.state_dir, intent)

    exit_code = run_main(repo, config_path, "abort")
    capsys.readouterr()

    # Stale lock cleaned up silently; real submit intent retracted.
    assert exit_code == 0
    assert fake_repo.pull_requests[1].state == "closed"
    assert not state_store.list_intents()
