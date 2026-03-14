from __future__ import annotations

import subprocess
from pathlib import Path

from jj_review.bookmarks import BookmarkResolver
from jj_review.cache import ReviewStateStore, ReviewStateUnavailable, resolve_state_path
from jj_review.cli import main
from jj_review.jj import JjClient


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

    assert exit_code == 0
    state = ReviewStateStore.for_repo(repo).load()

    assert set(state.changes) == {revision.change_id for revision in stack.revisions}
    for revision in stack.revisions:
        cached_change = state.changes[revision.change_id]
        assert cached_change.bookmark is not None
    assert not (repo / ".jj-review.toml").exists()


def test_resolve_state_path_bootstraps_jj_repo_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    repo = _init_repo(tmp_path)
    config_id_path = repo / ".jj" / "repo" / "config-id"
    config_id_path.unlink()

    state_path = resolve_state_path(repo)

    assert config_id_path.exists()
    assert state_path.name == "state.toml"


def test_status_continues_when_repo_id_cannot_be_materialized(
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

    assert exit_code == 0
    assert "Stack:" in captured.out
    assert "(generated)" in captured.out
    assert not list((tmp_path / "state-home").rglob("state.toml"))


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
