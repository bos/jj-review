# Generated integration testing

These constraints supplement the [testing philosophy](testing-philosophy.md):

- In `StackMachine`, keep server events and recovery as separate actions. A server merge must
  leave room for local edits before sync; an interrupted submit must leave room for edits or
  remote changes before retry. Combining these actions would hide the inconsistent states the
  harness exists to test.
- Shared-file edits use single-line replacements, and generated moves preserve the relative order
  of changes that edit the same file. These restrictions let the harness predict conflicts without
  implementing `jj`'s merge algorithm; broadening the edits requires revisiting that assumption.
- Each independent seeded search (shard) in the property tests has its own Hypothesis example
  database. Sharing a database would spend parallel search time replaying and shrinking the same
  saved failure.
