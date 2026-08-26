from __future__ import annotations

from pathlib import Path

from jj_stack.errors import EXIT_GITHUB
from jj_stack.state.store import TrackingStore

from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo,
    init_fake_github_repo_with_manual_pr,
    init_fake_github_repo_with_submitted_feature,
    run_command,
    selected_stack,
)
from .submit_command_helpers import (
    configure_submit_environment,
    read_remote_ref,
    run_main,
)


def test_relink_repairs_existing_pr_link_for_rewritten_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_manual_pr(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = selected_stack(repo).changes[-1].change_id
    manual_bookmark = fake_repo.prs[1].head_ref
    run_command(
        ["jj", "describe", "--ignore-immutable", "-r", change_id, "-m", "feature 1 relinked"],
        repo,
    )

    exit_code = run_main(
        repo,
        config_path,
        "relink",
        "https://github.com/octo-org/stacked-prs/pull/1",
        change_id,
    )
    captured = capsys.readouterr()
    relinked_state = TrackingStore.for_repo(repo).load()

    assert exit_code == 0
    assert "Relinked PR #1" in captured.out
    assert relinked_state.pr_identities[change_id].head_ref == manual_bookmark
    assert relinked_state.pr_identities[change_id].pr_number == 1

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()
    rewritten_stack = selected_stack(repo, change_id)

    assert exit_code == 0
    assert "PR #1 unchanged" in captured.out
    assert set(fake_repo.prs) == {1}
    assert fake_repo.prs[1].title == "manual title"
    assert fake_repo.prs[1].body == "manual body"
    assert (
        read_remote_ref(fake_repo.git_dir, manual_bookmark)
        == rewritten_stack.changes[-1].commit_id
    )


def test_relink_replaces_stale_submitted_commit_with_remote_pr_head(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = selected_stack(repo).changes[-1].change_id
    state_store = TrackingStore.for_repo(repo)
    initial_state = state_store.load()
    identity = initial_state.pr_identities[change_id]
    bookmark = identity.head_ref
    remote_pr_head = read_remote_ref(fake_repo.git_dir, bookmark)
    stale_submitted_commit = read_remote_ref(fake_repo.git_dir, "main")
    assert stale_submitted_commit != remote_pr_head
    baseline = initial_state.submitted_baselines[change_id]
    state_store.relink_pr(
        change_id,
        identity=identity,
        baseline=baseline.model_copy(update={"commit_id": stale_submitted_commit}),
    )
    run_command(
        ["jj", "describe", "--ignore-immutable", "-r", change_id, "-m", "feature repaired"],
        repo,
    )

    exit_code = run_main(repo, config_path, "relink", "1", change_id)
    capsys.readouterr()
    relinked_baseline = state_store.load().submitted_baselines[change_id]

    assert exit_code == 0
    assert relinked_baseline.commit_id == remote_pr_head

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "PR #1 updated" in captured.out
    assert set(fake_repo.prs) == {1}


def test_relink_reports_missing_pr_without_traceback(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    change_id = selected_stack(repo).changes[-1].change_id

    exit_code = run_main(repo, config_path, "relink", "999", change_id)
    captured = capsys.readouterr()

    assert exit_code == EXIT_GITHUB
    assert "Could not load pull request #999" in captured.err
    assert "Traceback" not in captured.err


def test_relink_explains_recovery_after_change_id_replacement(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_manual_pr(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    original_change_id = selected_stack(repo).changes[-1].change_id
    run_command(
        ["jj", "new", f"{original_change_id}-", "-m", "replacement feature 1"],
        repo,
    )
    run_command(["jj", "restore", "--from", original_change_id], repo)
    replacement_change_id = selected_stack(repo).changes[-1].change_id
    run_command(["jj", "abandon", original_change_id], repo)

    exit_code = run_main(repo, config_path, "relink", "1", replacement_change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert (
        f"belongs to change {original_change_id[:8]}, not selected change "
        f"{replacement_change_id[:8]}" in captured.err
    )
    assert "jj-stack checkout --pull-request 1" in captured.err
    assert replacement_change_id not in TrackingStore.for_repo(repo).load().pr_identities


def test_relink_rejects_pr_with_missing_remote_head_branch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_manual_pr(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = selected_stack(repo).changes[-1].change_id
    manual_bookmark = fake_repo.prs[1].head_ref
    run_command(
        ["jj", "describe", "--ignore-immutable", "-r", change_id, "-m", "feature 1 relinked"],
        repo,
    )
    run_command(
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

    exit_code = run_main(repo, config_path, "relink", "1", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "does not exist" in captured.err
