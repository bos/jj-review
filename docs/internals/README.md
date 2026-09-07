# Contributor reference

Start with [CONTRIBUTING.md](../../CONTRIBUTING.md) for setup and commands, and
[AGENTS.md](../../AGENTS.md) for the repo's working agreement. For using the tool, see the
[user guide](../README.md).

Each internal document answers a different question:

- [Design](design.md): What does the product promise, and what must it refuse? Read before
  changing behavior, starting with the summary, core concepts, and safety rules.
- [Implementation strategy](implementation-strategy.md): Where does the code live, and how do
  observation, policy, mutations, and storage fit together?
- [Testing philosophy](testing-philosophy.md): Which risks deserve tests, and at which layer?
  Read before changing tests or fixtures.
- [Generated integration testing](property-testing.md): How does the scenario harness work, and
  how can a failure be reproduced?
- [Code reviews](code-reviews.md): What should a reviewer look for in code, tests, and docs?
- [Releasing](releasing.md): How are release notes written, candidates checked, and versions
  published?

Keep rules in the relevant source above and link to them from other documents. These files
describe current behavior and rationale; commit history records how the design changed.
