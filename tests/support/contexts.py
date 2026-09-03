"""One shared way to build a `CommandContext` over cheap fake collaborators."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from jj_stack.bootstrap import CommandContext, RuntimeOptions
from jj_stack.config import AppConfig
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import JjClient
from jj_stack.models.tracking import TrackingState
from jj_stack.state.store import TrackingStore


class InMemoryTrackingStore:
    """Serve one fixed tracking state without touching the state directory."""

    def __init__(self, state: TrackingState) -> None:
        self.state = state

    def load(self) -> TrackingState:
        return self.state


def fake_command_context(
    repo_root: Path = Path("/repo"),
    *,
    config: AppConfig | None = None,
    jj_client: JjClient | None = None,
) -> CommandContext:
    """Build a real `CommandContext` whose collaborators are cheap stand-ins.

    The state store is an in-memory double, so no test reads the process
    state directory. `jj_client` accepts a protocol-shaped fake; `JjClient`
    is a concrete class, so callers cast the fake at their call site.
    """

    return CommandContext(
        config=config if config is not None else AppConfig(),
        jj_client=jj_client if jj_client is not None else JjClient(repo_root),
        options=RuntimeOptions(cli_args=JjCliArgs(), debug=False, repo=repo_root),
        repo_root=repo_root,
        state_store=cast(
            TrackingStore,
            InMemoryTrackingStore(TrackingState()),
        ),
    )
