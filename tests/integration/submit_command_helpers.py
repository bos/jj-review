from __future__ import annotations

import subprocess
from pathlib import Path

from jj_stack.cli import main

from ..support.fake_github import FakeGithubRepo
from ..support.integration_helpers import (
    configure_fake_github_environment,
    run_command,
)


def configure_submit_environment(
    monkeypatch,
    tmp_path: Path,
    fake_repo: FakeGithubRepo,
    *,
    extra_config_lines: list[str] | None = None,
) -> Path:
    return configure_fake_github_environment(
        command_modules=(
            "jj_stack.commands.submit.command",
            "jj_stack.commands.relink",
            "jj_stack.commands.unstack",
            "jj_stack.commands.cleanup.command",
            "jj_stack.commands.merge.command",
            "jj_stack.commands.sync",
            "jj_stack.commands.list_",
            "jj_stack.stack.status",
        ),
        fake_repo=fake_repo,
        extra_config_lines=extra_config_lines,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )


def issue_comments(fake_repo: FakeGithubRepo, issue_number: int):
    return fake_repo.issue_comments.get(issue_number, [])


def read_remote_ref(remote: Path, bookmark: str) -> str:
    completed = run_command(
        ["git", "--git-dir", str(remote), "rev-parse", f"refs/heads/{bookmark}"],
        remote.parent,
    )
    return completed.stdout.strip()


def remote_refs(remote: Path) -> dict[str, str]:
    completed = subprocess.run(
        ["git", "--git-dir", str(remote), "show-ref", "--heads"],
        capture_output=True,
        check=False,
        cwd=remote.parent,
        text=True,
    )
    if completed.returncode not in (0, 1):
        raise AssertionError(
            "['git', '--git-dir', "
            f"{str(remote)!r}, 'show-ref', '--heads'] failed:\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    refs: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        commit_id, ref_name = line.split(" ", maxsplit=1)
        refs[ref_name] = commit_id
    return refs


def run_main(repo: Path, config_path: Path, command: str, *command_args: str) -> int:
    argv = ["--config-file", str(config_path), "--repository", str(repo), command]
    argv.extend(command_args)
    return main(argv)
