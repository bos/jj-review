from pathlib import Path
from types import SimpleNamespace

import pytest


def app_context(
    tmp_path: Path,
    *,
    repo_config: object | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        repo_root=tmp_path,
        config=SimpleNamespace(
            change={},
            logging=SimpleNamespace(level="WARNING"),
            repo=repo_config if repo_config is not None else SimpleNamespace(trunk_branch=None),
        ),
    )


def patch_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    module,
    tmp_path: Path,
    *,
    repo_config: object | None = None,
) -> None:
    monkeypatch.setattr(
        module,
        "bootstrap_context",
        lambda **kwargs: app_context(tmp_path, repo_config=repo_config),
    )


def fake_submit_state_store(tmp_path: Path) -> SimpleNamespace:
    state_dir = tmp_path / "jj-review-state"
    return SimpleNamespace(
        state_dir=state_dir,
        require_writable=lambda: state_dir,
    )
