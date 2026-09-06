# jj-stack: manage stacked GitHub PRs with jj

`jj-stack` turns a linear series of local `jj` changes into a stack of GitHub pull requests.
Rewrite, split, squash, or reorder the changes with `jj`, then run `jj-stack submit` to update
GitHub. Existing PRs follow their change IDs, keeping comments and review history together.

## Quick start

You need Python 3.14 or newer, `jj` 0.45.1 or newer, and a repo on github.com where you can push
branches and open pull requests. `jj-stack` uses `GITHUB_TOKEN`, then `GH_TOKEN`, then your GitHub
CLI login for authentication.

Install with `uv`:

```bash
uv tool install jj-stack
```

Inside your `jj` repo, check the setup and configure fetches to skip the PR branches that
`jj-stack` manages:

```bash
jj-stack doctor --fix
```

Start with a linear series of described, non-empty changes above `trunk()`. Inspect the stack,
then create one PR per change:

```bash
jj-stack
jj-stack submit
```

By default, these commands select `@` when it has a description and changes, or `@-` otherwise.
Use `jj-stack list` to see all tracked stacks in the repo.

The [quick start](https://www.serpentine.com/software/jj-stack/quick-start/) walks through a
complete example, alternative installation methods, and setup for the `jj stack` alias and shell
completion. To upgrade a `uv` installation, run `uv tool upgrade jj-stack`.

### Use `jj stack` with tab completion

Add this alias with `jj config edit --user`:

```toml
[aliases]
stack = ["util", "exec", "--", "jj-stack"]
```

Also set up completion for the alias. For zsh, add this to `~/.zshrc` after your shell and `jj`
completion setup:

```zsh
eval "$(jj-stack completion zsh --jj-alias stack)"
```

`--jj-alias stack` updates `jj`'s completion so `jj stack s<TAB>` offers `submit`, `sync`, and
other matching commands. It also enables completion for `jj-stack`. See
[shell completion](https://www.serpentine.com/software/jj-stack/reference/configuration/#shell-completion)
for bash and fish instructions.

## How it works

Your local `jj` history determines which changes form a stack and their order. On GitHub, each
change gets a stable PR branch and a PR. The bottom PR targets trunk; each PR above it targets
the PR branch below:

```text
Local changes:  trunk() <- A     <- B     <- C
GitHub PRs:     main    <- PR #1 <- PR #2 <- PR #3
```

Each PR shows only its own change's diff. `jj-stack` manages the PR branches, so you can keep
using ordinary `jj` commands to arrange your work.

## Everyday workflow

1. Write code as a series of local `jj` changes.
2. Run `jj-stack submit` to open the PRs.
3. Revise your changes as reviews come in, then run `jj-stack submit` again.
4. Run `jj-stack merge` when the PRs at the bottom are ready. If GitHub completes the merge
   immediately, the command also updates your local stack.
5. After a queued merge finishes, or if you merge on GitHub, run `jj-stack sync`.

Pass a stack's top change ID to `view`, `submit`, `merge`, or `sync` to work on that stack without
switching your working copy. Add `--dry-run` to `submit`, `merge`, or `sync` to preview what the
command would do.

## Learn more

- [How jj-stack works](https://www.serpentine.com/software/jj-stack/mental-model/)
- [Submit and update](https://www.serpentine.com/software/jj-stack/guides/submit-and-update/)
- [Merge and sync](https://www.serpentine.com/software/jj-stack/guides/merge-and-sync/)
- [Multiple stacks](https://www.serpentine.com/software/jj-stack/guides/multiple-stacks/)
- [Configuration](https://www.serpentine.com/software/jj-stack/reference/configuration/)
- [Writing PR descriptions](https://www.serpentine.com/software/jj-stack/reference/descriptions/)
- [Troubleshooting](https://www.serpentine.com/software/jj-stack/troubleshooting/)
- [Compare tools](https://www.serpentine.com/software/jj-stack/tool-comparison/)
- [Automation](https://www.serpentine.com/software/jj-stack/reference/automation/)

For all flags and aliases, use the built-in help:

```bash
jj-stack --help
jj-stack <command> --help
jj-stack help --all
```

## Coding agent integration

Install the bundled skill to give coding agents instructions for working with `jj-stack`:

```bash
gh skill install bos/jj-stack jj-stack
```

The [skill source](skills/jj-stack/SKILL.md) and [evaluation notes](evals/jj-stack-skill.md) are
included in this repo.

## Development

With `uv`, `jj`, and `just` installed, run `just` to list the development workflows. See
[CONTRIBUTING.md](CONTRIBUTING.md) for setup and validation instructions.
