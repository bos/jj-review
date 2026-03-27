from __future__ import annotations

from pathlib import Path

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
    read_remote_ref as _read_remote_ref,
)
from .submit_command_helpers import (
    run as _run,
)
from .submit_command_helpers import (
    run_main as _main,
)
from .submit_command_helpers import (
    write_file_contents as _write_file,
)


def test_relink_repairs_existing_pull_request_link_for_rewritten_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    manual_bookmark = "review/manual-feature-1"
    _run(["jj", "bookmark", "create", manual_bookmark, "-r", change_id], repo)
    _run(["jj", "git", "push", "--remote", "origin", "--bookmark", manual_bookmark], repo)
    fake_repo.create_pull_request(
        base_ref="main",
        body="manual body",
        head_ref=manual_bookmark,
        title="manual title",
    )
    _run(["jj", "bookmark", "forget", manual_bookmark], repo)
    _run(
        ["jj", "describe", "--ignore-immutable", "-r", change_id, "-m", "feature 1 relinked"],
        repo,
    )

    exit_code = _main(
        repo,
        config_path,
        "relink",
        "https://github.test/octo-org/stacked-review/pull/1",
        change_id,
    )
    captured = capsys.readouterr()
    relinked_state = ReviewStateStore.for_repo(repo).load()

    assert exit_code == 0
    assert "Relinked PR #1" in captured.out
    assert relinked_state.changes[change_id].bookmark == manual_bookmark
    assert relinked_state.changes[change_id].pr_number == 1
    assert relinked_state.changes[change_id].pr_state == "open"
    assert relinked_state.changes[change_id].pr_url == (
        "https://github.test/octo-org/stacked-review/pull/1"
    )

    exit_code = _main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()
    rewritten_stack = JjClient(repo).discover_review_stack(change_id)

    assert exit_code == 0
    assert "PR #1 updated" in captured.out
    assert set(fake_repo.pull_requests) == {1}
    assert fake_repo.pull_requests[1].title == "feature 1 relinked"
    assert (
        _read_remote_ref(fake_repo.git_dir, manual_bookmark)
        == rewritten_stack.revisions[-1].commit_id
    )

def test_relink_reports_missing_pull_request_without_traceback(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    exit_code = _main(repo, config_path, "relink", "--current", "999")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Could not load pull request #999" in captured.err
    assert "Traceback" not in captured.err

def test_relink_rejects_existing_local_bookmark_on_different_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _write_file(repo / "feature-2.txt", "feature 2\n")
    _run(["jj", "commit", "-m", "feature 2"], repo)

    stack = JjClient(repo).discover_review_stack()
    bottom_change_id = stack.revisions[0].change_id
    bottom_commit_id = stack.revisions[0].commit_id
    top_change_id = stack.revisions[-1].change_id
    manual_bookmark = "review/manual-feature-1"
    _run(["jj", "bookmark", "create", manual_bookmark, "-r", bottom_change_id], repo)
    _run(["jj", "git", "push", "--remote", "origin", "--bookmark", manual_bookmark], repo)
    fake_repo.create_pull_request(
        base_ref="main",
        body="manual body",
        head_ref=manual_bookmark,
        title="manual title",
    )

    exit_code = _main(repo, config_path, "relink", "1", top_change_id)
    captured = capsys.readouterr()
    bookmark_state = JjClient(repo).get_bookmark_state(manual_bookmark)

    assert exit_code == 1
    assert "already points to a different revision" in captured.err
    assert bookmark_state.local_target == bottom_commit_id

def test_relink_rejects_closed_pull_request(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    manual_bookmark = "review/manual-feature-1"
    _run(["jj", "bookmark", "create", manual_bookmark, "-r", change_id], repo)
    _run(["jj", "git", "push", "--remote", "origin", "--bookmark", manual_bookmark], repo)
    fake_repo.create_pull_request(
        base_ref="main",
        body="manual body",
        head_ref=manual_bookmark,
        title="manual title",
    )
    fake_repo.pull_requests[1].state = "closed"

    exit_code = _main(repo, config_path, "relink", "1", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "is not open" in captured.err

def test_relink_rejects_cross_repository_pull_request_head(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    manual_bookmark = "review/manual-feature-1"
    _run(["jj", "bookmark", "create", manual_bookmark, "-r", change_id], repo)
    _run(["jj", "git", "push", "--remote", "origin", "--bookmark", manual_bookmark], repo)
    fake_repo.create_pull_request(
        base_ref="main",
        body="manual body",
        head_ref=manual_bookmark,
        title="manual title",
    )
    fake_repo.pull_requests[1].head_label = f"someone-else:{manual_bookmark}"

    exit_code = _main(repo, config_path, "relink", "1", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "same-repository pull request branches" in captured.err

def test_relink_rejects_pull_request_with_missing_remote_head_branch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    manual_bookmark = "review/manual-feature-1"
    _run(["jj", "bookmark", "create", manual_bookmark, "-r", change_id], repo)
    _run(["jj", "git", "push", "--remote", "origin", "--bookmark", manual_bookmark], repo)
    fake_repo.create_pull_request(
        base_ref="main",
        body="manual body",
        head_ref=manual_bookmark,
        title="manual title",
    )
    _run(["jj", "bookmark", "forget", manual_bookmark], repo)
    _run(
        ["jj", "describe", "--ignore-immutable", "-r", change_id, "-m", "feature 1 relinked"],
        repo,
    )
    _run(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            "-d",
            f"refs/heads/{manual_bookmark}",
        ],
        fake_repo.git_dir.parent,
    )

    exit_code = _main(repo, config_path, "relink", "1", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "does not exist" in captured.err

def test_relink_clears_unlinked_state(
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

    exit_code = _main(repo, config_path, "relink", "1", change_id)
    captured = capsys.readouterr()
    relinked_change = ReviewStateStore.for_repo(repo).load().changes[change_id]

    assert exit_code == 0
    assert "Relinked PR #1" in captured.out
    assert relinked_change.unlinked_at is None
    assert relinked_change.link_state == "active"
    assert relinked_change.pr_number == 1
    assert relinked_change.pr_state == "open"

def test_relink_writes_and_deletes_intent_file_on_success(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """relink deletes its intent file on success."""
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    manual_bookmark = "review/manual-feature-1"
    _run(["jj", "bookmark", "create", manual_bookmark, "-r", change_id], repo)
    _run(["jj", "git", "push", "--remote", "origin", "--bookmark", manual_bookmark], repo)
    fake_repo.create_pull_request(
        base_ref="main",
        body="manual body",
        head_ref=manual_bookmark,
        title="manual title",
    )
    _run(["jj", "bookmark", "forget", manual_bookmark], repo)
    _run(
        ["jj", "describe", "--ignore-immutable", "-r", change_id, "-m", "feature 1 relinked"],
        repo,
    )

    exit_code = _main(repo, config_path, "relink", "1", change_id)
    capsys.readouterr()

    assert exit_code == 0
    state_dir = resolve_state_path(repo).parent
    intent_files = list(state_dir.glob("incomplete-*.toml"))
    assert intent_files == [], f"Expected no intent files after success, found: {intent_files}"
