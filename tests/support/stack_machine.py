"""Generated client commands and external GitHub events against real jj repositories."""

from __future__ import annotations

import io
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha1
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, get_args

import pytest
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule

import jj_stack.cli as cli_module
from jj_stack.errors import CliError, DriftError
from jj_stack.identifiers import ChangeId
from jj_stack.jj.client import JjClient, UnsupportedStackError
from jj_stack.models.tracking import PRIdentity, SubmittedBaseline, TrackedPR
from jj_stack.state.store import TrackingStore
from tests.integration.submit_command_helpers import configure_submit_environment, run_main

from .fake_github import FakeGithubPR, FakeStackMergeOperation, _complete_stack_merge
from .integration_helpers import (
    commit_file,
    init_fake_github_repo,
    init_fake_github_repo_with_submitted_stack,
    remote_refs,
    run_command,
    selected_stack,
    update_remote_ref,
    write_file,
)
from .stack_edit_scenarios import (
    StackEditOperation,
    StackEditOperationKind,
    apply_stack_edit,
    move_after_candidates,
    move_before_candidates,
)
from .submit_faults import install_submit_fault

Drift = Literal[
    "closed_pr",
    "reopened_pr",
    "remote_branch_deleted",
    "foreign_branch_fetched",
    "pr_base_retargeted",
    "pr_draft_toggled",
    "trunk_advanced",
]
MergeMethod = Literal["squash", "rebase"]


def subject(label: str) -> str:
    return f"feature {int(label[1:])}"


def filename(label: str) -> str:
    return f"feature-{int(label[1:])}.txt"


def pr_state(pr: FakeGithubPR) -> dict[str, object]:
    return {key: value for key, value in asdict(pr).items() if key != "head_sha"}


def blob(contents: str) -> str:
    data = contents.encode()
    return sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


class StackMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.resources = ExitStack()
        self.root = Path(self.resources.enter_context(TemporaryDirectory(prefix="jj-property-")))
        self.patch = self.resources.enter_context(pytest.MonkeyPatch.context())
        self.paths: list[tuple[str, ...]] = []
        self.ids: dict[str, ChangeId] = {}
        self.submitted: dict[str, TrackedPR] = {}
        self.dirty: set[str] = set()
        self.foreign: set[str] = set()
        self.contents: dict[str, dict[str, str]] = {}
        self.rebased: dict[str, str] = {}
        self.pr_count = 0
        self.last_error: CliError | None = None
        config = self.root / "jj-config.toml"
        config.write_text('[revset-aliases]\n"trunk()" = "main"\n')
        self.patch.setenv("JJ_CONFIG", str(config))
        self.patch.setenv("JJ_USER", "Test User")
        self.patch.setenv("JJ_EMAIL", "test@example.com")

    @initialize(size=st.integers(1, 4), submitted=st.booleans())
    def start(self, size: int, submitted: bool) -> None:
        if submitted:
            self.repo, self.fake = init_fake_github_repo_with_submitted_stack(
                self.root, size=size
            )
        else:
            self.repo, self.fake = init_fake_github_repo(self.root)
        self.config = configure_submit_environment(
            self.patch,
            self.root,
            self.fake,
            extra_config_lines=[
                'labels = ["needs-review"]',
                'reviewers = ["alice"]',
                'team_reviewers = ["platform"]',
            ],
        )
        self.fake.allow_rebase_merge = True
        self.jj = JjClient(self.repo)
        self.jj.ensure_pr_branch_fetch_isolation(remote="origin")
        self.trunk = self.trunk_files()
        self.store = TrackingStore.for_repo(self.repo)
        self.patch.setattr(cli_module, "_print_cli_error", self.record_error)
        if submitted:
            changes = selected_stack(self.repo).changes
            path = tuple(f"c{index}" for index in range(1, size + 1))
            self.ids = dict(zip(path, (change.change_id for change in changes), strict=True))
            self.paths.append(path)
            self.contents = {label: {filename(label): subject(label) + "\n"} for label in path}
            state = self.store.load()
            self.submitted = {label: state.prs[cid] for label, cid in self.ids.items()}
            self.pr_count = size
            self.approve(path)
        else:
            self.new_stack(size)

    def teardown(self) -> None:
        self.resources.close()

    def record_error(self, error: CliError) -> None:
        self.last_error = error

    def cli(self, *args: str) -> tuple[int, str]:
        self.last_error = None
        reviews = deepcopy(self.fake.pr_reviews)
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = run_main(self.repo, self.config, *args)
        assert self.fake.pr_reviews == reviews
        return code, output.getvalue()

    def ok(self, *args: str) -> str:
        code, output = self.cli(*args)
        assert code == 0, (args, code, self.last_error, output)
        return output

    def new_stack(self, size: int) -> None:
        run_command(["jj", "new", "main"], self.repo)
        labels = []
        for _ in range(size):
            label = f"c{len(self.ids) + 1}"
            commit_file(self.repo, subject(label), filename(label))
            self.ids[label] = selected_stack(self.repo).head.change_id
            self.contents[label] = {filename(label): subject(label) + "\n"}
            labels.append(label)
        self.paths.append(tuple(labels))
        self.dirty.update(labels)

    def approve(self, labels: tuple[str, ...]) -> None:
        for label in labels:
            self.fake.create_pr_review(
                pr_number=self.submitted[label].pr_identity.pr_number,
                reviewer_login=label,
                state="APPROVED",
            )

    def published(self, path: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(label for label in path if label in self.submitted)

    def merged(self, path: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(label for label in self.published(path) if self.pr(label).merged_at)

    def pr(self, label: str):
        return self.fake.prs[self.submitted[label].pr_identity.pr_number]

    def open_pr(self, label: str) -> FakeGithubPR | None:
        matches = [
            pr
            for pr in self.fake.prs.values()
            if pr.state == "open" and pr.head_ref.endswith(f"-{self.ids[label][:8]}")
        ]
        assert len(matches) <= 1, (label, matches)
        return next(iter(matches), None)

    def editable(self) -> list[int]:
        return [
            i
            for i, path in enumerate(self.paths)
            if not self.merged(path)
            and not self.foreign.intersection(path)
            and not self.rebased.keys() & set(path)
        ]

    def edits(self) -> list[tuple[int, StackEditOperation]]:
        options = []
        for i, path in enumerate(self.paths):
            if self.foreign.intersection(path) or self.rebased.keys() & set(path):
                continue
            merged = self.merged(path)
            for kind in get_args(StackEditOperationKind):
                if kind.startswith("insert") and len(path) >= 8:
                    continue
                if kind == "abandon" and len(path) == 1:
                    continue
                pairs = (
                    move_after_candidates(path)
                    if kind == "move_after"
                    else move_before_candidates(path)
                    if kind == "move_before"
                    else tuple(
                        (label, None)
                        for label in (
                            path[1:]
                            if kind == "squash_into_previous"
                            else path[:-1]
                            if kind == "move_to_top"
                            else path
                        )
                    )
                )
                for label, target in pairs:
                    if label in merged and not kind.startswith("insert"):
                        continue
                    if kind == "squash_into_previous" and path[path.index(label) - 1] in merged:
                        continue
                    new = f"c{len(self.ids) + 1}" if kind.startswith("insert") else None
                    options.append((i, StackEditOperation(kind, label, new, target)))
        return options

    def ready(self) -> list[int]:
        live = {label for path in self.paths for label in path}
        if set(self.submitted) - live:
            return []
        return [
            i
            for i in self.editable()
            if (published := self.published(self.paths[i]))
            and self.paths[i][: len(published)] == published
            and not self.dirty.intersection(published)
            and all(
                self.pr(label).state == "open"
                and not self.pr(label).is_draft
                and self.pr(label).base_ref
                == ("main" if position == 0 else self.pr(published[position - 1]).head_ref)
                for position, label in enumerate(published)
            )
            and self.grouped(published)
        ]

    def grouped(self, path: tuple[str, ...]) -> bool:
        numbers = tuple(self.pr(label).number for label in path)
        return len(numbers) < 2 or numbers in (
            tuple(number for number in members if self.fake.prs[number].merged_at is None)
            for members in self.fake.github_stacks.values()
        )

    def outside(self, selected: tuple[str, ...]) -> dict[str, object]:
        refs = remote_refs(self.fake.git_dir)
        state = self.store.load()
        return {
            label: (
                state.prs.get(self.ids[label]),
                refs.get(f"refs/heads/{record.pr_identity.head_ref}"),
                pr_state(self.pr(label)),
            )
            for label, record in self.submitted.items()
            if label not in selected
        }

    def diagnosis(self) -> str | None:
        error: BaseException | None = self.last_error
        if isinstance(error, DriftError):
            return error.condition
        while error is not None:
            if isinstance(error, UnsupportedStackError):
                return f"unsupported_stack:{error.reason}"
            error = error.__cause__
        return None

    def submit_path(self, index: int) -> None:
        path = self.paths[index]
        head = self.ids[path[-1]]
        failures = self.submit_failures(path)
        if failures:
            before = self.snapshot()
            code, output = self.cli("submit", head)
            assert (code, self.diagnosis()) in failures, (code, self.last_error, output)
            assert self.snapshot() == before
            code, output = self.cli("view", head)
            assert code in {0, 2, 10}, (code, output)
            return
        outside = self.outside(path)
        fresh = set(path) - self.submitted.keys()
        numbers = set(self.fake.prs)
        self.fake.pr_events.clear()
        self.ok("submit", head)
        self.pr_count += len(fresh)
        self.accept_submit(path, fresh=fresh, prior_numbers=numbers)
        assert self.outside(path) == outside
        assert all(event.kind != "state" for event in self.fake.pr_events)

    def submit_failures(self, path: tuple[str, ...]) -> set[tuple[int, str | None]]:
        if self.foreign.intersection(path):
            return {
                (2, "unsupported_stack:divergent_change"),
                (2, "unsupported_stack:immutable_commit"),
            }
        for label in path:
            if label in self.submitted:
                if self.fake.ref_target(self.pr(label).head_ref) is None:
                    return {(1, "remote_branch_missing")}
                if self.pr(label).state == "closed":
                    return {(1, "pr_not_open")}
                if self.fake.ref_target(self.pr(label).head_ref) not in {
                    self.submitted[label].submitted_baseline.commit_id,
                    self.jj.resolve_commit(self.ids[label]).commit_id,
                }:
                    return {(1, "remote_branch_moved")}
            elif self.open_pr(label) is not None:
                return {(1, "saved_pr_missing")}
        if self.rebased.keys() & set(path):
            return {(1, "remote_branch_moved")}
        selected = {self.pr(label).number for label in path if label in self.submitted}
        for members in self.fake.github_stacks.values():
            active = {number for number in members if self.fake.prs[number].merged_at is None}
            if selected & active and active - selected and selected - set(members):
                return {(1, None)}
        return set()

    def accept_submit(
        self,
        path: tuple[str, ...],
        *,
        fresh: set[str] | None = None,
        prior_numbers: set[int] | None = None,
        publish: bool = True,
    ) -> None:
        state = self.store.load()
        changes = selected_stack(self.repo, self.ids[path[-1]]).changes
        assert tuple(change.change_id for change in changes) == tuple(
            self.ids[label] for label in path
        )
        refs = remote_refs(self.fake.git_dir)
        base = "main"
        for label, change in zip(path, changes, strict=True):
            if not publish and label not in self.submitted:
                assert self.ids[label] not in state.prs
                continue
            record = state.prs[self.ids[label]]
            if label in self.submitted:
                assert record.pr_identity == self.submitted[label].pr_identity
            elif fresh is not None:
                assert label in fresh and prior_numbers is not None
                assert record.pr_identity.pr_number not in prior_numbers
            self.submitted[label] = record
            pr = self.pr(label)
            assert pr.state == "open" and pr.merged_at is None
            assert pr.head_ref == record.pr_identity.head_ref and pr.base_ref == base
            assert pr.title == change.subject
            assert refs[f"refs/heads/{pr.head_ref}"] == change.commit_id
            assert record.submitted_baseline.commit_id == change.commit_id
            base = pr.head_ref
        assert self.grouped(self.published(path))
        self.dirty.difference_update(self.published(path))

    def apply_edit(self, index: int, operation: StackEditOperation) -> None:
        path = self.paths[index]
        effect = apply_stack_edit(path, operation)
        label = operation.label
        cid = self.ids[label]
        if operation.kind in {"move_to_top", "move_after", "move_before"}:
            target = path[-1] if operation.kind == "move_to_top" else operation.target_label
            assert target is not None
            flag = "-B" if operation.kind == "move_before" else "-A"
            run_command(["jj", "rebase", "-r", cid, flag, self.ids[target]], self.repo)
        elif operation.kind in {"insert_after", "insert_before"}:
            new = operation.new_label
            assert new is not None
            args = (
                ["jj", "new", "-B", cid]
                if operation.kind == "insert_before"
                else ["jj", "new", cid]
            )
            run_command(args, self.repo)
            commit_file(self.repo, subject(new), filename(new))
            self.ids[new] = selected_stack(self.repo).head.change_id
            self.contents[new] = {filename(new): subject(new) + "\n"}
            if operation.kind == "insert_after" and path.index(label) + 1 < len(path):
                child = path[path.index(label) + 1]
                run_command(
                    ["jj", "rebase", "-s", self.ids[child], "-d", self.ids[new]], self.repo
                )
            self.dirty.add(new)
        elif operation.kind == "abandon":
            run_command(["jj", "abandon", cid], self.repo)
        elif operation.kind == "rewrite":
            run_command(["jj", "new", cid], self.repo)
            file = self.repo / filename(label)
            write_file(file, file.read_text() + "rewritten\n")
            self.contents[label][filename(label)] += "rewritten\n"
            run_command(["jj", "squash", "--into", cid, "--use-destination-message"], self.repo)
        else:
            previous = path[path.index(label) - 1]
            self.contents[previous].update(self.contents[label])
            run_command(
                [
                    "jj",
                    "squash",
                    "--from",
                    cid,
                    "--into",
                    self.ids[previous],
                    "--use-destination-message",
                ],
                self.repo,
            )
        self.paths[index] = effect.live_labels
        self.dirty.update(effect.rewritten_labels)
        if effect.removed_label is not None:
            self.dirty.discard(effect.removed_label)

    def join_paths(self, source: int, target: int) -> None:
        lower, upper = self.paths[target], self.paths[source]
        run_command(
            ["jj", "rebase", "-s", self.ids[upper[0]], "-d", self.ids[lower[-1]]], self.repo
        )
        self.paths[target] = (*lower, *upper)
        self.paths.pop(source)
        self.dirty.update(upper)

    def move_between(
        self, source: int, target: int, position: int, anchor: int, before: bool
    ) -> None:
        source_path, target_path = self.paths[source], list(self.paths[target])
        label, target_label = source_path[position], target_path[anchor]
        run_command(
            [
                "jj",
                "rebase",
                "-r",
                self.ids[label],
                "-B" if before else "-A",
                self.ids[target_label],
            ],
            self.repo,
        )
        self.paths[source] = tuple(item for item in source_path if item != label)
        target_path.insert(anchor if before else anchor + 1, label)
        self.paths[target] = tuple(target_path)
        self.dirty.update((*self.paths[source], *target_path))
        if not self.paths[source]:
            self.paths.pop(source)

    def drift(self, kind: Drift, label: str | None = None) -> None:
        if kind == "trunk_advanced":
            contents = f"after {self.trunk.get('external.txt', 'initial')}\n"
            self.fake.advance_branch("main", path="external.txt", contents=contents)
            self.trunk["external.txt"] = blob(contents)
            return
        assert label is not None
        pr = self.pr(label)
        if kind == "closed_pr":
            self.fake.update_pr_state(pr, state="closed")
        elif kind == "reopened_pr":
            self.fake.update_pr_state(pr, state="open")
        elif kind == "remote_branch_deleted":
            self.fake._run_backing_git("update-ref", "-d", f"refs/heads/{pr.head_ref}")
            self.fake.update_pr_state(pr, state="closed")
        elif kind == "foreign_branch_fetched":
            update_remote_ref(
                self.fake,
                branch=f"agent/{label}",
                target=self.submitted[label].submitted_baseline.commit_id,
            )
            run_command(["jj", "git", "fetch", "--remote", "origin"], self.repo)
            self.foreign.add(label)
        elif kind == "pr_base_retargeted":
            stack = self.fake.stack_number_for_pr(pr.number)
            if stack is not None:
                del self.fake.github_stacks[stack]
            self.fake.update_pr_base(pr, base_ref="main")
        else:
            pr.is_draft = not pr.is_draft

    def server_merge(self, index: int, count: int, method: MergeMethod) -> None:
        path = self.paths[index]
        pr = self.pr(path[count - 1])
        head = self.fake.ref_target(pr.head_ref)
        assert head is not None
        before = self.store.load()
        operation = FakeStackMergeOperation(
            expected_head_sha=head,
            merge_action="direct_merge",
            merge_method=method,
            pr_number=pr.number,
            uuid="external",
        )
        _complete_stack_merge(self.fake, operation)
        assert operation.status == "merged"
        assert self.store.load() == before
        self.land(path[:count])

    def land(self, labels: tuple[str, ...]) -> None:
        for label in labels:
            self.trunk.update({name: blob(text) for name, text in self.contents[label].items()})

    def rebase_on_server(self, index: int) -> None:
        path = self.paths[index]
        stack = self.fake.stack_number_for_pr(self.pr(path[0]).number)
        assert stack is not None
        self.fake.rebase_stack_onto_base(stack, base_ref="main")
        base = self.fake.ref_target("main")
        assert base is not None
        self.rebased[path[0]] = base

    def sync_blocked(self, path: tuple[str, ...]) -> bool:
        merged = self.merged(path)
        remaining = path[len(merged) :]
        published = self.published(remaining)
        selected = {self.pr(label).number for label in self.published(path)}
        return (
            path[: len(merged)] != merged
            or remaining[: len(published)] != published
            or any(
                set(members) & selected
                and any(
                    number not in selected and self.fake.prs[number].merged_at is None
                    for number in members
                )
                for members in self.fake.github_stacks.values()
            )
            or any(
                self.jj.resolve_commit(self.ids[label]).commit_id
                != self.submitted[label].submitted_baseline.commit_id
                for label in merged
            )
            or bool(
                self.rebased.keys() & set(path)
                and self.rebased[path[0]] != self.fake.ref_target("main")
            )
        )

    def sync_path(self, index: int) -> None:
        path = self.paths[index]
        if self.sync_blocked(path):
            run_command(["jj", "git", "fetch", "--remote", "origin"], self.repo)
            before = self.snapshot()
            code, output = self.cli("sync", self.ids[path[-1]])
            assert code == 1, (self.last_error, output)
            assert self.snapshot() == before
            return
        self.ok("sync", self.ids[path[-1]])
        if self.rebased.keys() & set(path):
            self.accept_submit(path, publish=False)
            del self.rebased[path[0]]
        else:
            self.accept_merge(index, len(self.merged(path)))

    def merge_path(self, index: int, count: int, method: MergeMethod) -> None:
        path = self.paths[index]
        boundary = self.submitted[path[count - 1]]
        self.ok(
            "merge", "--method", method, "--pull-request", str(boundary.pr_identity.pr_number)
        )
        assert self.fake.stack_merge_requests[-1] == (
            boundary.pr_identity.pr_number,
            method,
            "direct_merge",
            boundary.submitted_baseline.commit_id,
        )
        self.land(path[:count])
        self.accept_merge(index, count)

    def accept_merge(self, index: int, count: int) -> None:
        path = self.paths[index]
        refs = remote_refs(self.fake.git_dir)
        for label in path[:count]:
            pr = self.pr(label)
            assert pr.merged_at is not None
            assert f"refs/heads/{pr.head_ref}" not in refs
            copies = self.jj.query_commits_by_change_ids((self.ids[label],))[self.ids[label]]
            assert not copies or (
                len(copies) == 1
                and copies[0].immutable
                and not copies[0].divergent
                and copies[0].commit_id == pr.merge_commit_sha
            )
            del self.submitted[label]
            self.dirty.discard(label)
        remaining = path[count:]
        if remaining:
            self.paths[index] = remaining
            self.accept_submit(remaining, publish=False)
        else:
            self.paths.pop(index)

    def cleanup_label(self, label: str) -> None:
        pr = self.pr(label)
        self.ok("cleanup", "--pull-request", str(pr.number), "--close")
        assert self.fake.ref_target(pr.head_ref) is None
        del self.submitted[label]
        self.dirty.add(label)

    def cleanup_before_sync(self, index: int) -> None:
        before = self.snapshot()
        output = self.ok("cleanup", self.ids[self.paths[index][-1]])
        assert "sync" in output
        assert self.snapshot() == before

    def interrupted_submit(self, index: int, point: str, position: int) -> None:
        path = self.paths[index]
        label = path[position]
        if point == "update_pr":
            run_command(
                [
                    "jj",
                    "describe",
                    "-r",
                    self.ids[label],
                    "-m",
                    self.jj.resolve_commit(self.ids[label]).description + "\nupdated body",
                ],
                self.repo,
            )
            self.dirty.update(path[position:])
        numbers = set(self.fake.prs)
        before = self.store.load()
        changes = {
            change.change_id: change
            for change in selected_stack(self.repo, self.ids[path[-1]]).changes
        }
        with pytest.MonkeyPatch.context() as patch:
            install_submit_fault(patch, self.fake, point, subject(label))
            code, output = self.cli("submit", self.ids[path[-1]])
            assert code != 0, output
        state = self.store.load()
        assert before.prs.keys() <= state.prs.keys()
        for item in path:
            cid = self.ids[item]
            if cid in state.prs:
                record = state.prs[cid]
                if item in self.submitted:
                    assert record.pr_identity == self.submitted[item].pr_identity
                else:
                    assert record.pr_identity.pr_number not in numbers
                pr = self.fake.prs[record.pr_identity.pr_number]
                assert pr.head_ref.endswith(f"-{cid[:8]}")
                assert record.pr_identity.head_ref == pr.head_ref
                assert record.submitted_baseline.commit_id in {
                    changes[cid].commit_id,
                    before.prs[cid].submitted_baseline.commit_id if cid in before.prs else None,
                }
                self.submitted[item] = record
        created = set(self.fake.prs) - numbers
        assert not created or point == "create_pr"
        assert (
            created
            == {pr.number for item in path if (pr := self.open_pr(item)) is not None} - numbers
        )
        self.pr_count += len(created)

    def relink_label(self, label: str) -> None:
        pr = self.open_pr(label)
        assert pr is not None
        head = self.fake.ref_target(pr.head_ref)
        assert head is not None
        before = self.snapshot()[1:]
        self.ok("relink", "--replace-remote", str(pr.number), self.ids[label])
        self.submitted[label] = TrackedPR(
            pr_identity=PRIdentity(pr_number=pr.number, head_ref=pr.head_ref),
            submitted_baseline=SubmittedBaseline(commit_id=head),
        )
        assert self.snapshot()[1:] == before

    def snapshot(self) -> tuple[object, ...]:
        return (
            self.store.load(),
            remote_refs(self.fake.git_dir),
            {number: pr_state(pr) for number, pr in self.fake.prs.items()},
            deepcopy(self.fake.pr_reviews),
            deepcopy(self.fake.issue_comments),
            deepcopy(self.fake.github_stacks),
            self.jj.visible_pr_bookmark_targets(),
            self.jj._run_jj(("log", "--no-graph", "-r", "all()", "-T", "commit_id")),
        )

    def trunk_files(self) -> dict[str, str]:
        return {
            name: entry.split()[2]
            for line in self.fake._run_backing_git("ls-tree", "-r", "main").splitlines()
            for entry, name in (line.split("\t"),)
        }

    def check_contents(self, label: str, commit: str) -> None:
        diff = self.jj._run_git(("diff-tree", "--no-commit-id", "--no-abbrev", "-r", commit))
        actual = {
            name: (entry.split()[4], entry.split()[3])
            for line in diff.splitlines()
            for entry, name in (line.split("\t"),)
        }
        expected = {name: ("A", blob(text)) for name, text in self.contents[label].items()}
        assert actual == expected, (label, actual, expected)

    @invariant()
    def model_matches(self) -> None:
        assert self.store.load().prs == {
            self.ids[label]: record for label, record in self.submitted.items()
        }
        assert len(self.fake.prs) == self.pr_count
        assert self.trunk_files() == self.trunk
        for path in self.paths:
            if self.foreign.intersection(path):
                continue
            changes = selected_stack(self.repo, self.ids[path[-1]]).changes
            assert tuple(change.change_id for change in changes) == tuple(
                self.ids[label] for label in path
            )
            for label, change in zip(path, changes, strict=True):
                self.check_contents(label, change.commit_id)

    @precondition(lambda self: len(self.paths) < 3)
    @rule(size=st.integers(1, 3))
    def create_stack(self, size: int) -> None:
        self.new_stack(size)

    @precondition(lambda self: bool(self.edits()))
    @rule(data=st.data())
    def edit(self, data: st.DataObject) -> None:
        options = self.edits()
        kind = data.draw(st.sampled_from(sorted({op.kind for _, op in options})), label="edit")
        index, operation = data.draw(
            st.sampled_from([(i, op) for i, op in options if op.kind == kind]), label="change"
        )
        self.apply_edit(index, operation)

    @precondition(lambda self: any(not self.merged(p) for p in self.paths))
    @rule(data=st.data())
    def submit(self, data: st.DataObject) -> None:
        indices = [i for i, p in enumerate(self.paths) if not self.merged(p)]
        self.submit_path(data.draw(st.sampled_from(indices), label="stack"))

    @precondition(lambda self: len(self.ready()) >= 2)
    @rule(data=st.data())
    def join(self, data: st.DataObject) -> None:
        source, target = data.draw(
            st.sampled_from([(a, b) for a in self.ready() for b in self.ready() if a != b]),
            label="stacks",
        )
        self.join_paths(source, target)

    @precondition(lambda self: len(self.ready()) >= 2)
    @rule(data=st.data(), before=st.booleans())
    def move(self, data: st.DataObject, before: bool) -> None:
        source, target = data.draw(
            st.sampled_from([(a, b) for a in self.ready() for b in self.ready() if a != b]),
            label="stacks",
        )
        position = data.draw(st.integers(0, len(self.paths[source]) - 1), label="change")
        anchor = data.draw(st.integers(0, len(self.paths[target]) - 1), label="destination")
        self.move_between(source, target, position, anchor, before)

    @precondition(lambda self: bool(self.ready()))
    @rule(data=st.data(), method=st.sampled_from(get_args(MergeMethod)), external=st.booleans())
    def merge(self, data: st.DataObject, method: MergeMethod, external: bool) -> None:
        index = data.draw(st.sampled_from(self.ready()), label="stack")
        count = data.draw(st.integers(1, len(self.published(self.paths[index]))), label="prefix")
        if external:
            self.server_merge(index, count, method)
        else:
            self.merge_path(index, count, method)

    @precondition(lambda self: bool(self.rebased) or any(self.merged(p) for p in self.paths))
    @rule(data=st.data())
    def sync(self, data: st.DataObject) -> None:
        indices = [
            i for i, p in enumerate(self.paths) if self.merged(p) or self.rebased.keys() & set(p)
        ]
        self.sync_path(data.draw(st.sampled_from(indices), label="stack"))

    def rebasable(self) -> list[int]:
        return [
            i
            for i, path in enumerate(self.paths)
            if i in self.ready() or path[0] in self.rebased
            if tuple(self.pr(label).number for label in self.published(path))
            in self.fake.github_stacks.values()
            if self.fake._run_backing_git("rev-parse", f"{self.pr(path[0]).head_ref}^")
            != self.fake.ref_target("main")
        ]

    @precondition(lambda self: bool(self.rebasable()))
    @rule(data=st.data())
    def server_rebase(self, data: st.DataObject) -> None:
        self.rebase_on_server(data.draw(st.sampled_from(self.rebasable()), label="stack"))

    def cleanup_candidates(self) -> tuple[str, ...]:
        live = {label for path in self.paths for label in path}
        return tuple(
            label
            for label in self.submitted
            if self.pr(label).merged_at is None
            and label not in self.rebased
            and (self.pr(label).state == "closed" or label not in live)
            and self.fake.stack_number_for_pr(self.pr(label).number) is None
            and not any(
                other.base_ref == self.pr(label).head_ref
                and other.merged_at is None
                and self.fake.ref_target(other.head_ref) is not None
                for other in self.fake.prs.values()
            )
        )

    @precondition(lambda self: bool(self.cleanup_candidates()))
    @rule(data=st.data())
    def cleanup(self, data: st.DataObject) -> None:
        self.cleanup_label(data.draw(st.sampled_from(self.cleanup_candidates()), label="change"))

    @rule(kind=st.sampled_from(get_args(Drift)), data=st.data())
    def server_change(self, kind: Drift, data: st.DataObject) -> None:
        refs = self.fake.branch_heads() if kind == "reopened_pr" else {}
        labels = [
            label
            for p in self.paths
            if not self.merged(p) and not self.rebased.keys() & set(p)
            for label in p
            if label in self.submitted
            and self.pr(label).state == ("closed" if kind == "reopened_pr" else "open")
            and (
                kind != "reopened_pr"
                or (self.pr(label).head_ref in refs and self.pr(label).base_ref in refs)
            )
        ]
        if kind == "trunk_advanced":
            self.drift(kind)
        elif labels:
            self.drift(kind, data.draw(st.sampled_from(labels), label="change"))

    @precondition(lambda self: bool(self.submitted))
    @rule(data=st.data())
    def server_approve(self, data: st.DataObject) -> None:
        labels = tuple(label for label in self.submitted if self.pr(label).state == "open")
        if labels:
            self.approve((data.draw(st.sampled_from(labels), label="change"),))

    @precondition(lambda self: bool(self.paths))
    @rule(data=st.data(), point=st.sampled_from(("after_remote_push", "create_pr", "update_pr")))
    def interrupt_submit(self, data: st.DataObject, point: str) -> None:
        indices = [
            i
            for i in self.editable()
            if not self.submit_failures(self.paths[i])
            and (
                all(label not in self.submitted for label in self.paths[i])
                if point == "create_pr"
                else all(label in self.submitted for label in self.paths[i])
                if point == "update_pr"
                else any(
                    self.jj.resolve_commit(self.ids[label]).commit_id
                    not in self.fake.branch_heads().values()
                    for label in self.paths[i]
                )
            )
        ]
        if indices:
            index = data.draw(st.sampled_from(indices), label="stack")
            position = data.draw(st.integers(0, len(self.paths[index]) - 1), label="change")
            self.interrupted_submit(index, point, position)

    @rule(data=st.data())
    def relink(self, data: st.DataObject) -> None:
        labels = [
            label
            for i in self.editable()
            for label in self.paths[i]
            if (pr := self.open_pr(label)) is not None
            and self.fake.ref_target(pr.head_ref) is not None
            and (
                label not in self.submitted
                or self.submitted[label].submitted_baseline.commit_id
                != self.fake.ref_target(pr.head_ref)
            )
        ]
        if labels:
            self.relink_label(data.draw(st.sampled_from(labels), label="change"))
