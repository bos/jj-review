from __future__ import annotations

from pathlib import Path

from jj_review.github.client import GithubClient, GithubClientError
from jj_review.jj import JjClient
from jj_review.models.intent import SubmitIntent
from jj_review.state.intents import write_new_intent
from jj_review.state.store import ReviewStateStore, resolve_state_path

from ..support.fake_github import FakeGithubState, create_app
from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo,
    run_command,
)
from .submit_command_helpers import (
    configure_submit_environment,
    patch_github_client_builders,
    run_main,
)


def test_status_reports_pull_request_link_without_showing_managed_bookmark(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    exit_code = run_main(repo, config_path, "status")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "feature 1" in captured.out
    assert "PR #1" in captured.out
    assert "Submitted stack (https://github.test/octo-org/stacked-review/pull/1):" in (
        captured.out
    )
    assert "review/feature-1-" not in captured.out


def test_status_truncates_long_unsubmitted_stack_summary(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    for index in range(8):
        commit_file(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")

    exit_code = run_main(repo, config_path, "status")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Unsubmitted stack:" in captured.out
    assert "... 2 changes omitted ..." in captured.out
    assert "feature 4" not in captured.out
    assert "feature 3" in captured.out


def test_status_ignores_off_path_reviewable_child(
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
    feature_1_commit_id = stack.revisions[0].commit_id
    feature_2_commit_id = stack.revisions[-1].commit_id
    run_command(["jj", "new", feature_1_commit_id], repo)
    commit_file(repo, "feature side", "feature-side.txt")
    run_command(["jj", "new", feature_2_commit_id], repo)

    exit_code = run_main(repo, config_path, "status")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "feature 2" in captured.out
    assert "feature 1" in captured.out
    assert "feature side" not in captured.out


def test_status_preserves_remote_observations_when_github_lookup_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class FailingPullRequestLookupClient(GithubClient):
        async def get_pull_requests_by_head_refs(self, owner, repo, *, head_refs):
            raise GithubClientError(
                'GitHub request failed: 404 {"message":"Not Found","documentation_url":"x"}',
                status_code=404,
            )

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_review.review.status",),
        client_type=FailingPullRequestLookupClient,
    )

    exit_code = run_main(repo, config_path, "status")
    captured = capsys.readouterr()
    normalized_err = " ".join(captured.err.split())

    assert exit_code == 1
    assert "GitHub target: octo-org/stacked-review" in normalized_err
    assert "repo not found or inaccessible - check GITHUB_TOKEN or gh auth" in normalized_err
    assert "documentation_url" not in captured.out
    assert "saved PR #1 (open)" in captured.out


def test_status_reports_unknown_when_github_is_unavailable_and_no_cache_exists(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class OfflineGithubClient(GithubClient):
        async def get_pull_requests_by_head_refs(self, owner, repo, *, head_refs):
            raise GithubClientError("Connection refused")

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_review.review.status",),
        client_type=OfflineGithubClient,
    )

    exit_code = run_main(repo, config_path, "status")
    captured = capsys.readouterr()
    normalized_err = " ".join(captured.err.split())

    assert exit_code == 1
    assert "GitHub target: octo-org/stacked-review" in normalized_err
    assert "unavailable - check network connectivity" in normalized_err
    assert "GitHub status unknown" in captured.out


def test_status_exits_nonzero_when_pull_request_lookup_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class FailingPullRequestLookupClient(GithubClient):
        async def get_pull_requests_by_head_refs(self, owner, repo, *, head_refs):
            raise GithubClientError(
                'GitHub request failed: 422 {"message":"Validation Failed"}',
                status_code=422,
            )

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_review.review.status",),
        client_type=FailingPullRequestLookupClient,
    )

    exit_code = run_main(repo, config_path, "status")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "saved PR #1 (open), pull request lookup failed" in captured.out


def test_status_exits_nonzero_when_github_reports_multiple_pull_requests(
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
    fake_repo.create_pull_request(
        base_ref="main",
        body="duplicate",
        head_ref=bookmark,
        title="feature 1 duplicate",
    )

    exit_code = run_main(repo, config_path, "status", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "multiple pull requests" in captured.out
    assert "PR link note:" in captured.out
    assert "refresh remote and GitHub observations" in captured.out
    assert "relink <pr>" in captured.out


def test_status_exits_nonzero_when_github_reports_multiple_stack_comments(
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

    fake_repo.create_issue_comment(body="<!-- jj-review-stack -->\nextra", issue_number=2)

    exit_code = run_main(repo, config_path, "status")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "multiple `jj-review` stack summary comments" in captured.out


def test_status_fetch_surfaces_unlinked_state_without_repopulating_link(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id
    assert run_main(repo, config_path, "unlink", change_id) == 0
    capsys.readouterr()

    exit_code = run_main(repo, config_path, "status", "--fetch", change_id)
    captured = capsys.readouterr()
    unlinked_change = ReviewStateStore.for_repo(repo).load().changes[change_id]

    assert exit_code == 0
    assert "unlinked PR #1" in captured.out
    assert unlinked_change.link_state == "unlinked"
    assert unlinked_change.pr_number is None
    assert unlinked_change.pr_state is None
    assert unlinked_change.pr_url is None
    assert unlinked_change.stack_comment_id is None


def test_status_refreshes_cached_stack_comment_metadata_after_state_loss(
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
    initial_state = state_store.load()
    state_store.save(
        initial_state.model_copy(
            update={
                "changes": {
                    **initial_state.changes,
                    change_id: initial_state.changes[change_id].model_copy(
                        update={
                            "pr_number": None,
                            "pr_url": None,
                            "stack_comment_id": None,
                        }
                    ),
                }
            }
        )
    )

    exit_code = run_main(repo, config_path, "status", "--fetch", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "PR #2" in captured.out
    assert refreshed_state.changes[change_id].pr_number == 2
    assert refreshed_state.changes[change_id].stack_comment_id == 1


def test_status_refreshes_cached_pull_request_metadata_after_state_loss(
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
    bookmark = initial_state.changes[change_id].bookmark
    assert bookmark is not None
    resolve_state_path(repo).unlink()

    exit_code = run_main(repo, config_path, "status", change_id)
    captured = capsys.readouterr()
    refreshed_state = ReviewStateStore.for_repo(repo).load()

    assert exit_code == 0
    assert "PR #1" in captured.out
    assert refreshed_state.changes[change_id].bookmark == bookmark
    assert refreshed_state.changes[change_id].pr_number == 1
    assert refreshed_state.changes[change_id].pr_state == "open"
    assert (
        refreshed_state.changes[change_id].pr_url
        == "https://github.test/octo-org/stacked-review/pull/1"
    )


def test_status_uses_cached_pull_request_metadata_after_prior_online_run(
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
    bookmark = initial_state.changes[change_id].bookmark
    assert bookmark is not None
    resolve_state_path(repo).unlink()

    assert run_main(repo, config_path, "status", change_id) == 0
    capsys.readouterr()

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class OfflineGithubClient(GithubClient):
        async def get_pull_requests_by_head_refs(self, owner, repo, *, head_refs):
            raise GithubClientError("Connection refused")

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_review.review.status",),
        client_type=OfflineGithubClient,
    )

    exit_code = run_main(repo, config_path, "status", change_id)
    captured = capsys.readouterr()
    normalized_err = " ".join(captured.err.split())

    assert exit_code == 1
    assert "GitHub target: octo-org/stacked-review" in normalized_err
    assert "unavailable - check network connectivity" in normalized_err
    assert "saved PR #1 (open)" in captured.out


def test_status_clears_cached_pull_request_metadata_when_github_reports_missing(
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
    assert initial_state.changes[change_id].pr_number == 1
    assert initial_state.changes[change_id].stack_comment_id is None

    del fake_repo.pull_requests[1]

    exit_code = run_main(repo, config_path, "status", "--fetch", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 1
    assert "saved PR #1 (open), no GitHub PR" in captured.out
    assert "PR link note:" in captured.out
    assert "refresh remote and GitHub observations" in captured.out
    assert "relink <pr>" in captured.out
    assert refreshed_state.changes[change_id].pr_number is None
    assert refreshed_state.changes[change_id].pr_state is None
    assert refreshed_state.changes[change_id].pr_url is None
    assert refreshed_state.changes[change_id].stack_comment_id is None


def test_status_refreshes_closed_pull_request_state_in_cache(
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
    fake_repo.pull_requests[1].state = "closed"

    exit_code = run_main(repo, config_path, "status", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "PR #1 closed" in captured.out
    assert refreshed_state.changes[change_id].pr_number == 1
    assert refreshed_state.changes[change_id].pr_review_decision is None
    assert refreshed_state.changes[change_id].pr_state == "closed"
    assert (
        refreshed_state.changes[change_id].pr_url
        == "https://github.test/octo-org/stacked-review/pull/1"
    )
    assert refreshed_state.changes[change_id].stack_comment_id is None


def test_status_reports_draft_pull_request_state(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    assert run_main(repo, config_path, "submit", "--draft") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)

    exit_code = run_main(repo, config_path, "status", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "draft PR #1" in captured.out
    assert refreshed_state.changes[change_id].pr_is_draft is True
    assert refreshed_state.changes[change_id].pr_state == "open"


def test_status_reports_approved_pull_request_state(
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
    fake_repo.create_pull_request_review(
        pull_number=1,
        reviewer_login="reviewer-1",
        state="APPROVED",
    )

    exit_code = run_main(repo, config_path, "status", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "PR #1 approved" in captured.out
    assert refreshed_state.changes[change_id].pr_review_decision == "approved"
    assert refreshed_state.changes[change_id].pr_state == "open"


def test_status_preserves_cached_review_decision_when_review_lookup_fails(
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
    fake_repo.create_pull_request_review(
        pull_number=1,
        reviewer_login="reviewer-1",
        state="APPROVED",
    )

    assert run_main(repo, config_path, "status", change_id) == 0
    capsys.readouterr()
    assert state_store.load().changes[change_id].pr_review_decision == "approved"

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class FailingReviewLookupClient(GithubClient):
        async def get_review_decisions_by_pull_request_numbers(
            self, owner, repo, *, pull_numbers
        ):
            raise GithubClientError("Connection refused")

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_review.review.status",),
        client_type=FailingReviewLookupClient,
    )

    exit_code = run_main(repo, config_path, "status", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "PR #1 approved" in captured.out
    assert state_store.load().changes[change_id].pr_review_decision == "approved"


def test_status_reports_merged_pull_request_state(
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
    fake_repo.pull_requests[1].state = "closed"
    fake_repo.pull_requests[1].merged_at = "2026-03-16T12:00:00Z"

    exit_code = run_main(repo, config_path, "status", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "PR #1 merged, cleanup needed" in captured.out
    assert refreshed_state.changes[change_id].pr_state == "merged"
    assert refreshed_state.changes[change_id].pr_review_decision is None


def test_status_shows_outstanding_submit_intent(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    # First do a submit to create saved local data
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[0].change_id
    state_dir = resolve_state_path(repo).parent

    # Write an outstanding intent with dead PID
    intent = SubmitIntent(
        kind="submit",
        pid=99999999,
        label="submit on @",
        display_revset="@",
        ordered_commit_ids=(stack.revisions[0].commit_id,),
        head_change_id=change_id,
        remote_name="origin",
        github_host="github.test",
        github_owner="octo-org",
        github_repo="stacked-review",
        ordered_change_ids=(change_id,),
        bookmarks={},
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_new_intent(state_dir, intent)

    run_main(repo, config_path, "status")
    captured = capsys.readouterr()

    assert "Interrupted operations recorded:" in captured.out
    assert change_id[:8] in captured.out
