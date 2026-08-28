from __future__ import annotations

from pathlib import Path

from jj_stack.errors import CliError
from jj_stack.github.overview_comments import STACK_OVERVIEW_COMMENT_MARKER
from jj_stack.state.store import TrackingStore

from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo,
    init_fake_github_repo_with_submitted_feature,
    init_fake_github_repo_with_submitted_stack,
    run_command,
    selected_stack,
)
from .submit_command_helpers import (
    configure_submit_environment,
    issue_comments,
    read_remote_ref,
    remote_refs,
    run_main,
)


def test_cleanup_removes_closed_pr_after_local_change_is_abandoned(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = selected_stack(repo).changes[-1].change_id
    fake_repo.prs[1].state = "closed"
    run_command(["jj", "abandon", change_id], repo)

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "PR #1" in captured.out
    assert change_id[:8] in captured.out
    assert change_id not in TrackingStore.for_repo(repo).load().pr_identities
    assert not any(
        ref.startswith("refs/heads/jj-stack/") for ref in remote_refs(fake_repo.git_dir)
    )


def test_cleanup_dry_run_leaves_an_unadopted_repo_untouched(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    capsys.readouterr()

    exit_code = run_main(repo, config_path, "cleanup", "--dry-run")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No cleanup actions needed." in captured.out
    assert not TrackingStore.for_repo(repo).is_in_use()


def test_cleanup_change_only_removes_leftovers_for_selected_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    first_change_id = selected_stack(repo).head.change_id

    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "feature 2", "feature-2.txt")
    second_change_id = selected_stack(repo).head.change_id
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    fake_repo.prs[1].state = "closed"
    fake_repo.prs[2].state = "closed"

    exit_code = run_main(repo, config_path, "cleanup", second_change_id)
    state = TrackingStore.for_repo(repo).load()

    assert exit_code == 0
    assert second_change_id not in state.pr_identities
    assert first_change_id in state.pr_identities


def test_cleanup_pr_selects_a_saved_orphan_and_rejects_an_unlinked_pr(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_id = selected_stack(repo).head.change_id
    fake_repo.prs[1].state = "closed"
    run_command(["jj", "abandon", change_id], repo)

    exit_code = run_main(repo, config_path, "cleanup", "--pull-request", "1")

    assert exit_code == 0
    assert change_id not in TrackingStore.for_repo(repo).load().pr_identities

    outside = fake_repo.create_pr(
        base_ref="main",
        body="not created by jj-stack",
        head_ref="main",
        title="outside pull request",
    )
    capsys.readouterr()

    unlinked_exit_code = run_main(
        repo, config_path, "cleanup", "--pull-request", str(outside.number), "--close"
    )
    unlinked = capsys.readouterr()

    assert unlinked_exit_code == 1
    assert f"PR #{outside.number} is not linked to any local change" in unlinked.err
    assert fake_repo.prs[outside.number].state == "open"


def test_cleanup_close_finishes_open_and_terminal_orphans(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_ids = [selected_stack(repo).head.change_id]

    for index in (2, 3):
        run_command(["jj", "new", "main"], repo)
        commit_file(repo, f"feature {index}", f"feature-{index}.txt")
        change_ids.append(selected_stack(repo).head.change_id)
        assert run_main(repo, config_path, "submit") == 0
        capsys.readouterr()

    state_store = TrackingStore.for_repo(repo)
    initial_state = state_store.load()
    identities = tuple(initial_state.pr_identities[change_id] for change_id in change_ids)
    fake_repo.prs[identities[1].pr_number].state = "closed"
    merged_pr = fake_repo.prs[identities[2].pr_number]
    merged_pr.state = "closed"
    merged_pr.merged_at = "2026-08-13T12:00:00Z"
    run_command(["jj", "abandon", *change_ids], repo)

    preview_exit_code = run_main(
        repo,
        config_path,
        "cleanup",
        "--pull-request",
        "orphans",
        "--close",
        "--dry-run",
    )
    preview = capsys.readouterr()

    assert preview_exit_code == 0
    assert f"close PR #{identities[0].pr_number}" in preview.out
    assert f"close PR #{identities[1].pr_number}" not in preview.out
    assert f"close PR #{identities[2].pr_number}" not in preview.out
    assert all(identity.head_ref in preview.out for identity in identities)
    assert fake_repo.prs[identities[0].pr_number].state == "open"
    assert state_store.load() == initial_state

    exit_code = run_main(
        repo,
        config_path,
        "cleanup",
        "--pull-request",
        "orphans",
        "--close",
    )
    applied = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert f"close PR #{identities[0].pr_number}" in applied.out
    assert f"close PR #{identities[1].pr_number}" not in applied.out
    assert f"close PR #{identities[2].pr_number}" not in applied.out
    assert all(change_id not in refreshed_state.pr_identities for change_id in change_ids)
    assert all(
        f"refs/heads/{identity.head_ref}" not in remote_refs(fake_repo.git_dir)
        for identity in identities
    )
    assert all(fake_repo.prs[identity.pr_number].state == "closed" for identity in identities)


def test_cleanup_preserves_a_branch_its_closed_dependent_still_names(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_ids = tuple(change.change_id for change in stack.changes)
    state_store = TrackingStore.for_repo(repo)
    identities = tuple(state_store.load().pr_identities[change_id] for change_id in change_ids)
    bookmarks = tuple(identity.head_ref for identity in identities)

    for pr in fake_repo.prs.values():
        pr.state = "closed"
    # GitHub keeps a merged member in the stack forever, and the closed member above it
    # still names the merged member's branch as its base.
    fake_repo.prs[identities[0].pr_number].merged_at = "2026-08-13T12:00:00Z"
    assert fake_repo.prs[identities[1].pr_number].base_ref == bookmarks[0]
    run_command(["jj", "abandon", *change_ids], repo)
    fake_repo.github_stacks = {7: (1, 2)}
    state_before = state_store.load()

    selected_exit_code = run_main(
        repo, config_path, "cleanup", "--pull-request", str(identities[0].pr_number)
    )
    selected = capsys.readouterr()

    assert selected_exit_code == 1
    assert f"PR #{identities[1].pr_number} still uses" in " ".join(
        (selected.out + " " + selected.err).split()
    )
    assert state_store.load() == state_before
    assert all(
        f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir) for bookmark in bookmarks
    )

    preview_exit_code = run_main(repo, config_path, "cleanup", "--dry-run")
    preview = capsys.readouterr()
    normalized_preview = " ".join(preview.out.split())

    assert preview_exit_code == 1
    assert "Planned cleanup actions:" in preview.out
    assert "GitHub stack #7 blocks this jj-stack operation" in normalized_preview
    assert "jj-stack unstack --stack 7" in normalized_preview
    assert all(
        f"remote branch: delete {bookmark}@origin" not in normalized_preview
        for bookmark in bookmarks
    )
    assert state_store.load() == state_before
    assert all(
        f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir) for bookmark in bookmarks
    )

    blocked_exit_code = run_main(repo, config_path, "cleanup")
    blocked = capsys.readouterr()

    assert blocked_exit_code == 1
    assert "GitHub stack #7 blocks this jj-stack operation" in " ".join(blocked.out.split())
    assert all(change_id in state_store.load().pr_identities for change_id in change_ids)
    assert all(
        f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir) for bookmark in bookmarks
    )

    # Dissolving the GitHub grouping does not release the base branch. The closed member above
    # still names it, so cleaning up the one it does not block leaves the other refused.
    assert run_main(repo, config_path, "unstack", "--stack", "7") == 0
    capsys.readouterr()
    partial_exit_code = run_main(repo, config_path, "cleanup")
    partial = capsys.readouterr()
    normalized_partial = " ".join(partial.out.split())

    assert partial_exit_code == 1
    assert f"remote branch: delete {bookmarks[1]}@origin" in normalized_partial
    assert f"PR #{identities[1].pr_number} still uses" in normalized_partial
    assert f"refs/heads/{bookmarks[0]}" in remote_refs(fake_repo.git_dir)

    # Retargeting the dependent is the only move that frees the branch: GitHub refuses to
    # reopen a pull request whose base branch is gone, and refuses to retarget a closed one,
    # so deleting first would strand it for good.
    fake_repo.prs[identities[1].pr_number].base_ref = "main"
    apply_exit_code = run_main(repo, config_path, "cleanup")
    applied = capsys.readouterr()
    normalized_applied = " ".join(applied.out.split())

    assert apply_exit_code == 0
    assert f"remote branch: delete {bookmarks[0]}@origin" in normalized_applied
    assert all(change_id not in state_store.load().pr_identities for change_id in change_ids)
    assert all(
        f"refs/heads/{bookmark}" not in remote_refs(fake_repo.git_dir) for bookmark in bookmarks
    )


def test_cleanup_preserves_closed_pr_branch_used_by_open_pr(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    bottom_change_id = selected_stack(repo).changes[0].change_id
    state_store = TrackingStore.for_repo(repo)
    state = state_store.load()
    identity = state.pr_identities[bottom_change_id]
    comments_before = issue_comments(fake_repo, identity.pr_number)
    fake_repo.prs[identity.pr_number].state = "closed"

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    output = " ".join(captured.out.split())

    assert exit_code == 1
    assert "PR #2 still uses" in output
    assert "rerun cleanup" in output
    assert state_store.load() == state
    assert issue_comments(fake_repo, identity.pr_number) == comments_before
    assert f"refs/heads/{identity.head_ref}" in remote_refs(fake_repo.git_dir)


def test_cleanup_preserves_closed_pr_branch_used_as_head_by_another_open_pr(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_id = selected_stack(repo).head.change_id
    state_store = TrackingStore.for_repo(repo)
    state = state_store.load()
    identity = state.pr_identities[change_id]
    comments_before = issue_comments(fake_repo, identity.pr_number)
    fake_repo.prs[identity.pr_number].state = "closed"
    competing_pr = fake_repo.create_pr(
        base_ref="main",
        body="outside pull request sharing the head branch",
        head_ref=identity.head_ref,
        title="outside change on the same head branch",
    )

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    output = " ".join(captured.out.split())

    assert exit_code == 1
    assert "uses it as its head branch" in output
    assert state_store.load() == state
    assert issue_comments(fake_repo, identity.pr_number) == comments_before
    assert f"refs/heads/{identity.head_ref}" in remote_refs(fake_repo.git_dir)
    assert competing_pr.state == "open"


def test_cleanup_stops_later_prs_after_partial_mutation_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = selected_stack(repo)
    state_store = TrackingStore.for_repo(repo)
    initial_state = state_store.load()
    stack_change_ids = {change.change_id for change in stack.changes}
    ordered_change_ids = tuple(
        change_id for change_id in initial_state.pr_identities if change_id in stack_change_ids
    )
    blocking_change_id, later_change_id = ordered_change_ids
    blocking_identity = initial_state.pr_identities[blocking_change_id]
    later_identity = initial_state.pr_identities[later_change_id]
    fake_repo.github_stacks = {}
    fake_repo.prs[blocking_identity.pr_number].state = "closed"
    fake_repo.prs[later_identity.pr_number].state = "closed"
    # This case is about stopping after a failed mutation, not about base-branch dependencies.
    # Retarget the later PR so cleanup reaches the mutation it is here to fail.
    fake_repo.prs[later_identity.pr_number].base_ref = "main"
    fake_repo.create_issue_comment(
        body=f"{STACK_OVERVIEW_COMMENT_MARKER}\nstack overview",
        issue_number=blocking_identity.pr_number,
    )

    async def reject_comment_delete(**_kwargs) -> bool:
        raise CliError("comment deletion failed")

    monkeypatch.setattr(
        "jj_stack.commands._cleanup_actions.delete_stack_overview_comment",
        reject_comment_delete,
    )

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 1
    assert "comment deletion failed" in captured.out
    assert blocking_change_id in refreshed_state.pr_identities
    assert later_change_id in refreshed_state.pr_identities
    assert f"refs/heads/{blocking_identity.head_ref}" not in remote_refs(fake_repo.git_dir)
    assert f"refs/heads/{later_identity.head_ref}" in remote_refs(fake_repo.git_dir)


def test_cleanup_preserves_open_orphan_record_and_remote_branch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.changes[0].change_id
    state_store = TrackingStore.for_repo(repo)
    bookmark = state_store.load().pr_identities[change_id].head_ref

    run_command(["jj", "abandon", change_id], repo)
    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    refreshed_state = state_store.load()
    normalized_output = " ".join(captured.out.split())

    assert exit_code == 0
    assert "  - preserve open orphan" in captured.out
    assert "preserve open orphan" in normalized_output
    assert change_id in refreshed_state.pr_identities
    assert refreshed_state.pr_identities[change_id].head_ref == bookmark
    assert f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir)


def test_cleanup_removes_overview_comment_for_closed_pr(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.changes[-1].change_id
    state_store = TrackingStore.for_repo(repo)
    fake_repo.prs[2].state = "closed"
    fake_repo.github_stacks = {}
    fake_repo.create_issue_comment(
        body=f"{STACK_OVERVIEW_COMMENT_MARKER}\nstack overview",
        issue_number=2,
    )

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "delete stack overview comment" in captured.out
    assert change_id not in refreshed_state.pr_identities
    assert issue_comments(fake_repo, 2) == []


def test_cleanup_blocks_pr_head_drift_observed_during_planning(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_id = selected_stack(repo).head.change_id
    state_store = TrackingStore.for_repo(repo)
    initial_state = state_store.load()
    identity = initial_state.pr_identities[change_id]
    pr = fake_repo.prs[identity.pr_number]
    pr.state = "closed"
    run_command(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            f"refs/heads/{identity.head_ref}",
            read_remote_ref(fake_repo.git_dir, "main"),
        ],
        fake_repo.git_dir.parent,
    )
    fake_repo.create_issue_comment(
        body=f"{STACK_OVERVIEW_COMMENT_MARKER}\nstack overview",
        issue_number=identity.pr_number,
    )
    initial_comments = issue_comments(fake_repo, identity.pr_number)

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "head no longer matches the saved submitted commit" in " ".join(captured.out.split())
    assert state_store.load() == initial_state
    assert issue_comments(fake_repo, identity.pr_number) == initial_comments
    assert f"refs/heads/{identity.head_ref}" in remote_refs(fake_repo.git_dir)
