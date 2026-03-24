from __future__ import annotations

import subprocess
from pathlib import Path

import httpx

from jj_review.cache import ReviewStateStore
from jj_review.cli import main
from jj_review.github.client import GithubClient
from jj_review.jj import JjClient
from jj_review.testing.fake_github import (
    FakeGithubRepository,
    FakeGithubState,
    create_app,
    initialize_bare_repository,
)


def test_unlink_detaches_selected_change_and_preserves_bookmark(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    state_store = ReviewStateStore.for_repo(repo)
    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    bookmark = state_store.load().changes[change_id].bookmark

    assert _main(repo, config_path, "unlink", change_id) == 0
    detached_change = state_store.load().changes[change_id]

    assert detached_change.bookmark == bookmark
    assert detached_change.detached_at is not None
    assert detached_change.link_state == "detached"
    assert detached_change.pr_number is None
    assert detached_change.pr_review_decision is None
    assert detached_change.pr_state is None
    assert detached_change.pr_url is None
    assert detached_change.stack_comment_id is None


def test_unlink_is_idempotent_for_already_detached_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id

    assert _main(repo, config_path, "unlink", change_id) == 0
    capsys.readouterr()

    exit_code = _main(repo, config_path, "unlink", change_id)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "is already detached from managed review" in captured.out


def test_unlink_rejects_change_without_active_review_linkage(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    exit_code = _main(repo, config_path, "unlink", "--current")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "no active managed review linkage to unlink" in captured.err


def test_unlink_accepts_cached_active_linkage_without_live_remote_or_pr(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    state_store = ReviewStateStore.for_repo(repo)
    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    bookmark = state_store.load().changes[change_id].bookmark
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

    assert _main(repo, config_path, "unlink", change_id) == 0
    detached_change = state_store.load().changes[change_id]

    assert detached_change.bookmark == bookmark
    assert detached_change.link_state == "detached"
    assert detached_change.pr_number is None


def test_status_fetch_reports_detached_state_without_reattaching(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    assert _main(repo, config_path, "unlink", change_id) == 0
    capsys.readouterr()

    exit_code = _main(repo, config_path, "status", "--fetch", change_id)
    captured = capsys.readouterr()
    detached_change = ReviewStateStore.for_repo(repo).load().changes[change_id]

    assert exit_code == 0
    assert "detached" in captured.out
    assert detached_change.link_state == "detached"
    assert detached_change.pr_number is None


def test_submit_rejects_detached_change_until_relink(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    assert _main(repo, config_path, "unlink", change_id) == 0
    capsys.readouterr()

    exit_code = _main(repo, config_path, "submit", "--current")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "detached from managed review" in captured.err
    assert "relink" in captured.err


def test_relink_clears_detached_marker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    state_store = ReviewStateStore.for_repo(repo)
    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id

    assert _main(repo, config_path, "unlink", change_id) == 0
    assert _main(repo, config_path, "relink", "1", change_id) == 0
    relinked_change = state_store.load().changes[change_id]

    assert relinked_change.link_state == "active"
    assert relinked_change.detached_at is None
    assert relinked_change.pr_number == 1


def test_land_rejects_detached_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    assert _main(repo, config_path, "unlink", change_id) == 0
    capsys.readouterr()

    exit_code = _main(repo, config_path, "land", "--current")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "detached from managed review" in captured.out


def test_import_preserves_detached_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    state_store = ReviewStateStore.for_repo(repo)
    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    bookmark = state_store.load().changes[change_id].bookmark
    assert bookmark is not None

    assert _main(repo, config_path, "unlink", change_id) == 0
    _run(["jj", "bookmark", "forget", bookmark], repo)

    assert _main(repo, config_path, "import", "--current") == 0
    imported_change = state_store.load().changes[change_id]

    assert imported_change.bookmark == bookmark
    assert imported_change.link_state == "detached"
    assert imported_change.pr_number is None


def test_cleanup_deletes_managed_comment_for_detached_pull_request(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    state_store = ReviewStateStore.for_repo(repo)
    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    bookmark = state_store.load().changes[change_id].bookmark
    assert bookmark is not None
    assert _issue_comments(fake_repo, 2)

    assert _main(repo, config_path, "unlink", change_id) == 0
    capsys.readouterr()

    exit_code = _main(repo, config_path, "cleanup", "--apply")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "[applied] stack comment: delete managed stack comment #2 from PR #2" in (
        captured.out
    )
    detached_change = state_store.load().changes[change_id]
    assert detached_change.link_state == "detached"
    assert detached_change.pr_number is None
    assert detached_change.stack_comment_id is None
    assert _issue_comments(fake_repo, 2) == []


def _configure_environment(
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

    monkeypatch.setattr("jj_review.commands.submit._build_github_client", build_github_client)
    monkeypatch.setattr("jj_review.commands.relink._build_github_client", build_github_client)
    monkeypatch.setattr("jj_review.commands.close._build_github_client", build_github_client)
    monkeypatch.setattr("jj_review.commands.cleanup._build_github_client", build_github_client)
    monkeypatch.setattr("jj_review.commands.import_._build_github_client", build_github_client)
    monkeypatch.setattr("jj_review.commands.land._build_github_client", build_github_client)
    monkeypatch.setattr(
        "jj_review.commands.review_state._build_github_client",
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


def _commit(repo: Path, message: str, filename: str) -> None:
    _write_file(repo / filename, f"{message}\n")
    _run(["jj", "commit", "-m", message], repo)


def _issue_comments(fake_repo: FakeGithubRepository, issue_number: int):
    return fake_repo.issue_comments.get(issue_number, [])


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
