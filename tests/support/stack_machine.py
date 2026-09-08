"""Generated client commands and external GitHub events against real jj repositories."""

from __future__ import annotations

import io
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, get_args

import pytest
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule

import jj_stack.cli as cli_module
from jj_stack.errors import CliError, DriftError
from jj_stack.jj.client import JjClient, UnsupportedStackError
from jj_stack.models.tracking import TrackedPR
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


class StackMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.resources = ExitStack()
        self.root = Path(self.resources.enter_context(TemporaryDirectory(prefix="jj-property-")))
        self.patch = self.resources.enter_context(pytest.MonkeyPatch.context())
        self.paths: list[tuple[str, ...]] = []
        self.ids: dict[str, str] = {}
        self.submitted: dict[str, TrackedPR] = {}
        self.dirty: set[str] = set()
        self.foreign: set[str] = set()
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
        self.store = TrackingStore.for_repo(self.repo)
        self.patch.setattr(cli_module, "_print_cli_error", self.record_error)
        if submitted:
            changes = selected_stack(self.repo).changes
            path = tuple(f"c{index}" for index in range(1, size + 1))
            self.ids = dict(zip(path, (change.change_id for change in changes), strict=True))
            self.paths.append(path)
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

    def merged_prefix(self, path: tuple[str, ...]) -> int:
        count = 0
        for label in path:
            if label not in self.submitted or self.pr(label).merged_at is None:
                break
            count += 1
        return count

    def pr(self, label: str):
        return self.fake.prs[self.submitted[label].pr_identity.pr_number]

    def editable(self) -> list[int]:
        return [
            i
            for i, path in enumerate(self.paths)
            if not self.merged_prefix(path) and not self.foreign.intersection(path)
        ]

    def ready(self) -> list[int]:
        live = {label for path in self.paths for label in path}
        if set(self.submitted) - live:
            return []
        return [
            i
            for i in self.editable()
            if not self.dirty.intersection(self.paths[i])
            and all(
                label in self.submitted
                and self.pr(label).state == "open"
                and not self.pr(label).is_draft
                for label in self.paths[i]
            )
        ]

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

    def submit_failures(self, path: tuple[str, ...]) -> set[tuple[int, str]]:
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
        return set()

    def accept_submit(
        self,
        path: tuple[str, ...],
        *,
        fresh: set[str] | None = None,
        prior_numbers: set[int] | None = None,
    ) -> None:
        state = self.store.load()
        changes = selected_stack(self.repo, self.ids[path[-1]]).changes
        assert tuple(change.change_id for change in changes) == tuple(
            self.ids[label] for label in path
        )
        refs = remote_refs(self.fake.git_dir)
        base = "main"
        numbers = []
        for label, change in zip(path, changes, strict=True):
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
            numbers.append(pr.number)
        if len(numbers) > 1:
            assert tuple(numbers) in (
                tuple(number for number in members if self.fake.prs[number].merged_at is None)
                for members in self.fake.github_stacks.values()
            )
        self.dirty.difference_update(path)

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
            run_command(["jj", "squash", "--into", cid, "--use-destination-message"], self.repo)
        else:
            previous = path[path.index(label) - 1]
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
        if self.paths[source]:
            self.submit_path(source)
        self.submit_path(target)
        if not self.paths[source]:
            self.paths.pop(source)

    def drift(self, kind: Drift, label: str | None = None) -> None:
        if kind == "trunk_advanced":
            head = self.fake.ref_target("main")
            assert head is not None
            tree = self.fake._run_backing_git("rev-parse", f"{head}^{{tree}}")
            commit = self.fake._run_backing_git(
                "-c",
                "user.name=External",
                "-c",
                "user.email=external@example.com",
                "commit-tree",
                tree,
                "-p",
                head,
                "-m",
                "advance trunk",
            )
            update_remote_ref(self.fake, branch="main", target=commit)
            return
        assert label is not None
        pr = self.pr(label)
        if kind == "closed_pr":
            self.fake.update_pr_state(pr, state="closed")
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

    def sync_path(self, index: int) -> None:
        path = self.paths[index]
        count = self.merged_prefix(path)
        assert count
        self.ok("sync", self.ids[path[-1]])
        self.accept_merge(index, count)

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
            self.accept_submit(remaining)
        else:
            self.paths.pop(index)

    def cleanup_label(self, label: str) -> None:
        pr = self.pr(label)
        self.ok("cleanup", "--pull-request", str(pr.number))
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
        expected_count = self.pr_count + len(set(path) - self.submitted.keys())
        label = path[position]
        if point == "update_pr":
            self.submit_path(index)
            run_command(
                [
                    "jj",
                    "describe",
                    "-r",
                    self.ids[label],
                    "-m",
                    f"{subject(label)}\n\nupdated body",
                ],
                self.repo,
            )
        with pytest.MonkeyPatch.context() as patch:
            install_submit_fault(patch, self.fake, point, subject(label))
            code, output = self.cli("submit", self.ids[path[-1]])
            assert code != 0, output
        if point == "create_pr":
            before = self.snapshot()
            code, _ = self.cli("submit", self.ids[path[-1]])
            assert code != 0
            assert self.snapshot() == before
            pr = next(pr for pr in self.fake.prs.values() if pr.title == subject(label))
            self.ok("relink", str(pr.number), self.ids[label])
        state = self.store.load()
        for item in path:
            if self.ids[item] in state.prs:
                record = state.prs[self.ids[item]]
                if item in self.submitted:
                    assert record.pr_identity == self.submitted[item].pr_identity
                self.submitted[item] = record
        self.pr_count = len(self.fake.prs)
        self.submit_path(index)
        assert self.pr_count == expected_count
        self.ok(
            "submit",
            "--label",
            "needs-review",
            "--reviewers",
            "alice",
            "--team-reviewers",
            "platform",
            self.ids[path[-1]],
        )
        for item in path:
            pr = self.pr(item)
            assert "needs-review" in pr.labels
            assert "alice" in pr.requested_reviewers
            assert "platform" in pr.requested_team_reviewers

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

    @invariant()
    def model_matches(self) -> None:
        assert self.store.load().prs == {
            self.ids[label]: record for label, record in self.submitted.items()
        }
        assert len(self.fake.prs) == self.pr_count
        for path in self.paths:
            if self.foreign.intersection(path):
                continue
            changes = selected_stack(self.repo, self.ids[path[-1]]).changes
            assert tuple(change.change_id for change in changes) == tuple(
                self.ids[label] for label in path
            )

    @precondition(lambda self: len(self.paths) < 3)
    @rule(size=st.integers(1, 3))
    def create_stack(self, size: int) -> None:
        self.new_stack(size)

    @precondition(lambda self: bool(self.editable()))
    @rule(kind=st.sampled_from(get_args(StackEditOperationKind)), data=st.data())
    def edit(self, kind: StackEditOperationKind, data: st.DataObject) -> None:
        eligible = [
            i
            for i in self.editable()
            if (len(self.paths[i]) > 1 or kind in {"rewrite", "insert_after", "insert_before"})
            and (len(self.paths[i]) < 8 or not kind.startswith("insert"))
        ]
        if not eligible:
            return
        index = data.draw(st.sampled_from(eligible), label="stack")
        path = self.paths[index]
        target = None
        if kind in {"move_after", "move_before"}:
            candidates = (
                move_after_candidates(path)
                if kind == "move_after"
                else move_before_candidates(path)
            )
            label, target = data.draw(st.sampled_from(candidates), label="move")
        else:
            labels = (
                path[1:]
                if kind == "squash_into_previous"
                else path[:-1]
                if kind == "move_to_top"
                else path
            )
            label = data.draw(st.sampled_from(labels), label="change")
        new = f"c{len(self.ids) + 1}" if kind.startswith("insert") else None
        self.apply_edit(index, StackEditOperation(kind, label, new, target))

    @precondition(lambda self: any(not self.merged_prefix(p) for p in self.paths))
    @rule(data=st.data())
    def submit(self, data: st.DataObject) -> None:
        indices = [i for i, p in enumerate(self.paths) if not self.merged_prefix(p)]
        self.submit_path(data.draw(st.sampled_from(indices), label="stack"))

    @precondition(lambda self: len(self.ready()) >= 2)
    @rule(data=st.data())
    def join(self, data: st.DataObject) -> None:
        source, target = data.draw(
            st.sampled_from([(a, b) for a in self.ready() for b in self.ready() if a != b]),
            label="stacks",
        )
        self.join_paths(source, target)
        self.submit_path(target - (source < target))

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
        count = data.draw(st.integers(1, len(self.paths[index])), label="prefix")
        if external:
            self.server_merge(index, count, method)
        else:
            self.merge_path(index, count, method)

    @precondition(lambda self: any(self.merged_prefix(p) for p in self.paths))
    @rule(data=st.data())
    def sync(self, data: st.DataObject) -> None:
        indices = [i for i, p in enumerate(self.paths) if self.merged_prefix(p)]
        self.sync_path(data.draw(st.sampled_from(indices), label="stack"))

    @precondition(lambda self: bool(self.submitted))
    @rule(kind=st.sampled_from(get_args(Drift)), data=st.data())
    def server_change(self, kind: Drift, data: st.DataObject) -> None:
        labels = [
            label
            for p in self.paths
            if not self.merged_prefix(p)
            for label in p
            if label in self.submitted and self.pr(label).state == "open"
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
                if point != "update_pr"
                else all(label in self.submitted for label in self.paths[i])
            )
        ]
        if indices:
            index = data.draw(st.sampled_from(indices), label="stack")
            position = data.draw(st.integers(0, len(self.paths[index]) - 1), label="change")
            self.interrupted_submit(index, point, position)
