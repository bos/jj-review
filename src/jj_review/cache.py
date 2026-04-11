"""Persistence helpers for saved local jj-review data."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

from pydantic import ValidationError

from jj_review.errors import CliError
from jj_review.models.cache import ReviewState

logger = logging.getLogger(__name__)

STATE_DIRNAME = "jj-review"
STATE_FILENAME = "state.json"
_REPO_ID_RE = re.compile(r"^[0-9a-f]+$")


class ReviewStateError(CliError):
    """Raised when the saved local jj-review data is unreadable or invalid."""


class ReviewStateUnavailable(CliError):
    """Raised when optional repo-scoped jj-review data cannot be used."""


class ReviewStateStore:
    """Load and save jj-review data in a user state directory."""

    def __init__(self, path: Path | None, *, disabled_reason: str | None = None) -> None:
        self._path = path
        self._disabled_reason = disabled_reason

    @classmethod
    def for_repo(cls, repo_root: Path) -> ReviewStateStore:
        """Build a jj-review data store for the supplied repository root."""

        try:
            return cls(resolve_state_path(repo_root))
        except ReviewStateUnavailable as error:
            logger.debug("jj-review data disabled for %s: %s", repo_root, error)
            return cls(path=None, disabled_reason=str(error))

    @property
    def state_dir(self) -> Path | None:
        if self._path is None:
            return None
        return self._path.parent

    def require_writable(self) -> Path:
        """Return the data directory, or raise ReviewStateUnavailable if unavailable."""
        if self._path is None:
            raise ReviewStateUnavailable(
                f"The jj-review data directory is not available: {self._disabled_reason}. "
                "Mutating operations require a writable data directory. "
                "Ensure `jj config path --repo` succeeds and the path is writable."
            )
        return self._path.parent

    def load(self) -> ReviewState:
        """Load the saved data, or defaults when the file is missing."""

        if self._path is None:
            return ReviewState()
        try:
            return self._load_state()
        except ValidationError as error:
            raise ReviewStateError(
                f"Invalid jj-review data in {self._path}: {error}"
            ) from error

    def save(self, state: ReviewState) -> None:
        """Persist the supplied jj-review data."""

        if self._path is None:
            logger.debug("Skipping jj-review data save: %s", self._disabled_reason)
            return
        rendered = state.model_dump_json(exclude_none=True, indent=2) + "\n"
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=self._path.parent,
                prefix=self._path.name + ".",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                    tmp.write(rendered)
                Path(tmp_name).replace(self._path)
            except OSError:
                Path(tmp_name).unlink(missing_ok=True)
                raise
        except OSError as error:
            raise ReviewStateError(
                f"Could not write jj-review data file {self._path}: {error}"
            ) from error

    def _load_state(self) -> ReviewState:
        path = self._path
        if path is None:
            return ReviewState()
        if not path.exists():
            return ReviewState()
        if not path.is_file():
            raise ReviewStateError(f"jj-review data path is not a file: {path}")
        try:
            return ReviewState.model_validate_json(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise ReviewStateError(
                f"Could not read jj-review data file {path}: {error}"
            ) from error


def resolve_state_path(repo_root: Path) -> Path:
    """Return the machine-written jj-review data path for the repo."""

    repo_id = _resolve_repo_id(repo_root)
    return default_state_root() / STATE_DIRNAME / "repos" / repo_id / STATE_FILENAME


def default_state_root() -> Path:
    """Return the base directory used for machine-written jj-review data."""

    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path("~", ".local", "state").expanduser().resolve()


def _resolve_repo_id(repo_root: Path) -> str:
    config_id_path = repo_root / ".jj" / "repo" / "config-id"
    repo_id = _read_repo_id(config_id_path)
    if repo_id is not None:
        return repo_id

    _ensure_repo_config_id_exists(repo_root)
    repo_id = _read_repo_id(config_id_path)
    if repo_id is not None:
        return repo_id
    raise ReviewStateUnavailable(
        f"Could not determine jj repo config ID for {repo_root}: "
        "`jj config path --repo` did not create `.jj/repo/config-id`."
    )


def _read_repo_id(config_id_path: Path) -> str | None:
    if not config_id_path.exists():
        return None
    if not config_id_path.is_file():
        raise ReviewStateError(f"jj repo config ID path is not a file: {config_id_path}")
    try:
        repo_id = config_id_path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ReviewStateError(
            f"Could not read jj repo config ID {config_id_path}: {error}"
        ) from error
    if not repo_id:
        raise ReviewStateError(f"jj repo config ID file is empty: {config_id_path}")
    if not _REPO_ID_RE.fullmatch(repo_id):
        raise ReviewStateError(f"Invalid jj repo config ID in {config_id_path}: {repo_id!r}")
    return repo_id


def _ensure_repo_config_id_exists(repo_root: Path) -> None:
    try:
        completed = subprocess.run(
            ["jj", "config", "path", "--repo"],
            capture_output=True,
            check=False,
            cwd=repo_root,
            text=True,
        )
    except FileNotFoundError as error:
        raise ReviewStateError("`jj` is not installed or is not on PATH.") from error
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise ReviewStateUnavailable(
            f"Could not determine jj repo config path for {repo_root}: {message}"
        )
