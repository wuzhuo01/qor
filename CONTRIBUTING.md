# Contributing

QOR V1.0 core is frozen. The frozen part is the proven organizational runtime: task lifecycle, planning and assignment, review, synthesis, decision, governed action and verification, outcomes, scheduler and event routing as far as opening a task, and human responsibility.

Contributions should preserve that boundary.

- Keep QOR a generic organization runtime. Do not add a domain product identity to the generic profile or the default entry.
- Do not add a capability because the generic profile cannot complete a business task. Domain capabilities are supplied by the caller. A new capability needs evidence from a real task or a real failure, not from a wish to look more complete.
- Prefer a small extension that leaves the frozen model in place. A change that rewrites task lifecycle, assignment, review, decision, or the action path is a core change and needs a separate review before code.
- Tests are part of the architecture contract. A change to a proven path should include a contract test that exercises the production services.
- Do not weaken, skip, or delete a contract test to obtain a green result.
- Do not connect the scheduler or the event router to task execution as part of an unrelated change.
- Do not present lesson retrieval as behavioral learning, or a human-recorded outcome as something the system writes by itself.

The license is Apache-2.0. See `LICENSE`. Do not add a second license.
