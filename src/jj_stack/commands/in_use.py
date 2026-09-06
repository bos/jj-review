"""Check whether this repo contains valid `jj-stack` tracking data.

Use this in scripts to check whether jj-stack has been set up in this repo. It exits silently
with 0 when valid tracking data exists, or 1 when there is none. Repo or tracking errors produce
a diagnostic and exit 11. The command does not contact GitHub or change local state.
"""

from __future__ import annotations

from pathlib import Path

from jj_stack.bootstrap import resolve_repo_root, validate_repo_path
from jj_stack.errors import CliError, ProbeError
from jj_stack.state.store import TrackingStore

HELP = "Check whether this repo uses jj-stack"


def in_use(*, repo: Path | None) -> int:
    """CLI entrypoint for `in-use`."""

    start = Path.cwd() if repo is None else repo
    try:
        validate_repo_path(repo)
        repo_root = resolve_repo_root(start)
        return 0 if TrackingStore.for_repo(repo_root).is_in_use() else 1
    except CliError as error:
        raise ProbeError(error.message, hint=error.hint) from error
