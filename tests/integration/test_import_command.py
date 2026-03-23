from __future__ import annotations

import subprocess
from pathlib import Path

import httpx

from jj_review.cache import ReviewStateStore, resolve_state_path
from jj_review.cli import main
from jj_review.errors import CliError
from jj_review.github.client import GithubClient
from jj_review.jj import JjClient
from jj_review.testing.fake_github import (
    FakeGithubRepository,
    FakeGithubState,
    create_app,
    initialize_bare_repository,
)


def test_import_bootstraps_local_review_state_from_pull_request(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_import_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    state_before = ReviewStateStore.for_repo(repo).load()
    review_bookmarks = sorted(
        {
            change.bookmark
            for change in state_before.changes.values()
            if change.bookmark is not None and change.bookmark.startswith("review/")
        }
    )
    for bookmark in review_bookmarks:
        _run(["jj", "bookmark", "forget", bookmark], repo)
    resolve_state_path(repo).unlink()

    exit_code = _main(repo, config_path, "import", "--pull-request", "2")

    assert exit_code == 0
    state_after = ReviewStateStore.for_repo(repo).load()
    bookmarks_after = sorted(
        {
            change.bookmark
            for change in state_after.changes.values()
            if change.bookmark is not None
        }
    )
    assert bookmarks_after == review_bookmarks
    bookmark_states = JjClient(repo).list_bookmark_states(review_bookmarks)
    assert all(
        bookmark_states[bookmark].local_target is not None for bookmark in review_bookmarks
    )


def test_import_head_bootstraps_local_review_state_without_pull_requests(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_import_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    state_before = ReviewStateStore.for_repo(repo).load()
    stack = JjClient(repo).discover_review_stack()
    top_change_id = stack.revisions[-1].change_id
    top_bookmark = state_before.changes[top_change_id].bookmark
    assert top_bookmark is not None
    review_bookmarks = sorted(
        {
            change.bookmark
            for change in state_before.changes.values()
            if change.bookmark is not None and change.bookmark.startswith("review/")
        }
    )
    fake_repo.pull_requests.clear()
    for bookmark in review_bookmarks:
        _run(["jj", "bookmark", "forget", bookmark], repo)
    resolve_state_path(repo).unlink()

    exit_code = _main(repo, config_path, "import", "--head", top_bookmark)

    assert exit_code == 0
    state_after = ReviewStateStore.for_repo(repo).load()
    assert sorted(
        {
            change.bookmark
            for change in state_after.changes.values()
            if change.bookmark is not None
        }
    ) == review_bookmarks
    assert all(change.pr_number is None for change in state_after.changes.values())
    assert all(change.pr_state is None for change in state_after.changes.values())
    assert all(change.stack_comment_id is None for change in state_after.changes.values())
    bookmark_states = JjClient(repo).list_bookmark_states(review_bookmarks)
    assert all(
        bookmark_states[bookmark].local_target is not None for bookmark in review_bookmarks
    )


def test_import_reports_up_to_date_when_selected_stack_is_already_materialized(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_import_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    top_change_id = stack.revisions[-1].change_id
    top_bookmark = ReviewStateStore.for_repo(repo).load().changes[top_change_id].bookmark
    assert top_bookmark is not None

    exit_code = _main(repo, config_path, "import", "--head", top_bookmark)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Review state is already up to date for the selected stack." in captured.out
    assert "No reviewable commits" not in captured.out


def test_import_current_requires_discoverable_remote_review_linkage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_import_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    exit_code = _main(repo, config_path, "import", "--current")

    assert exit_code == 1


def test_import_revset_fails_closed_without_remote_bookmark_identity(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo_without_remote(tmp_path)
    config_path = _configure_import_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id

    exit_code = _main(repo, config_path, "import", "--revset", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "has no discoverable review bookmark on the selected remote" in captured.err
    assert ReviewStateStore.for_repo(repo).load().changes == {}
    assert not {
        bookmark
        for bookmark in JjClient(repo).list_bookmark_states()
        if bookmark.startswith("review/")
    }


def test_import_head_rejects_ambiguous_pull_request_linkage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_import_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    stack = JjClient(repo).discover_review_stack()
    top_change_id = stack.revisions[-1].change_id
    top_bookmark = ReviewStateStore.for_repo(repo).load().changes[top_change_id].bookmark
    assert top_bookmark is not None
    fake_repo.create_pull_request(
        base_ref=fake_repo.pull_requests[2].base_ref,
        body="duplicate linkage",
        head_ref=top_bookmark,
        title="duplicate linkage",
    )

    exit_code = _main(repo, config_path, "import", "--head", top_bookmark)

    assert exit_code == 1


def test_import_fails_closed_when_stack_would_need_generated_bookmarks(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_import_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    state_before = ReviewStateStore.for_repo(repo).load()
    stack = JjClient(repo).discover_review_stack()
    bottom_change_id = stack.revisions[0].change_id
    top_change_id = stack.revisions[-1].change_id
    bottom_bookmark = state_before.changes[bottom_change_id].bookmark
    top_bookmark = state_before.changes[top_change_id].bookmark
    assert bottom_bookmark is not None
    assert top_bookmark is not None

    for bookmark in (bottom_bookmark, top_bookmark):
        _run(["jj", "bookmark", "forget", bookmark], repo)
    resolve_state_path(repo).unlink()
    _run(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            "-d",
            f"refs/heads/{bottom_bookmark}",
        ],
        repo,
    )

    exit_code = _main(repo, config_path, "import", "--head", top_bookmark)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "has no discoverable review bookmark on the selected remote" in captured.err
    assert ReviewStateStore.for_repo(repo).load().changes == {}
    bookmark_states = JjClient(repo).list_bookmark_states((bottom_bookmark, top_bookmark))
    assert bookmark_states[bottom_bookmark].local_target is None
    assert bookmark_states[top_bookmark].local_target is None


def test_import_fails_closed_when_cached_bookmark_is_missing_on_selected_remote(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_import_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    state_before = ReviewStateStore.for_repo(repo).load()
    stack = JjClient(repo).discover_review_stack()
    bottom_change_id = stack.revisions[0].change_id
    top_change_id = stack.revisions[-1].change_id
    bottom_bookmark = state_before.changes[bottom_change_id].bookmark
    top_bookmark = state_before.changes[top_change_id].bookmark
    assert bottom_bookmark is not None
    assert top_bookmark is not None

    for bookmark in (bottom_bookmark, top_bookmark):
        _run(["jj", "bookmark", "forget", bookmark], repo)
    _run(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            "-d",
            f"refs/heads/{bottom_bookmark}",
        ],
        repo,
    )

    exit_code = _main(repo, config_path, "import", "--head", top_bookmark)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "cached review bookmark" in captured.err
    assert "is not present on the selected remote" in captured.err
    bookmark_states = JjClient(repo).list_bookmark_states((bottom_bookmark, top_bookmark))
    assert bookmark_states[bottom_bookmark].local_target is None
    assert bookmark_states[top_bookmark].local_target is None


def test_import_fails_closed_without_partial_local_bookmark_updates(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_import_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    state_before = ReviewStateStore.for_repo(repo).load()
    stack = JjClient(repo).discover_review_stack()
    bottom_change_id = stack.revisions[0].change_id
    top_change_id = stack.revisions[-1].change_id
    bottom_bookmark = state_before.changes[bottom_change_id].bookmark
    top_bookmark = state_before.changes[top_change_id].bookmark
    assert bottom_bookmark is not None
    assert top_bookmark is not None

    for bookmark in (bottom_bookmark, top_bookmark):
        _run(["jj", "bookmark", "forget", bookmark], repo)
    main_target = JjClient(repo).resolve_revision("main").commit_id
    _run(["jj", "bookmark", "set", top_bookmark, "--revision", "main"], repo)

    exit_code = _main(repo, config_path, "import", "--head", top_bookmark)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "already points to a different revision" in captured.err
    bookmark_states = JjClient(repo).list_bookmark_states((bottom_bookmark, top_bookmark))
    assert bookmark_states[bottom_bookmark].local_target is None
    assert bookmark_states[top_bookmark].local_target == main_target


def test_import_prefers_exact_remote_bookmarks_over_stale_cached_names(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_import_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    state_store = ReviewStateStore.for_repo(repo)
    state_before = state_store.load()
    stack = JjClient(repo).discover_review_stack()
    bottom_change_id = stack.revisions[0].change_id
    top_change_id = stack.revisions[-1].change_id
    bottom_bookmark = state_before.changes[bottom_change_id].bookmark
    top_bookmark = state_before.changes[top_change_id].bookmark
    assert bottom_bookmark is not None
    assert top_bookmark is not None

    stale_bookmark = f"review/stale-name-{bottom_change_id[:8]}"
    state_store.save(
        state_before.model_copy(
            update={
                "changes": {
                    **state_before.changes,
                    bottom_change_id: state_before.changes[bottom_change_id].model_copy(
                        update={"bookmark": stale_bookmark}
                    ),
                }
            }
        )
    )
    for bookmark in (bottom_bookmark, top_bookmark):
        _run(["jj", "bookmark", "forget", bookmark], repo)

    exit_code = _main(repo, config_path, "import", "--head", top_bookmark)

    assert exit_code == 0
    state_after = state_store.load()
    assert state_after.changes[bottom_change_id].bookmark == bottom_bookmark
    bookmark_states = JjClient(repo).list_bookmark_states((bottom_bookmark, stale_bookmark))
    assert bookmark_states[bottom_bookmark].local_target is not None
    assert bookmark_states[stale_bookmark].local_target is None


def test_import_current_rejects_cache_only_linkage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_import_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    state_before = ReviewStateStore.for_repo(repo).load()
    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    bookmark = state_before.changes[change_id].bookmark
    assert bookmark is not None

    _run(["jj", "bookmark", "forget", bookmark], repo)
    fake_repo.pull_requests.clear()
    _run(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            "-d",
            f"refs/heads/{bookmark}",
        ],
        repo,
    )

    assert _main(repo, config_path, "import", "--current") == 1


def test_import_revset_rejects_generated_bookmarks_without_selected_remote(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_import_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    state_before = ReviewStateStore.for_repo(repo).load()
    stack = JjClient(repo).discover_review_stack()
    bottom_change_id = stack.revisions[0].change_id
    top_change_id = stack.revisions[-1].change_id
    bottom_bookmark = state_before.changes[bottom_change_id].bookmark
    top_bookmark = state_before.changes[top_change_id].bookmark
    assert bottom_bookmark is not None
    assert top_bookmark is not None

    for bookmark in (bottom_bookmark, top_bookmark):
        _run(["jj", "bookmark", "forget", bookmark], repo)
    resolve_state_path(repo).unlink()

    def _no_selected_remote(*args, **kwargs):
        raise CliError("No submit remote configured.")

    monkeypatch.setattr(
        "jj_review.commands.review_state.select_submit_remote",
        _no_selected_remote,
    )

    exit_code = _main(repo, config_path, "import", "--revset", top_change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "has no discoverable review bookmark on the selected remote" in captured.err
    assert ReviewStateStore.for_repo(repo).load().changes == {}
    bookmark_states = JjClient(repo).list_bookmark_states((bottom_bookmark, top_bookmark))
    assert bookmark_states[bottom_bookmark].local_target is None
    assert bookmark_states[top_bookmark].local_target is None


def test_import_head_accepts_exact_custom_remote_branch_name(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_import_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    state_before = ReviewStateStore.for_repo(repo).load()
    stack = JjClient(repo).discover_review_stack()
    bottom_change_id = stack.revisions[0].change_id
    top_change_id = stack.revisions[-1].change_id
    bottom_bookmark = state_before.changes[bottom_change_id].bookmark
    top_bookmark = state_before.changes[top_change_id].bookmark
    assert bottom_bookmark is not None
    assert top_bookmark is not None

    custom_head = "custom/pr-head"
    top_target = _run(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "rev-parse",
            f"refs/heads/{top_bookmark}",
        ],
        repo,
    ).stdout.strip()
    _run(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            f"refs/heads/{custom_head}",
            top_target,
        ],
        repo,
    )
    _run(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            "-d",
            f"refs/heads/{top_bookmark}",
        ],
        repo,
    )
    fake_repo.pull_requests.clear()
    for bookmark in (bottom_bookmark, top_bookmark):
        _run(["jj", "bookmark", "forget", bookmark], repo)
    resolve_state_path(repo).unlink()

    exit_code = _main(repo, config_path, "import", "--head", custom_head)

    assert exit_code == 0
    state_after = ReviewStateStore.for_repo(repo).load()
    assert state_after.changes[bottom_change_id].bookmark == bottom_bookmark
    assert state_after.changes[top_change_id].bookmark == custom_head
    bookmark_states = JjClient(repo).list_bookmark_states((bottom_bookmark, custom_head))
    assert bookmark_states[bottom_bookmark].local_target is not None
    assert bookmark_states[custom_head].local_target is not None


def _configure_import_environment(
    monkeypatch,
    tmp_path: Path,
    fake_repo: FakeGithubRepository,
) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    config_path = _write_config(tmp_path, fake_repo)
    app = create_app(FakeGithubState.single_repository(fake_repo))

    def build_github_client(*, base_url: str) -> GithubClient:
        return GithubClient(
            base_url=base_url,
            transport=httpx.ASGITransport(app=app),
        )

    monkeypatch.setattr(
        "jj_review.commands.submit._build_github_client",
        build_github_client,
    )
    monkeypatch.setattr(
        "jj_review.commands.review_state._build_github_client",
        build_github_client,
    )
    monkeypatch.setattr(
        "jj_review.commands.import_._build_github_client",
        build_github_client,
    )
    return config_path


def _init_repo(tmp_path: Path) -> tuple[Path, FakeGithubRepository]:
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
    _run(["jj", "git", "push", "--remote", "origin", "--bookmark", "main"], repo)
    return repo, fake_repo


def _init_repo_without_remote(tmp_path: Path) -> tuple[Path, FakeGithubRepository]:
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
    return repo, fake_repo


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


def _main(repo: Path, config_path: Path, command: str, *command_args: str) -> int:
    argv = ["--config", str(config_path), "--repository", str(repo), command]
    argv.extend(command_args)
    return main(argv)


def _write_config(
    tmp_path: Path,
    fake_repo: FakeGithubRepository,
) -> Path:
    config_path = tmp_path / "config-home" / "jj-review" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    _write_file(
        config_path,
        "\n".join(
            [
                "[repo]",
                'github_host = "github.test"',
                f'github_owner = "{fake_repo.owner}"',
                f'github_repo = "{fake_repo.name}"',
            ]
        )
        + "\n",
    )
    return config_path


def _write_file(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
