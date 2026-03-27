from pathlib import Path
from types import SimpleNamespace

import pytest

from jj_review.commands import review_state as review_state_module
from jj_review.errors import CliError
from jj_review.jj import UnsupportedStackError
from jj_review.models.bookmarks import BookmarkState

from .entrypoint_test_helpers import patch_bootstrap


def test_status_passes_fetch_to_prepare_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_bootstrap(monkeypatch, review_state_module, tmp_path)
    prepare_calls: list[bool] = []

    def fake_prepare_status(**kwargs):
        prepare_calls.append(bool(kwargs["fetch_remote_state"]))
        return SimpleNamespace(
            prepared=SimpleNamespace(
                client=SimpleNamespace(list_bookmark_states=lambda: {}),
                remote=None,
                remote_error=None,
                stack=SimpleNamespace(
                    trunk=SimpleNamespace(
                        change_id="trunkchangeid",
                        commit_id="trunk-commit",
                        subject="base",
                    )
                ),
                status_revisions=(),
            ),
            selected_revset="@",
            trunk_subject="base",
            outstanding_intents=(),
        )

    monkeypatch.setattr(review_state_module, "prepare_status", fake_prepare_status)
    monkeypatch.setattr(
        review_state_module,
        "stream_status",
        lambda **kwargs: SimpleNamespace(incomplete=False),
    )

    exit_code = review_state_module.status(
        config_path=None,
        debug=False,
        fetch=True,
        repository=tmp_path,
        revset=None,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No reviewable commits" in captured.out
    assert prepare_calls == [True]


def test_status_reports_targeted_divergent_stack_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_bootstrap(monkeypatch, review_state_module, tmp_path)

    def raise_unsupported_stack(**kwargs):
        raise UnsupportedStackError(
            "Unsupported stack shape at nznokxmvrnysowwwkktpmroswxqsozqq: "
            "divergent changes are not supported.",
            change_id="nznokxmvrnysowwwkktpmroswxqsozqq",
            reason="divergent_change",
        )

    monkeypatch.setattr(review_state_module, "prepare_status", raise_unsupported_stack)

    with pytest.raises(CliError, match="Could not inspect review status"):
        review_state_module.status(
            config_path=None,
            debug=False,
            fetch=False,
            repository=tmp_path,
            revset=None,
        )


def test_describe_status_preparation_error_falls_back_without_structured_context() -> None:
    error = UnsupportedStackError(
        "Unsupported stack shape at nznokxmvrnysowwwkktpmroswxqsozqq: "
        "divergent changes are not supported."
    )

    assert "jj log -r" not in review_state_module.describe_status_preparation_error(error)


def test_status_reports_uninspected_github_target_for_empty_stack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_bootstrap(monkeypatch, review_state_module, tmp_path)
    monkeypatch.setattr(
        review_state_module,
        "prepare_status",
        lambda **kwargs: SimpleNamespace(
            prepared=SimpleNamespace(
                client=SimpleNamespace(list_bookmark_states=lambda: {}),
                remote=SimpleNamespace(name="origin"),
                remote_error=None,
                stack=SimpleNamespace(
                    trunk=SimpleNamespace(
                        change_id="trunkchangeid",
                        commit_id="trunk-commit",
                        subject="base",
                    )
                ),
                status_revisions=(),
            ),
            github_repository=SimpleNamespace(full_name="octo-org/stacked-review"),
            github_repository_error=None,
            selected_revset="main",
            trunk_subject="base",
            outstanding_intents=(),
        ),
    )

    def fake_stream_status(**kwargs):
        kwargs["on_github_status"](
            "octo-org/stacked-review",
            "not inspected; no reviewable commits",
        )
        return SimpleNamespace(incomplete=False)

    monkeypatch.setattr(review_state_module, "stream_status", fake_stream_status)

    exit_code = review_state_module.status(
        config_path=None,
        debug=False,
        fetch=False,
        repository=tmp_path,
        revset="main",
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert (
        "GitHub target: octo-org/stacked-review "
        "(not inspected; no reviewable commits)"
    ) in captured.out
    assert "No reviewable commits" in captured.out


def test_status_prints_local_header_before_streaming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_bootstrap(
        monkeypatch,
        review_state_module,
        tmp_path,
        repo_config=SimpleNamespace(trunk_branch=None),
    )
    monkeypatch.setattr(
        review_state_module,
        "prepare_status",
        lambda **kwargs: SimpleNamespace(
            prepared=SimpleNamespace(
                client=SimpleNamespace(
                    list_bookmark_states=lambda: {
                        "main": BookmarkState(
                            name="main",
                            local_targets=("trunk-commit",),
                        )
                    }
                ),
                remote=SimpleNamespace(name="origin"),
                remote_error=None,
                stack=SimpleNamespace(
                    trunk=SimpleNamespace(
                        change_id="trunkchangeid",
                        commit_id="trunk-commit",
                        subject="base",
                    )
                ),
                status_revisions=(object(),),
            ),
            outstanding_intents=(),
            selected_revset="@",
            stale_intents=(),
            trunk_subject="base",
        ),
    )

    def fake_stream_status(**kwargs):
        streamed = capsys.readouterr()
        assert "Selected revset: @" in streamed.out
        assert "Selected remote: origin" in streamed.out
        assert "◆ base [trunkcha" not in streamed.out
        kwargs["on_github_status"]("octo-org/stacked-review", None)
        kwargs["on_revision"](
            SimpleNamespace(
                cached_change=None,
                change_id="abcdefghijkl",
                pull_request_lookup=SimpleNamespace(
                    pull_request=SimpleNamespace(number=1),
                    state="open",
                ),
                stack_comment_lookup=None,
                subject="feature 1",
            ),
            True,
        )
        return SimpleNamespace(incomplete=False)

    monkeypatch.setattr(review_state_module, "stream_status", fake_stream_status)

    exit_code = review_state_module.status(
        config_path=None,
        debug=False,
        fetch=False,
        repository=tmp_path,
        revset=None,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "GitHub: octo-org/stacked-review" in captured.out
    assert "Stack:" in captured.out
    assert "- feature 1 [abcdefgh]: PR #1" in captured.out
    assert "◆ base [trunkcha]: main" in captured.out
    assert captured.out.index("- feature 1 [abcdefgh]: PR #1") < captured.out.index(
        "◆ base [trunkcha]: main"
    )


def test_status_prints_cleanup_advisories_for_merged_review_units(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_bootstrap(monkeypatch, review_state_module, tmp_path)
    monkeypatch.setattr(
        review_state_module,
        "prepare_status",
        lambda **kwargs: SimpleNamespace(
            prepared=SimpleNamespace(
                client=SimpleNamespace(list_bookmark_states=lambda: {}),
                remote=SimpleNamespace(name="origin"),
                remote_error=None,
                stack=SimpleNamespace(
                    trunk=SimpleNamespace(
                        change_id="trunkchangeid",
                        commit_id="trunk-commit",
                        subject="base",
                    )
                ),
                status_revisions=(object(),),
            ),
            outstanding_intents=(),
            selected_revset="@",
            stale_intents=(),
            trunk_subject="base",
        ),
    )

    merged_revision = SimpleNamespace(
        cached_change=None,
        change_id="abcdefghijkl",
        local_divergent=False,
        pull_request_lookup=SimpleNamespace(
            pull_request=SimpleNamespace(
                base=SimpleNamespace(ref="review/feature-base"),
                number=5,
                state="merged",
            ),
            state="closed",
        ),
        stack_comment_lookup=None,
        subject="feature 1",
    )

    def fake_stream_status(**kwargs):
        kwargs["on_github_status"]("octo-org/stacked-review", None)
        kwargs["on_revision"](merged_revision, True)
        return SimpleNamespace(
            incomplete=False,
            revisions=(merged_revision,),
            selected_revset="@",
        )

    monkeypatch.setattr(review_state_module, "stream_status", fake_stream_status)

    exit_code = review_state_module.status(
        config_path=None,
        debug=False,
        fetch=False,
        repository=tmp_path,
        revset=None,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "- feature 1 [abcdefgh]: PR #5 merged, cleanup needed" in captured.out
    assert "◆ base [trunkcha]: trunk()\n\nAdvisories:" in captured.out
    assert "jj-review cleanup --restack @" in captured.out
