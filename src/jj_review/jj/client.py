"""Typed access to local `jj` stack state."""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jj_review.errors import CliError
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.stack import LocalRevision, LocalStack

_COMMIT_TEMPLATE = (
    r'json(change_id) ++ "\t" ++ json(commit_id) ++ "\t" ++ json(description) ++ "\t" ++ '
    r'json(parents.map(|p| p.commit_id())) ++ "\t" ++ '
    r'json(empty) ++ "\t" ++ json(divergent) ++ "\t" ++ '
    r'json(current_working_copy) ++ "\t" ++ json(immutable) ++ "\n"'
)
_BOOKMARK_TEMPLATE = r'json(self) ++ "\n"'


class JjCommandError(CliError):
    """Raised when a `jj` invocation fails."""


class RevsetResolutionError(CliError):
    """Raised when a revset does not resolve to exactly one visible revision."""


class UnsupportedStackError(CliError):
    """Raised when local history cannot be treated as a linear review stack."""


type JjRunner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


@dataclass(slots=True)
class _RawBookmarkState:
    local_targets: tuple[str, ...] = ()
    remote_targets: list[RemoteBookmarkState] = field(default_factory=list)


class JjClient:
    """Thin wrapper around `jj` commands used by the review tool."""

    def __init__(self, repo_root: Path, *, runner: JjRunner | None = None) -> None:
        self._repo_root = repo_root
        self._runner = runner or _default_runner

    def discover_review_stack(self, revset: str | None = None) -> LocalStack:
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
                    "Select a concrete change instead."
                )

        if head.commit_id == trunk.commit_id:
            return LocalStack(
                head=head,
                revisions=(),
                selected_revset=selected_revset,
                trunk=trunk,
            )

        stack_head_first: list[LocalRevision] = []
        child_in_path: LocalRevision | None = None
        current = head
        while current.commit_id != trunk.commit_id:
            self._validate_reviewable_revision(current)
            if child_in_path is not None:
                reviewable_children = self.list_reviewable_children(current.commit_id)
                if len(reviewable_children) > 1:
                    raise UnsupportedStackError(
                        f"Unsupported stack shape at {current.change_id}: multiple "
                        "reviewable children require separate PR chains."
                    )
                child_matches_path = any(
                    child.commit_id == child_in_path.commit_id for child in reviewable_children
                )
                if not child_matches_path:
                    raise UnsupportedStackError(
                        f"Unsupported stack shape at {current.change_id}: selected head does "
                        "not follow the only reviewable child of this ancestor."
                    )

            stack_head_first.append(current)
            parent_commit_id = current.only_parent_commit_id()
            child_in_path = current
            current = self.resolve_revision(parent_commit_id)

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
        """Resolve a revset to exactly one visible revision."""

        revisions = self._query_revisions(revset, limit=2)
        if not revisions:
            raise RevsetResolutionError(
                f"Revset {revset!r} did not resolve to a visible revision."
            )
        if len(revisions) > 1:
            raise RevsetResolutionError(f"Revset {revset!r} resolved to more than one revision.")
        return revisions[0]

    def _resolve_trunk(self) -> LocalRevision:
        """Resolve `trunk()` and reject the implicit root fallback."""

        trunk = self.resolve_revision("trunk()")
        if len(trunk.parents) == 0:
            raise UnsupportedStackError(
                "`trunk()` resolved to the root commit. Configure a concrete trunk bookmark "
                "before discovering a review stack."
            )
        return trunk

    def list_reviewable_children(self, commit_id: str) -> list[LocalRevision]:
        """List visible mutable children that count as reviewable units."""

        revisions = self._query_revisions(f"children('{commit_id}')")
        return [revision for revision in revisions if revision.is_reviewable()]

    def list_git_remotes(self) -> tuple[GitRemote, ...]:
        """List configured Git remotes for the repository."""

        stdout = self._run(("git", "remote", "list"))
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

        stdout = self._run(command)
        grouped: dict[str, _RawBookmarkState] = {}
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            raw_bookmark = json.loads(stripped)
            if not isinstance(raw_bookmark, dict):
                raise JjCommandError(
                    "Unexpected `jj bookmark list` payload: expected a JSON object."
                )
            name = raw_bookmark["name"]
            if not isinstance(name, str):
                raise JjCommandError(
                    "Unexpected `jj bookmark list` payload: missing bookmark name."
                )
            bookmark_state = grouped.setdefault(name, _RawBookmarkState())
            targets = tuple(_require_sequence(raw_bookmark.get("target", ())))
            remote_name = raw_bookmark.get("remote")
            if remote_name is None:
                bookmark_state.local_targets = targets
                continue
            if not isinstance(remote_name, str):
                raise JjCommandError(
                    "Unexpected `jj bookmark list` payload: invalid remote bookmark entry."
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

    def set_bookmark(self, bookmark: str, revision: str) -> None:
        """Create or move a local bookmark to the supplied revision."""

        self._run(("bookmark", "set", bookmark, "-r", revision))

    def push_bookmark(self, *, remote: str, bookmark: str) -> None:
        """Push one bookmark to the selected remote."""

        self._run(("git", "push", "--remote", remote, "--bookmark", bookmark))

    def _query_revisions(self, revset: str, *, limit: int | None = None) -> list[LocalRevision]:
        command = ["log", "--no-graph", "-r", revset, "-T", _COMMIT_TEMPLATE]
        if limit is not None:
            command.extend(["--limit", str(limit)])

        stdout = self._run(command)
        revisions: list[LocalRevision] = []
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            revisions.append(_parse_revision_line(stripped))
        return revisions

    def _run(self, args: Sequence[str]) -> str:
        command = ["jj", *args]
        try:
            completed = self._runner(command, self._repo_root)
        except FileNotFoundError as error:
            raise JjCommandError("`jj` is not installed or is not on PATH.") from error

        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            raise JjCommandError(f"{shlex.join(command)} failed: {message}")
        return completed.stdout

    def _validate_reviewable_revision(self, revision: LocalRevision) -> None:
        # Check the root-commit condition before immutable, because the root
        # is always immutable in jj and "reached root before trunk()" is more
        # actionable than "immutable commit".
        if len(revision.parents) == 0:
            raise UnsupportedStackError(
                f"Unsupported stack shape at {revision.change_id}: stack reached the root "
                "commit before `trunk()`."
            )
        if revision.immutable:
            raise UnsupportedStackError(
                f"Unsupported stack shape at {revision.change_id}: immutable commits are not "
                "reviewable."
            )
        if revision.divergent:
            raise UnsupportedStackError(
                f"Unsupported stack shape at {revision.change_id}: divergent changes are not "
                "supported."
            )
        if len(revision.parents) > 1:
            raise UnsupportedStackError(
                f"Unsupported stack shape at {revision.change_id}: merge commits are not "
                "supported."
            )


def _default_runner(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        check=False,
        cwd=cwd,
        text=True,
    )


_EXPECTED_FIELD_COUNT = 8


def _parse_revision_line(line: str) -> LocalRevision:
    parts = line.split("\t")
    if len(parts) != _EXPECTED_FIELD_COUNT:
        raise JjCommandError(
            f"`jj log` output has unexpected format: expected {_EXPECTED_FIELD_COUNT} "
            f"tab-separated fields, got {len(parts)}. Raw line: {line!r}"
        )
    (
        change_id_json,
        commit_id_json,
        description_json,
        parents_json,
        empty_json,
        divergent_json,
        working_copy_json,
        immutable_json,
    ) = parts
    try:
        parents_raw = json.loads(parents_json)
        if not isinstance(parents_raw, list):
            raise JjCommandError(
                f"`jj log` output has unexpected field types: "
                f"parents field is not a JSON array. Raw line: {line!r}"
            )
        return LocalRevision(
            change_id=json.loads(change_id_json),
            commit_id=json.loads(commit_id_json),
            current_working_copy=json.loads(working_copy_json),
            description=json.loads(description_json),
            divergent=json.loads(divergent_json),
            empty=json.loads(empty_json),
            immutable=json.loads(immutable_json),
            parents=tuple(parents_raw),
        )
    except json.JSONDecodeError as error:
        raise JjCommandError(
            f"`jj log` output contains invalid JSON: {error}. Raw line: {line!r}"
        ) from error
    except (TypeError, ValueError) as error:
        raise JjCommandError(
            f"`jj log` output has unexpected field types: {error}. Raw line: {line!r}"
        ) from error


def _require_sequence(value: Any) -> Sequence[str]:
    if not isinstance(value, list | tuple):
        raise JjCommandError("Unexpected `jj bookmark list` payload: expected a sequence.")
    return tuple(str(item) for item in value)
