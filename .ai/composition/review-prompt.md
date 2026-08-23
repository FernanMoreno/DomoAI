Perform a complete system composition review of the current significant change.

Use `system-composition-review`.

Do not review only the files directly modified. Determine the actual impact
surface with Graphify and source inspection and review relevant upstream and
downstream subsystems.

Run the architecture gate, contract tests, integration/composition tests and
normal regression tests that apply.

Use Testcontainers/real disposable dependencies where mocks could hide
integration behavior. Use Pact when independently evolving consumers/providers
have an API/message contract.

Exercise partial failure, retry, duplicate/idempotency, timeout/cancellation,
stale cache/state and concurrency scenarios when relevant.

If a failure appears, use `systematic-debugging` and find the first incorrect
boundary before proposing a fix.

Use `verification-before-completion`.

Finish with the Composition Review Report required by the skill:
- changed subsystems
- neighbors reviewed
- contracts/invariants checked
- architecture checks
- composition/integration scenarios
- contract tests
- real dependencies used
- failures/root causes
- residual risks
- PASS / PASS WITH RISKS / FAIL
