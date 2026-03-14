"""Synthetic bookmark naming and resolution."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from jj_review.config import ChangeConfig
from jj_review.models.cache import CachedChange, ReviewState
from jj_review.models.stack import LocalRevision

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_DEFAULT_SLUG = "change"
_REVIEW_NAMESPACE = "review"
_SHORT_CHANGE_ID_LENGTH = 8

BookmarkSource = Literal["cache", "generated", "override"]


@dataclass(frozen=True, slots=True)
class ResolvedBookmark:
    """Resolved synthetic bookmark for one local revision."""

    bookmark: str
    change_id: str
    source: BookmarkSource


@dataclass(frozen=True, slots=True)
class BookmarkResolutionResult:
    """Bookmark resolutions plus the updated sparse local state."""

    changed: bool
    resolutions: tuple[ResolvedBookmark, ...]
    state: ReviewState


class BookmarkResolver:
    """Resolve synthetic bookmark names using cache-first semantics."""

    def __init__(
        self,
        state: ReviewState,
        overrides: Mapping[str, ChangeConfig] | None = None,
    ) -> None:
        self._state = state
        self._overrides = overrides or {}

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
                        source="cache",
                    )
                )
                continue

            bookmark = generate_bookmark_name(revision)
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


def generate_bookmark_name(revision: LocalRevision) -> str:
    """Generate the default synthetic bookmark for a change."""

    first_line = revision.description.splitlines()[0] if revision.description else ""
    slug = _slugify(first_line)
    short_change_id = revision.change_id[:_SHORT_CHANGE_ID_LENGTH]
    return f"{_REVIEW_NAMESPACE}/{slug}-{short_change_id}"


def _slugify(subject: str) -> str:
    slug = _NON_ALNUM_RE.sub("-", subject.lower()).strip("-")
    return slug or _DEFAULT_SLUG


def _updated_cached_change(
    cached_change: CachedChange | None,
    bookmark: str,
) -> CachedChange:
    if cached_change is None:
        return CachedChange(bookmark=bookmark)
    return cached_change.model_copy(update={"bookmark": bookmark})
