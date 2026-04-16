"""Typed access to local `jj` stack state."""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from jj_review import ui
from jj_review.errors import CliError, ErrorMessage
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.stack import LocalRevision, LocalStack

_COMMIT_TEMPLATE = (
    r'json(change_id) ++ "\t" ++ json(commit_id) ++ "\t" ++ json(description) ++ "\t" ++ '
    r'json(parents.map(|p| p.commit_id())) ++ "\t" ++ '
    r'json(empty) ++ "\t" ++ json(divergent) ++ "\t" ++ '
    r'json(current_working_copy) ++ "\t" ++ json(self.hidden()) ++ "\t" ++ '
    r'json(immutable) ++ "\n"'
)
_BOOKMARK_TEMPLATE = r'json(self) ++ "\n"'


class JjCommandError(CliError):
    """Raised when a `jj` invocation fails."""


UnsupportedStackReason = Literal[
    "divergent_change",
    "empty_working_copy",
    "hidden_commit",
    "immutable_commit",
    "merge_commit",
    "reached_root_before_trunk",
    "trunk_resolved_to_root",
]


class UnsupportedStackError(CliError):
    """Raised when local history cannot be treated as a linear review stack."""

    def __init__(
        self,
        message: ErrorMessage,
        *,
        change_id: str | None = None,
        reason: UnsupportedStackReason | None = None,
    ) -> None:
        super().__init__(message)
        self.change_id = change_id
        self.reason = reason

    @classmethod
    def stack_shape(
        cls,
        change_id: str,
        detail: ErrorMessage,
        *,
        reason: UnsupportedStackReason,
    ) -> UnsupportedStackError:
        return cls(
            t"Unsupported stack shape at {ui.change_id(change_id)}: {detail}",
            change_id=change_id,
            reason=reason,
        )


class StaleWorkspaceError(CliError):
    """Raised when `jj` refuses to run because the current workspace is stale."""


JjRunner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]
CliColorMode = Literal["always", "auto", "debug", "never"]
JjColorWhen = Literal["always", "debug", "never"]


@dataclass(slots=True)
class _RawBookmarkState:
    local_targets: tuple[str, ...] = ()
    remote_targets: list[RemoteBookmarkState] = field(default_factory=list)


class JjClient:
    """Thin wrapper around `jj` commands used by the review tool."""

    def __init__(self, repo_root: Path, *, runner: JjRunner | None = None) -> None:
        self._repo_root = repo_root
        self._runner = runner or _default_runner

    def discover_review_stack(
        self,
        revset: str | None = None,
        *,
        allow_divergent: bool = False,
        allow_immutable: bool = False,
        allow_trunk_ancestors: bool = False,
    ) -> LocalStack:
        """Resolve a review stack from a selected head back to `trunk()`."""

        trunk = self._resolve_trunk()
        if revset is None:
            head, selected_revset = self.resolve_default_head()
        else:
            head = self.resolve_revision(revset)
            selected_revset = revset
            if head.current_working_copy and head.empty:
                raise UnsupportedStackError(
                    "Selected revision resolves to the empty working-copy commit. "
                    "Select a concrete change instead.",
                    reason="empty_working_copy",
                )

        if head.commit_id == trunk.commit_id:
            return LocalStack(
                head=head,
                revisions=(),
                selected_revset=selected_revset,
                trunk=trunk,
            )

        merged_trunk_side_branch_commit_ids: set[str] = set()
        if allow_trunk_ancestors:
            trunk_ancestors_revset = f"::{_quote_revset_symbol(trunk.commit_id)}"
            merged_trunk_side_branch_commit_ids = self._merged_trunk_side_branch_commit_ids(
                trunk_ancestors_revset
            )

        self._validate_reviewable_revision(
            head,
            allow_divergent=allow_divergent,
            allow_immutable=allow_immutable,
        )
        ancestor_revisions = self._query_revisions(f"::{_quote_revset_symbol(head.commit_id)}")
        revisions_by_commit_id = {revision.commit_id: revision for revision in ancestor_revisions}
        revisions_by_commit_id[head.commit_id] = head
        revisions_by_commit_id[trunk.commit_id] = trunk

        stack_head_first: list[LocalRevision] = []
        current = head
        while current.commit_id != trunk.commit_id:
            if current.commit_id != head.commit_id:
                self._validate_reviewable_revision(
                    current,
                    allow_divergent=allow_divergent,
                    allow_immutable=allow_immutable,
                )

            stack_head_first.append(current)
            if allow_trunk_ancestors and current.commit_id in merged_trunk_side_branch_commit_ids:
                break
            parent_commit_id = current.only_parent_commit_id()
            current = revisions_by_commit_id.get(parent_commit_id) or self.resolve_revision(
                parent_commit_id
            )

        return LocalStack(
            head=head,
            revisions=tuple(reversed(stack_head_first)),
            selected_revset=selected_revset,
            trunk=trunk,
        )

    def resolve_default_head(self) -> tuple[LocalRevision, str]:
        """Resolve the default head revision used when the CLI omits `<revset>`."""

        working_copy = self.resolve_revision("@")
        if working_copy.current_working_copy and working_copy.empty:
            return self.resolve_revision("@-"), "@-"
        return working_copy, "@"

    def resolve_revision(self, revset: str) -> LocalRevision:
        """Resolve a revset to exactly one revision."""

        try:
            revisions = self._query_revisions(revset, limit=2)
        except JjCommandError as error:
            friendly_error = _revset_resolution_error(revset, error)
            if friendly_error is not None:
                raise friendly_error from error
            raise
        if not revisions:
            raise CliError(t"Revset {ui.revset(revset)} did not resolve to a visible revision.")
        if len(revisions) > 1:
            raise CliError(t"Revset {ui.revset(revset)} resolved to more than one revision.")
        return revisions[0]

    def query_revisions(
        self,
        revset: str,
        *,
        limit: int | None = None,
    ) -> tuple[LocalRevision, ...]:
        """Return revisions matching the supplied revset."""

        try:
            return tuple(self._query_revisions(revset, limit=limit))
        except JjCommandError as error:
            if _is_missing_revision_error(_unwrap_command_error_message(str(error))):
                return ()
            raise

    def query_revisions_by_change_ids(
        self,
        change_ids: Sequence[str],
    ) -> dict[str, tuple[LocalRevision, ...]]:
        """Return visible revisions grouped by logical change ID."""

        ordered_change_ids = tuple(dict.fromkeys(change_ids))
        if not ordered_change_ids:
            return {}

        grouped: dict[str, list[LocalRevision]] = {
            change_id: [] for change_id in ordered_change_ids
        }
        for chunk in _chunked(ordered_change_ids):
            revisions = self._query_revisions(
                _union_revset_symbols(
                    tuple(f"present({_quote_revset_symbol(change_id)})" for change_id in chunk),
                    quote=False,
                )
            )
            for revision in revisions:
                grouped.setdefault(revision.change_id, []).append(revision)
        return {change_id: tuple(grouped.get(change_id, ())) for change_id in ordered_change_ids}

    def query_ancestor_revisions(
        self,
        commit_ids: Sequence[str],
    ) -> tuple[LocalRevision, ...]:
        """Return ancestors for the supplied commits, including the commits themselves."""

        ordered_commit_ids = tuple(dict.fromkeys(commit_ids))
        if not ordered_commit_ids:
            return ()

        revisions_by_commit_id: dict[str, LocalRevision] = {}
        for chunk in _chunked(ordered_commit_ids):
            revisions = self._query_revisions(f"::{_union_revset_symbols(chunk)}")
            for revision in revisions:
                revisions_by_commit_id.setdefault(revision.commit_id, revision)
        return tuple(revisions_by_commit_id.values())

    def supported_review_stack_change_ids(
        self,
        candidate_revisions: Sequence[LocalRevision],
        *,
        allow_divergent: bool = False,
        allow_immutable: bool = False,
        allow_trunk_ancestors: bool = False,
    ) -> set[str]:
        """Return change IDs whose selected-parent path remains a supported review stack."""

        ordered_revisions = tuple(candidate_revisions)
        if not ordered_revisions:
            return set()

        trunk = self._resolve_trunk()
        commit_ids = tuple(revision.commit_id for revision in ordered_revisions)
        revisions_by_commit_id = {
            revision.commit_id: revision for revision in self.query_ancestor_revisions(commit_ids)
        }
        revisions_by_commit_id[trunk.commit_id] = trunk

        merged_trunk_side_branch_commit_ids: set[str] = set()
        if allow_trunk_ancestors:
            trunk_ancestors_revset = f"::{_quote_revset_symbol(trunk.commit_id)}"
            merged_trunk_side_branch_commit_ids = self._merged_trunk_side_branch_commit_ids(
                trunk_ancestors_revset
            )

        support_by_commit_id: dict[str, bool] = {trunk.commit_id: True}

        def is_supported(commit_id: str) -> bool:
            if commit_id in support_by_commit_id:
                return support_by_commit_id[commit_id]

            revision = revisions_by_commit_id.get(commit_id)
            if revision is None:
                support_by_commit_id[commit_id] = False
                return False

            try:
                self._validate_reviewable_revision(
                    revision,
                    allow_divergent=allow_divergent,
                    allow_immutable=allow_immutable,
                )
            except UnsupportedStackError:
                support_by_commit_id[commit_id] = False
                return False

            if (
                allow_trunk_ancestors
                and revision.commit_id in merged_trunk_side_branch_commit_ids
            ):
                support_by_commit_id[commit_id] = True
                return True

            support = is_supported(revision.only_parent_commit_id())
            support_by_commit_id[commit_id] = support
            return support

        return {
            revision.change_id
            for revision in ordered_revisions
            if is_supported(revision.commit_id)
        }

    def query_children_by_parent_for_commit_ids(
        self,
        commit_ids: Sequence[str],
    ) -> dict[str, tuple[LocalRevision, ...]]:
        """Return visible children grouped by parent for the ancestors of the supplied commits."""

        ordered_commit_ids = tuple(dict.fromkeys(commit_ids))
        if not ordered_commit_ids:
            return {}

        grouped: dict[str, dict[str, LocalRevision]] = {}
        for chunk in _chunked(ordered_commit_ids):
            children_by_parent = self._query_children_by_parent(
                f"children(::{_union_revset_symbols(chunk)})"
            )
            for parent_commit_id, children in children_by_parent.items():
                parent_group = grouped.setdefault(parent_commit_id, {})
                for child in children:
                    parent_group.setdefault(child.commit_id, child)
        return {
            parent_commit_id: tuple(children.values())
            for parent_commit_id, children in grouped.items()
        }

    def _resolve_trunk(self) -> LocalRevision:
        """Resolve `trunk()` and reject the implicit root fallback."""

        trunk = self.resolve_revision("trunk()")
        if len(trunk.parents) == 0:
            raise UnsupportedStackError(
                t"{ui.revset('trunk()')} resolved to the root commit. Configure a concrete trunk "
                t"bookmark before discovering a review stack.",
                reason="trunk_resolved_to_root",
            )
        return trunk

    def _query_children_by_parent(
        self,
        revset: str,
    ) -> dict[str, tuple[LocalRevision, ...]]:
        revisions = self._query_revisions(revset)
        grouped: dict[str, list[LocalRevision]] = {}
        for revision in revisions:
            for parent_commit_id in revision.parents:
                grouped.setdefault(parent_commit_id, []).append(revision)
        return {
            parent_commit_id: tuple(children) for parent_commit_id, children in grouped.items()
        }

    def _merged_trunk_side_branch_commit_ids(self, trunk_ancestors_revset: str) -> set[str]:
        trunk_ancestor_commit_ids = {
            revision.commit_id for revision in self._query_revisions(trunk_ancestors_revset)
        }
        trunk_children_by_parent = self._query_children_by_parent(
            f"children({trunk_ancestors_revset})"
        )
        return {
            parent_commit_id
            for parent_commit_id, children in trunk_children_by_parent.items()
            if parent_commit_id in trunk_ancestor_commit_ids
            and any(
                child.commit_id in trunk_ancestor_commit_ids
                and len(child.parents) > 1
                and child.parents[0] != parent_commit_id
                for child in children
            )
        }

    def get_config_string(self, key: str) -> str | None:
        """Return the string value of a jj config key, or None if unset."""

        try:
            value = self._run_jj(("config", "get", key))
        except JjCommandError:
            return None
        stripped = value.strip()
        return stripped if stripped else None

    def resolve_color_when(
        self,
        *,
        cli_color: CliColorMode | None = None,
        stdout_is_tty: bool,
    ) -> JjColorWhen:
        """Resolve the effective `jj --color` mode for embedded log rendering."""

        configured = cli_color or self.get_config_string("ui.color")
        if configured == "always":
            return "always"
        if configured == "debug":
            return "debug"
        if configured == "never":
            return "never"
        return "always" if stdout_is_tty else "never"

    def render_revision_log_lines(
        self,
        revision: LocalRevision,
        *,
        color_when: JjColorWhen,
    ) -> tuple[str, ...]:
        """Render one revision with the user's native `jj log` formatting."""

        stdout = self._run_jj(
            (
                "--ignore-working-copy",
                "--no-pager",
                "--color",
                color_when,
                "log",
                "-r",
                _quote_revset_symbol(revision.commit_id),
                "--limit",
                "1",
            )
        )
        return tuple(line for line in stdout.rstrip("\n").splitlines() if line.strip() != "~")

    def find_private_commits(
        self,
        revisions: tuple[LocalRevision, ...],
    ) -> tuple[LocalRevision, ...]:
        """Return revisions blocked by the repo's git.private-commits policy."""

        private_commits_revset = self.get_config_string("git.private-commits")
        if not private_commits_revset or not revisions:
            return ()
        commit_ids_revset = " | ".join(_quote_revset_symbol(r.commit_id) for r in revisions)
        combined_revset = f"({private_commits_revset}) & ({commit_ids_revset})"
        return tuple(self.query_revisions(combined_revset))

    def list_git_remotes(self) -> tuple[GitRemote, ...]:
        """List configured Git remotes for the repository."""

        stdout = self._run_jj(("git", "remote", "list"))
        remotes: list[GitRemote] = []
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            name, url = stripped.split(maxsplit=1)
            remotes.append(GitRemote(name=name, url=url))
        return tuple(remotes)

    def get_bookmark_state(self, bookmark: str) -> BookmarkState:
        """Return local and remote state for the named bookmark."""

        return self.list_bookmark_states((bookmark,)).get(bookmark, BookmarkState(name=bookmark))

    def list_bookmark_states(
        self,
        bookmarks: Sequence[str] | None = None,
    ) -> dict[str, BookmarkState]:
        """Return local and remote state for the requested bookmark names."""

        command = ["bookmark", "list", "--all-remotes", "-T", _BOOKMARK_TEMPLATE]
        if bookmarks:
            command.extend(bookmarks)

        stdout = self._run_jj(command)
        grouped: dict[str, _RawBookmarkState] = {}
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            raw_bookmark = json.loads(stripped)
            if not isinstance(raw_bookmark, dict):
                raise JjCommandError(
                    t"Unexpected {ui.cmd('jj bookmark list')} payload: expected a JSON object."
                )
            name = raw_bookmark["name"]
            if not isinstance(name, str):
                raise JjCommandError(
                    t"Unexpected {ui.cmd('jj bookmark list')} payload: missing bookmark name."
                )
            bookmark_state = grouped.setdefault(name, _RawBookmarkState())
            targets = tuple(_require_sequence(raw_bookmark.get("target", ())))
            remote_name = raw_bookmark.get("remote")
            if remote_name is None:
                bookmark_state.local_targets = targets
                continue
            if not isinstance(remote_name, str):
                raise JjCommandError(
                    t"Unexpected {ui.cmd('jj bookmark list')} payload: invalid remote bookmark "
                    t"entry."
                )
            tracking_target = raw_bookmark.get("tracking_target", ())
            bookmark_state.remote_targets.append(
                RemoteBookmarkState(
                    remote=remote_name,
                    targets=targets,
                    tracking_targets=tuple(_require_sequence(tracking_target)),
                )
            )

        states = {
            name: BookmarkState(
                name=name,
                local_targets=raw_state.local_targets,
                remote_targets=tuple(raw_state.remote_targets),
            )
            for name, raw_state in grouped.items()
        }
        if bookmarks:
            for bookmark in bookmarks:
                states.setdefault(bookmark, BookmarkState(name=bookmark))
        return states

    def set_bookmark(
        self,
        bookmark: str,
        revision: str,
        *,
        allow_backwards: bool = False,
    ) -> None:
        """Create or move a local bookmark to the supplied revision."""

        command = ["bookmark", "set"]
        if allow_backwards:
            command.append("--allow-backwards")
        command.extend((bookmark, "-r", revision))
        self._run_jj(command)

    def forget_bookmarks(self, bookmarks: Sequence[str]) -> None:
        """Forget one or more local bookmarks without scheduling remote deletions."""

        ordered_bookmarks = tuple(bookmarks)
        if not ordered_bookmarks:
            return
        self._run_jj(("bookmark", "forget", *ordered_bookmarks))

    def push_bookmarks(
        self,
        *,
        remote: str,
        bookmarks: Sequence[str],
    ) -> None:
        """Push one or more bookmarks to the selected remote."""

        ordered_bookmarks = tuple(bookmarks)
        if not ordered_bookmarks:
            return
        command = ["git", "push", "--remote", remote]
        for bookmark in ordered_bookmarks:
            command.extend(["--bookmark", bookmark])
        self._run_jj(command)

    def fetch_remote(
        self,
        *,
        remote: str,
        branches: Sequence[str] | None = None,
    ) -> None:
        """Refresh remembered remote bookmark state for the selected remote."""

        command = ["git", "fetch", "--remote", remote]
        if branches:
            for branch in branches:
                command.extend(["--branch", branch])
        self._run_jj(command)

    def list_remote_branches(
        self,
        *,
        remote: str,
        patterns: Sequence[str],
    ) -> dict[str, str]:
        """List matching remote branch heads without importing unrelated bookmark state."""

        if not patterns:
            return {}
        stdout = self._run_git(("ls-remote", "--refs", remote, *patterns))
        branches: dict[str, str] = {}
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            commit_id, separator, ref = stripped.partition("\t")
            if not separator or not commit_id or not ref.startswith("refs/heads/"):
                raise JjCommandError(
                    t"{ui.cmd('git ls-remote')} output has unexpected format: {line!r}"
                )
            branches[ref.removeprefix("refs/heads/")] = commit_id
        return branches

    def track_bookmark(self, *, remote: str, bookmark: str) -> None:
        """Track an existing remote bookmark locally."""

        self._run_jj(("bookmark", "track", bookmark, "--remote", remote))

    def update_untracked_remote_bookmark(
        self,
        *,
        remote: str,
        bookmark: str,
        desired_target: str,
        expected_remote_target: str,
    ) -> None:
        """Update an existing untracked remote bookmark without importing it first."""

        self._run_git(
            (
                "push",
                f"--force-with-lease=refs/heads/{bookmark}:{expected_remote_target}",
                remote,
                f"{desired_target}:refs/heads/{bookmark}",
            )
        )
        self.fetch_remote(remote=remote)
        self.track_bookmark(remote=remote, bookmark=bookmark)

    def delete_remote_bookmarks(
        self,
        *,
        remote: str,
        deletions: Sequence[tuple[str, str]],
        fetch: bool = True,
    ) -> None:
        """Delete one or more remote bookmarks by name."""

        ordered_deletions = tuple(deletions)
        if not ordered_deletions:
            return
        command = ["push"]
        for bookmark, expected_remote_target in ordered_deletions:
            command.append(f"--force-with-lease=refs/heads/{bookmark}:{expected_remote_target}")
        command.append(remote)
        for bookmark, _expected_remote_target in ordered_deletions:
            command.append(f":refs/heads/{bookmark}")
        self._run_git(command)
        if fetch:
            self.fetch_remote(remote=remote)

    def rebase_revision(self, *, source: str, destination: str) -> None:
        """Rebase one revision and its descendants onto a new destination."""

        self._run_jj(("rebase", "-s", source, "-d", destination))

    def _query_revisions(self, revset: str, *, limit: int | None = None) -> list[LocalRevision]:
        command = ["log", "--no-graph", "-r", revset, "-T", _COMMIT_TEMPLATE]
        if limit is not None:
            command.extend(["--limit", str(limit)])

        stdout = self._run_jj(command)
        revisions: list[LocalRevision] = []
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            revisions.append(_parse_revision_line(stripped))
        return revisions

    def _run_jj(self, args: Sequence[str]) -> str:
        return self._run_command(
            ["jj", *args],
            missing_tool_message=t"{ui.cmd('jj')} is not installed or is not on PATH.",
            detect_stale_workspace=True,
        )

    def _run_git(self, args: Sequence[str]) -> str:
        return self._run_command(
            ["git", *args],
            missing_tool_message=t"{ui.cmd('git')} is not installed or is not on PATH.",
            detect_stale_workspace=False,
        )

    def _run_command(
        self,
        command: Sequence[str],
        *,
        missing_tool_message: ErrorMessage,
        detect_stale_workspace: bool,
    ) -> str:
        try:
            completed = self._runner(command, self._repo_root)
        except FileNotFoundError as error:
            raise JjCommandError(missing_tool_message) from error

        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            if detect_stale_workspace and "The working copy is stale" in message:
                raise StaleWorkspaceError(
                    t"The current workspace is stale. Run {ui.cmd('jj workspace update-stale')} "
                    t"and retry."
                )
            raise JjCommandError(t"{ui.cmd(shlex.join(command))} failed: {message}")
        return completed.stdout

    def _validate_reviewable_revision(
        self,
        revision: LocalRevision,
        *,
        allow_divergent: bool = False,
        allow_immutable: bool = False,
    ) -> None:
        # Check the root-commit condition before immutable, because the root
        # is always immutable in jj and "reached root before trunk()" is more
        # actionable than "immutable commit".
        if len(revision.parents) == 0:
            raise UnsupportedStackError.stack_shape(
                revision.change_id,
                t"stack reached the root commit before {ui.revset('trunk()')}.",
                reason="reached_root_before_trunk",
            )
        if revision.hidden:
            raise UnsupportedStackError.stack_shape(
                revision.change_id,
                "hidden commits are not reviewable.",
                reason="hidden_commit",
            )
        if revision.immutable and not allow_immutable:
            raise UnsupportedStackError.stack_shape(
                revision.change_id,
                "immutable commits are not reviewable.",
                reason="immutable_commit",
            )
        if revision.divergent and not allow_divergent:
            raise UnsupportedStackError.stack_shape(
                revision.change_id,
                "divergent changes are not supported.",
                reason="divergent_change",
            )
        if len(revision.parents) > 1:
            raise UnsupportedStackError.stack_shape(
                revision.change_id,
                "merge commits are not supported.",
                reason="merge_commit",
            )


def _default_runner(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        check=False,
        cwd=cwd,
        text=True,
    )


_EXPECTED_FIELD_COUNT = 9


def _is_missing_revision_error(message: str) -> bool:
    return "Revision `" in message and "doesn't exist" in message


def _unwrap_command_error_message(message: str) -> str:
    _prefix, separator, suffix = message.partition(" failed: ")
    return suffix if separator else message


def _revset_resolution_error(revset: str, error: JjCommandError) -> CliError | None:
    raw_message = _unwrap_command_error_message(str(error))
    if _is_missing_revision_error(raw_message):
        first_line = raw_message.splitlines()[0].strip()
        if first_line.startswith("Error: "):
            first_line = first_line.removeprefix("Error: ").strip()
        return CliError(first_line.rstrip("."))

    first_line = raw_message.splitlines()[0].strip()
    if first_line.startswith("Error: Failed to parse revset:"):
        detail = first_line.removeprefix("Error: ").strip()
        return CliError(t"Invalid revset {ui.revset(revset)}: {detail}.")

    return None


def _parse_revision_line(line: str) -> LocalRevision:
    parts = line.split("\t")
    if len(parts) != _EXPECTED_FIELD_COUNT:
        raise JjCommandError(
            t"{ui.cmd('jj log')} output has unexpected format: expected {_EXPECTED_FIELD_COUNT} "
            t"tab-separated fields, got {len(parts)}. Raw line: {line!r}"
        )
    (
        change_id_json,
        commit_id_json,
        description_json,
        parents_json,
        empty_json,
        divergent_json,
        working_copy_json,
        hidden_json,
        immutable_json,
    ) = parts
    try:
        parents_raw = json.loads(parents_json)
        if not isinstance(parents_raw, list):
            raise JjCommandError(
                t"{ui.cmd('jj log')} output has unexpected field types: "
                t"parents field is not a JSON array. Raw line: {line!r}"
            )
        return LocalRevision(
            change_id=json.loads(change_id_json),
            commit_id=json.loads(commit_id_json),
            current_working_copy=json.loads(working_copy_json),
            description=json.loads(description_json),
            divergent=json.loads(divergent_json),
            empty=json.loads(empty_json),
            hidden=json.loads(hidden_json),
            immutable=json.loads(immutable_json),
            parents=tuple(parents_raw),
        )
    except json.JSONDecodeError as error:
        raise JjCommandError(
            t"{ui.cmd('jj log')} output contains invalid JSON: {error}. Raw line: {line!r}"
        ) from error
    except (TypeError, ValueError) as error:
        raise JjCommandError(
            t"{ui.cmd('jj log')} output has unexpected field types: {error}. Raw line: {line!r}"
        ) from error


def _require_sequence(value: Any) -> Sequence[str]:
    if not isinstance(value, list | tuple):
        raise JjCommandError(
            t"Unexpected {ui.cmd('jj bookmark list')} payload: expected a sequence."
        )
    return tuple(str(item) for item in value if item is not None)


def _quote_revset_symbol(symbol: str) -> str:
    return f"'{symbol}'"


def _union_revset_symbols(symbols: Sequence[str], *, quote: bool = True) -> str:
    parts = [_quote_revset_symbol(symbol) if quote else symbol for symbol in symbols]
    if not parts:
        raise ValueError("Expected at least one revset symbol.")
    if len(parts) == 1:
        return parts[0]
    return f"({' | '.join(parts)})"


def _chunked(values: Sequence[str], *, size: int = 200) -> tuple[tuple[str, ...], ...]:
    if size <= 0:
        raise ValueError("Chunk size must be positive.")
    return tuple(tuple(values[index : index + size]) for index in range(0, len(values), size))
