from __future__ import annotations

from jj_review.cli import build_parser, main
from jj_review.completion import emit_shell_completion


def test_bash_completion_lists_visible_commands_but_not_hidden_alias() -> None:
    script = emit_shell_completion(build_parser(), "bash")

    assert (
        'printf "%s" "submit status relink unlink land close import cleanup '
        'completion help"' in script
    )
    assert 'adopt) printf "%s" "-h --help --repository --config --debug ' in script
    assert 'printf "%s" "submit status relink adopt unlink' not in script


def test_bash_completion_includes_value_handling() -> None:
    script = emit_shell_completion(build_parser(), "bash")

    assert '--repository)\n            COMPREPLY=( $(compgen -d -- "$cur") )' in script
    assert '--config)\n            COMPREPLY=( $(compgen -f -- "$cur") )' in script
    assert 'completion) printf "%s" "bash zsh fish" ;;' in script


def test_zsh_completion_wraps_bash_support() -> None:
    script = emit_shell_completion(build_parser(), "zsh")

    assert script.startswith("#compdef jj-review\n")
    assert "autoload -U +X bashcompinit || return 1" in script
    assert "bashcompinit || return 1" in script
    assert "complete -F _jj_review jj-review" in script


def test_fish_completion_emits_shell_lines() -> None:
    script = emit_shell_completion(build_parser(), "fish")

    assert "complete -c jj-review -f" in script
    assert "complete -c jj-review -n '__fish_use_subcommand' -a 'completion'" in script
    assert "complete -c jj-review -n '__fish_use_subcommand' -a 'help'" in script
    assert (
        "complete -c jj-review -n '__fish_seen_subcommand_from completion' -a "
        "'bash zsh fish'" in script
    )
    assert (
        "complete -c jj-review -n '__fish_seen_subcommand_from submit' -l repository "
        "-r -a '(__fish_complete_directories)'" in script
    )


def test_main_completion_prints_requested_script(capsys) -> None:
    exit_code = main(["completion", "bash"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "_jj_review_completion_visible_commands()" in captured.out
    assert captured.err == ""
