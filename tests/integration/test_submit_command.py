from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import httpx
import pytest

from jj_review.cache import ReviewStateStore, resolve_state_path
from jj_review.cli import main
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.intent import write_intent
from jj_review.jj import JjClient
from jj_review.jj.client import JjCommandError
from jj_review.models.intent import SubmitIntent

from ..support.fake_github import (
    FakeGithubRepository,
    FakeGithubState,
    create_app,
)
from ..support.integration_helpers import (
    commit_file,
    configure_fake_github_environment,
    init_fake_github_repo,
    run_command,
    write_fake_github_config,
    write_file,
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

    exit_code = _main(repo, config_path, "submit", "--current")
    captured = capsys.readouterr()
    stack = JjClient(repo).discover_review_stack()
    state = ReviewStateStore.for_repo(repo).load()
    first_bookmark = state.changes[stack.revisions[0].change_id].bookmark
    top_pr_url = state.changes[stack.revisions[-1].change_id].pr_url

    assert exit_code == 0
    assert "Selected remote: origin" in captured.out
    assert "Trunk: base -> main" in captured.out
    assert top_pr_url is not None
    assert f"Top of stack: {top_pr_url}" in captured.out
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


def test_submit_draft_creates_draft_pull_requests_and_persists_draft_state(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    exit_code = _main(repo, config_path, "submit", "--draft", "--current")
    captured = capsys.readouterr()
    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    cached_change = ReviewStateStore.for_repo(repo).load().changes[change_id]

    assert exit_code == 0
    assert "draft PR #1" in captured.out
    assert fake_repo.pull_requests[1].is_draft
    assert cached_change.pr_is_draft is True
    assert cached_change.pr_state == "open"


def test_submit_draft_new_does_not_convert_published_pull_requests_back_to_draft(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()
    assert not fake_repo.pull_requests[1].is_draft

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id

    assert _main(repo, config_path, "submit", "--draft=new", change_id) == 0
    capsys.readouterr()

    assert not fake_repo.pull_requests[1].is_draft
    assert ReviewStateStore.for_repo(repo).load().changes[change_id].pr_is_draft is False


def test_submit_draft_all_converts_existing_published_stack_to_draft(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()
    assert fake_repo.pull_requests[1].is_draft is False
    assert fake_repo.pull_requests[2].is_draft is False

    stack = JjClient(repo).discover_review_stack()
    exit_code = _main(repo, config_path, "submit", "--draft=all", stack.revisions[-1].change_id)
    captured = capsys.readouterr()
    refreshed_state = ReviewStateStore.for_repo(repo).load()

    assert exit_code == 0
    assert "draft PR #1 updated" in captured.out
    assert "draft PR #2 updated" in captured.out
    assert fake_repo.pull_requests[1].is_draft
    assert fake_repo.pull_requests[2].is_draft
    assert refreshed_state.changes[stack.revisions[0].change_id].pr_is_draft is True
    assert refreshed_state.changes[stack.revisions[1].change_id].pr_is_draft is True


def test_submit_requires_explicit_revision_selection(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    exit_code = _main(repo, config_path, "submit")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "requires an explicit revision selection" in captured.err
    assert ReviewStateStore.for_repo(repo).load().changes == {}
    assert set(_remote_refs(fake_repo.git_dir)) == {"refs/heads/main"}
    assert fake_repo.pull_requests == {}


def test_submit_creates_stack_comments_for_each_pull_request(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
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


def test_submit_skips_stack_comment_for_single_commit_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()
    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state = ReviewStateStore.for_repo(repo).load()

    assert _issue_comments(fake_repo, 1) == []
    assert state.changes[change_id].stack_comment_id is None


def test_submit_describe_with_generates_pull_request_and_stack_metadata(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")
    helper = tmp_path / "describe.py"
    helper.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json",
                "import os",
                "from pathlib import Path",
                "import sys",
                "",
                "stack_input_env = 'JJ_REVIEW_STACK_INPUT_FILE'",
                "kind, revset = sys.argv[1], sys.argv[2]",
                "if kind == '--pr':",
                "    payload = {",
                "        'title': f'AI {revset[:8]}',",
                "        'body': f'Generated body for {revset}',",
                "    }",
                "elif kind == '--stack':",
                "    stack_input = json.loads(",
                "        Path(os.environ[stack_input_env]).read_text(encoding='utf-8')",
                "    )",
                "    revisions = stack_input['revisions']",
                "    payload = {",
                "        'title': 'Generated stack summary',",
                "        'body': (",
                "            f\"Generated stack body for {revset}: \"",
                "            f\"{revisions[0]['title']} -> {revisions[1]['title']} | \"",
                "            f\"{revisions[0]['diffstat'].splitlines()[0]}\"",
                "        ),",
                "    }",
                "else:",
                "    raise SystemExit(f'unexpected args: {sys.argv[1:]}')",
                "print(json.dumps(payload))",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)

    exit_code = _main(
        repo,
        config_path,
        "submit",
        "--current",
        "--describe-with",
        str(helper),
    )
    captured = capsys.readouterr()
    stack = JjClient(repo).discover_review_stack()

    assert exit_code == 0
    assert "Submitted review bookmarks:" in captured.out
    assert fake_repo.pull_requests[1].title == f"AI {stack.revisions[0].change_id[:8]}"
    assert fake_repo.pull_requests[1].body == (
        f"Generated body for {stack.revisions[0].change_id}"
    )
    assert fake_repo.pull_requests[2].title == f"AI {stack.revisions[1].change_id[:8]}"
    assert fake_repo.pull_requests[2].body == (
        f"Generated body for {stack.revisions[1].change_id}"
    )
    assert "## Generated stack summary" in _issue_comments(fake_repo, 1)[0].body
    assert (
        f"Generated stack body for {stack.selected_revset}: "
        f"AI {stack.revisions[0].change_id[:8]} -> AI {stack.revisions[1].change_id[:8]} | "
        "feature-1.txt"
        in _issue_comments(fake_repo, 1)[0].body
    )
    assert "This pull request is part of a stack tracked by `jj-review`." in (
        _issue_comments(fake_repo, 1)[0].body
    )


def test_submit_describe_with_skips_stack_helper_for_single_commit_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    helper = tmp_path / "describe.py"
    log_path = tmp_path / "helper.log"
    helper.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json",
                "import pathlib",
                "import sys",
                "",
                f"log_path = pathlib.Path({str(log_path)!r})",
                "log_path.write_text(",
                "    log_path.read_text() + ' '.join(sys.argv[1:]) + '\\n' if log_path.exists()",
                "    else ' '.join(sys.argv[1:]) + '\\n'",
                ")",
                "kind, revset = sys.argv[1], sys.argv[2]",
                "if kind != '--pr':",
                "    raise SystemExit(f'unexpected args: {sys.argv[1:]}')",
                "print(json.dumps({'title': f'AI {revset[:8]}', 'body': f'Body {revset}'}))",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)

    assert _main(
        repo,
        config_path,
        "submit",
        "--current",
        "--describe-with",
        str(helper),
    ) == 0
    capsys.readouterr()
    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state = ReviewStateStore.for_repo(repo).load()

    assert fake_repo.pull_requests[1].title == f"AI {change_id[:8]}"
    assert log_path.read_text(encoding="utf-8").splitlines() == [f"--pr {change_id}"]
    assert _issue_comments(fake_repo, 1) == []
    assert state.changes[change_id].stack_comment_id is None


def test_submit_describe_with_failure_aborts_before_mutation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    helper = tmp_path / "describe.py"
    helper.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "print('not json')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)

    exit_code = _main(
        repo,
        config_path,
        "submit",
        "--current",
        "--describe-with",
        str(helper),
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "returned invalid JSON" in captured.err
    assert ReviewStateStore.for_repo(repo).load().changes == {}
    assert set(_remote_refs(fake_repo.git_dir)) == {"refs/heads/main"}
    assert fake_repo.pull_requests == {}
    assert _issue_comments(fake_repo, 1) == []


def test_submit_batches_pull_request_discovery_with_graphql(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    for index in range(4):
        _commit(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")

    app = create_app(FakeGithubState.single_repository(fake_repo))
    batch_calls: list[tuple[str, ...]] = []

    class TrackingGithubClient(GithubClient):
        async def get_pull_requests_by_head_refs(self, owner, repo, *, head_refs):
            batch_calls.append(tuple(head_refs))
            return await super().get_pull_requests_by_head_refs(
                owner,
                repo,
                head_refs=head_refs,
            )

        async def list_pull_requests(self, owner, repo, *, head, state="all"):
            raise AssertionError("submit should batch pull request discovery")

    def build_github_client(*, base_url: str) -> GithubClient:
        return TrackingGithubClient(
            base_url=base_url,
            transport=httpx.ASGITransport(app=app),
        )

    monkeypatch.setattr("jj_review.commands.submit._build_github_client", build_github_client)

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()
    state = ReviewStateStore.for_repo(repo).load()

    assert len(batch_calls) == 1
    assert set(batch_calls[0]) == {
        change.bookmark for change in state.changes.values() if change.bookmark is not None
    }


def test_submit_batches_ordinary_pushes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    for index in range(3):
        _commit(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")

    push_calls: list[tuple[str, ...]] = []
    original_push_bookmarks = JjClient.push_bookmarks

    def tracking_push_bookmarks(self, *, remote: str, bookmarks: tuple[str, ...]) -> None:
        push_calls.append(tuple(bookmarks))
        original_push_bookmarks(self, remote=remote, bookmarks=bookmarks)

    def fail_push_bookmark(*args, **kwargs) -> None:
        raise AssertionError("submit should batch ordinary bookmark pushes")

    monkeypatch.setattr(JjClient, "push_bookmarks", tracking_push_bookmarks)
    monkeypatch.setattr(JjClient, "push_bookmark", fail_push_bookmark)

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()
    state = ReviewStateStore.for_repo(repo).load()

    assert len(push_calls) == 1
    assert set(push_calls[0]) == {
        change.bookmark for change in state.changes.values() if change.bookmark is not None
    }


def test_submit_limits_stack_comment_github_inspection_concurrency(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    for index in range(4):
        _commit(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")

    app = create_app(FakeGithubState.single_repository(fake_repo))
    max_in_flight = 0
    in_flight = 0

    class TrackingGithubClient(GithubClient):
        async def list_issue_comments(self, owner, repo, *, issue_number):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                await asyncio.sleep(0.02)
                return await super().list_issue_comments(
                    owner,
                    repo,
                    issue_number=issue_number,
                )
            finally:
                in_flight -= 1

    def build_github_client(*, base_url: str) -> GithubClient:
        return TrackingGithubClient(
            base_url=base_url,
            transport=httpx.ASGITransport(app=app),
        )

    monkeypatch.setattr("jj_review.commands.submit._GITHUB_INSPECTION_CONCURRENCY", 2)
    monkeypatch.setattr("jj_review.commands.submit._build_github_client", build_github_client)

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    assert max_in_flight == 2


def test_submit_reports_repository_error_before_batched_pr_discovery(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    app = create_app(FakeGithubState.single_repository(fake_repo))
    batch_lookup_called = False

    class MissingRepositoryClient(GithubClient):
        async def get_repository(self, owner: str, repo: str):
            raise GithubClientError("GitHub request failed: 404 Not Found", status_code=404)

        async def get_pull_requests_by_head_refs(self, owner, repo, *, head_refs):
            nonlocal batch_lookup_called
            batch_lookup_called = True
            return await super().get_pull_requests_by_head_refs(
                owner,
                repo,
                head_refs=head_refs,
            )

    def build_github_client(*, base_url: str) -> GithubClient:
        return MissingRepositoryClient(
            base_url=base_url,
            transport=httpx.ASGITransport(app=app),
        )

    monkeypatch.setattr("jj_review.commands.submit._build_github_client", build_github_client)

    exit_code = _main(repo, config_path, "submit", "--current")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Could not load GitHub repository octo-org/stacked-review" in captured.err
    assert not batch_lookup_called


def test_submit_dry_run_does_not_mutate_local_remote_or_github_state(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    initial_remote_refs = _remote_refs(fake_repo.git_dir)

    exit_code = _main(repo, config_path, "submit", "--current", "--dry-run")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Dry run: no local, remote, or GitHub changes applied." in captured.out
    assert "Planned review bookmarks:" in captured.out
    assert "feature 1 [" in captured.out
    assert "  -> review/" in captured.out
    assert " [new PR]" in captured.out
    assert fake_repo.pull_requests == {}
    assert _remote_refs(fake_repo.git_dir) == initial_remote_refs


def test_submit_dry_run_keeps_revision_output_grouped(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    exit_code = _main(repo, config_path, "submit", "--current", "--dry-run")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "[push [pushed]ed]" not in captured.out
    assert captured.out.count("[new PR]") == 2
    lines = captured.out.splitlines()
    feature_1_index = next(
        index for index, line in enumerate(lines) if "- feature 1 [" in line
    )
    assert "  -> review/" in lines[feature_1_index + 1]
    assert "[new PR]" in lines[feature_1_index + 1]
    feature_2_index = next(
        index for index, line in enumerate(lines) if "- feature 2 [" in line
    )
    assert "  -> review/" in lines[feature_2_index + 1]
    assert "[new PR]" in lines[feature_2_index + 1]
    assert ReviewStateStore.for_repo(repo).load().changes == {}
    assert list(resolve_state_path(repo).parent.glob("incomplete-*.toml")) == []

    bookmark_states = JjClient(repo).list_bookmark_states()
    assert all(not name.startswith("review/") for name in bookmark_states)


def test_submit_dry_run_reports_update_without_mutating_remote_or_github(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_before = ReviewStateStore.for_repo(repo).load()
    remote_refs_before = _remote_refs(fake_repo.git_dir)

    _run(["jj", "describe", "-r", change_id, "-m", "feature 1 renamed"], repo)

    exit_code = _main(repo, config_path, "submit", "--dry-run", change_id)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Dry run: no local, remote, or GitHub changes applied." in captured.out
    assert "  -> review/" in captured.out
    assert "[pushed]" in captured.out
    assert "PR #1 updated" in captured.out
    assert fake_repo.pull_requests[1].title == "feature 1"
    assert _remote_refs(fake_repo.git_dir) == remote_refs_before
    assert ReviewStateStore.for_repo(repo).load() == state_before
    assert list(resolve_state_path(repo).parent.glob("incomplete-*.toml")) == []


def test_submit_dry_run_warns_on_stale_intent_without_retiring_it(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    stack = JjClient(repo).discover_review_stack()
    change_id_1 = stack.revisions[0].change_id
    change_id_2 = stack.revisions[1].change_id
    state_dir = resolve_state_path(repo).parent
    old_intent = SubmitIntent(
        kind="submit",
        pid=99999999,
        label="submit on @",
        display_revset="@",
        head_change_id=change_id_2,
        ordered_change_ids=(change_id_1, change_id_2),
        bookmarks={},
        bases={},
        started_at="2026-01-01T00:00:00+00:00",
    )
    old_intent_path = write_intent(state_dir, old_intent)

    exit_code = _main(repo, config_path, "submit", "--current", "--dry-run")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Resuming interrupted submit on @" in captured.out
    assert old_intent_path.exists()
    assert fake_repo.pull_requests == {}


def test_submit_rediscovers_and_regenerates_stack_comments_when_cache_is_missing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
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
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    manual_comment = fake_repo.create_issue_comment(body="manual note", issue_number=2)
    state_store.save(
        initial_state.model_copy(
            update={
                "changes": {
                    **initial_state.changes,
                    change_id: initial_state.changes[change_id].model_copy(
                        update={"stack_comment_id": manual_comment.id}
                    ),
                }
            }
        )
    )

    exit_code = _main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "does not belong to `jj-review`" in captured.err
    assert manual_comment in _issue_comments(fake_repo, 2)


def test_submit_rejects_ambiguous_discovered_stack_comments(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    fake_repo.create_issue_comment(body="<!-- jj-review-stack -->\nextra", issue_number=2)
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
    assert "multiple `jj-review` stack summary comments" in captured.err


def test_submit_reports_stack_comment_update_failures_without_traceback(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
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
    assert "Could not update stack summary comment" in captured.err
    assert "Traceback" not in captured.err


def test_submit_reports_up_to_date_when_remote_bookmark_and_pr_already_match(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    first_output = capsys.readouterr().out
    first_refs = _remote_refs(fake_repo.git_dir)
    first_prs = {
        number: pull_request.title
        for number, pull_request in fake_repo.pull_requests.items()
    }

    exit_code = _main(repo, config_path, "submit", "--current")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "[PR #1]" in first_output
    assert "[already pushed]" in captured.out
    assert "unchanged" in captured.out
    assert _remote_refs(fake_repo.git_dir) == first_refs
    assert {number: pr.title for number, pr in fake_repo.pull_requests.items()} == first_prs


def test_status_reports_remote_and_github_link(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
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

    assert _main(repo, config_path, "submit", "--current") == 0
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

    assert _main(repo, config_path, "submit", "--current") == 0
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

    assert _main(repo, config_path, "submit", "--current") == 0
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

    assert _main(repo, config_path, "submit", "--current") == 0
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
    assert ": saved PR #1 (open)" in captured.out
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

    assert _main(repo, config_path, "submit", "--current") == 0
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

    assert _main(repo, config_path, "submit", "--current") == 0
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
    assert ": saved PR #1 (open), pull request lookup failed (GitHub 422)" in captured.out


def test_status_exits_nonzero_when_github_reports_multiple_pull_requests(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
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
    assert "PR link note:" in captured.out
    assert "refresh remote and GitHub observations" in captured.out
    assert "relink <pr>" in captured.out


def test_status_exits_nonzero_when_github_reports_multiple_stack_comments(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    fake_repo.create_issue_comment(body="<!-- jj-review-stack -->\nextra", issue_number=2)

    exit_code = _main(repo, config_path, "status")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "multiple `jj-review` stack summary comments" in captured.out


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


def test_unlink_detaches_change_and_preserves_local_bookmark(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    bookmark = state_store.load().changes[change_id].bookmark
    assert bookmark is not None

    exit_code = _main(repo, config_path, "unlink", change_id)
    captured = capsys.readouterr()
    unlinked_change = state_store.load().changes[change_id]

    assert exit_code == 0
    assert "Stopped review tracking for" in captured.out
    assert unlinked_change.bookmark == bookmark
    assert unlinked_change.unlinked_at is not None
    assert unlinked_change.link_state == "unlinked"
    assert unlinked_change.pr_number is None
    assert unlinked_change.pr_review_decision is None
    assert unlinked_change.pr_state is None
    assert unlinked_change.pr_url is None
    assert unlinked_change.stack_comment_id is None
    assert JjClient(repo).get_bookmark_state(bookmark).local_target is not None
    assert fake_repo.pull_requests[1].state == "open"
    assert _issue_comments(fake_repo, 1) == []


def test_unlink_is_idempotent_for_unlinked_change(
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
    exit_code = _main(repo, config_path, "unlink", change_id)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "already unlinked from review tracking" in captured.out


def test_unlink_rejects_change_without_active_review_link(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id

    exit_code = _main(repo, config_path, "unlink", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "no active review tracking link to unlink" in captured.err


def test_status_fetch_surfaces_unlinked_state_without_repopulating_link(
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

    exit_code = _main(repo, config_path, "status", "--fetch", change_id)
    captured = capsys.readouterr()
    unlinked_change = ReviewStateStore.for_repo(repo).load().changes[change_id]

    assert exit_code == 0
    assert ": unlinked PR #1" in captured.out
    assert unlinked_change.link_state == "unlinked"
    assert unlinked_change.pr_number is None
    assert unlinked_change.pr_state is None
    assert unlinked_change.pr_url is None
    assert unlinked_change.stack_comment_id is None


def test_submit_rejects_unlinked_change_until_relink(
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

    exit_code = _main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "unlinked from review tracking" in captured.err
    assert "relink" in captured.err


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


def test_land_blocks_unlinked_change(
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

    exit_code = _main(repo, config_path, "land", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Land blocked:" in captured.out
    assert "unlinked from review tracking" in captured.out


def test_cleanup_apply_prunes_unlinked_state_for_stale_change(
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
    _run(["jj", "abandon", change_id], repo)

    exit_code = _main(repo, config_path, "cleanup", "--apply")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "remove saved jj-review data" in captured.out
    assert change_id not in ReviewStateStore.for_repo(repo).load().changes


def test_status_refreshes_cached_stack_comment_metadata_after_state_loss(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
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
    assert ": PR #2" in captured.out
    assert refreshed_state.changes[change_id].pr_number == 2
    assert refreshed_state.changes[change_id].stack_comment_id == 2


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
    assert _main(repo, config_path, "submit", "--current") == 0
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

    assert _main(repo, config_path, "submit", "--current") == 0
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


def test_submit_rerun_recovers_after_failure_following_untracked_remote_update(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
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

    original_update_untracked_remote_bookmark = JjClient.update_untracked_remote_bookmark

    def update_untracked_remote_bookmark_then_fail(
        self,
        *,
        remote: str,
        bookmark: str,
        desired_target: str,
        expected_remote_target: str,
    ) -> None:
        original_update_untracked_remote_bookmark(
            self,
            remote=remote,
            bookmark=bookmark,
            desired_target=desired_target,
            expected_remote_target=expected_remote_target,
        )
        raise RuntimeError("Simulated failure after untracked remote update")

    monkeypatch.setattr(
        "jj_review.commands.submit.JjClient.update_untracked_remote_bookmark",
        update_untracked_remote_bookmark_then_fail,
    )

    with pytest.raises(RuntimeError, match="Simulated failure after untracked remote update"):
        _main(repo, config_path, "submit", change_id)
    capsys.readouterr()

    bookmark_state = JjClient(repo).get_bookmark_state(bookmark)
    remote_state = bookmark_state.remote_target("origin")
    assert remote_state is not None
    assert remote_state.is_tracked is True

    state_dir = resolve_state_path(repo).parent
    [intent_path] = state_dir.glob("incomplete-*.toml")
    intent_text = intent_path.read_text(encoding="utf-8")
    intent_path.write_text(
        intent_text.replace(f"pid = {os.getpid()}", "pid = 99999999"),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "jj_review.commands.submit.JjClient.update_untracked_remote_bookmark",
        original_update_untracked_remote_bookmark,
    )

    exit_code = _main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()
    rewritten_stack = JjClient(repo).discover_review_stack(change_id)

    assert exit_code == 0
    assert "updated" in captured.out
    assert (
        _read_remote_ref(fake_repo.git_dir, bookmark)
        == rewritten_stack.revisions[-1].commit_id
    )
    assert fake_repo.pull_requests[pr_number].title == "feature 1 renamed"


def test_submit_rediscovers_review_branch_after_state_and_local_bookmark_loss(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
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


def test_submit_fails_closed_when_cached_pull_request_is_missing_on_github(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    bookmark = initial_state.changes[change_id].bookmark
    assert bookmark is not None
    initial_remote_target = _read_remote_ref(fake_repo.git_dir, bookmark)

    del fake_repo.pull_requests[1]

    exit_code = _main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Saved pull request link exists" in captured.err
    assert "status --fetch" in captured.err
    assert "relink" in captured.err
    assert state_store.load() == initial_state
    assert _read_remote_ref(fake_repo.git_dir, bookmark) == initial_remote_target
    assert fake_repo.pull_requests == {}


def test_submit_fails_closed_when_github_reports_multiple_pull_requests(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    bookmark = initial_state.changes[change_id].bookmark
    assert bookmark is not None
    initial_remote_target = _read_remote_ref(fake_repo.git_dir, bookmark)
    fake_repo.create_pull_request(
        base_ref="main",
        body="duplicate",
        head_ref=bookmark,
        title="feature 1 duplicate",
    )

    exit_code = _main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "multiple pull requests" in captured.err
    assert "status --fetch" in captured.err
    assert "relink" in captured.err
    assert state_store.load() == initial_state
    assert _read_remote_ref(fake_repo.git_dir, bookmark) == initial_remote_target
    assert set(fake_repo.pull_requests) == {1, 2}


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

    exit_code = main(
        ["--config", str(config_path), "--repository", str(repo), "submit", "--current"]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "multiple changes to the same bookmark" in captured.err
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

    assert _main(repo, config_path, "submit", "--current") == 0
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

    assert _main(repo, config_path, "submit", "--current") == 0
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
    assert ": saved PR #1 (open)" in captured.out


def test_status_clears_cached_pull_request_metadata_when_github_reports_missing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    assert initial_state.changes[change_id].pr_number == 1
    assert initial_state.changes[change_id].stack_comment_id is None

    del fake_repo.pull_requests[1]

    exit_code = _main(repo, config_path, "status", "--fetch", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 1
    assert ": saved PR #1 (open), no GitHub PR" in captured.out
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
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
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


def test_status_reports_draft_pull_request_state(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--draft", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)

    exit_code = _main(repo, config_path, "status", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert ": draft PR #1" in captured.out
    assert refreshed_state.changes[change_id].pr_is_draft is True
    assert refreshed_state.changes[change_id].pr_state == "open"


def test_status_reports_approved_pull_request_state(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
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

    assert _main(repo, config_path, "submit", "--current") == 0
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


def test_submit_publish_marks_existing_draft_pull_requests_ready_for_review(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--draft", "--current") == 0
    capsys.readouterr()
    assert fake_repo.pull_requests[1].is_draft is True

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id

    exit_code = _main(repo, config_path, "submit", "--publish", change_id)
    captured = capsys.readouterr()
    refreshed_state = ReviewStateStore.for_repo(repo).load()

    assert exit_code == 0
    assert "PR #1 updated" in captured.out
    assert not fake_repo.pull_requests[1].is_draft
    assert refreshed_state.changes[change_id].pr_is_draft is False


def test_status_preserves_cached_review_decision_when_review_lookup_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
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

    assert _main(repo, config_path, "submit", "--current") == 0
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

    assert _main(repo, config_path, "submit", "--current") == 0
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

    assert _main(repo, config_path, "submit", "--current") == 0
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
    assert "[planned] tracking:" in captured.out
    assert f"[planned] remote branch: delete remote branch {bookmark}@origin" in (
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

    assert _main(repo, config_path, "submit", "--current") == 0
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
    assert f"[applied] remote branch: delete remote branch {bookmark}@origin" in (
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

    assert _main(repo, config_path, "submit", "--current") == 0
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
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    fake_repo.pull_requests[2].state = "closed"

    exit_code = _main(repo, config_path, "cleanup", "--apply")
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "[applied] stack summary comment: delete stack summary comment #2 from PR #2" in (
        captured.out
    )
    assert refreshed_state.changes[change_id].pr_number == 2
    assert refreshed_state.changes[change_id].stack_comment_id is None
    assert _issue_comments(fake_repo, 2) == []


def test_cleanup_apply_deletes_discovered_stack_comment_when_cache_id_is_missing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    fake_repo.pull_requests[2].state = "closed"
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
    assert "[applied] stack summary comment: delete stack summary comment #2 from PR #2" in (
        captured.out
    )
    assert refreshed_state.changes[change_id].stack_comment_id is None
    assert _issue_comments(fake_repo, 2) == []


def test_close_apply_closes_pull_request_and_retires_active_state(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)

    exit_code = _main(repo, config_path, "close", "--apply", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "Applied close actions:" in captured.out
    assert fake_repo.pull_requests[1].state == "closed"
    assert refreshed_state.changes[change_id].pr_state == "closed"
    assert refreshed_state.changes[change_id].pr_review_decision is None
    assert refreshed_state.changes[change_id].stack_comment_id is None
    assert _issue_comments(fake_repo, 1) == []


def test_close_preview_closes_no_remote_state_and_reports_planned_actions(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()

    exit_code = _main(repo, config_path, "close", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "Planned close actions:" in captured.out
    assert fake_repo.pull_requests[1].state == "open"
    assert refreshed_state == initial_state
    assert _issue_comments(fake_repo, 1) == []


def test_close_apply_reports_blocked_when_github_is_unavailable(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
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

    def build_github_client(*, base_url: str) -> GithubClient:
        return OfflineGithubClient(
            base_url=base_url,
            transport=httpx.ASGITransport(app=app),
        )

    monkeypatch.setattr("jj_review.commands.close._build_github_client", build_github_client)
    monkeypatch.setattr(
        "jj_review.commands.review_state._build_github_client",
        build_github_client,
    )

    exit_code = _main(repo, config_path, "close", "--apply", change_id)
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
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    bookmark = ReviewStateStore.for_repo(repo).load().changes[change_id].bookmark
    assert bookmark is not None
    state_store = ReviewStateStore.for_repo(repo)
    action_order: list[str] = []
    original_delete_remote_bookmark = JjClient.delete_remote_bookmark
    original_forget_bookmark = JjClient.forget_bookmark

    def tracking_delete_remote_bookmark(
        self,
        *,
        remote: str,
        bookmark: str,
        expected_remote_target: str,
    ) -> None:
        action_order.append("remote")
        return original_delete_remote_bookmark(
            self,
            remote=remote,
            bookmark=bookmark,
            expected_remote_target=expected_remote_target,
        )

    def tracking_forget_bookmark(self, bookmark: str) -> None:
        action_order.append("local")
        return original_forget_bookmark(self, bookmark)

    monkeypatch.setattr(
        JjClient,
        "delete_remote_bookmark",
        tracking_delete_remote_bookmark,
    )
    monkeypatch.setattr(
        JjClient,
        "forget_bookmark",
        tracking_forget_bookmark,
    )

    exit_code = _main(repo, config_path, "close", "--apply", "--cleanup", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "Applied close actions:" in captured.out
    assert fake_repo.pull_requests[1].state == "closed"
    assert refreshed_state.changes[change_id].pr_state == "closed"
    assert refreshed_state.changes[change_id].stack_comment_id is None
    assert _issue_comments(fake_repo, 1) == []
    assert bookmark not in _remote_refs(fake_repo.git_dir)
    assert JjClient(repo).get_bookmark_state(bookmark).local_target is None
    assert action_order == ["remote", "local"]


def test_close_apply_rerun_is_idempotent(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)

    first_exit_code = _main(repo, config_path, "close", "--apply", change_id)
    capsys.readouterr()
    first_state = state_store.load()
    del fake_repo.pull_requests[1]

    second_exit_code = _main(repo, config_path, "close", "--apply", change_id)
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
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    bookmark = state_store.load().changes[change_id].bookmark
    assert bookmark is not None

    assert _main(repo, config_path, "close", "--apply", change_id) == 0
    capsys.readouterr()
    del fake_repo.pull_requests[1]

    exit_code = _main(repo, config_path, "close", "--apply", "--cleanup", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "Applied close actions:" in captured.out
    assert refreshed_state.changes[change_id].pr_state == "closed"
    assert refreshed_state.changes[change_id].stack_comment_id is None
    assert _issue_comments(fake_repo, 1) == []
    assert bookmark not in _remote_refs(fake_repo.git_dir)
    assert JjClient(repo).get_bookmark_state(bookmark).local_target is None


def test_close_apply_blocks_when_github_no_longer_reports_the_cached_pull_request(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    del fake_repo.pull_requests[1]

    exit_code = _main(repo, config_path, "close", "--apply", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "GitHub no longer reports a pull request" in captured.out
    assert state_store.load() == initial_state


def test_close_apply_checkpoints_prior_progress_before_later_block(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
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

    first_exit_code = _main(repo, config_path, "close", "--apply", head_change_id)
    first_run = capsys.readouterr()
    checkpointed_state = state_store.load()

    second_exit_code = _main(repo, config_path, "close", "--apply", head_change_id)
    second_run = capsys.readouterr()

    assert first_exit_code == 1
    assert second_exit_code == 1
    assert "Close blocked:" in first_run.out
    assert checkpointed_state.changes[first_change_id].pr_state == "open"
    assert checkpointed_state.changes[head_change_id].pr_state == "closed"
    assert fake_repo.pull_requests[1].state == "open"
    assert fake_repo.pull_requests[2].state == "closed"
    assert list(state_dir.glob("incomplete-*.toml")) == []
    assert "previous close was interrupted" not in second_run.out
    assert f"close PR #{head_pr_number}" not in second_run.out


def test_close_apply_cleanup_rechecks_cached_comment_ownership_when_pr_is_missing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)

    assert _main(repo, config_path, "close", "--apply", change_id) == 0
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

    exit_code = _main(repo, config_path, "close", "--apply", "--cleanup", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "cannot delete saved stack summary comment" in captured.out
    assert "does not belong to `jj-review`" in captured.out
    assert manual_comment in _issue_comments(fake_repo, 1)


def test_close_apply_cleanup_keeps_comment_cleanup_after_bookmark_block(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    bookmark = ReviewStateStore.for_repo(repo).load().changes[change_id].bookmark
    assert bookmark is not None
    initial_remote_target = _read_remote_ref(fake_repo.git_dir, bookmark)
    _run(["jj", "bookmark", "move", "--allow-backwards", bookmark, "--to", "main"], repo)

    exit_code = _main(repo, config_path, "close", "--apply", "--cleanup", change_id)
    captured = capsys.readouterr()
    local_target = JjClient(repo).get_bookmark_state(bookmark).local_target

    assert exit_code == 1
    assert "Close blocked:" in captured.out
    assert _issue_comments(fake_repo, 1) == []
    assert local_target == _read_remote_ref(fake_repo.git_dir, "main")
    assert _read_remote_ref(fake_repo.git_dir, bookmark) == initial_remote_target
    assert fake_repo.pull_requests[1].state == "closed"


def test_close_apply_closes_discovered_pull_request_after_sparse_state_loss(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    resolve_state_path(repo).unlink()

    exit_code = _main(repo, config_path, "close", "--apply", change_id)
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
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
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

    exit_code = _main(repo, config_path, "close", "--apply", "--cleanup", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Close blocked:" in captured.out
    assert "[blocked] stack summary comment:" in captured.out
    assert fake_repo.pull_requests[2].state == "closed"


def test_submit_checkpoints_successful_in_flight_pull_request_before_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    stack = JjClient(repo).discover_review_stack()
    change_id_1 = stack.revisions[0].change_id
    change_id_2 = stack.revisions[1].change_id

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class FailSpecificPullRequestClient(GithubClient):
        async def create_pull_request(
            self,
            owner,
            repo,
            *,
            base,
            body,
            draft=False,
            head,
            title,
        ):
            if title == "feature 2":
                await asyncio.sleep(0.01)
                raise GithubClientError(
                    "Simulated failure for feature 2",
                    status_code=500,
                )
            if title == "feature 1":
                await asyncio.sleep(0.03)
            return await super().create_pull_request(
                owner,
                repo,
                base=base,
                body=body,
                draft=draft,
                head=head,
                title=title,
            )

    def build_github_client(*, base_url: str) -> GithubClient:
        return FailSpecificPullRequestClient(
            base_url=base_url,
            transport=httpx.ASGITransport(app=app),
        )

    monkeypatch.setattr("jj_review.commands.submit._build_github_client", build_github_client)

    exit_code = _main(repo, config_path, "submit", "--current")
    capsys.readouterr()

    assert exit_code != 0

    state = ReviewStateStore.for_repo(repo).load()
    assert state.changes.get(change_id_1) is not None
    assert state.changes[change_id_1].pr_number is not None
    change2 = state.changes.get(change_id_2)
    assert change2 is None or change2.pr_number is None
    assert len(fake_repo.pull_requests) == 1
    assert fake_repo.pull_requests[1].title == "feature 1"
    pushed_review_refs = {
        ref: target
        for ref, target in _remote_refs(fake_repo.git_dir).items()
        if ref.startswith("refs/heads/review/")
    }
    assert len(pushed_review_refs) == 2
    assert set(pushed_review_refs.values()) == {
        revision.commit_id for revision in stack.revisions
    }


def test_submit_rerun_converges_pull_request_metadata_after_partial_create_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    config_path = _write_config(
        tmp_path,
        fake_repo,
        extra_lines=[
            'labels = ["needs-review"]',
            'reviewers = ["alice"]',
            'team_reviewers = ["platform"]',
        ],
    )
    _commit(repo, "feature 1", "feature-1.txt")

    app = create_app(FakeGithubState.single_repository(fake_repo))
    metadata_failure_injected = False

    class FlakyMetadataClient(GithubClient):
        async def add_labels(self, owner, repo, *, issue_number, labels):
            nonlocal metadata_failure_injected
            if not metadata_failure_injected:
                metadata_failure_injected = True
                raise GithubClientError(
                    "Simulated label failure",
                    status_code=500,
                )
            await super().add_labels(
                owner,
                repo,
                issue_number=issue_number,
                labels=labels,
            )

    def build_github_client(*, base_url: str) -> GithubClient:
        return FlakyMetadataClient(
            base_url=base_url,
            transport=httpx.ASGITransport(app=app),
        )

    monkeypatch.setattr("jj_review.commands.submit._build_github_client", build_github_client)

    assert _main(repo, config_path, "submit", "--current") == 1
    capsys.readouterr()

    state_after_failure = ReviewStateStore.for_repo(repo).load()
    assert len(fake_repo.pull_requests) == 1
    assert state_after_failure.changes == {}
    assert fake_repo.pull_requests[1].requested_reviewers == ["alice"]
    assert fake_repo.pull_requests[1].requested_team_reviewers == ["platform"]
    assert fake_repo.pull_requests[1].labels == []
    for intent_path in resolve_state_path(repo).parent.glob("incomplete-*.toml"):
        intent_path.unlink()

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    state_after_rerun = ReviewStateStore.for_repo(repo).load()

    assert state_after_rerun.changes[stack.revisions[0].change_id].pr_number == 1
    assert fake_repo.pull_requests[1].requested_reviewers == ["alice"]
    assert fake_repo.pull_requests[1].requested_team_reviewers == ["platform"]
    assert fake_repo.pull_requests[1].labels == ["needs-review"]


def test_submit_unchanged_rerun_skips_pull_request_metadata_writes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    config_path = _write_config(
        tmp_path,
        fake_repo,
        extra_lines=[
            'labels = ["needs-review"]',
            'reviewers = ["alice"]',
            'team_reviewers = ["platform"]',
        ],
    )
    _commit(repo, "feature 1", "feature-1.txt")
    app = create_app(FakeGithubState.single_repository(fake_repo))

    def initial_build_github_client(*, base_url: str) -> GithubClient:
        return GithubClient(
            base_url=base_url,
            transport=httpx.ASGITransport(app=app),
        )

    monkeypatch.setattr(
        "jj_review.commands.submit._build_github_client",
        initial_build_github_client,
    )

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    metadata_write_calls: list[str] = []

    class NoMetadataWritesClient(GithubClient):
        async def request_reviewers(
            self,
            owner,
            repo,
            *,
            pull_number,
            reviewers,
            team_reviewers,
        ) -> None:
            metadata_write_calls.append("reviewers")
            raise AssertionError("unchanged rerun should not request reviewers")

        async def add_labels(self, owner, repo, *, issue_number, labels) -> None:
            metadata_write_calls.append("labels")
            raise AssertionError("unchanged rerun should not add labels")

    def build_github_client(*, base_url: str) -> GithubClient:
        return NoMetadataWritesClient(
            base_url=base_url,
            transport=httpx.ASGITransport(app=app),
        )

    monkeypatch.setattr("jj_review.commands.submit._build_github_client", build_github_client)

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    assert metadata_write_calls == []


def test_submit_cli_reviewers_override_configured_reviewers(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    config_path = _write_config(
        tmp_path,
        fake_repo,
        extra_lines=[
            'reviewers = ["config-user"]',
            'team_reviewers = ["config-team"]',
        ],
    )
    _commit(repo, "feature 1", "feature-1.txt")
    app = create_app(FakeGithubState.single_repository(fake_repo))

    def build_github_client(*, base_url: str) -> GithubClient:
        return GithubClient(
            base_url=base_url,
            transport=httpx.ASGITransport(app=app),
        )

    monkeypatch.setattr("jj_review.commands.submit._build_github_client", build_github_client)

    exit_code = _main(
        repo,
        config_path,
        "submit",
        "--reviewers",
        "alice,bob",
        "--team-reviewers",
        "platform",
        "--reviewers",
        "carol,bob",
        "--team-reviewers",
        "infra,platform",
        "--current",
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "PR #1" in captured.out
    assert fake_repo.pull_requests[1].requested_reviewers == ["alice", "bob", "carol"]
    assert fake_repo.pull_requests[1].requested_team_reviewers == ["platform", "infra"]


def test_submit_checkpoints_successful_in_flight_stack_comment_before_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    for index in range(3):
        _commit(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    state_store = ReviewStateStore.for_repo(repo)
    stack = JjClient(repo).discover_review_stack()
    initial_state = state_store.load()
    change_id_1 = stack.revisions[0].change_id
    change_id_2 = stack.revisions[1].change_id
    change_id_3 = stack.revisions[2].change_id
    issue_number_1 = initial_state.changes[change_id_1].pr_number
    issue_number_2 = initial_state.changes[change_id_2].pr_number
    issue_number_3 = initial_state.changes[change_id_3].pr_number
    if issue_number_1 is None or issue_number_2 is None or issue_number_3 is None:
        raise AssertionError("Expected pull request numbers after initial submit.")

    for issue_number in (issue_number_1, issue_number_2, issue_number_3):
        fake_repo.issue_comments[issue_number] = []

    state_store.save(
        initial_state.model_copy(
            update={
                "changes": {
                    change_id: cached_change.model_copy(update={"stack_comment_id": None})
                    for change_id, cached_change in initial_state.changes.items()
                }
            }
        )
    )

    app = create_app(FakeGithubState.single_repository(fake_repo))
    started_issue_numbers: list[int] = []

    class FlakyCommentClient(GithubClient):
        async def list_issue_comments(self, owner, repo, *, issue_number):
            started_issue_numbers.append(issue_number)
            if issue_number == issue_number_2:
                await asyncio.sleep(0.01)
                raise GithubClientError(
                    "Simulated stack comment failure",
                    status_code=500,
                )
            if issue_number == issue_number_1:
                await asyncio.sleep(0.03)
            return await super().list_issue_comments(
                owner,
                repo,
                issue_number=issue_number,
            )

    def build_github_client(*, base_url: str) -> GithubClient:
        return FlakyCommentClient(
            base_url=base_url,
            transport=httpx.ASGITransport(app=app),
        )

    monkeypatch.setattr("jj_review.commands.submit._GITHUB_INSPECTION_CONCURRENCY", 2)
    monkeypatch.setattr("jj_review.commands.submit._build_github_client", build_github_client)

    assert _main(repo, config_path, "submit", "--current") == 1
    capsys.readouterr()

    refreshed_state = state_store.load()

    assert refreshed_state.changes[change_id_1].stack_comment_id is not None
    assert refreshed_state.changes[change_id_2].stack_comment_id is None
    assert refreshed_state.changes[change_id_3].stack_comment_id is None
    assert issue_number_1 in started_issue_numbers
    assert issue_number_2 in started_issue_numbers
    assert issue_number_3 not in started_issue_numbers
    assert len(_issue_comments(fake_repo, issue_number_1)) == 1
    assert _issue_comments(fake_repo, issue_number_2) == []
    assert _issue_comments(fake_repo, issue_number_3) == []


def test_submit_writes_and_deletes_intent_file_on_success(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    exit_code = _main(repo, config_path, "submit", "--current")
    capsys.readouterr()

    assert exit_code == 0
    state_dir = resolve_state_path(repo).parent
    intent_files = list(state_dir.glob("incomplete-*.toml"))
    assert intent_files == [], f"Expected no intent files, found: {intent_files}"


def test_submit_leaves_intent_file_on_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    stack = JjClient(repo).discover_review_stack()
    change_id_1 = stack.revisions[0].change_id
    change_id_2 = stack.revisions[1].change_id

    app = create_app(FakeGithubState.single_repository(fake_repo))
    call_count = [0]

    class FailOnFirstPRClient(GithubClient):
        async def create_pull_request(
            self,
            owner,
            repo,
            *,
            base,
            body,
            draft=False,
            head,
            title,
        ):
            call_count[0] += 1
            if call_count[0] >= 1:
                raise GithubClientError(
                    "Simulated failure on first PR", status_code=500
                )
            return await super().create_pull_request(
                owner,
                repo,
                base=base,
                body=body,
                draft=draft,
                head=head,
                title=title,
            )

    def build_github_client(*, base_url: str) -> GithubClient:
        return FailOnFirstPRClient(
            base_url=base_url,
            transport=httpx.ASGITransport(app=app),
        )

    monkeypatch.setattr("jj_review.commands.submit._build_github_client", build_github_client)

    exit_code = _main(repo, config_path, "submit", "--current")
    capsys.readouterr()

    assert exit_code != 0
    pushed_review_refs = {
        ref: target
        for ref, target in _remote_refs(fake_repo.git_dir).items()
        if ref.startswith("refs/heads/review/")
    }
    assert len(pushed_review_refs) == 2
    assert set(pushed_review_refs.values()) == {
        revision.commit_id for revision in stack.revisions
    }
    state_dir = resolve_state_path(repo).parent
    intent_files = list(state_dir.glob("incomplete-*.toml"))
    assert len(intent_files) == 1

    import tomllib
    with intent_files[0].open("rb") as f:
        data = tomllib.load(f)
    assert data["kind"] == "submit"
    stored_ids = data.get("ordered_change_ids", [])
    assert change_id_1 in stored_ids
    assert change_id_2 in stored_ids


def test_submit_resumes_and_retires_stale_intent(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    stack = JjClient(repo).discover_review_stack()
    change_id_1 = stack.revisions[0].change_id
    change_id_2 = stack.revisions[1].change_id
    state_dir = resolve_state_path(repo).parent

    # Write a stale intent with dead PID (99999999 is almost certainly dead)
    old_intent = SubmitIntent(
        kind="submit",
        pid=99999999,
        label="submit on @",
        display_revset="@",
        head_change_id=change_id_2,
        ordered_change_ids=(change_id_1, change_id_2),
        bookmarks={},
        bases={},
        started_at="2026-01-01T00:00:00+00:00",
    )
    old_intent_path = write_intent(state_dir, old_intent)

    exit_code = _main(repo, config_path, "submit", "--current")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Resuming interrupted" in captured.out
    # Old intent file should be gone after success
    assert not old_intent_path.exists()
    # No intent files remain
    intent_files = list(state_dir.glob("incomplete-*.toml"))
    assert intent_files == []


def test_submit_warns_on_overlapping_stale_intent(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")
    _commit(repo, "feature 2", "feature-2.txt")

    stack = JjClient(repo).discover_review_stack()
    change_id_1 = stack.revisions[0].change_id
    state_dir = resolve_state_path(repo).parent

    # Write a stale intent with only the first change ID (partial overlap)
    old_intent = SubmitIntent(
        kind="submit",
        pid=99999999,
        label="submit on @",
        display_revset="@",
        head_change_id=change_id_1,
        ordered_change_ids=(change_id_1,),
        bookmarks={},
        bases={},
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_intent(state_dir, old_intent)

    exit_code = _main(repo, config_path, "submit", "--current")
    capsys.readouterr()

    assert exit_code == 0
    # Old intent is a prefix of new, so it should be retired (superset match)
    # No warning should appear for superset (it proceeds silently)
    # Actually: old=(change_id_1,), new=(change_id_1, change_id_2)
    # match_ordered_change_ids(old, new) == "superset" => silent retirement
    intent_files = list(state_dir.glob("incomplete-*.toml"))
    assert intent_files == []


def test_status_shows_outstanding_submit_intent(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    # First do a submit to create cache state
    assert _main(repo, config_path, "submit", "--current") == 0
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
        head_change_id=change_id,
        ordered_change_ids=(change_id,),
        bookmarks={},
        bases={},
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_intent(state_dir, intent)

    _main(repo, config_path, "status")
    captured = capsys.readouterr()

    assert "submit on @" in captured.out
    assert "interrupted" in captured.out


def test_status_exits_nonzero_for_overlapping_intent(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    # First do a submit to create cache state
    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[0].change_id
    state_dir = resolve_state_path(repo).parent

    # Write an outstanding intent overlapping the current stack
    intent = SubmitIntent(
        kind="submit",
        pid=99999999,
        label="submit on @",
        display_revset="@",
        head_change_id=change_id,
        ordered_change_ids=(change_id,),
        bookmarks={},
        bases={},
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_intent(state_dir, intent)

    exit_code = _main(repo, config_path, "status")
    capsys.readouterr()

    assert exit_code == 1


def test_status_exits_zero_for_stale_intent(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Stale intents are advisory only when their change IDs no longer resolve."""
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    # First do a submit to create cache state
    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    state_dir = resolve_state_path(repo).parent

    # Write an intent with a non-resolving change_id — classifies as stale
    intent = SubmitIntent(
        kind="submit",
        pid=99999999,
        label="submit on other-branch",
        display_revset="other-branch",
        head_change_id="zzzzzzzzzzzz",
        ordered_change_ids=("zzzzzzzzzzzz",),
        bookmarks={},
        bases={},
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_intent(state_dir, intent)

    exit_code = _main(repo, config_path, "status")
    capsys.readouterr()

    # Stale intent: shown in stale section, exit code 0 (advisory only)
    assert exit_code == 0


def test_status_exits_zero_for_disjoint_intent(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """An outstanding intent on a different stack is advisory only and doesn't raise exit code."""
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    # Create feature 1 on top of main
    _commit(repo, "feature 1", "feature-1.txt")
    stack_1 = JjClient(repo).discover_review_stack()
    feature_1_change_id = stack_1.revisions[0].change_id

    # Submit feature 1 to get a PR link (ensures the change_id resolves)
    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    # Create feature 2 branching off main independently (not on top of feature 1)
    _run(["jj", "new", "main", "-m", "feature 2"], repo)
    _write_file(repo / "feature-2.txt", "feature 2\n")
    _run(["jj", "describe", "-m", "feature 2"], repo)

    # Submit feature 2 to create its own PR link
    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack_2 = JjClient(repo).discover_review_stack()
    feature_2_change_id = stack_2.revisions[0].change_id
    state_dir = resolve_state_path(repo).parent

    # Write an outstanding (not stale) intent referencing ONLY feature-1's change ID
    # with a dead PID — so it is outstanding (change ID resolves in repo) but
    # disjoint from feature-2's stack
    intent = SubmitIntent(
        kind="submit",
        pid=99999999,
        label="submit on feature-1-branch",
        display_revset="feature-1",
        head_change_id=feature_1_change_id,
        ordered_change_ids=(feature_1_change_id,),
        bookmarks={},
        bases={},
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_intent(state_dir, intent)

    # Run status scoped to feature-2 stack — the intent is outstanding but disjoint
    exit_code = _main(repo, config_path, "status", feature_2_change_id)
    captured = capsys.readouterr()

    # Disjoint outstanding intent: advisory-only, exit code is not raised
    assert exit_code == 0
    # The intent label should appear in the output (advisory notice)
    assert "submit on feature-1-branch" in captured.out


def test_cleanup_apply_writes_and_deletes_intent_file_on_success(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """cleanup --apply deletes its intent file on success."""
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    bookmark = ReviewStateStore.for_repo(repo).load().changes[change_id].bookmark
    assert bookmark is not None

    # Abandon the change and delete the bookmark to make it stale
    _run(["jj", "abandon", change_id], repo)
    _run(["jj", "bookmark", "delete", bookmark], repo)

    exit_code = _main(repo, config_path, "cleanup", "--apply")
    capsys.readouterr()

    assert exit_code == 0
    state_dir = resolve_state_path(repo).parent
    intent_files = list(state_dir.glob("incomplete-*.toml"))
    assert intent_files == [], f"Expected no intent files after success, found: {intent_files}"


def test_cleanup_apply_leaves_intent_file_on_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """cleanup --apply leaves its intent file behind when it fails mid-way."""
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    bookmark = ReviewStateStore.for_repo(repo).load().changes[change_id].bookmark
    assert bookmark is not None

    _run(["jj", "abandon", change_id], repo)
    _run(["jj", "bookmark", "delete", bookmark], repo)

    def failing_delete_remote_bookmark(self, *, remote, bookmark, expected_remote_target):
        raise RuntimeError("Simulated failure during cleanup apply")

    monkeypatch.setattr(
        "jj_review.commands.cleanup.JjClient.delete_remote_bookmark",
        failing_delete_remote_bookmark,
    )

    with pytest.raises(RuntimeError, match="Simulated failure"):
        _main(repo, config_path, "cleanup", "--apply")
    capsys.readouterr()

    state_dir = resolve_state_path(repo).parent
    intent_files = list(state_dir.glob("incomplete-*.toml"))
    assert len(intent_files) == 1, f"Expected 1 intent file after failure, found: {intent_files}"

    import tomllib
    with intent_files[0].open("rb") as f:
        data = tomllib.load(f)
    assert data["kind"] == "cleanup-apply"


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


def test_land_previews_and_applies_trunk_open_prefix(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    for index in range(3):
        _commit(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    state_store = ReviewStateStore.for_repo(repo)
    submitted_state = state_store.load()
    change_id_1 = stack.revisions[0].change_id
    change_id_2 = stack.revisions[1].change_id
    change_id_3 = stack.revisions[2].change_id
    bookmark_1 = submitted_state.changes[change_id_1].bookmark
    bookmark_2 = submitted_state.changes[change_id_2].bookmark
    if bookmark_1 is None or bookmark_2 is None:
        raise AssertionError("Expected saved review bookmarks after submit.")

    fake_repo.pull_requests[3].state = "closed"

    preview_exit_code = _main(repo, config_path, "land", "--current")
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

    apply_exit_code = _main(repo, config_path, "land", "--current", "--apply")
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


def test_land_apply_requires_saved_preview(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    exit_code = _main(repo, config_path, "land", "--current", "--apply")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "`land --apply` requires a saved preview" in captured.err


def test_land_apply_rejects_changed_plan_since_preview(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    for index in range(2):
        _commit(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    assert _main(repo, config_path, "land", "--current") == 0
    capsys.readouterr()

    fake_repo.pull_requests[2].state = "closed"

    exit_code = _main(repo, config_path, "land", "--current", "--apply")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "changed since the saved preview" in captured.err
    stack = JjClient(repo).discover_review_stack()
    assert _read_remote_ref(fake_repo.git_dir, "main") == stack.trunk.commit_id


def test_land_restores_local_trunk_bookmark_when_push_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    for index in range(2):
        _commit(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()
    assert _main(repo, config_path, "land", "--current") == 0
    capsys.readouterr()

    client = JjClient(repo)
    trunk_before = client.get_bookmark_state("main").local_target
    remote_before = _read_remote_ref(fake_repo.git_dir, "main")
    original_push_bookmark = JjClient.push_bookmark

    def fail_push_bookmark(self, *, remote: str, bookmark: str) -> None:
        raise JjCommandError("simulated trunk push failure")

    monkeypatch.setattr(JjClient, "push_bookmark", fail_push_bookmark)

    exit_code = _main(repo, config_path, "land", "--current", "--apply")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "simulated trunk push failure" in captured.err
    assert JjClient(repo).get_bookmark_state("main").local_target == trunk_before
    assert _read_remote_ref(fake_repo.git_dir, "main") == remote_before
    monkeypatch.setattr(JjClient, "push_bookmark", original_push_bookmark)


def test_land_restores_local_trunk_bookmark_when_push_is_interrupted(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    for index in range(2):
        _commit(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()
    assert _main(repo, config_path, "land", "--current") == 0
    capsys.readouterr()

    client = JjClient(repo)
    trunk_before = client.get_bookmark_state("main").local_target
    remote_before = _read_remote_ref(fake_repo.git_dir, "main")

    def interrupt_push_bookmark(self, *, remote: str, bookmark: str) -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr(JjClient, "push_bookmark", interrupt_push_bookmark)

    exit_code = _main(repo, config_path, "land", "--current", "--apply")
    captured = capsys.readouterr()

    assert exit_code == 130
    assert "Interrupted." in captured.err
    assert JjClient(repo).get_bookmark_state("main").local_target == trunk_before
    assert _read_remote_ref(fake_repo.git_dir, "main") == remote_before


def test_land_rejects_pre_push_resume_when_plan_changed_since_preview(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    for index in range(2):
        _commit(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()
    assert _main(repo, config_path, "land", "--current") == 0
    capsys.readouterr()

    push_calls = 0
    original_push_bookmark = JjClient.push_bookmark

    def fail_first_push_bookmark(self, *, remote: str, bookmark: str) -> None:
        nonlocal push_calls
        push_calls += 1
        if push_calls == 1:
            raise JjCommandError("simulated trunk push failure")
        original_push_bookmark(self, remote=remote, bookmark=bookmark)

    monkeypatch.setattr(JjClient, "push_bookmark", fail_first_push_bookmark)

    first_exit_code = _main(repo, config_path, "land", "--current", "--apply")
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

    second_exit_code = _main(repo, config_path, "land", "--current", "--apply")
    second_run = capsys.readouterr()

    assert second_exit_code == 1
    assert "changed since the saved preview" in second_run.err


def test_land_resumes_after_trunk_push_interruption(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    for index in range(2):
        _commit(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()
    assert _main(repo, config_path, "land", "--current") == 0
    capsys.readouterr()
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

    def failing_build_github_client(*, base_url: str) -> GithubClient:
        return FailingFinalizeClient(
            base_url=base_url,
            transport=httpx.ASGITransport(app=app),
        )

    def build_github_client(*, base_url: str) -> GithubClient:
        return GithubClient(
            base_url=base_url,
            transport=httpx.ASGITransport(app=app),
        )

    monkeypatch.setattr(
        "jj_review.commands.land._build_github_client",
        failing_build_github_client,
    )

    first_exit_code = _main(repo, config_path, "land", "--current", "--apply")
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

    monkeypatch.setattr("jj_review.commands.land._build_github_client", build_github_client)

    second_exit_code = _main(repo, config_path, "land", "--current", "--apply")
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


def test_land_preview_reports_saved_preview_write_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = _init_repo(tmp_path)
    config_path = _configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _commit(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit", "--current") == 0
    capsys.readouterr()

    def fail_preview_write(*args, **kwargs):
        raise OSError("simulated preview write failure")

    monkeypatch.setattr("jj_review.commands.land.tempfile.mkstemp", fail_preview_write)

    exit_code = _main(repo, config_path, "land", "--current")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Could not write saved land preview" in captured.err


def _configure_submit_environment(
    monkeypatch,
    tmp_path: Path,
    fake_repo: FakeGithubRepository,
) -> Path:
    return configure_fake_github_environment(
        command_modules=(
            "jj_review.commands.submit",
            "jj_review.commands.relink",
            "jj_review.commands.close",
            "jj_review.commands.cleanup",
            "jj_review.commands.land",
            "jj_review.commands.review_state",
        ),
        fake_repo=fake_repo,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )


def _init_repo(tmp_path: Path) -> tuple[Path, FakeGithubRepository]:
    return init_fake_github_repo(tmp_path)


def _commit(repo: Path, message: str, filename: str) -> None:
    commit_file(repo, message, filename)


def _issue_comments(fake_repo: FakeGithubRepository, issue_number: int):
    return fake_repo.issue_comments.get(issue_number, [])


def _read_remote_ref(remote: Path, bookmark: str) -> str:
    completed = run_command(
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


def _run(command: list[str], cwd: Path):
    return run_command(command, cwd)


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
    return write_fake_github_config(
        tmp_path,
        fake_repo,
        extra_lines=extra_lines,
    )


def _write_file(path: Path, contents: str) -> None:
    write_file(path, contents)
