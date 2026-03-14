## Crash handling

There's a variety of ways in which a crash or ctrl-C can cause potentially persistent
failures. We need to figure out how to test for those, what the failure modes are, and how to
recover from those that we can't prevent.

We should also be mindful of atomic write-tempfile-then-rename modifications to tool-controlled
files, so that partially written files can't exist or be read.
