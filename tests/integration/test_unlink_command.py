from __future__ import annotations

from pathlib import Path

from jj_review.cache import ReviewStateStore
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
    run_main as _main,
)


def test_unlink_detaches_change_and_preserves_local_bookmark(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    bookmark = state_store.load().changes[change_id].bookmark
    assert bookmark is not None

    exit_code = _main(repo, config_path, "unlink", change_id)
    captured = capsys.readouterr()
    unlinked_change = state_store.load().changes[change_id]

    assert exit_code == 0
    assert "Stopped review tracking for" in captured.out
    assert unlinked_change.bookmark == bookmark
    assert unlinked_change.unlinked_at is not None
    assert unlinked_change.link_state == "unlinked"
    assert unlinked_change.pr_number is None
    assert unlinked_change.pr_review_decision is None
    assert unlinked_change.pr_state is None
    assert unlinked_change.pr_url is None
    assert unlinked_change.stack_comment_id is None
    assert JjClient(repo).get_bookmark_state(bookmark).local_target is not None
    assert fake_repo.pull_requests[1].state == "open"
    assert _issue_comments(fake_repo, 1) == []

def test_unlink_is_idempotent_for_unlinked_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit") == 0
    capsys.readouterr()

    change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id

    assert _main(repo, config_path, "unlink", change_id) == 0
    capsys.readouterr()
    exit_code = _main(repo, config_path, "unlink", change_id)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "already unlinked from review tracking" in captured.out

def test_unlink_rejects_change_without_active_review_link(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id

    exit_code = _main(repo, config_path, "unlink", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "no active review tracking link to unlink" in captured.err
