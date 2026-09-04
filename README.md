# jj-stack: manage stacked GitHub PRs with jj

`jj-stack` turns a linear series of local `jj` changes into a stack of GitHub pull requests.
Rewrite, split, squash, or reorder the changes with `jj`, then let `jj-stack` update the
matching PRs.

## Quick start

### Requirements

- Python 3.14 or newer
- `jj` 0.45.1 or newer
- GitHub authentication

### Install

Install `jj-stack` from PyPI with `uv` in an isolated tool environment (recommended):

```bash
uv tool install jj-stack
```

`pipx` provides another isolated installation:

```bash
pipx install jj-stack
```

You can also use `pip` inside an activated virtual environment:

```bash
python -m pip install jj-stack
```

To upgrade an installation made with `uv`, rerun its command with `--force`. If the command is
not on your shell `PATH`, run `uv tool update-shell`.

### Invoke it as `jj stack`

Add a command alias to your user configuration with `jj config edit --user`:

```toml
[aliases]
stack = ["util", "exec", "--", "jj-stack"]
```

For tab completion of both `jj-stack` and `jj stack`, add the output of `jj-stack completion` to
your shell startup file:

```bash
eval "$(jj-stack completion zsh --jj-alias stack)"
```

`bash` and `fish` work the same way. See
[Configuration](https://www.serpentine.com/software/jj-stack/reference/configuration/) for more
setup options.

### Submit your first stack

Start with a linear series of local `jj` changes on top of `trunk()`. In a new repo, check
the setup and apply the safe local fixes:

```bash
jj-stack doctor --fix
```

Inspect the stack that ends at your working copy:

```bash
jj-stack
```

Create one GitHub PR per local change:

```bash
jj-stack submit
```

Revise the changes locally with `jj` and rerun `jj-stack submit` whenever the stack is ready to
refresh. Use `jj-stack list` to see every tracked stack in the repo.

## Mental model

Your local `jj` history determines which changes form a stack and their order. On GitHub, each
change gets a stable PR branch and a PR; every PR targets the PR branch below it, except the
bottom PR, which targets trunk by default:

```text
jj-stack/add-ui-...         -> PR #3 (base: jj-stack/add-api-...)
jj-stack/add-api-...        -> PR #2 (base: jj-stack/refactor-model-...)
jj-stack/refactor-model-... -> PR #1 (base: main)
main                        -> trunk
```

The PR branches normally stay out of your local bookmark view. When you rewrite a change,
`jj-stack` updates that change's existing PR branch and PR, along with the PR branches and PRs for
dependent changes.

## Everyday workflow

1. Write code as a series of local `jj` changes.
2. Run `jj-stack submit`.
3. Revise, add, remove, or reorder the changes locally as reviews come in.
4. Run `jj-stack submit` again to refresh GitHub.
5. Run `jj-stack merge` when the changes at the bottom are ready.
6. After a queued or externally initiated merge finishes, run
   `jj-stack sync <head-change-id>`.

`view`, `submit`, `merge`, and `sync` accept a change ID when you need to select a stack other
than the one ending at the working copy.

See the [user guide](https://www.serpentine.com/software/jj-stack/) for drafts, descriptions,
merge queues, cleanup, and working with multiple stacks.

## Learn more

- [Mental model](https://www.serpentine.com/software/jj-stack/mental-model/)
- [Quick start](https://www.serpentine.com/software/jj-stack/quick-start/)
- [Everyday workflows](https://www.serpentine.com/software/jj-stack/guides/submit-and-update/)
- [Configuration](https://www.serpentine.com/software/jj-stack/reference/configuration/)
- [Writing PR descriptions](https://www.serpentine.com/software/jj-stack/reference/descriptions/)
- [Troubleshooting](https://www.serpentine.com/software/jj-stack/troubleshooting/)
- [Tool comparison](https://www.serpentine.com/software/jj-stack/tool-comparison/)
- [JSON output](https://www.serpentine.com/software/jj-stack/reference/json-output/)
- [Automation and exit codes](https://www.serpentine.com/software/jj-stack/reference/automation/)

The built-in help is the canonical flag reference:

```bash
jj-stack --help
jj-stack <command> --help
jj-stack help --all
```

## Development

Contributor workflows live in the [`justfile`](justfile). With `uv`, `jj`, and `just` installed,
run `just` to list the setup, formatting, focused test, verification, documentation, and release
recipes.

## Coding agent integration

Install the bundled skill to teach coding agents to work with local `jj` stacks and refresh their
GitHub PRs safely:

```bash
gh skill install bos/jj-stack jj-stack
```

See the [skill source](https://github.com/bos/jj-stack/blob/main/skills/jj-stack/SKILL.md). In
my evaluations with Codex and Claude Code, agents with the `jj-stack` skill succeeded in 11/12
scenarios versus 6/12 without it, with one critical error versus four, using 60% fewer failed
command attempts and 18% fewer tool calls. Treat that as a small pilot rather than a published
benchmark:
[`evals/jj-stack-skill.md`](https://github.com/bos/jj-stack/blob/main/evals/jj-stack-skill.md)
gives the evaluation design, but this repo does not include the traces behind those numbers.
(The critical error was due to Claude Haiku understanding a rule and ignoring it. I haven't
figured out how to get smaller Claude models to behave better, and I don't personally use them.)

## Performance

Although `jj-stack` is written in Python, this does not significantly affect its speed.
The real determinants of its performance are the GitHub API and the `jj` command.

The GitHub API is *slow*; a single roundtrip takes many hundreds of milliseconds. `jj-stack`
reduces its impact with:

- GraphQL batch requests where possible
- concurrent use of the GitHub REST API
- periodic audits that its queries are minimal in extent

In pursuit of good performance, `jj-stack` also batches calls to `jj` and minimizes the amount
of work those calls must do.
