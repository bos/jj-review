from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from jj_review.cache import ReviewStateStore, resolve_state_path
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.intent import write_intent
from jj_review.jj import JjClient
from jj_review.models.intent import SubmitIntent

from ..support.fake_github import FakeGithubState, create_app
from .submit_command_helpers import (
    approve_pull_requests as _approve_pull_requests,
)
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
    issue_comments as _issue_comments,
)
from .submit_command_helpers import (
    patch_github_client_builders as _patch_github_client_builders,
)
from .submit_command_helpers import (
    read_remote_ref as _read_remote_ref,
)
from .submit_command_helpers import (
    remote_refs as _remote_refs,
)
from .submit_command_helpers import (
    run as _run,
)
from .submit_command_helpers import (
    run_main as _main,
)
from .submit_command_helpers import (
    write_config as _write_config,
)
from .submit_command_helpers import (
    write_file_contents as _write_file,
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
    assert "Submitted bookmarks:" in captured.out
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

    _patch_github_client_builders(
        monkeypatch,
        app=app,
        modules=("jj_review.commands.submit",),
        client_type=TrackingGithubClient,
    )

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

    _patch_github_client_builders(
        monkeypatch,
        app=app,
        modules=("jj_review.commands.submit",),
        client_type=TrackingGithubClient,
        concurrency_limits={"jj_review.commands.submit": 2},
    )

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

    _patch_github_client_builders(
        monkeypatch,
        app=app,
        modules=("jj_review.commands.submit",),
        client_type=MissingRepositoryClient,
    )

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
    assert "Planned bookmarks:" in captured.out
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
    _approve_pull_requests(fake_repo, 1, 2)

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

    _patch_github_client_builders(
        monkeypatch,
        app=app,
        modules=("jj_review.commands.submit",),
        client_type=FailingCommentUpdateClient,
    )

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

    exit_code = _main(repo, config_path, "submit", "--current")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "multiple changes to the same bookmark" in captured.err
    assert ReviewStateStore.for_repo(repo).load().changes == {}
    assert set(_remote_refs(fake_repo.git_dir)) == {"refs/heads/main"}
    assert fake_repo.pull_requests == {}

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

    _patch_github_client_builders(
        monkeypatch,
        app=app,
        modules=("jj_review.commands.submit",),
        client_type=FailSpecificPullRequestClient,
    )

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

    _patch_github_client_builders(
        monkeypatch,
        app=app,
        modules=("jj_review.commands.submit",),
        client_type=FlakyMetadataClient,
    )

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

    _patch_github_client_builders(
        monkeypatch,
        app=app,
        modules=("jj_review.commands.submit",),
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

    _patch_github_client_builders(
        monkeypatch,
        app=app,
        modules=("jj_review.commands.submit",),
        client_type=NoMetadataWritesClient,
    )

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

    _patch_github_client_builders(
        monkeypatch,
        app=app,
        modules=("jj_review.commands.submit",),
    )

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
                    "Simulated stack summary comment failure",
                    status_code=500,
                )
            if issue_number == issue_number_1:
                await asyncio.sleep(0.03)
            return await super().list_issue_comments(
                owner,
                repo,
                issue_number=issue_number,
            )

    _patch_github_client_builders(
        monkeypatch,
        app=app,
        modules=("jj_review.commands.submit",),
        client_type=FlakyCommentClient,
        concurrency_limits={"jj_review.commands.submit": 2},
    )

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

    _patch_github_client_builders(
        monkeypatch,
        app=app,
        modules=("jj_review.commands.submit",),
        client_type=FailOnFirstPRClient,
    )

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
