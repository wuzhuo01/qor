"""Print the public organization example.

The agents are scripts. This is not an organization that runs itself.
"""

from examples.organization_runtime.scenario import render, run_full, run_gap


def main() -> None:
    print(
        "\n".join(
            [
                "AI agents can finish a task.",
                "Who decides?",
                "Who reviews?",
                "What is the basis?",
                "What happens when a capability is missing?",
                "",
                "This example uses:",
                "- StubPlanner",
                "- scripted agents",
                "- no LLM",
                "",
                "The runtime records the producer, the reviewer, the evidence,",
                "the decision, and the reason. It does not organize itself.",
                "",
                "FULL",
                "Goal",
                " ↓",
                "Plan",
                " ↓",
                "Assignment",
                " ↓",
                "Artifact",
                " ↓",
                "Review",
                " ↓",
                "Decision",
                " ↓",
                "Outcome",
                "",
                "GAP",
                "Capability Gap",
                " ↓",
                "BLOCKED",
                "",
            ]
        )
    )
    print(render(run_full()))
    print()
    print(render(run_gap()))


if __name__ == "__main__":
    main()
