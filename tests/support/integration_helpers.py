from __future__ import annotations

import atexit
import importlib
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx

from jj_review.github.client import GithubClient
from jj_review.github.resolution import ParsedGithubRepo

from .fake_github import (
    FakeGithubRepository,
    FakeGithubState,
    create_app,
    initialize_bare_repository,
)

_TEMPLATE_OWNER = "octo-org"
_TEMPLATE_NAME = "stacked-review"
_CACHED_TEMPLATE: Path | None = None


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

    def parse_github_repo(*_args, **_kwargs) -> ParsedGithubRepo:
        return ParsedGithubRepo(
            host="github.test",
            owner=fake_repo.owner,
            repo=fake_repo.name,
        )

    for module in command_modules:
        module_object = importlib.import_module(module)
        monkeypatch.setattr(module_object, "build_github_client", build_github_client)
        monkeypatch.setattr(module_object, "parse_github_repo", parse_github_repo, raising=False)
        monkeypatch.setattr(
            module_object, "require_github_repo", parse_github_repo, raising=False
        )
    return config_path


def _copy_fake_github_repo_from_template(
    tmp_path: Path,
    template_root: Path,
) -> tuple[Path, FakeGithubRepository]:
    shutil.copytree(template_root / "repo", tmp_path / "repo")
    shutil.copytree(template_root / "remotes", tmp_path / "remotes")
    repo = tmp_path / "repo"
    git_dir = tmp_path / "remotes" / _TEMPLATE_OWNER / f"{_TEMPLATE_NAME}.git"
    run_command(["jj", "git", "remote", "set-url", "origin", str(git_dir)], repo)
    fake_repo = FakeGithubRepository(
        default_branch="main",
        git_dir=git_dir,
        name=_TEMPLATE_NAME,
        owner=_TEMPLATE_OWNER,
    )
    return repo, fake_repo


def _init_fake_github_repo_fresh(
    tmp_path: Path,
    *,
    with_remote: bool,
) -> tuple[Path, FakeGithubRepository]:
    repo = tmp_path / "repo"
    fake_repo = initialize_bare_repository(
        tmp_path / "remotes",
        owner=_TEMPLATE_OWNER,
        name=_TEMPLATE_NAME,
    )
    run_command(["jj", "git", "init", str(repo)], tmp_path)
    write_file(repo / "README.md", "base\n")
    run_command(["jj", "commit", "-m", "base"], repo)
    run_command(["jj", "bookmark", "create", "main", "-r", "@-"], repo)
    if with_remote:
        run_command(["jj", "git", "remote", "add", "origin", str(fake_repo.git_dir)], repo)
        run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", "main"], repo)
    return repo, fake_repo


def _get_cached_template() -> Path:
    global _CACHED_TEMPLATE
    if _CACHED_TEMPLATE is None:
        template_root = Path(tempfile.mkdtemp(prefix="jjr_tpl_"))
        atexit.register(lambda: shutil.rmtree(template_root, ignore_errors=True))
        _init_fake_github_repo_fresh(template_root, with_remote=True)
        _CACHED_TEMPLATE = template_root
    return _CACHED_TEMPLATE


def init_fake_github_repo(
    tmp_path: Path,
    *,
    with_remote: bool = True,
) -> tuple[Path, FakeGithubRepository]:
    if not with_remote:
        return _init_fake_github_repo_fresh(tmp_path, with_remote=False)
    template_root = _get_cached_template()
    return _copy_fake_github_repo_from_template(tmp_path, template_root)


def init_repo(
    tmp_path: Path,
    *,
    configure_trunk: bool = True,
) -> Path:
    repo = tmp_path / "repo"
    run_command(["jj", "git", "init", str(repo)], tmp_path)
    write_file(repo / "README.md", "base\n")
    run_command(["jj", "commit", "-m", "base"], repo)
    if configure_trunk:
        run_command(["jj", "bookmark", "create", "main", "-r", "@-"], repo)
    return repo


def write_fake_github_config(
    tmp_path: Path, _fake_repo: FakeGithubRepository, *, extra_lines: list[str] | None = None
) -> Path:
    config_path = tmp_path / "jj-review-config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["[jj-review.repo]"]
    if extra_lines:
        lines.append("")
        lines.extend(extra_lines)
    write_file(config_path, "\n".join(lines) + "\n")
    return config_path


def commit_file(repo: Path, message: str, filename: str) -> None:
    write_file(repo / filename, f"{message}\n")
    run_command(["jj", "commit", "-m", message], repo)


def run_command(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, capture_output=True, check=False, cwd=cwd, text=True)
    if completed.returncode != 0:
        raise AssertionError(
            f"{command!r} failed:\nstdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed


def write_file(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
