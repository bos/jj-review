from __future__ import annotations

from pathlib import Path

from jj_review.bookmarks import BookmarkResolver
from jj_review.cache import ReviewStateStore, ReviewStateUnavailable
from jj_review.cli import main
from jj_review.jj import JjClient

from ..support.integration_helpers import (
    commit_file as _commit,
)
from ..support.integration_helpers import (
    init_repo as _init_repo,
)
from ..support.integration_helpers import (
    run_command as _run,
)


def test_bookmark_pins_survive_subject_rewrites(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    repo = _init_repo(tmp_path)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")
    state_store = ReviewStateStore.for_repo(repo)

    first_stack = JjClient(repo).discover_review_stack()
    first_result = BookmarkResolver(state_store.load()).pin_revisions(first_stack.revisions)
    state_store.save(first_result.state)
    top_change_id = first_stack.revisions[-1].change_id
    initial_bookmark = first_result.resolutions[-1].bookmark

    _run(["jj", "describe", "-r", top_change_id, "-m", "renamed feature 2"], repo)

    second_stack = JjClient(repo).discover_review_stack(top_change_id)
    second_result = BookmarkResolver(state_store.load()).pin_revisions(second_stack.revisions)

    assert second_result.resolutions[-1].bookmark == initial_bookmark
    assert second_result.resolutions[-1].source == "cache"


def test_status_persists_generated_bookmark_pins(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    repo = _init_repo(tmp_path)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")
    stack = JjClient(repo).discover_review_stack()

    exit_code = main(["--repository", str(repo), "status"])

    assert exit_code == 1
    state = ReviewStateStore.for_repo(repo).load()

    assert set(state.changes) == {revision.change_id for revision in stack.revisions}
    for revision in stack.revisions:
        cached_change = state.changes[revision.change_id]
        assert cached_change.bookmark is not None
    assert not (repo / ".jj-review.toml").exists()


def test_status_continues_when_review_state_persistence_is_unavailable(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    repo = _init_repo(tmp_path)
    _commit(repo, "feature 1", "feature-1.txt")
    monkeypatch.setattr(
        "jj_review.cache._resolve_repo_id",
        lambda _: (_ for _ in ()).throw(ReviewStateUnavailable("repo config ID missing")),
    )

    exit_code = main(["--repository", str(repo), "status"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Unsubmitted stack:" in captured.out
    assert ": GitHub status unknown" in captured.out
    assert not list((tmp_path / "state-home").rglob("state.toml"))
