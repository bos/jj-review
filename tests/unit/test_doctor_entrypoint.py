from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jj_review import ui
from jj_review.commands.doctor import (
    _check_git_remote,
    _check_github_auth,
    _check_interruptions,
)
from jj_review.errors import CliError
from jj_review.models.bookmarks import GitRemote

# ---------------------------------------------------------------------------
# _check_git_remote
# ---------------------------------------------------------------------------


def test_check_git_remote_no_remotes() -> None:
    jj_client = MagicMock()
    jj_client.list_git_remotes.return_value = ()

    result, selected = _check_git_remote(jj_client)

    assert result.status == "fail"
    assert "no Git remotes" in ui.plain_text(result.detail)
    assert selected is None


def test_check_git_remote_ambiguous_multiple(monkeypatch: pytest.MonkeyPatch) -> None:
    import jj_review.commands.doctor as doctor_module

    jj_client = MagicMock()
    jj_client.list_git_remotes.return_value = (
        GitRemote(name="upstream", url="https://github.com/org/repo.git"),
        GitRemote(name="fork", url="https://github.com/user/repo.git"),
    )
    monkeypatch.setattr(
        doctor_module,
        "select_submit_remote",
        lambda remotes: (_ for _ in ()).throw(
            CliError("Could not determine which Git remote to use for submit.")
        ),
    )

    result, selected = _check_git_remote(jj_client)

    assert result.status == "fail"
    assert selected is None


# ---------------------------------------------------------------------------
# _check_github_auth
# ---------------------------------------------------------------------------


def test_check_github_auth_from_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    import jj_review.commands.doctor as doctor_module

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_token")
    monkeypatch.setattr(doctor_module, "github_token_from_env", lambda: "ghp_fake_token")

    result, token = _check_github_auth("https://api.github.com")

    assert result.status == "ok"
    assert "GITHUB_TOKEN" in ui.plain_text(result.detail)
    assert token == "ghp_fake_token"


def test_check_github_auth_from_gh_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    import jj_review.commands.doctor as doctor_module

    monkeypatch.setattr(doctor_module, "github_token_from_env", lambda: None)
    monkeypatch.setattr(
        doctor_module, "_github_token_for_base_url", lambda base_url: "ghp_cli_token"
    )

    result, token = _check_github_auth("https://api.github.com")

    assert result.status == "ok"
    assert "gh CLI" in ui.plain_text(result.detail)
    assert token == "ghp_cli_token"


def test_check_github_auth_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import jj_review.commands.doctor as doctor_module

    monkeypatch.setattr(doctor_module, "github_token_from_env", lambda: None)
    monkeypatch.setattr(doctor_module, "_github_token_for_base_url", lambda base_url: None)

    result, token = _check_github_auth("https://api.github.com")

    assert result.status == "fail"
    assert "gh auth login" in ui.plain_text(result.detail)
    assert token is None


# ---------------------------------------------------------------------------
# _check_interruptions
# ---------------------------------------------------------------------------


def test_check_interruptions_one_intent(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_store = MagicMock()
    state_store.state_dir = state_dir
    fake_intent = SimpleNamespace(
        intent=SimpleNamespace(label="submit on abc12345", pid=99999999)
    )
    state_store.list_intents.return_value = [fake_intent]

    result = _check_interruptions(state_store)

    assert result.status == "warn"
    assert "1 interrupted operation" in ui.plain_text(result.detail)
    assert "submit on abc12345" in ui.plain_text(result.detail)
