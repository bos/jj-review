"""Bookmark naming, rediscovery, and resolution helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from jj_review import ui
from jj_review.config import DEFAULT_BOOKMARK_PREFIX, ChangeConfig
from jj_review.errors import CliError
from jj_review.formatting import short_change_id
from jj_review.models.bookmarks import BookmarkState
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.models.stack import LocalRevision

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_DEFAULT_SLUG = "change"

BookmarkSource = Literal["saved", "discovered", "generated", "override"]


@dataclass(frozen=True, slots=True)
class ResolvedBookmark:
    """Resolved bookmark for one local revision."""

    bookmark: str
    change_id: str
    source: BookmarkSource


@dataclass(frozen=True, slots=True)
class BookmarkResolutionResult:
    """Bookmark resolutions plus the updated saved local data."""

    changed: bool
    resolutions: tuple[ResolvedBookmark, ...]
    state: ReviewState


class RevisionWithChangeId(Protocol):
    """Minimal revision shape needed for bookmark discovery."""

    @property
    def change_id(self) -> str: ...


class BookmarkResolver:
    """Resolve bookmark names using saved-data-first semantics."""

    def __init__(
        self,
        state: ReviewState,
        overrides: Mapping[str, ChangeConfig] | None = None,
        *,
        prefix: str = DEFAULT_BOOKMARK_PREFIX,
        discovered_bookmarks: Mapping[str, str] | None = None,
    ) -> None:
        self._state = state
        self._overrides = overrides or {}
        self._prefix = prefix
        self._discovered_bookmarks = discovered_bookmarks or {}

    def pin_revisions(
        self,
        revisions: tuple[LocalRevision, ...],
    ) -> BookmarkResolutionResult:
        """Resolve bookmarks and pin generated names into the returned state."""

        changed = False
        changes = dict(self._state.changes)
        resolutions: list[ResolvedBookmark] = []
        for revision in revisions:
            configured_change = self._overrides.get(revision.change_id)
            cached_change = changes.get(revision.change_id)
            if configured_change and configured_change.bookmark_override:
                resolutions.append(
                    ResolvedBookmark(
                        bookmark=configured_change.bookmark_override,
                        change_id=revision.change_id,
                        source="override",
                    )
                )
                continue
            if cached_change and cached_change.bookmark:
                resolutions.append(
                    ResolvedBookmark(
                        bookmark=cached_change.bookmark,
                        change_id=revision.change_id,
                        source="saved",
                    )
                )
                continue
            if discovered_bookmark := self._discovered_bookmarks.get(revision.change_id):
                changes[revision.change_id] = _updated_cached_change(
                    cached_change,
                    discovered_bookmark,
                )
                resolutions.append(
                    ResolvedBookmark(
                        bookmark=discovered_bookmark,
                        change_id=revision.change_id,
                        source="discovered",
                    )
                )
                changed = True
                continue

            bookmark = generate_bookmark_name(
                revision,
                prefix=self._prefix,
            )
            changes[revision.change_id] = _updated_cached_change(cached_change, bookmark)
            resolutions.append(
                ResolvedBookmark(
                    bookmark=bookmark,
                    change_id=revision.change_id,
                    source="generated",
                )
            )
            changed = True

        return BookmarkResolutionResult(
            changed=changed,
            resolutions=tuple(resolutions),
            state=self._state.model_copy(update={"changes": changes}),
        )


def bookmark_glob(prefix: str) -> str:
    """Return the wildcard pattern for managed review branches."""

    return f"{prefix}/*"


def is_review_bookmark(bookmark: str, *, prefix: str) -> bool:
    """Whether `bookmark` uses the configured managed review prefix."""

    return bookmark.startswith(f"{prefix}/")


def generate_bookmark_name(
    revision: LocalRevision,
    *,
    prefix: str = DEFAULT_BOOKMARK_PREFIX,
) -> str:
    """Generate the default bookmark for a change."""

    first_line = revision.description.splitlines()[0] if revision.description else ""
    slug = _NON_ALNUM_RE.sub("-", first_line.lower()).strip("-") or _DEFAULT_SLUG
    return f"{prefix}/{slug}-{short_change_id(revision.change_id)}"


def discover_bookmarks_for_revisions(
    *,
    bookmark_states: dict[str, BookmarkState],
    prefix: str = DEFAULT_BOOKMARK_PREFIX,
    remote_name: str,
    revisions: tuple[RevisionWithChangeId, ...],
) -> dict[str, str]:
    discovered: dict[str, str] = {}
    for revision in revisions:
        candidates = [
            bookmark
            for bookmark, bookmark_state in bookmark_states.items()
            if bookmark_matches_generated_change_id(
                bookmark,
                revision.change_id,
                prefix=prefix,
            )
            and _bookmark_state_is_discoverable(bookmark_state, remote_name)
        ]
        if not candidates:
            continue
        unique_candidates = sorted(set(candidates))
        if len(unique_candidates) > 1:
            raise CliError(
                t"Could not safely rediscover the bookmark for change "
                t"{ui.change_id(revision.change_id)}: multiple existing bookmarks match "
                t"its stable change-ID suffix: {ui.join(ui.bookmark, unique_candidates)}."
            )
        discovered[revision.change_id] = unique_candidates[0]
    return discovered


def ensure_unique_bookmarks(resolutions: tuple[ResolvedBookmark, ...]) -> None:
    bookmarks_to_changes: dict[str, list[str]] = {}
    for resolution in resolutions:
        bookmarks_to_changes.setdefault(resolution.bookmark, []).append(resolution.change_id)

    duplicates = {
        bookmark: change_ids
        for bookmark, change_ids in bookmarks_to_changes.items()
        if len(change_ids) > 1
    }
    if not duplicates:
        return

    collisions = ui.join(
        lambda item: t"{ui.bookmark(item[0])} for changes {ui.join(ui.change_id, item[1])}",
        sorted(duplicates.items()),
    )
    raise CliError(
        t"Selected stack resolves multiple changes to the same bookmark: "
        t"{collisions}.",
        hint="Configure distinct bookmark names before submitting.",
    )


def bookmark_matches_generated_change_id(
    bookmark: str,
    change_id: str,
    *,
    prefix: str = DEFAULT_BOOKMARK_PREFIX,
) -> bool:
    return is_review_bookmark(
        bookmark,
        prefix=prefix,
    ) and bookmark.endswith(f"-{short_change_id(change_id)}")


def _bookmark_state_is_discoverable(bookmark_state: BookmarkState, remote_name: str) -> bool:
    if bookmark_state.local_targets:
        return True
    remote_state = bookmark_state.remote_target(remote_name)
    return remote_state is not None and bool(remote_state.targets)


def _updated_cached_change(
    cached_change: CachedChange | None,
    bookmark: str,
) -> CachedChange:
    if cached_change is None:
        return CachedChange(bookmark=bookmark)
    return cached_change.model_copy(update={"bookmark": bookmark})
