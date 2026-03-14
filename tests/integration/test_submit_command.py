from __future__ import annotations

import subprocess
from pathlib import Path

from jj_review.cache import ReviewStateStore
from jj_review.cli import main
from jj_review.jj import JjClient
from jj_review.testing.fake_github import initialize_bare_repository


def test_submit_projects_review_bookmarks_to_selected_remote(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    repo, remote = _init_repo(tmp_path)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    exit_code = main(["--repository", str(repo), "submit"])
    captured = capsys.readouterr()
    stack = JjClient(repo).discover_review_stack()
    state = ReviewStateStore.for_repo(repo).load()

    assert exit_code == 0
    assert "Selected remote: origin" in captured.out
    for revision in stack.revisions:
        bookmark = state.changes[revision.change_id].bookmark
        assert bookmark is not None
        assert _read_remote_ref(remote, bookmark) == revision.commit_id


def test_submit_reports_up_to_date_when_remote_bookmark_already_matches(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    repo, remote = _init_repo(tmp_path)
    _commit(repo, "feature 1", "feature-1.txt")

    assert main(["--repository", str(repo), "submit"]) == 0
    first_output = capsys.readouterr().out
    first_refs = _remote_refs(remote)

    exit_code = main(["--repository", str(repo), "submit"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "pushed" in first_output
    assert "up to date" in captured.out
    assert _remote_refs(remote) == first_refs


def test_submit_updates_remote_bookmark_after_change_rewrite(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    repo, remote = _init_repo(tmp_path)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")
    assert main(["--repository", str(repo), "submit"]) == 0
    capsys.readouterr()

    first_stack = JjClient(repo).discover_review_stack()
    top_change_id = first_stack.revisions[-1].change_id
    initial_bookmark = ReviewStateStore.for_repo(repo).load().changes[top_change_id].bookmark
    assert initial_bookmark is not None

    _run(["jj", "describe", "-r", top_change_id, "-m", "feature 2 renamed"], repo)

    exit_code = main(["--repository", str(repo), "submit", top_change_id])
    captured = capsys.readouterr()
    rewritten_stack = JjClient(repo).discover_review_stack(top_change_id)
    rewritten_bookmark = ReviewStateStore.for_repo(repo).load().changes[top_change_id].bookmark

    assert exit_code == 0
    assert rewritten_bookmark == initial_bookmark
    assert "pushed" in captured.out
    assert _read_remote_ref(remote, initial_bookmark) == rewritten_stack.revisions[-1].commit_id


def test_submit_reports_no_reviewable_commits_when_head_is_trunk(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    repo, _remote = _init_repo(tmp_path)

    # main itself is trunk(); selecting it means there is nothing to review.
    exit_code = main(["--repository", str(repo), "submit", "main"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No reviewable commits" in captured.out
    # Nothing should have been persisted or pushed.
    assert ReviewStateStore.for_repo(repo).load().changes == {}


def test_submit_rejects_duplicate_bookmark_overrides_before_projection(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    repo, remote = _init_repo(tmp_path)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")
    stack = JjClient(repo).discover_review_stack()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                f'[change."{stack.revisions[0].change_id}"]',
                'bookmark_override = "review/same"',
                "",
                f'[change."{stack.revisions[1].change_id}"]',
                'bookmark_override = "review/same"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(["--config", str(config_path), "--repository", str(repo), "submit"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "multiple review units to the same bookmark" in captured.err
    assert ReviewStateStore.for_repo(repo).load().changes == {}
    assert _remote_refs(remote) == {}


def _init_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    fake_repo = initialize_bare_repository(
        tmp_path / "remotes",
        owner="octo-org",
        name="stacked-review",
    )
    _run(["jj", "git", "init", str(repo)], tmp_path)
    _run(["jj", "config", "set", "--repo", "user.name", "Test User"], repo)
    _run(["jj", "config", "set", "--repo", "user.email", "test@example.com"], repo)
    _write_file(repo / "README.md", "base\n")
    _run(["jj", "commit", "-m", "base"], repo)
    _run(["jj", "bookmark", "create", "main", "-r", "@-"], repo)
    _run(["jj", "config", "set", "--repo", 'revset-aliases."trunk()"', "main"], repo)
    _run(["jj", "git", "remote", "add", "origin", str(fake_repo.git_dir)], repo)
    return repo, fake_repo.git_dir


def _commit(repo: Path, message: str, filename: str) -> None:
    _write_file(repo / filename, f"{message}\n")
    _run(["jj", "commit", "-m", message], repo)


def _read_remote_ref(remote: Path, bookmark: str) -> str:
    completed = _run(
        ["git", "--git-dir", str(remote), "rev-parse", f"refs/heads/{bookmark}"],
        remote.parent,
    )
    return completed.stdout.strip()


def _remote_refs(remote: Path) -> dict[str, str]:
    completed = subprocess.run(
        ["git", "--git-dir", str(remote), "show-ref", "--heads"],
        capture_output=True,
        check=False,
        cwd=remote.parent,
        text=True,
    )
    if completed.returncode not in (0, 1):
        raise AssertionError(
            "['git', '--git-dir', "
            f"{str(remote)!r}, 'show-ref', '--heads'] failed:\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    refs: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        commit_id, ref_name = line.split(" ", maxsplit=1)
        refs[ref_name] = commit_id
    return refs


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
