from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import httpx
import pytest

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


@pytest.fixture(autouse=True)
def _isolate_jj_user_config(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    xdg_config_home = tmp_path / "xdg-config"
    home.mkdir()
    xdg_config_home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config_home))


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
        assert cached_change.pr_state == "open"
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


def test_status_prints_trunk_below_stack_like_jj_log(
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
    assert "◆ base [" in captured.out
    assert ": main" in captured.out
    assert captured.out.index("- feature 1 [") < captured.out.index("◆ base [")


def test_status_limits_concurrent_github_lookups(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    for index in range(4):
        _commit(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")

    assert _main(repo, config_path, "submit") == 0
    capsys.readouterr()

    app = create_app(FakeGithubState.single_repository(fake_repo))
    max_in_flight = 0
    in_flight = 0

    class TrackingGithubClient(GithubClient):
        async def list_pull_requests(
            self,
            owner: str,
            repo: str,
            *,
            head: str,
            state: str = "all",
        ) -> tuple:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                await asyncio.sleep(0.02)
                return await super().list_pull_requests(
                    owner,
                    repo,
                    head=head,
                    state=state,
                )
            finally:
                in_flight -= 1

    def build_github_client(*, base_url: str) -> GithubClient:
        return TrackingGithubClient(
            base_url=base_url,
            transport=httpx.ASGITransport(app=app),
        )

    monkeypatch.setattr(
        "jj_review.commands.review_state._GITHUB_INSPECTION_CONCURRENCY",
        2,
    )
    monkeypatch.setattr(
        "jj_review.commands.review_state._build_github_client",
        build_github_client,
    )

    exit_code = _main(repo, config_path, "status")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"GitHub: {fake_repo.owner}/{fake_repo.name}" in captured.out
    assert max_in_flight == 2


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

    class FailingPullRequestLookupClient(GithubClient):
        async def list_pull_requests(
            self,
            owner: str,
            repo: str,
            *,
            head: str,
            state: str = "all",
        ):
            raise GithubClientError(
                'GitHub request failed: 404 {"message":"Not Found","documentation_url":"x"}',
                status_code=404,
            )

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
    assert (
        "GitHub target: octo-org/stacked-review "
        "(repo not found or inaccessible - check GITHUB_TOKEN or gh auth)"
    ) in captured.out
    assert "documentation_url" not in captured.out
    assert ": cached PR #1 (open)" in captured.out
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
        async def list_pull_requests(
            self,
            owner: str,
            repo: str,
            *,
            head: str,
            state: str = "all",
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


def test_status_does_not_probe_repository_before_pull_request_lookup(
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

    class NoRepositoryProbeClient(GithubClient):
        async def get_repository(
            self,
            owner: str,
            repo: str,
        ):
            raise AssertionError("status should not probe repository availability")

    def build_github_client(*, base_url: str) -> GithubClient:
        return NoRepositoryProbeClient(
            base_url=base_url,
            transport=httpx.ASGITransport(app=app),
        )

    monkeypatch.setattr(
        "jj_review.commands.review_state._build_github_client",
        build_github_client,
    )

    exit_code = _main(repo, config_path, "status")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "GitHub: octo-org/stacked-review" in captured.out
    assert ": PR #1" in captured.out


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
            raise GithubClientError(
                'GitHub request failed: 422 {"message":"Validation Failed"}',
                status_code=422,
            )

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
    assert ": cached PR #1 (open), pull request lookup failed (GitHub 422)" in captured.out


def test_status_exits_nonzero_when_github_reports_multiple_pull_requests(
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
    fake_repo.create_pull_request(
        base_ref="main",
        body="duplicate",
        head_ref=bookmark,
        title="feature 1 duplicate",
    )

    exit_code = _main(repo, config_path, "status", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "multiple pull requests" in captured.out


def test_status_exits_nonzero_when_github_reports_multiple_stack_comments(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit") == 0
    capsys.readouterr()

    fake_repo.create_issue_comment(body="<!-- jj-review-stack -->\nextra", issue_number=1)

    exit_code = _main(repo, config_path, "status")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "multiple `jj-review` stack comments" in captured.out


def test_adopt_repairs_existing_pull_request_linkage_for_rewritten_change(
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
        ["jj", "describe", "--ignore-immutable", "-r", change_id, "-m", "feature 1 adopted"],
        repo,
    )

    exit_code = _main(
        repo,
        config_path,
        "adopt",
        "https://github.test/octo-org/stacked-review/pull/1",
        change_id,
    )
    captured = capsys.readouterr()
    adopted_state = ReviewStateStore.for_repo(repo).load()

    assert exit_code == 0
    assert "Adopted PR #1" in captured.out
    assert adopted_state.changes[change_id].bookmark == manual_bookmark
    assert adopted_state.changes[change_id].pr_number == 1
    assert adopted_state.changes[change_id].pr_state == "open"
    assert adopted_state.changes[change_id].pr_url == (
        "https://github.test/octo-org/stacked-review/pull/1"
    )

    exit_code = _main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()
    rewritten_stack = JjClient(repo).discover_review_stack(change_id)

    assert exit_code == 0
    assert "PR #1 updated" in captured.out
    assert set(fake_repo.pull_requests) == {1}
    assert fake_repo.pull_requests[1].title == "feature 1 adopted"
    assert (
        _read_remote_ref(fake_repo.git_dir, manual_bookmark)
        == rewritten_stack.revisions[-1].commit_id
    )


def test_adopt_reports_missing_pull_request_without_traceback(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    exit_code = _main(repo, config_path, "adopt", "999")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Could not load pull request #999" in captured.err
    assert "Traceback" not in captured.err


def test_adopt_rejects_existing_local_bookmark_on_different_change(
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

    exit_code = _main(repo, config_path, "adopt", "1", top_change_id)
    captured = capsys.readouterr()
    bookmark_state = JjClient(repo).get_bookmark_state(manual_bookmark)

    assert exit_code == 1
    assert "already points to a different revision" in captured.err
    assert bookmark_state.local_target == bottom_commit_id


def test_adopt_rejects_closed_pull_request(
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

    exit_code = _main(repo, config_path, "adopt", "1", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "is not open" in captured.err


def test_adopt_rejects_cross_repository_pull_request_head(
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

    exit_code = _main(repo, config_path, "adopt", "1", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "same-repository review branches" in captured.err


def test_adopt_rejects_pull_request_with_missing_remote_head_branch(
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
        ["jj", "describe", "--ignore-immutable", "-r", change_id, "-m", "feature 1 adopted"],
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

    exit_code = _main(repo, config_path, "adopt", "1", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "does not exist" in captured.err


def test_status_refreshes_cached_stack_comment_metadata_after_state_loss(
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

    exit_code = _main(repo, config_path, "status", "--fetch", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "GitHub: octo-org/stacked-review" in captured.out
    assert ": PR #1" in captured.out
    assert refreshed_state.changes[change_id].pr_number == 1
    assert refreshed_state.changes[change_id].stack_comment_id == 1


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


def test_status_refreshes_cached_pull_request_metadata_after_state_loss(
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
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    bookmark = initial_state.changes[change_id].bookmark
    assert bookmark is not None
    resolve_state_path(repo).unlink()

    assert _main(repo, config_path, "status", change_id) == 0
    capsys.readouterr()

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class OfflineGithubClient(GithubClient):
        async def list_pull_requests(
            self,
            owner: str,
            repo: str,
            *,
            head: str,
            state: str = "all",
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

    exit_code = _main(repo, config_path, "status", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert (
        "GitHub target: octo-org/stacked-review "
        "(unavailable - check network connectivity)"
    ) in captured.out
    assert ": cached PR #1 (open)" in captured.out


def test_status_clears_cached_pull_request_metadata_when_github_reports_missing(
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
    assert initial_state.changes[change_id].pr_number == 1
    assert initial_state.changes[change_id].stack_comment_id == 1

    del fake_repo.pull_requests[1]

    exit_code = _main(repo, config_path, "status", "--fetch", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 1
    assert ": cached PR #1 (open), no GitHub PR" in captured.out
    assert refreshed_state.changes[change_id].pr_number is None
    assert refreshed_state.changes[change_id].pr_state is None
    assert refreshed_state.changes[change_id].pr_url is None
    assert refreshed_state.changes[change_id].stack_comment_id is None


def test_status_refreshes_closed_pull_request_state_in_cache(
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
    fake_repo.pull_requests[1].state = "closed"

    exit_code = _main(repo, config_path, "status", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert ": PR #1 closed" in captured.out
    assert refreshed_state.changes[change_id].pr_number == 1
    assert refreshed_state.changes[change_id].pr_review_decision is None
    assert refreshed_state.changes[change_id].pr_state == "closed"
    assert (
        refreshed_state.changes[change_id].pr_url
        == "https://github.test/octo-org/stacked-review/pull/1"
    )
    assert refreshed_state.changes[change_id].stack_comment_id is None


def test_status_reports_approved_pull_request_state(
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
    fake_repo.create_pull_request_review(
        pull_number=1,
        reviewer_login="reviewer-1",
        state="APPROVED",
    )

    exit_code = _main(repo, config_path, "status", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert ": PR #1 approved" in captured.out
    assert refreshed_state.changes[change_id].pr_review_decision == "approved"
    assert refreshed_state.changes[change_id].pr_state == "open"


def test_submit_preserves_cached_review_decision(
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
    fake_repo.create_pull_request_review(
        pull_number=1,
        reviewer_login="reviewer-1",
        state="APPROVED",
    )

    assert _main(repo, config_path, "status", change_id) == 0
    capsys.readouterr()
    assert state_store.load().changes[change_id].pr_review_decision == "approved"

    assert _main(repo, config_path, "submit", change_id) == 0
    capsys.readouterr()

    refreshed_state = state_store.load()
    assert refreshed_state.changes[change_id].pr_review_decision == "approved"
    assert refreshed_state.changes[change_id].pr_state == "open"


def test_status_preserves_cached_review_decision_when_review_lookup_fails(
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
    fake_repo.create_pull_request_review(
        pull_number=1,
        reviewer_login="reviewer-1",
        state="APPROVED",
    )

    assert _main(repo, config_path, "status", change_id) == 0
    capsys.readouterr()
    assert state_store.load().changes[change_id].pr_review_decision == "approved"

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class FailingReviewLookupClient(GithubClient):
        async def list_pull_request_reviews(
            self,
            owner: str,
            repo: str,
            *,
            pull_number: int,
        ):
            raise GithubClientError("Connection refused")

    def build_github_client(*, base_url: str) -> GithubClient:
        return FailingReviewLookupClient(
            base_url=base_url,
            transport=httpx.ASGITransport(app=app),
        )

    monkeypatch.setattr(
        "jj_review.commands.review_state._build_github_client",
        build_github_client,
    )

    exit_code = _main(repo, config_path, "status", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert ": PR #1 approved" in captured.out
    assert state_store.load().changes[change_id].pr_review_decision == "approved"
def test_status_reports_merged_pull_request_state(
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
    fake_repo.pull_requests[1].state = "closed"
    fake_repo.pull_requests[1].merged_at = "2026-03-16T12:00:00Z"

    exit_code = _main(repo, config_path, "status", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert ": PR #1 merged, cleanup needed" in captured.out
    assert refreshed_state.changes[change_id].pr_state == "merged"
    assert refreshed_state.changes[change_id].pr_review_decision is None


def test_cleanup_restack_previews_and_applies_survivor_rebase_after_merged_ancestor(
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
    bottom_change_id = stack.revisions[0].change_id
    top_change_id = stack.revisions[1].change_id
    trunk_commit_id = stack.trunk.commit_id
    fake_repo.pull_requests[1].state = "closed"
    fake_repo.pull_requests[1].merged_at = "2026-03-16T12:00:00Z"

    preview_exit_code = _main(repo, config_path, "cleanup", "--restack", top_change_id)
    preview = capsys.readouterr()

    assert preview_exit_code == 0
    assert "Planned restack actions:" in preview.out
    assert f"rebase {top_change_id[:8]} onto trunk()" in preview.out

    apply_exit_code = _main(
        repo,
        config_path,
        "cleanup",
        "--restack",
        "--apply",
        top_change_id,
    )
    applied = capsys.readouterr()
    rewritten_top = JjClient(repo).resolve_revision(top_change_id)

    assert apply_exit_code == 0
    assert "Applied restack actions:" in applied.out
    assert rewritten_top.only_parent_commit_id() == trunk_commit_id
    assert JjClient(repo).resolve_revision(bottom_change_id).commit_id != rewritten_top.commit_id


def test_cleanup_reports_stale_cache_and_remote_branch_without_applying(
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
    bookmark = initial_state.changes[change_id].bookmark
    assert bookmark is not None

    _run(["jj", "abandon", change_id], repo)
    _run(["jj", "bookmark", "delete", bookmark], repo)

    exit_code = _main(repo, config_path, "cleanup")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Planned cleanup actions:" in captured.out
    assert "[planned] cache:" in captured.out
    assert f"[planned] remote branch: delete remote review branch {bookmark}@origin" in (
        captured.out
    )
    assert "cleanup --apply" in captured.out
    assert change_id in state_store.load().changes
    assert f"refs/heads/{bookmark}" in _remote_refs(fake_repo.git_dir)


def test_cleanup_apply_removes_stale_cache_and_remote_branch(
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
    bookmark = initial_state.changes[change_id].bookmark
    assert bookmark is not None

    _run(["jj", "abandon", change_id], repo)
    _run(["jj", "bookmark", "delete", bookmark], repo)

    exit_code = _main(repo, config_path, "cleanup", "--apply")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Applied cleanup actions:" in captured.out
    assert f"[applied] remote branch: delete remote review branch {bookmark}@origin" in (
        captured.out
    )
    assert change_id not in state_store.load().changes
    assert f"refs/heads/{bookmark}" not in _remote_refs(fake_repo.git_dir)


def test_cleanup_apply_keeps_remote_branch_when_target_changes_mid_delete(
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
    bookmark = initial_state.changes[change_id].bookmark
    assert bookmark is not None

    _run(["jj", "abandon", change_id], repo)
    _run(["jj", "bookmark", "delete", bookmark], repo)

    original_delete_remote_bookmark = JjClient.delete_remote_bookmark

    def delete_remote_bookmark_with_race(
        self,
        *,
        remote: str,
        bookmark: str,
        expected_remote_target: str,
    ) -> None:
        _run(
            [
                "git",
                "--git-dir",
                str(fake_repo.git_dir),
                "update-ref",
                f"refs/heads/{bookmark}",
                _read_remote_ref(fake_repo.git_dir, "main"),
            ],
            fake_repo.git_dir.parent,
        )
        original_delete_remote_bookmark(
            self,
            remote=remote,
            bookmark=bookmark,
            expected_remote_target=expected_remote_target,
        )

    monkeypatch.setattr(
        "jj_review.commands.cleanup.JjClient.delete_remote_bookmark",
        delete_remote_bookmark_with_race,
    )

    exit_code = _main(repo, config_path, "cleanup", "--apply")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert change_id in state_store.load().changes
    assert _read_remote_ref(fake_repo.git_dir, bookmark) == _read_remote_ref(
        fake_repo.git_dir, "main"
    )
    assert "force-with-lease" in captured.err


def test_cleanup_apply_deletes_managed_stack_comment_for_closed_pull_request(
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
    fake_repo.pull_requests[1].state = "closed"

    exit_code = _main(repo, config_path, "cleanup", "--apply")
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "[applied] stack comment: delete managed stack comment #1 from PR #1" in (
        captured.out
    )
    assert refreshed_state.changes[change_id].pr_number == 1
    assert refreshed_state.changes[change_id].stack_comment_id is None
    assert _issue_comments(fake_repo, 1) == []


def test_cleanup_apply_deletes_discovered_stack_comment_when_cache_id_is_missing(
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
    fake_repo.pull_requests[1].state = "closed"
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

    exit_code = _main(repo, config_path, "cleanup", "--apply")
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "[applied] stack comment: delete managed stack comment #1 from PR #1" in (
        captured.out
    )
    assert refreshed_state.changes[change_id].stack_comment_id is None
    assert _issue_comments(fake_repo, 1) == []


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
    monkeypatch.setattr("jj_review.commands.adopt._build_github_client", build_github_client)
    monkeypatch.setattr("jj_review.commands.cleanup._build_github_client", build_github_client)
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


def _main(repo: Path, config_path: Path, command: str, *command_args: str) -> int:
    argv = ["--config", str(config_path), "--repository", str(repo), command]
    argv.extend(command_args)
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
