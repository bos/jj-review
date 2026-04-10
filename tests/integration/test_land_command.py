from __future__ import annotations

import os
from pathlib import Path

import pytest

from jj_review.cache import ReviewStateStore, resolve_state_path
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.jj import JjClient
from jj_review.jj.client import JjCommandError

from ..support.fake_github import FakeGithubState, create_app
from ..support.integration_helpers import (
    commit_file as _commit,
    init_fake_github_repo as _init_repo,
)
from .submit_command_helpers import (
    approve_pull_requests as _approve_pull_requests,
    configure_submit_environment as _configure_submit_environment,
    patch_github_client_builders as _patch_github_client_builders,
    read_remote_ref as _read_remote_ref,
    run_main as _main,
)


def test_land_blocks_unlinked_change(
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

    exit_code = _main(repo, config_path, "land", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Land blocked:" in captured.out
    assert "unlinked from review tracking" in captured.out

def test_land_previews_and_finalizes_maximal_ready_prefix(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    for index in range(3):
        _commit(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")

    assert _main(repo, config_path, "submit") == 0
    capsys.readouterr()
    _approve_pull_requests(fake_repo, 1, 2)

    stack = JjClient(repo).discover_review_stack()
    state_store = ReviewStateStore.for_repo(repo)
    submitted_state = state_store.load()
    change_id_1 = stack.revisions[0].change_id
    change_id_2 = stack.revisions[1].change_id
    change_id_3 = stack.revisions[2].change_id
    bookmark_1 = submitted_state.changes[change_id_1].bookmark
    bookmark_2 = submitted_state.changes[change_id_2].bookmark
    if bookmark_1 is None or bookmark_2 is None:
        raise AssertionError("Expected saved bookmarks after submit.")

    fake_repo.pull_requests[3].state = "closed"

    preview_exit_code = _main(repo, config_path, "land", "--dry-run")
    preview = capsys.readouterr()

    assert preview_exit_code == 0
    assert "Selected remote: origin" in preview.out
    assert "Planned land actions:" in preview.out
    assert "push main to feature 2" in preview.out
    assert "finalize PR #1" in preview.out
    assert "finalize PR #2" in preview.out
    assert "stop before feature 3" in preview.out
    assert "cleanup --restack @-" in preview.out
    assert "submit @-" in preview.out

    apply_exit_code = _main(repo, config_path, "land")
    applied = capsys.readouterr()

    assert apply_exit_code == 0
    assert "Finalizing PR #1 for feature 1" in applied.out
    assert "Finalizing PR #2 for feature 2" in applied.out
    assert "Applied land actions:" in applied.out
    assert "cleanup --restack @-" in applied.out
    assert "submit @-" in applied.out
    assert _read_remote_ref(fake_repo.git_dir, "main") == stack.revisions[1].commit_id
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[1].merged_at is not None
    assert fake_repo.pull_requests[2].state == "closed"
    assert fake_repo.pull_requests[2].merged_at is not None
    assert fake_repo.pull_requests[2].base_ref == "main"
    assert fake_repo.pull_requests[3].state == "closed"
    assert _read_remote_ref(fake_repo.git_dir, bookmark_1) == stack.revisions[0].commit_id
    assert _read_remote_ref(fake_repo.git_dir, bookmark_2) == stack.revisions[1].commit_id

    landed_state = state_store.load()
    assert landed_state.changes[change_id_1].pr_state == "merged"
    assert landed_state.changes[change_id_1].stack_comment_id is None
    assert landed_state.changes[change_id_2].pr_state == "merged"
    assert landed_state.changes[change_id_2].stack_comment_id is None
    assert landed_state.changes[change_id_3].pr_state == "closed"

def test_land_blocks_unapproved_prefix_by_default(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit") == 0
    capsys.readouterr()

    exit_code = _main(repo, config_path, "land")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Land blocked:" in captured.out
    assert "PR #1 is not approved" in captured.out

def test_land_bypass_readiness_previews_and_finalizes_unapproved_change(
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

    preview_exit_code = _main(
        repo,
        config_path,
        "land",
        "--bypass-readiness",
        "--dry-run",
    )
    preview = capsys.readouterr()

    assert preview_exit_code == 0
    assert "Planned land actions:" in preview.out
    assert "push main to feature 1" in preview.out

    apply_exit_code = _main(
        repo,
        config_path,
        "land",
        "--bypass-readiness",
    )
    applied = capsys.readouterr()

    assert apply_exit_code == 0
    assert "Applied land actions:" in applied.out
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[1].merged_at is not None
    assert _read_remote_ref(fake_repo.git_dir, "main") == stack.revisions[0].commit_id

@pytest.mark.parametrize(
    ("push_error", "expected_exit_code", "expected_error"),
    [
        (JjCommandError("simulated trunk push failure"), 1, "simulated trunk push failure"),
        (KeyboardInterrupt(), 130, "Interrupted."),
    ],
)
def test_land_restores_local_trunk_bookmark_when_push_does_not_complete(
    tmp_path: Path,
    monkeypatch,
    capsys,
    push_error: BaseException,
    expected_exit_code: int,
    expected_error: str,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    for index in range(2):
        _commit(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")

    assert _main(repo, config_path, "submit") == 0
    capsys.readouterr()
    _approve_pull_requests(fake_repo, 1, 2)

    client = JjClient(repo)
    trunk_before = client.get_bookmark_state("main").local_target
    remote_before = _read_remote_ref(fake_repo.git_dir, "main")

    def fail_push_bookmarks(self, *, remote: str, bookmarks) -> None:
        raise push_error

    monkeypatch.setattr(JjClient, "push_bookmarks", fail_push_bookmarks)

    exit_code = _main(repo, config_path, "land")
    captured = capsys.readouterr()

    assert exit_code == expected_exit_code
    assert expected_error in captured.err
    assert JjClient(repo).get_bookmark_state("main").local_target == trunk_before
    assert _read_remote_ref(fake_repo.git_dir, "main") == remote_before

def test_land_replans_after_interrupted_push_when_landable_prefix_changes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    for index in range(2):
        _commit(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")

    assert _main(repo, config_path, "submit") == 0
    capsys.readouterr()
    _approve_pull_requests(fake_repo, 1, 2)
    initial_stack = JjClient(repo).discover_review_stack()
    first_landable_commit_id = initial_stack.revisions[0].commit_id

    push_calls = 0
    original_push_bookmarks = JjClient.push_bookmarks

    def fail_first_push_bookmarks(self, *, remote: str, bookmarks) -> None:
        nonlocal push_calls
        push_calls += 1
        if push_calls == 1:
            raise JjCommandError("simulated trunk push failure")
        original_push_bookmarks(self, remote=remote, bookmarks=bookmarks)

    monkeypatch.setattr(JjClient, "push_bookmarks", fail_first_push_bookmarks)

    first_exit_code = _main(repo, config_path, "land")
    first_run = capsys.readouterr()

    assert first_exit_code == 1
    assert "simulated trunk push failure" in first_run.err
    [intent_path] = resolve_state_path(repo).parent.glob("incomplete-*.toml")
    intent_text = intent_path.read_text(encoding="utf-8")
    intent_path.write_text(
        intent_text.replace(f"pid = {os.getpid()}", "pid = 99999999"),
        encoding="utf-8",
    )

    fake_repo.pull_requests[2].state = "closed"

    second_exit_code = _main(repo, config_path, "land")
    second_run = capsys.readouterr()

    assert second_exit_code == 0
    assert "simulated trunk push failure" in first_run.err
    assert "Resuming interrupted" not in second_run.out
    assert _read_remote_ref(fake_repo.git_dir, "main") == first_landable_commit_id

def test_land_resumes_after_trunk_push_interruption(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    for index in range(2):
        _commit(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")

    assert _main(repo, config_path, "submit") == 0
    capsys.readouterr()
    _approve_pull_requests(fake_repo, 1, 2)
    submitted_stack = JjClient(repo).discover_review_stack()
    first_change_id = submitted_stack.revisions[0].change_id
    second_change_id = submitted_stack.revisions[1].change_id
    landed_commit_id = submitted_stack.revisions[1].commit_id

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class FailingFinalizeClient(GithubClient):
        get_pull_request_calls = 0

        async def get_pull_request(self, owner: str, repo: str, *, pull_number: int):
            type(self).get_pull_request_calls += 1
            if type(self).get_pull_request_calls == 1:
                raise GithubClientError("simulated PR finalization failure")
            return await super().get_pull_request(owner, repo, pull_number=pull_number)

    _patch_github_client_builders(
        monkeypatch,
        app=app,
        modules=("jj_review.commands.land",),
        client_type=FailingFinalizeClient,
    )

    first_exit_code = _main(repo, config_path, "land")
    first_run = capsys.readouterr()

    assert first_exit_code == 1
    assert "simulated PR finalization failure" in first_run.err
    assert _read_remote_ref(fake_repo.git_dir, "main") == landed_commit_id
    [intent_path] = resolve_state_path(repo).parent.glob("incomplete-*.toml")
    intent_text = intent_path.read_text(encoding="utf-8")
    intent_path.write_text(
        intent_text.replace(f"pid = {os.getpid()}", "pid = 99999999"),
        encoding="utf-8",
    )

    _patch_github_client_builders(
        monkeypatch,
        app=app,
        modules=("jj_review.commands.land",),
    )

    second_exit_code = _main(repo, config_path, "land")
    second_run = capsys.readouterr()

    assert second_exit_code == 0
    assert "Resuming interrupted land on @-" in second_run.out
    state = ReviewStateStore.for_repo(repo).load()
    assert _read_remote_ref(fake_repo.git_dir, "main") == landed_commit_id
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[1].merged_at is not None
    assert fake_repo.pull_requests[2].state == "closed"
    assert fake_repo.pull_requests[2].merged_at is not None
    assert state.changes[first_change_id].pr_state == "merged"
    assert state.changes[second_change_id].pr_state == "merged"
    assert list(resolve_state_path(repo).parent.glob("incomplete-*.toml")) == []
