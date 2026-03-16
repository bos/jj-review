from __future__ import annotations

import subprocess
from pathlib import Path

import httpx

from jj_review.cache import ReviewStateStore, resolve_state_path
from jj_review.cli import main
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.jj import JjClient
from jj_review.testing.fake_github import (
    FakeGithubRepository,
    FakeGithubState,
    create_app,
    initialize_bare_repository,
)


def test_submit_projects_review_bookmarks_to_selected_remote(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    exit_code = _main(repo, config_path, "submit")
    captured = capsys.readouterr()
    stack = JjClient(repo).discover_review_stack()
    state = ReviewStateStore.for_repo(repo).load()
    first_bookmark = state.changes[stack.revisions[0].change_id].bookmark

    assert exit_code == 0
    assert "Selected remote: origin" in captured.out
    assert "Trunk: base -> main" in captured.out
    assert len(fake_repo.pull_requests) == 2
    for index, revision in enumerate(stack.revisions, start=1):
        cached_change = state.changes[revision.change_id]
        bookmark = cached_change.bookmark
        assert bookmark is not None
        assert cached_change.pr_number == index
        assert cached_change.pr_url == fake_repo.pull_requests[index].to_payload(
            repository=fake_repo,
            web_origin="https://github.test",
        )["html_url"]
        assert _read_remote_ref(fake_repo.git_dir, bookmark) == revision.commit_id

    assert fake_repo.pull_requests[1].base_ref == "main"
    assert fake_repo.pull_requests[2].base_ref == first_bookmark


def test_submit_creates_stack_comments_for_each_pull_request(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit") == 0
    capsys.readouterr()
    state = ReviewStateStore.for_repo(repo).load()

    assert len(_issue_comments(fake_repo, 1)) == 1
    assert len(_issue_comments(fake_repo, 2)) == 1
    assert "<!-- jj-review-stack -->" in _issue_comments(fake_repo, 1)[0].body
    assert "Previous: trunk `main`" in _issue_comments(fake_repo, 1)[0].body
    assert "Next: [#2](https://github.test/octo-org/stacked-review/pull/2) feature 2" in (
        _issue_comments(fake_repo, 1)[0].body
    )
    assert "Previous: [#1](https://github.test/octo-org/stacked-review/pull/1) feature 1" in (
        _issue_comments(fake_repo, 2)[0].body
    )
    assert "Next: none" in _issue_comments(fake_repo, 2)[0].body
    assert {change.stack_comment_id for change in state.changes.values()} == {1, 2}


def test_submit_rediscovers_and_regenerates_stack_comments_when_cache_is_missing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    top_change_id = stack.revisions[-1].change_id
    bottom_change_id = stack.revisions[0].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    initial_comment_id = initial_state.changes[top_change_id].stack_comment_id
    assert initial_comment_id is not None

    fake_repo.issue_comments[2][0].body = "<!-- jj-review-stack -->\nmanually edited"
    state_store.save(
        initial_state.model_copy(
            update={
                "changes": {
                    **initial_state.changes,
                    top_change_id: initial_state.changes[top_change_id].model_copy(
                        update={"stack_comment_id": None}
                    ),
                }
            }
        )
    )

    _run(["jj", "describe", "-r", top_change_id, "-m", "feature 2 renamed"], repo)

    assert _main(repo, config_path, "submit", top_change_id) == 0
    capsys.readouterr()
    refreshed_state = state_store.load()

    assert len(_issue_comments(fake_repo, 2)) == 1
    assert _issue_comments(fake_repo, 2)[0].id == initial_comment_id
    assert "Current: [#2](https://github.test/octo-org/stacked-review/pull/2) " in (
        _issue_comments(fake_repo, 2)[0].body
    )
    assert "feature 2 renamed" in _issue_comments(fake_repo, 2)[0].body
    assert "feature 2 renamed" in _issue_comments(fake_repo, 1)[0].body
    assert refreshed_state.changes[top_change_id].stack_comment_id == initial_comment_id
    assert refreshed_state.changes[bottom_change_id].stack_comment_id == 1


def test_submit_rejects_cached_stack_comment_id_for_non_stack_comment(
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
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    fake_repo.create_issue_comment(body="manual note", issue_number=1)
    state_store.save(
        initial_state.model_copy(
            update={
                "changes": {
                    **initial_state.changes,
                    change_id: initial_state.changes[change_id].model_copy(
                        update={"stack_comment_id": 2}
                    ),
                }
            }
        )
    )

    exit_code = _main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "is not managed by `jj-review`" in captured.err
    assert fake_repo.issue_comments[1][1].body == "manual note"


def test_submit_rejects_ambiguous_discovered_stack_comments(
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
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    fake_repo.create_issue_comment(body="<!-- jj-review-stack -->\nextra", issue_number=1)
    state_store.save(
        initial_state.model_copy(
            update={
                "changes": {
                    **initial_state.changes,
                    change_id: initial_state.changes[change_id].model_copy(
                        update={"stack_comment_id": None}
                    ),
                }
            }
        )
    )

    exit_code = _main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "multiple `jj-review` stack comments" in captured.err


def test_submit_reports_stack_comment_update_failures_without_traceback(
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
    change_id = stack.revisions[-1].change_id
    _run(["jj", "describe", "-r", change_id, "-m", "feature 1 renamed"], repo)

    class FailingCommentUpdateClient(GithubClient):
        async def update_issue_comment(
            self,
            owner: str,
            repo: str,
            *,
            comment_id: int,
            body: str,
        ):
            raise GithubClientError("GitHub request failed: 404 Not Found", status_code=404)

    app = create_app(FakeGithubState.single_repository(fake_repo))

    def build_github_client(*, base_url: str) -> GithubClient:
        return FailingCommentUpdateClient(
            base_url=base_url,
            transport=httpx.ASGITransport(app=app),
        )

    monkeypatch.setattr("jj_review.commands.submit._build_github_client", build_github_client)

    exit_code = _main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Could not update stack comment" in captured.err
    assert "Traceback" not in captured.err


def test_submit_reports_up_to_date_when_remote_bookmark_and_pr_already_match(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit") == 0
    first_output = capsys.readouterr().out
    first_refs = _remote_refs(fake_repo.git_dir)
    first_prs = {
        number: pull_request.title
        for number, pull_request in fake_repo.pull_requests.items()
    }

    exit_code = _main(repo, config_path, "submit")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "pushed" in first_output
    assert "up to date" in captured.out
    assert "unchanged" in captured.out
    assert _remote_refs(fake_repo.git_dir) == first_refs
    assert {number: pr.title for number, pr in fake_repo.pull_requests.items()} == first_prs


def test_status_reports_remote_and_github_linkage(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit") == 0
    capsys.readouterr()

    exit_code = _main(repo, config_path, "status")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Selected remote: origin" in captured.out
    assert f"GitHub: {fake_repo.owner}/{fake_repo.name}" in captured.out
    assert "feature 1 [" in captured.out
    assert ": PR #1" in captured.out
    assert "review/" not in captured.out
    assert "stack comment" not in captured.out


def test_status_prints_stack_tip_first_like_jj_log(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit") == 0
    capsys.readouterr()

    exit_code = _main(repo, config_path, "status")
    captured = capsys.readouterr()

    assert exit_code == 0
    feature_2_line = captured.out.index("- feature 2 [")
    feature_1_line = captured.out.index("- feature 1 [")
    assert feature_2_line < feature_1_line


def test_status_preserves_remote_observations_when_github_lookup_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit") == 0
    capsys.readouterr()

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class FailingRepositoryLookupClient(GithubClient):
        async def get_repository(
            self,
            owner: str,
            repo: str,
        ):
            raise GithubClientError(
                'GitHub request failed: 404 {"message":"Not Found","documentation_url":"x"}',
                status_code=404,
            )

    def build_github_client(*, base_url: str) -> GithubClient:
        return FailingRepositoryLookupClient(
            base_url=base_url,
            transport=httpx.ASGITransport(app=app),
        )

    monkeypatch.setattr(
        "jj_review.commands.review_state._build_github_client",
        build_github_client,
    )

    exit_code = _main(repo, config_path, "status")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert (
        "GitHub target: octo-org/stacked-review "
        "(repo not found or inaccessible - check GITHUB_TOKEN or gh auth)"
    ) in captured.out
    assert "documentation_url" not in captured.out
    assert ": cached PR #1" in captured.out
    assert ": PR #1" not in captured.out


def test_status_reports_unknown_when_github_is_unavailable_and_no_cache_exists(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class OfflineGithubClient(GithubClient):
        async def get_repository(
            self,
            owner: str,
            repo: str,
        ):
            raise GithubClientError("Connection refused")

    def build_github_client(*, base_url: str) -> GithubClient:
        return OfflineGithubClient(
            base_url=base_url,
            transport=httpx.ASGITransport(app=app),
        )

    monkeypatch.setattr(
        "jj_review.commands.review_state._build_github_client",
        build_github_client,
    )

    exit_code = _main(repo, config_path, "status")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert (
        "GitHub target: octo-org/stacked-review "
        "(unavailable - check network connectivity)"
    ) in captured.out
    assert ": GitHub status unknown" in captured.out


def test_status_exits_nonzero_when_pull_request_lookup_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit") == 0
    capsys.readouterr()

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class FailingPullRequestLookupClient(GithubClient):
        async def list_pull_requests(
            self,
            owner: str,
            repo: str,
            *,
            head: str,
            state: str = "all",
        ) -> tuple:
            raise GithubClientError("Connection reset")

    def build_github_client(*, base_url: str) -> GithubClient:
        return FailingPullRequestLookupClient(
            base_url=base_url,
            transport=httpx.ASGITransport(app=app),
        )

    monkeypatch.setattr(
        "jj_review.commands.review_state._build_github_client",
        build_github_client,
    )

    exit_code = _main(repo, config_path, "status")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert f"GitHub: {fake_repo.owner}/{fake_repo.name}" in captured.out
    assert ": cached PR #1, GitHub is unavailable - check network connectivity" in captured.out


def test_sync_refreshes_cached_pull_request_and_stack_comment_metadata(
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

    exit_code = _main(repo, config_path, "sync", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "GitHub PR #1" in captured.out
    assert "stack comment #1" in captured.out
    assert refreshed_state.changes[change_id].pr_number == 1
    assert refreshed_state.changes[change_id].stack_comment_id == 1


def test_sync_rejects_mismatched_cached_pull_request_metadata(
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
                            "pr_number": 99,
                            "pr_url": "https://github.test/octo-org/stacked-review/pull/99",
                        }
                    ),
                }
            }
        )
    )

    exit_code = _main(repo, config_path, "sync", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 1
    assert "Cached pull request #99 does not match" in captured.err
    assert refreshed_state.changes[change_id].pr_number == 99
    assert refreshed_state.changes[change_id].pr_url == (
        "https://github.test/octo-org/stacked-review/pull/99"
    )


def test_sync_rejects_missing_open_pull_request_for_cached_linkage(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit") == 0
    capsys.readouterr()

    del fake_repo.pull_requests[1]

    exit_code = _main(repo, config_path, "sync")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Cached pull request linkage exists" in captured.err
    assert "Traceback" not in captured.err


def test_submit_updates_existing_pull_request_after_change_rewrite(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _write_file(repo / "feature-2.txt", "feature 2\n")
    _write_file(repo / "details.txt", "more detail\n")
    _run(["jj", "commit", "-m", "feature 2\n\nbody line"], repo)
    assert _main(repo, config_path, "submit") == 0
    capsys.readouterr()

    first_stack = JjClient(repo).discover_review_stack()
    top_change_id = first_stack.revisions[-1].change_id
    initial_bookmark = ReviewStateStore.for_repo(repo).load().changes[top_change_id].bookmark
    assert initial_bookmark is not None
    initial_pr_number = ReviewStateStore.for_repo(repo).load().changes[top_change_id].pr_number
    assert initial_pr_number is not None

    _run(
        ["jj", "describe", "-r", top_change_id, "-m", "feature 2 renamed\n\nupdated body"],
        repo,
    )

    exit_code = _main(repo, config_path, "submit", top_change_id)
    captured = capsys.readouterr()
    rewritten_stack = JjClient(repo).discover_review_stack(top_change_id)
    rewritten_state = ReviewStateStore.for_repo(repo).load()
    rewritten_bookmark = rewritten_state.changes[top_change_id].bookmark

    assert exit_code == 0
    assert rewritten_bookmark == initial_bookmark
    assert "updated" in captured.out
    assert (
        _read_remote_ref(fake_repo.git_dir, initial_bookmark)
        == rewritten_stack.revisions[-1].commit_id
    )
    assert fake_repo.pull_requests[initial_pr_number].title == "feature 2 renamed"
    assert fake_repo.pull_requests[initial_pr_number].body == "updated body"


def test_submit_updates_existing_untracked_remote_bookmark(
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
    change_id = stack.revisions[-1].change_id
    cached_change = ReviewStateStore.for_repo(repo).load().changes[change_id]
    bookmark = cached_change.bookmark
    pr_number = cached_change.pr_number
    assert bookmark is not None
    assert pr_number is not None

    _run(["jj", "bookmark", "forget", bookmark], repo)
    _run(
        ["jj", "describe", "--ignore-immutable", "-r", change_id, "-m", "feature 1 renamed"],
        repo,
    )

    exit_code = _main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()
    rewritten_stack = JjClient(repo).discover_review_stack(change_id)
    bookmark_state = JjClient(repo).get_bookmark_state(bookmark)
    remote_state = bookmark_state.remote_target("origin")

    assert exit_code == 0
    assert "pushed" in captured.out
    assert (
        _read_remote_ref(fake_repo.git_dir, bookmark)
        == rewritten_stack.revisions[-1].commit_id
    )
    assert remote_state is not None
    assert remote_state.is_tracked is True
    assert fake_repo.pull_requests[pr_number].title == "feature 1 renamed"


def test_submit_rediscovers_review_branch_after_state_and_local_bookmark_loss(
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
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    cached_change = state_store.load().changes[change_id]
    bookmark = cached_change.bookmark
    pr_number = cached_change.pr_number
    assert bookmark is not None
    assert pr_number is not None

    state_path = resolve_state_path(repo)
    state_path.unlink()
    _run(["jj", "bookmark", "forget", bookmark], repo)
    _run(
        ["jj", "describe", "--ignore-immutable", "-r", change_id, "-m", "feature 1 renamed"],
        repo,
    )

    exit_code = _main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()
    rewritten_stack = JjClient(repo).discover_review_stack(change_id)
    rewritten_state = state_store.load()

    assert exit_code == 0
    assert "PR #1 updated" in captured.out
    assert set(fake_repo.pull_requests) == {pr_number}
    assert rewritten_state.changes[change_id].bookmark == bookmark
    assert rewritten_state.changes[change_id].pr_number == pr_number
    assert (
        _read_remote_ref(fake_repo.git_dir, bookmark)
        == rewritten_stack.revisions[-1].commit_id
    )
    assert fake_repo.pull_requests[pr_number].title == "feature 1 renamed"


def test_submit_reports_no_reviewable_commits_when_head_is_trunk(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    exit_code = _main(repo, config_path, "submit", "main")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Trunk: base -> main" in captured.out
    assert "No reviewable commits" in captured.out
    assert ReviewStateStore.for_repo(repo).load().changes == {}
    assert set(_remote_refs(fake_repo.git_dir)) == {"refs/heads/main"}
    assert fake_repo.pull_requests == {}


def test_submit_rejects_duplicate_bookmark_overrides_before_projection(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")
    stack = JjClient(repo).discover_review_stack()
    config_path = _write_config(
        tmp_path,
        fake_repo,
        extra_lines=[
            f'[change."{stack.revisions[0].change_id}"]',
            'bookmark_override = "review/same"',
            "",
            f'[change."{stack.revisions[1].change_id}"]',
            'bookmark_override = "review/same"',
        ],
    )

    exit_code = main(["--config", str(config_path), "--repository", str(repo), "submit"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "multiple review units to the same bookmark" in captured.err
    assert ReviewStateStore.for_repo(repo).load().changes == {}
    assert set(_remote_refs(fake_repo.git_dir)) == {"refs/heads/main"}
    assert fake_repo.pull_requests == {}


def test_status_reports_remote_linkage_without_refreshing_pr_cache(
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
    change_id = stack.revisions[-1].change_id
    initial_state = ReviewStateStore.for_repo(repo).load()
    bookmark = initial_state.changes[change_id].bookmark
    assert bookmark is not None
    resolve_state_path(repo).unlink()

    exit_code = _main(repo, config_path, "status", change_id)
    captured = capsys.readouterr()
    refreshed_state = ReviewStateStore.for_repo(repo).load()

    assert exit_code == 0
    assert "Selected remote: origin" in captured.out
    assert "GitHub: octo-org/stacked-review" in captured.out
    assert ": PR #1" in captured.out
    assert refreshed_state.changes[change_id].bookmark == bookmark
    assert refreshed_state.changes[change_id].pr_number is None
    assert refreshed_state.changes[change_id].pr_url is None


def test_sync_refreshes_cached_pull_request_metadata_after_state_loss(
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
    change_id = stack.revisions[-1].change_id
    bookmark = ReviewStateStore.for_repo(repo).load().changes[change_id].bookmark
    assert bookmark is not None
    resolve_state_path(repo).unlink()

    exit_code = _main(repo, config_path, "sync", change_id)
    captured = capsys.readouterr()
    refreshed_state = ReviewStateStore.for_repo(repo).load()

    assert exit_code == 0
    assert "Selected remote: origin" in captured.out
    assert "GitHub: octo-org/stacked-review" in captured.out
    assert "no cached PR, GitHub PR #1" in captured.out
    assert refreshed_state.changes[change_id].bookmark == bookmark
    assert refreshed_state.changes[change_id].pr_number == 1
    assert (
        refreshed_state.changes[change_id].pr_url
        == "https://github.test/octo-org/stacked-review/pull/1"
    )


def test_sync_rejects_mismatched_cached_pull_request_number(
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
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    state_store.save(
        initial_state.model_copy(
            update={
                "changes": {
                    **initial_state.changes,
                    change_id: initial_state.changes[change_id].model_copy(
                        update={"pr_number": 99}
                    ),
                }
            }
        )
    )

    exit_code = _main(repo, config_path, "sync", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 1
    assert "Cached pull request #99 does not match" in captured.err
    assert refreshed_state.changes[change_id].pr_number == 99


def test_sync_skips_unlinked_generated_changes_without_persisting_cache(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    exit_code = _main(repo, config_path, "sync")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "generated" in captured.out
    assert "no cached PR, no GitHub PR" in captured.out
    assert ReviewStateStore.for_repo(repo).load().changes == {}


def _configure_submit_environment(
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


def _main(repo: Path, config_path: Path, command: str, revset: str | None = None) -> int:
    argv = ["--config", str(config_path), "--repository", str(repo), command]
    if revset is not None:
        argv.append(revset)
    return main(argv)


def _write_config(
    tmp_path: Path,
    fake_repo: FakeGithubRepository,
    *,
    extra_lines: list[str] | None = None,
) -> Path:
    config_path = tmp_path / "config-home" / "jj-review" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "[repo]",
        'github_host = "github.test"',
        f'github_owner = "{fake_repo.owner}"',
        f'github_repo = "{fake_repo.name}"',
    ]
    if extra_lines:
        lines.append("")
        lines.extend(extra_lines)
    _write_file(config_path, "\n".join(lines) + "\n")
    return config_path


def _write_file(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
