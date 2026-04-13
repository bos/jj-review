from __future__ import annotations

from pathlib import Path

from jj_review.cache import ReviewStateStore, resolve_state_path
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.intent import write_new_intent
from jj_review.jj import JjClient
from jj_review.models.intent import CloseIntent

from ..support.fake_github import FakeGithubState, create_app
from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo,
    run_command,
)
from .submit_command_helpers import (
    configure_submit_environment,
    issue_comments,
    patch_github_client_builders,
    read_remote_ref,
    remote_refs,
    run_main,
)


def test_close_apply_closes_pull_request_and_retires_active_state(
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

    exit_code = run_main(repo, config_path, "close", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "Applied close actions:" in captured.out
    assert fake_repo.pull_requests[1].state == "closed"
    assert refreshed_state.changes[change_id].pr_state == "closed"
    assert refreshed_state.changes[change_id].pr_review_decision is None
    assert refreshed_state.changes[change_id].stack_comment_id is None
    assert issue_comments(fake_repo, 1) == []

def test_close_dry_run_leaves_remote_state_unchanged_and_reports_planned_actions(
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

    exit_code = run_main(repo, config_path, "close", "--dry-run", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "Planned close actions:" in captured.out
    assert fake_repo.pull_requests[1].state == "open"
    assert refreshed_state == initial_state
    assert issue_comments(fake_repo, 1) == []

def test_close_apply_reports_blocked_when_github_is_unavailable(
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
    initial_state = ReviewStateStore.for_repo(repo).load()
    app = create_app(FakeGithubState.single_repository(fake_repo))

    class OfflineGithubClient(GithubClient):
        async def list_pull_requests(self, owner, repo, *, head, state="all"):
            raise GithubClientError("Connection refused")

        async def list_pull_requests_by_head_refs(self, owner, repo, *, head_refs):
            raise GithubClientError("Connection refused")

        async def get_pull_requests_by_head_refs(self, owner, repo, *, head_refs):
            raise GithubClientError("Connection refused")

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_review.commands.close", "jj_review.review_inspection"),
        client_type=OfflineGithubClient,
    )

    exit_code = run_main(repo, config_path, "close", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Close blocked:" in captured.out
    assert "Applied close actions:" not in captured.out
    assert "cannot close pull requests tracked by jj-review without live GitHub state" in (
        captured.out
    )
    assert ReviewStateStore.for_repo(repo).load() == initial_state

def test_close_apply_cleanup_deletes_owned_bookmarks_and_comments(
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
    bookmark = ReviewStateStore.for_repo(repo).load().changes[change_id].bookmark
    assert bookmark is not None
    state_store = ReviewStateStore.for_repo(repo)
    action_order: list[str] = []
    original_delete_remote_bookmarks = JjClient.delete_remote_bookmarks
    original_forget_bookmarks = JjClient.forget_bookmarks

    def tracking_delete_remote_bookmarks(
        self,
        *,
        remote: str,
        deletions,
        fetch=True,
    ) -> None:
        action_order.append("remote")
        return original_delete_remote_bookmarks(
            self,
            remote=remote,
            deletions=deletions,
            fetch=fetch,
        )

    def tracking_forget_bookmarks(self, bookmarks) -> None:
        action_order.append("local")
        return original_forget_bookmarks(self, bookmarks)

    monkeypatch.setattr(
        JjClient,
        "delete_remote_bookmarks",
        tracking_delete_remote_bookmarks,
    )
    monkeypatch.setattr(
        JjClient,
        "forget_bookmarks",
        tracking_forget_bookmarks,
    )

    exit_code = run_main(repo, config_path, "close", "--cleanup", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "Applied close actions:" in captured.out
    assert fake_repo.pull_requests[1].state == "closed"
    assert refreshed_state.changes[change_id].pr_state == "closed"
    assert refreshed_state.changes[change_id].stack_comment_id is None
    assert issue_comments(fake_repo, 1) == []
    assert bookmark not in remote_refs(fake_repo.git_dir)
    assert JjClient(repo).get_bookmark_state(bookmark).local_target is None
    assert action_order == ["remote", "local"]

def test_close_apply_rerun_is_idempotent(
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

    first_exit_code = run_main(repo, config_path, "close", change_id)
    capsys.readouterr()
    first_state = state_store.load()
    del fake_repo.pull_requests[1]

    second_exit_code = run_main(repo, config_path, "close", change_id)
    captured = capsys.readouterr()
    second_state = state_store.load()

    assert first_exit_code == 0
    assert second_exit_code == 0
    assert "No close actions were needed for the selected stack." in captured.out
    assert first_state.changes[change_id].pr_state == "closed"
    assert second_state.changes[change_id].pr_state == "closed"
    assert 1 not in fake_repo.pull_requests

def test_close_apply_cleanup_rerun_completes_after_prior_close_when_pr_is_missing(
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

    assert run_main(repo, config_path, "close", change_id) == 0
    capsys.readouterr()
    del fake_repo.pull_requests[1]

    exit_code = run_main(repo, config_path, "close", "--cleanup", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "Applied close actions:" in captured.out
    assert refreshed_state.changes[change_id].pr_state == "closed"
    assert refreshed_state.changes[change_id].stack_comment_id is None
    assert issue_comments(fake_repo, 1) == []
    assert bookmark not in remote_refs(fake_repo.git_dir)
    assert JjClient(repo).get_bookmark_state(bookmark).local_target is None

def test_close_apply_blocks_when_github_no_longer_reports_the_cached_pull_request(
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
    del fake_repo.pull_requests[1]

    exit_code = run_main(repo, config_path, "close", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "GitHub no longer reports a pull request" in captured.out
    assert state_store.load() == initial_state

def test_close_apply_checkpoints_prior_progress_before_later_block(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    first_change_id = stack.revisions[0].change_id
    head_change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    state_dir = resolve_state_path(repo).parent
    initial_state = state_store.load()
    first_bookmark = initial_state.changes[first_change_id].bookmark
    head_pr_number = initial_state.changes[head_change_id].pr_number
    assert first_bookmark is not None
    assert head_pr_number is not None

    fake_repo.create_pull_request(
        base_ref="main",
        body="duplicate",
        head_ref=first_bookmark,
        title="feature 1 duplicate",
    )

    first_exit_code = run_main(repo, config_path, "close", head_change_id)
    first_run = capsys.readouterr()
    checkpointed_state = state_store.load()

    second_exit_code = run_main(repo, config_path, "close", head_change_id)
    second_run = capsys.readouterr()

    assert first_exit_code == 1
    assert second_exit_code == 1
    assert "Close blocked:" in first_run.out
    assert checkpointed_state.changes[first_change_id].pr_state == "open"
    assert checkpointed_state.changes[head_change_id].pr_state == "closed"
    assert fake_repo.pull_requests[1].state == "open"
    assert fake_repo.pull_requests[2].state == "closed"
    assert list(state_dir.glob("incomplete-*.json")) == []
    assert "previous close was interrupted" not in second_run.out
    assert f"close PR #{head_pr_number}" not in second_run.out

def test_close_apply_cleanup_rechecks_cached_comment_ownership_when_pr_is_missing(
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

    assert run_main(repo, config_path, "close", change_id) == 0
    capsys.readouterr()

    manual_comment = fake_repo.create_issue_comment(body="manual note", issue_number=1)
    state = state_store.load()
    cached_change = state.changes[change_id]
    state_store.save(
        state.model_copy(
            update={
                "changes": {
                    **state.changes,
                    change_id: cached_change.model_copy(
                        update={"stack_comment_id": manual_comment.id}
                    ),
                }
            }
        )
    )
    del fake_repo.pull_requests[1]

    exit_code = run_main(repo, config_path, "close", "--cleanup", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "cannot delete saved stack summary comment" in captured.out
    assert "does not belong to `jj-review`" in captured.out
    assert manual_comment in issue_comments(fake_repo, 1)

def test_close_apply_cleanup_keeps_comment_cleanup_after_bookmark_block(
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
    bookmark = ReviewStateStore.for_repo(repo).load().changes[change_id].bookmark
    assert bookmark is not None
    initial_remote_target = read_remote_ref(fake_repo.git_dir, bookmark)
    run_command(["jj", "bookmark", "move", "--allow-backwards", bookmark, "--to", "main"], repo)

    exit_code = run_main(repo, config_path, "close", "--cleanup", change_id)
    captured = capsys.readouterr()
    local_target = JjClient(repo).get_bookmark_state(bookmark).local_target

    assert exit_code == 1
    assert "Close blocked:" in captured.out
    assert issue_comments(fake_repo, 1) == []
    assert local_target == read_remote_ref(fake_repo.git_dir, "main")
    assert read_remote_ref(fake_repo.git_dir, bookmark) == initial_remote_target
    assert fake_repo.pull_requests[1].state == "closed"

def test_close_apply_closes_discovered_pull_request_after_sparse_state_loss(
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
    resolve_state_path(repo).unlink()

    exit_code = run_main(repo, config_path, "close", change_id)
    captured = capsys.readouterr()
    refreshed_state = ReviewStateStore.for_repo(repo).load()

    assert exit_code == 0
    assert "Applied close actions:" in captured.out
    assert fake_repo.pull_requests[1].state == "closed"
    assert refreshed_state.changes[change_id].pr_number == 1
    assert refreshed_state.changes[change_id].pr_state == "closed"

def test_close_apply_cleanup_exits_nonzero_when_cleanup_is_blocked(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    cached_change = state_store.load().changes[change_id]
    state_store.save(
        state_store.load().model_copy(
            update={
                "changes": {
                    **state_store.load().changes,
                    change_id: cached_change.model_copy(update={"stack_comment_id": None}),
                }
            }
        )
    )
    fake_repo.create_issue_comment(body="<!-- jj-review-stack -->\nextra", issue_number=2)

    exit_code = run_main(repo, config_path, "close", "--cleanup", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Close blocked:" in captured.out
    assert "[blocked] stack summary comment:" in captured.out
    assert fake_repo.pull_requests[2].state == "closed"


def test_close_retires_covered_interrupted_close_intent(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    first = stack.revisions[0]
    second = stack.revisions[1]
    state_dir = resolve_state_path(repo).parent
    old_intent_path = write_new_intent(
        state_dir,
        CloseIntent(
            kind="close",
            pid=99999999,
            label="close on @",
            display_revset="@",
            ordered_change_ids=(first.change_id,),
            ordered_commit_ids=(first.commit_id,),
            cleanup=False,
            started_at="2026-01-01T00:00:00+00:00",
        ),
    )

    exit_code = run_main(repo, config_path, "close")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Continuing interrupted" not in captured.out
    assert "all included in the current stack" in captured.out
    assert not old_intent_path.exists()
    assert list(state_dir.glob("incomplete-*.json")) == []


def test_close_continues_exact_interrupted_close_on_multi_revision_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    first = stack.revisions[0]
    second = stack.revisions[1]
    state_dir = resolve_state_path(repo).parent
    old_intent_path = write_new_intent(
        state_dir,
        CloseIntent(
            kind="close",
            pid=99999999,
            label="close on @",
            display_revset="@",
            ordered_change_ids=(first.change_id, second.change_id),
            ordered_commit_ids=(first.commit_id, second.commit_id),
            cleanup=False,
            started_at="2026-01-01T00:00:00+00:00",
        ),
    )

    exit_code = run_main(repo, config_path, "close")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert (
        f"Continuing interrupted close for {second.change_id[:8]} (from @)"
        in captured.out
    )
    assert not old_intent_path.exists()


def test_close_does_not_resume_or_retire_interrupted_cleanup_close(
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
    revision = stack.revisions[-1]
    state_dir = resolve_state_path(repo).parent
    old_intent_path = write_new_intent(
        state_dir,
        CloseIntent(
            kind="close",
            pid=99999999,
            label="close --cleanup on @",
            display_revset="@",
            ordered_change_ids=(revision.change_id,),
            ordered_commit_ids=(revision.commit_id,),
            cleanup=True,
            started_at="2026-01-01T00:00:00+00:00",
        ),
    )

    exit_code = run_main(repo, config_path, "close")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Continuing interrupted" not in captured.out
    assert "plain close does not finish cleanup" in captured.out
    assert old_intent_path.exists()


def test_cleanup_close_supersedes_plain_interrupted_close(
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
    revision = stack.revisions[-1]
    state_dir = resolve_state_path(repo).parent
    old_intent_path = write_new_intent(
        state_dir,
        CloseIntent(
            kind="close",
            pid=99999999,
            label="close on @",
            display_revset="@",
            ordered_change_ids=(revision.change_id,),
            ordered_commit_ids=(revision.commit_id,),
            cleanup=False,
            started_at="2026-01-01T00:00:00+00:00",
        ),
    )

    exit_code = run_main(repo, config_path, "close", "--cleanup")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Continuing interrupted" not in captured.out
    assert "covered by this close --cleanup run" in captured.out
    assert not old_intent_path.exists()
