"""Replay the fixed and opt-in submit property scenarios."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from tests.integration.submit_command_helpers import (
    configure_submit_environment,
    run_main,
)
from tests.run_submit_property_scenarios import (
    DEFAULT_PROPERTY_SEED,
    PROPERTY_DRIFT_SCENARIOS_ENV,
    PROPERTY_LIFECYCLE_SCENARIOS_ENV,
    PROPERTY_RETRY_SCENARIOS_ENV,
    PROPERTY_SCENARIOS_ENV,
    PROPERTY_SEED_ENV,
    PROPERTY_STACK_JOIN_SCENARIOS_ENV,
    PROPERTY_STACK_MOVE_SCENARIOS_ENV,
)
from tests.support.fake_github import FakeGithubState, create_app
from tests.support.integration_helpers import (
    init_fake_github_repo,
    patch_github_client_builders,
)
from tests.support.submit_property_harness import (
    replay_external_drift_scenario,
    replay_failed_submit_retry_scenario,
    replay_lifecycle,
    replay_stack_join_scenario,
    replay_stack_move_scenario,
    replay_successful_stack_edit_scenario,
)
from tests.support.submit_property_scenarios import (
    DEFAULT_EXTERNAL_DRIFT_SCENARIO_COUNT,
    DEFAULT_STACK_EDIT_SCENARIO_COUNT,
    DEFAULT_STACK_JOIN_SCENARIO_COUNT,
    DEFAULT_STACK_MOVE_SCENARIO_COUNT,
    DEFAULT_SUBMIT_RETRY_SCENARIO_COUNT,
    LIFECYCLE_SCENARIOS as FIXED_LIFECYCLE_SCENARIOS,
    ExternalDriftScenario,
    LifecycleScenario,
    StackEditScenario,
    StackJoinScenario,
    StackMoveScenario,
    SubmitRetryScenario,
    generate_external_drift_scenarios,
    generate_lifecycle_scenarios,
    generate_stack_edit_scenarios,
    generate_stack_join_scenarios,
    generate_stack_move_scenarios,
    generate_submit_retry_scenarios,
    subject_for_label,
)

import jj_stack.cli as cli_module
import jj_stack.commands.submit.command as submit_command
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError

pytestmark = pytest.mark.fixed_property


def _scenarios_from_environment[Scenario](
    generator: Callable[..., tuple[Scenario, ...]],
    *,
    count_environment_name: str,
    default_count: int,
) -> tuple[Scenario, ...]:
    count = int(os.environ.get(count_environment_name, str(default_count)))
    seed = int(os.environ.get(PROPERTY_SEED_ENV, str(DEFAULT_PROPERTY_SEED)))
    return generator(count=count, seed=seed)


STACK_EDIT_SCENARIOS = _scenarios_from_environment(
    generate_stack_edit_scenarios,
    count_environment_name=PROPERTY_SCENARIOS_ENV,
    default_count=DEFAULT_STACK_EDIT_SCENARIO_COUNT,
)
STACK_JOIN_SCENARIOS = _scenarios_from_environment(
    generate_stack_join_scenarios,
    count_environment_name=PROPERTY_STACK_JOIN_SCENARIOS_ENV,
    default_count=DEFAULT_STACK_JOIN_SCENARIO_COUNT,
)
STACK_MOVE_SCENARIOS = _scenarios_from_environment(
    generate_stack_move_scenarios,
    count_environment_name=PROPERTY_STACK_MOVE_SCENARIOS_ENV,
    default_count=DEFAULT_STACK_MOVE_SCENARIO_COUNT,
)
SUBMIT_RETRY_SCENARIOS = _scenarios_from_environment(
    generate_submit_retry_scenarios,
    count_environment_name=PROPERTY_RETRY_SCENARIOS_ENV,
    default_count=DEFAULT_SUBMIT_RETRY_SCENARIO_COUNT,
)
EXTERNAL_DRIFT_SCENARIOS = _scenarios_from_environment(
    generate_external_drift_scenarios,
    count_environment_name=PROPERTY_DRIFT_SCENARIOS_ENV,
    default_count=DEFAULT_EXTERNAL_DRIFT_SCENARIO_COUNT,
)
LIFECYCLE_SCENARIOS = _scenarios_from_environment(
    generate_lifecycle_scenarios,
    count_environment_name=PROPERTY_LIFECYCLE_SCENARIOS_ENV,
    default_count=len(FIXED_LIFECYCLE_SCENARIOS),
)
RETRY_CONFIG_LINES = [
    'labels = ["needs-review"]',
    'reviewers = ["alice"]',
    'team_reviewers = ["platform"]',
]


def _submit_runner(repo: Path, config_path: Path):
    def submit(revset: str | None) -> int:
        args = () if revset is None else (revset,)
        return run_main(repo, config_path, "submit", *args)

    return submit


@pytest.mark.parametrize("scenario", LIFECYCLE_SCENARIOS, ids=lambda scenario: scenario.name)
def test_lifecycles(tmp_path, monkeypatch, capsys, scenario: LifecycleScenario) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    def run_cli(args):
        return run_main(repo, config_path, *args)

    replay_lifecycle(fake_repo, repo, run_cli, capsys.readouterr, scenario)


@pytest.mark.parametrize(
    "scenario",
    STACK_EDIT_SCENARIOS,
    ids=lambda scenario: scenario.name,
)
def test_submit_property_stack_edits_preserve_pr_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: StackEditScenario,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    submit = _submit_runner(repo, config_path)

    replay_successful_stack_edit_scenario(
        discard_output=capsys.readouterr,
        fake_repo=fake_repo,
        repo=repo,
        scenario=scenario,
        submit=submit,
    )


@pytest.mark.parametrize(
    "scenario",
    STACK_JOIN_SCENARIOS,
    ids=lambda scenario: scenario.name,
)
def test_submit_property_stack_join_preserves_pr_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: StackJoinScenario,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    submit = _submit_runner(repo, config_path)

    replay_stack_join_scenario(
        discard_output=capsys.readouterr,
        fake_repo=fake_repo,
        repo=repo,
        scenario=scenario,
        submit=submit,
    )


@pytest.mark.parametrize(
    "scenario",
    STACK_MOVE_SCENARIOS,
    ids=lambda scenario: scenario.name,
)
def test_submit_property_stack_move_refreshes_both_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: StackMoveScenario,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    submit = _submit_runner(repo, config_path)

    replay_stack_move_scenario(
        discard_output=capsys.readouterr,
        fake_repo=fake_repo,
        repo=repo,
        scenario=scenario,
        submit=submit,
    )


@pytest.mark.parametrize(
    "scenario",
    EXTERNAL_DRIFT_SCENARIOS,
    ids=lambda scenario: scenario.name,
)
def test_submit_property_external_drift_matches_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: ExternalDriftScenario,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    # `main` swallows CliError into an exit code, so record the error it hands
    # to the top-level printer; the replay asserts the fail-closed diagnosis.
    last_error: list[CliError | None] = [None]
    print_cli_error = cli_module._print_cli_error

    def print_and_record_cli_error(error: CliError) -> None:
        last_error[0] = error
        print_cli_error(error)

    monkeypatch.setattr(cli_module, "_print_cli_error", print_and_record_cli_error)

    def run_cli(args: tuple[str, ...]) -> int:
        last_error[0] = None
        return run_main(repo, config_path, *args)

    replay_external_drift_scenario(
        discard_output=capsys.readouterr,
        fake_repo=fake_repo,
        last_cli_error=lambda: last_error[0],
        repo=repo,
        run_cli=run_cli,
        scenario=scenario,
    )


@pytest.mark.parametrize(
    "scenario",
    SUBMIT_RETRY_SCENARIOS,
    ids=lambda scenario: scenario.name,
)
def test_submit_property_failed_submit_retry_converges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: SubmitRetryScenario,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(
        monkeypatch,
        tmp_path,
        fake_repo,
        extra_config_lines=RETRY_CONFIG_LINES,
    )
    _install_submit_retry_fault(
        fake_repo=fake_repo,
        monkeypatch=monkeypatch,
        scenario=scenario,
    )

    submit = _submit_runner(repo, config_path)

    def relink(pr_number: int, change_id: str) -> int:
        return run_main(
            repo,
            config_path,
            "relink",
            str(pr_number),
            change_id,
        )

    def submit_after_relink(revset: str | None) -> int:
        args = () if revset is None else (revset,)
        return run_main(
            repo,
            config_path,
            "submit",
            "--reviewers",
            "alice",
            *args,
        )

    replay_failed_submit_retry_scenario(
        discard_output=capsys.readouterr,
        fake_repo=fake_repo,
        relink=relink,
        repo=repo,
        scenario=scenario,
        submit=submit,
        submit_after_relink=submit_after_relink,
    )


def _install_submit_retry_fault(
    *,
    fake_repo,
    monkeypatch: pytest.MonkeyPatch,
    scenario: SubmitRetryScenario,
) -> None:
    if scenario.failure_point == "after_remote_push":
        _install_remote_push_fault(monkeypatch)
        return

    app = create_app(FakeGithubState.single_repo(fake_repo))
    failed = False
    target_title = subject_for_label(scenario.failure_label)

    class FaultingGithubClient(GithubClient):
        async def create_pr(self, *, base, body, draft=False, head, title):
            nonlocal failed
            pr = await super().create_pr(
                base=base,
                body=body,
                draft=draft,
                head=head,
                title=title,
            )
            if not failed and scenario.failure_point == "create_pr" and title == target_title:
                failed = True
                raise GithubClientError(
                    "Simulated pull request creation failure",
                    status_code=500,
                )
            return pr

        async def update_pr(
            self,
            *,
            pr_number,
            base=None,
            body=None,
            title=None,
        ):
            nonlocal failed
            pr = await super().update_pr(
                pr_number=pr_number,
                base=base,
                body=body,
                title=title,
            )
            if not failed and scenario.failure_point == "update_pr" and pr.title == target_title:
                failed = True
                raise GithubClientError("Simulated pull request update failure", status_code=500)
            return pr

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.submit.command",),
        client_type=FaultingGithubClient,
    )


def _install_remote_push_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = False
    original_mutate_pr_branch_refs = submit_command.JjClient.mutate_remote_pr_branch_refs

    def mutate_pr_branch_refs_then_fail(self, *, remote, updates) -> None:
        nonlocal failed
        original_mutate_pr_branch_refs(
            self,
            remote=remote,
            updates=updates,
        )
        if not failed:
            failed = True
            raise CliError("Simulated failure after remote branch push")

    monkeypatch.setattr(
        submit_command.JjClient,
        "mutate_remote_pr_branch_refs",
        mutate_pr_branch_refs_then_fail,
    )
