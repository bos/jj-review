from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from jj_review.commands.submit import (
    GeneratedDescription,
    SubmittedRevision,
    _ensure_pull_request_link_is_consistent,
    _ensure_remote_can_be_updated,
    _preflight_private_commits,
    _pull_request_body,
    _render_stack_comment,
    _repair_interrupted_untracked_remote_bookmarks,
    _resolve_generated_descriptions,
    _resolve_local_action,
    _run_description_command,
)
from jj_review.errors import CliError
from jj_review.intent import write_new_intent
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.cache import CachedChange, ReviewState
from jj_review.models.github import GithubBranchRef, GithubPullRequest
from jj_review.models.intent import SubmitIntent
from jj_review.models.stack import LocalRevision
from tests.support.revision_helpers import make_revision


def test_run_description_command_returns_title_and_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_run(command, *, capture_output, check, cwd, env, text):
        assert command == ["helper", "--pr", "abc123"]
        assert capture_output is True
        assert check is False
        assert cwd == tmp_path
        assert env is None
        assert text is True
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"title":"generated title","body":"generated body"}\n',
            stderr="",
        )

    monkeypatch.setattr("jj_review.commands.submit.subprocess.run", fake_run)

    assert _run_description_command(
        command="helper",
        kind="pr",
        repo_root=tmp_path,
        revset="abc123",
    ) == GeneratedDescription(title="generated title", body="generated body")


def test_run_description_command_rejects_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_run(command, *, capture_output, check, cwd, env, text):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="not json\n",
            stderr="",
        )

    monkeypatch.setattr("jj_review.commands.submit.subprocess.run", fake_run)

    with pytest.raises(CliError, match="invalid JSON"):
        _run_description_command(
            command="helper",
            kind="stack",
            repo_root=tmp_path,
            revset="@",
        )


def test_run_description_command_passes_extra_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_run(command, *, capture_output, check, cwd, env, text):
        assert env is not None
        assert env["JJ_REVIEW_STACK_INPUT_FILE"] == "/tmp/stack-input.json"
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"title":"generated title","body":"generated body"}\n',
            stderr="",
        )

    monkeypatch.setattr("jj_review.commands.submit.subprocess.run", fake_run)

    assert _run_description_command(
        command="helper",
        extra_env={"JJ_REVIEW_STACK_INPUT_FILE": "/tmp/stack-input.json"},
        kind="stack",
        repo_root=tmp_path,
        revset="@",
    ) == GeneratedDescription(title="generated title", body="generated body")


def test_resolve_generated_descriptions_uses_default_commit_mapping(tmp_path: Path) -> None:
    revisions = (
        make_revision(
            commit_id="head",
            change_id="head-change",
            description="feature\n\nbody line\n",
        ),
    )

    descriptions, stack_description = _resolve_generated_descriptions(
        describe_with=None,
        repo_root=tmp_path,
        revisions=revisions,
        selected_revset="@",
    )

    assert descriptions == {
        "head-change": GeneratedDescription(title="feature", body="body line")
    }
    assert stack_description is None


def test_resolve_generated_descriptions_passes_generated_pr_data_to_stack_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    revisions = (
        make_revision(
            commit_id="bottom",
            change_id="bottom-change",
            description="bottom feature\n\nbottom body\n",
        ),
        make_revision(
            commit_id="top",
            change_id="top-change",
            description="top feature\n\ntop body\n",
        ),
    )
    seen_stack_payload: dict[str, object] | None = None

    def fake_run_description_command(*, command, extra_env=None, kind, repo_root, revset):
        nonlocal seen_stack_payload
        assert command == "helper"
        assert repo_root == tmp_path
        if kind == "pr":
            return GeneratedDescription(title=f"AI {revset}", body=f"Body {revset}")
        assert extra_env is not None
        stack_input_path = extra_env["JJ_REVIEW_STACK_INPUT_FILE"]
        seen_stack_payload = json.loads(Path(stack_input_path).read_text(encoding="utf-8"))
        return GeneratedDescription(title="stack title", body="stack body")

    def fake_diffstat(*, repo_root: Path, revset: str) -> str:
        assert repo_root == tmp_path
        return f"diffstat {revset}"

    monkeypatch.setattr(
        "jj_review.commands.submit._run_description_command",
        fake_run_description_command,
    )
    monkeypatch.setattr("jj_review.commands.submit._describe_with_diffstat", fake_diffstat)

    descriptions, stack_description = _resolve_generated_descriptions(
        describe_with="helper",
        repo_root=tmp_path,
        revisions=revisions,
        selected_revset="@",
    )

    assert descriptions == {
        "bottom-change": GeneratedDescription(
            title="AI bottom-change",
            body="Body bottom-change",
        ),
        "top-change": GeneratedDescription(title="AI top-change", body="Body top-change"),
    }
    assert stack_description == GeneratedDescription(title="stack title", body="stack body")
    assert seen_stack_payload == {
        "revisions": [
            {
                "body": "Body bottom-change",
                "change_id": "bottom-change",
                "diffstat": "diffstat bottom-change",
                "title": "AI bottom-change",
            },
            {
                "body": "Body top-change",
                "change_id": "top-change",
                "diffstat": "diffstat top-change",
                "title": "AI top-change",
            },
        ]
    }


def test_render_stack_comment_lists_full_stack_top_to_bottom() -> None:
    bottom = _submitted_revision(
        change_id="bottom-change",
        pull_request_number=1,
        pull_request_title="feature 1",
        pull_request_url="https://github.test/octo-org/repo/pull/1",
        subject="local feature 1",
    )
    top = _submitted_revision(
        change_id="top-change",
        pull_request_number=2,
        pull_request_title="feature 2",
        pull_request_url="https://github.test/octo-org/repo/pull/2",
        subject="local feature 2",
    )

    rendered = _render_stack_comment(
        current=bottom,
        revisions=(bottom, top),
        stack_description=None,
        trunk_branch="main",
    )

    assert "Stack:\n[feature 2](https://github.test/octo-org/repo/pull/2)\n" in rendered
    assert "**feature 1**\ntrunk `main`" in rendered
    assert "[#1]" not in rendered
    assert "[#2]" not in rendered


def test_pull_request_body_falls_back_to_subject_when_commit_has_no_body() -> None:
    assert _pull_request_body("subject only") == "subject only"


def test_pull_request_body_uses_remaining_commit_description_when_present() -> None:
    assert _pull_request_body("subject line\n\nbody line\nbody detail") == (
        "body line\nbody detail"
    )


def test_resolve_local_action_rejects_conflicted_bookmark() -> None:
    with pytest.raises(
        CliError,
        match="2 conflicting local targets",
    ):
        _resolve_local_action("review/foo", ("abc123", "def456"), "abc123")


def test_ensure_remote_can_be_updated_rejects_conflicted_remote_bookmark() -> None:
    with pytest.raises(
        CliError,
        match="Remote bookmark 'review/foo'@origin is conflicted",
    ):
        _ensure_remote_can_be_updated(
            bookmark="review/foo",
            bookmark_source="cache",
            bookmark_state=BookmarkState(name="review/foo"),
            change_id="change-a",
            desired_target="zzz999",
            remote="origin",
            remote_state=RemoteBookmarkState(
                remote="origin",
                targets=("abc123", "def456"),
                tracking_targets=("abc123", "def456"),
            ),
            state=ReviewState(changes={"change-a": CachedChange(bookmark="review/foo")}),
        )


def test_ensure_remote_can_be_updated_rejects_unproven_existing_remote_branch() -> None:
    with pytest.raises(
        CliError,
        match="already exists and points elsewhere",
    ):
        _ensure_remote_can_be_updated(
            bookmark="review/foo",
            bookmark_source="generated",
            bookmark_state=BookmarkState(name="review/foo"),
            change_id="change-a",
            desired_target="def456",
            remote="origin",
            remote_state=RemoteBookmarkState(remote="origin", targets=("abc123",)),
            state=ReviewState(),
        )


def test_ensure_remote_can_be_updated_allows_matching_untracked_remote_branch() -> None:
    _ensure_remote_can_be_updated(
        bookmark="review/foo",
        bookmark_source="generated",
        bookmark_state=BookmarkState(name="review/foo"),
        change_id="change-a",
        desired_target="abc123",
        remote="origin",
        remote_state=RemoteBookmarkState(remote="origin", targets=("abc123",)),
        state=ReviewState(),
    )


def test_repair_interrupted_untracked_remote_bookmarks_tracks_matching_remote_targets(
    tmp_path,
) -> None:
    calls: list[tuple[str, str, tuple[str, ...] | str]] = []

    class FakeJjClient:
        def fetch_remote(self, *, remote: str) -> None:
            calls.append(("fetch", remote, ""))

        def list_bookmark_states(
            self,
            bookmarks: tuple[str, ...] | None = None,
        ) -> dict[str, BookmarkState]:
            calls.append(("list", "origin", tuple(bookmarks or ())))
            return {
                "review/foo": BookmarkState(
                    name="review/foo",
                    local_targets=("abc123",),
                    remote_targets=(
                        RemoteBookmarkState(
                            remote="origin",
                            targets=("abc123",),
                            tracking_targets=(),
                        ),
                    ),
                ),
                "review/bar": BookmarkState(
                    name="review/bar",
                    local_targets=("new456",),
                    remote_targets=(
                        RemoteBookmarkState(
                            remote="origin",
                            targets=("old456",),
                            tracking_targets=(),
                        ),
                    ),
                ),
                "review/foreign-remote": BookmarkState(
                    name="review/foreign-remote",
                    local_targets=("abc123",),
                    remote_targets=(
                        RemoteBookmarkState(
                            remote="origin",
                            targets=("abc123",),
                            tracking_targets=(),
                        ),
                    ),
                ),
                "review/foreign-repo": BookmarkState(
                    name="review/foreign-repo",
                    local_targets=("abc123",),
                    remote_targets=(
                        RemoteBookmarkState(
                            remote="origin",
                            targets=("abc123",),
                            tracking_targets=(),
                        ),
                    ),
                ),
            }

        def track_bookmark(self, *, remote: str, bookmark: str) -> None:
            calls.append(("track", remote, bookmark))

    write_new_intent(
        tmp_path,
        SubmitIntent(
            kind="submit",
            pid=99999999,
            label="submit on @",
            display_revset="@",
            head_change_id="change-b",
            remote_name="origin",
            github_host="github.test",
            github_owner="octo-org",
            github_repo="stacked-review",
            ordered_change_ids=("change-a", "change-b"),
            bookmarks={"change-a": "review/foo", "change-b": "review/bar"},
            started_at="2026-01-01T00:00:00+00:00",
        ),
    )
    write_new_intent(
        tmp_path,
        SubmitIntent(
            kind="submit",
            pid=99999998,
            label="submit on foreign remote",
            display_revset="@",
            head_change_id="change-c",
            remote_name="upstream",
            github_host="github.test",
            github_owner="octo-org",
            github_repo="stacked-review",
            ordered_change_ids=("change-c",),
            bookmarks={"change-c": "review/foreign-remote"},
            started_at="2026-01-01T00:00:00+00:00",
        ),
    )
    write_new_intent(
        tmp_path,
        SubmitIntent(
            kind="submit",
            pid=99999997,
            label="submit on reused origin",
            display_revset="@",
            head_change_id="change-d",
            remote_name="origin",
            github_host="github.test",
            github_owner="octo-org",
            github_repo="other-review",
            ordered_change_ids=("change-d",),
            bookmarks={"change-d": "review/foreign-repo"},
            started_at="2026-01-01T00:00:00+00:00",
        ),
    )

    _repair_interrupted_untracked_remote_bookmarks(
        client=FakeJjClient(),
        remote=GitRemote(
            name="origin",
            url="https://github.test/octo-org/stacked-review.git",
        ),
        state_dir=tmp_path,
    )

    assert calls == [
        ("fetch", "origin", ""),
        ("list", "origin", ("review/bar", "review/foo")),
        ("track", "origin", "review/foo"),
    ]


def test_pull_request_link_rejects_missing_discovered_pull_request() -> None:
    with pytest.raises(
        CliError,
        match="Saved pull request link exists",
    ):
        _ensure_pull_request_link_is_consistent(
            bookmark="review/foo",
            cached_change=CachedChange(
                bookmark="review/foo",
                pr_number=17,
                pr_url="https://github.test/octo-org/repo/pull/17",
            ),
            change_id="change-17",
            discovered_pull_request=None,
        )


def test_pull_request_link_rejects_mismatched_pull_request_number() -> None:
    with pytest.raises(
        CliError,
        match="Saved pull request #17 does not match",
    ):
        _ensure_pull_request_link_is_consistent(
            bookmark="review/foo",
            cached_change=CachedChange(bookmark="review/foo", pr_number=17),
            change_id="change-17",
            discovered_pull_request=_github_pull_request(number=21),
        )


class _FakeJjClientWithPrivateCommits:
    def __init__(self, private_revisions: tuple[LocalRevision, ...]) -> None:
        self._private_revisions = private_revisions

    def find_private_commits(
        self, revisions: tuple[LocalRevision, ...]
    ) -> tuple[LocalRevision, ...]:
        return self._private_revisions


def test_preflight_private_commits_passes_when_no_private_commits() -> None:
    client = _FakeJjClientWithPrivateCommits(())
    revisions = (
        make_revision(commit_id="head", change_id="head-change", description="feature\n"),
    )

    _preflight_private_commits(client, revisions)  # no exception


def test_preflight_private_commits_raises_on_private_commit() -> None:
    private = make_revision(
        commit_id="head", change_id="head-change", description="private thing\n"
    )
    client = _FakeJjClientWithPrivateCommits((private,))

    with pytest.raises(CliError, match="git.private-commits"):
        _preflight_private_commits(client, (private,))


def test_preflight_private_commits_error_names_the_blocked_changes() -> None:
    private = make_revision(
        commit_id="abc12345", change_id="abcd1234", description="secret work\n"
    )
    client = _FakeJjClientWithPrivateCommits((private,))

    with pytest.raises(CliError, match="secret work"):
        _preflight_private_commits(client, (private,))


def _github_pull_request(number: int) -> GithubPullRequest:
    return GithubPullRequest(
        base=GithubBranchRef(ref="main"),
        body="",
        head=GithubBranchRef(ref="review/foo"),
        html_url=f"https://github.test/octo-org/repo/pull/{number}",
        number=number,
        state="open",
        title="feature",
    )


def _submitted_revision(
    *,
    change_id: str,
    commit_id: str | None = None,
    pull_request_number: int,
    pull_request_title: str,
    pull_request_url: str,
    subject: str,
) -> SubmittedRevision:
    return SubmittedRevision(
        bookmark=f"review/{change_id}",
        bookmark_source="generated",
        change_id=change_id,
        commit_id=commit_id or f"{change_id}-commit",
        local_action="unchanged",
        pull_request_action="unchanged",
        pull_request_is_draft=False,
        pull_request_number=pull_request_number,
        pull_request_title=pull_request_title,
        pull_request_url=pull_request_url,
        remote_action="up to date",
        subject=subject,
    )
