from __future__ import annotations

import subprocess
from pathlib import Path

import httpx

from jj_review.github.client import GithubClient

from .fake_github import (
    FakeGithubRepository,
    FakeGithubState,
    create_app,
    initialize_bare_repository,
)


def configure_fake_github_environment(
    *,
    command_modules: tuple[str, ...],
    fake_repo: FakeGithubRepository,
    monkeypatch,
    tmp_path: Path,
    extra_config_lines: list[str] | None = None,
) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    config_path = write_fake_github_config(
        tmp_path,
        fake_repo,
        extra_lines=extra_config_lines,
    )
    app = create_app(FakeGithubState.single_repository(fake_repo))

    def build_github_client(*, base_url: str) -> GithubClient:
        return GithubClient(
            base_url=base_url,
            transport=httpx.ASGITransport(app=app),
        )

    for module in command_modules:
        monkeypatch.setattr(f"{module}._build_github_client", build_github_client)
    return config_path


def init_fake_github_repo(
    tmp_path: Path,
    *,
    with_remote: bool = True,
) -> tuple[Path, FakeGithubRepository]:
    repo = tmp_path / "repo"
    fake_repo = initialize_bare_repository(
        tmp_path / "remotes",
        owner="octo-org",
        name="stacked-review",
    )
    run_command(["jj", "git", "init", str(repo)], tmp_path)
    run_command(["jj", "config", "set", "--repo", "user.name", "Test User"], repo)
    run_command(["jj", "config", "set", "--repo", "user.email", "test@example.com"], repo)
    write_file(repo / "README.md", "base\n")
    run_command(["jj", "commit", "-m", "base"], repo)
    run_command(["jj", "bookmark", "create", "main", "-r", "@-"], repo)
    run_command(["jj", "config", "set", "--repo", 'revset-aliases."trunk()"', "main"], repo)
    if with_remote:
        run_command(["jj", "git", "remote", "add", "origin", str(fake_repo.git_dir)], repo)
        run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", "main"], repo)
    return repo, fake_repo


def write_fake_github_config(
    tmp_path: Path,
    fake_repo: FakeGithubRepository,
    *,
    extra_lines: list[str] | None = None,
) -> Path:
    config_path = tmp_path / "config-home" / "jj-review" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "[repo]",
        'github_host = "github.test"',
        f'github_owner = "{fake_repo.owner}"',
        f'github_repo = "{fake_repo.name}"',
    ]
    if extra_lines:
        lines.append("")
        lines.extend(extra_lines)
    write_file(config_path, "\n".join(lines) + "\n")
    return config_path


def commit_file(repo: Path, message: str, filename: str) -> None:
    write_file(repo / filename, f"{message}\n")
    run_command(["jj", "commit", "-m", message], repo)


def run_command(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        capture_output=True,
        check=False,
        cwd=cwd,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"{command!r} failed:\nstdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed


def write_file(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
