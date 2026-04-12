# User Guide

These pages are the user-facing guide set for `jj-review`.

One recurring term in the docs is the `review/...` bookmark. That is the
user-visible local `jj` bookmark `jj-review` uses as the GitHub head branch for
one review change.

- [Mental Model](mental-model.md)
  Understand what stays in `jj` and what `jj-review` owns on GitHub.
- [Daily Workflow](daily-workflow.md)
  The normal author loop for submit, review, land, and cleanup.
- [Troubleshooting](troubleshooting.md)
  Common symptoms, likely causes, and the next command to run.

The repository [README](../README.md) is the canonical install and first-run
quickstart.

The command-line help remains the canonical reference for flags and exact
parser behavior:

```bash
jj-review --help
jj-review help --all
jj-review <command> --help
```
