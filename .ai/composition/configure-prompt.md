Configure the repository's system-composition quality layer.

Use the `system-composition-review` skill as the governing process and inspect
the actual repository before changing configuration.

Required work:

1. Inspect git status and do not overwrite unrelated changes.
2. Read AI_WORKFLOW.md and .ai/composition/README.md.
3. Use Graphify for architecture/dependency/call-path impact if available, then
   verify important conclusions against source code.
4. Identify real subsystems, modules/layers and their allowed dependency
   direction. Do not infer architecture from folder names alone.
5. Refine the ecosystem-specific architecture gate:
   - Python: .importlinter with justified forbidden/protected/layers/
     independence/acyclic_siblings contracts.
   - JS/TS: .dependency-cruiser.cjs with justified dependency rules.
   - .NET: actual ArchUnitNET tests in the existing test project.
   - Java: integrate ArchUnit, Testcontainers and Pact dependencies safely into
     Maven/Gradle (respect BOMs, version catalogs, dependencyManagement,
     convention plugins and multi-module builds), then add ArchUnit tests.
   - Other ecosystems: select a project-appropriate equivalent.
6. Identify boundaries where mocks can hide real behavior. Add composition/
   integration test scaffolding and Testcontainers-based tests where justified.
7. If independently evolving consumers/providers communicate through APIs or
   messages, configure Pact contract tests where justified. Do not add meaningless
   Pact tests to a monolithic in-process boundary.
8. Add or refine project test commands/scripts so architecture, contract and
   composition tests can be run non-interactively.
9. Run the relevant checks.
10. If anything fails, use `systematic-debugging`; determine root cause before
    changing code.
11. Use `verification-before-completion`.
12. Report what was configured, what remains untested and any residual risks.

Do not weaken architecture rules merely to make existing violations disappear.
If the current code violates a justified rule, report it and propose a migration
path.
