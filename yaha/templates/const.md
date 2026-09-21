<!--comment-->
- SPDX-License-Identifier: Apache-2.0
- SPDX-FileCopyrightText: 2026 TAN Kun
<!--endcomment-->

<!-- define name="grillme", params=[] -->
    > GRILL-ME PROTOCOL:
    > - Goal: Interview the user relentlessly about every aspect of the task until a shared, concrete understanding is reached.
    > - Method: Walk down each branch of the design tree, resolving dependencies between decisions one by one.
    > - Rules:
    >   1. Ask ONE question at a time using the `ask_user` tool.
    >   2. For each question, provide your recommended answer and briefly explain the trade-offs.
    >   3. If a question can be answered by exploring the codebase or existing files, inspect them instead of asking the user.
    >   4. Do NOT start writing code or implementation details until all decision branches are resolved.
    > - Mission End: After all decisions have been made, store the detailed specifications in a file in `.yaha/memory/` folder.
<!-- enddefine -->

<!-- define name="query_protocol", params=[] -->
    > QUERY USER PROTOCOL:
    > - Role: Interaction Coordinator.
    > - Goal: Prompt the user for the next task and hand it back to the chain.
    > - Actions:
    >   1. Call the `ask_user` tool to present the results of your previous work and ask what they want to do next.
    >   2. Wait for the user's feedback.
    >   3. If the user's feedback indicates they want to stop, exit, quit, or end (e.g. "stop", "exit", "quit", "bye"), call `success_tool(summary="...")` with a polite goodbye message as the summary. This ends the chat.
    >   4. Otherwise, call `exit_to(name='Preamble', summary=<the user's new task, restated>)` to jump back to the first stage, which executes the new task. The task itself is already in the conversation via your `ask_user` exchange.
    > - CRITICAL CONSTRAINT (STRICT):
    >   - DO NOT answer, explain, or execute the user's new task/question directly in this stage!
    >   - You can ONLY call either `success_tool` or `exit_to` to end the stage.
<!-- enddefine -->

<!-- define name="snap", params=[] -->
    > Revisit the context and distill the "Critical Wisdom" and store them in files in `.yaha/memory/` folder. You transform noisy context into durable, high-density engineering knowledge in your memory.
    > Examples of knowledge:
    > - **The Golden Path**: The specific tool calls and logic that solved the problem.
    > - **Environmental Quirks**: Environmental-specific behaviors (e.g., "This tool requires Python 3.11+", "This tool is slow, try to avoid to use it" ).
    > - **Negative Lessons**: What failed and why, to prevent future sub-agents from repeating mistakes.
    > Update the `.yaha/memory/` folder using surgical edits. If you create or changes any file you MUST update the `description` field in the YAML frontmatter of any edited file to reflect its expanded scope.
    > EDITING RULES (CRITICAL)
    > - **YAML Frontmatter**: Ensure the `---` blocks are preserved. The `description` should be a single-line summary of EVERYTHING in that file and MUST be wrapped in double quotes to avoid parsing errors (e.g., description: "your summary").
    > - **Verification**: If a previously "curated" method in memory failed this time, mark it as `[DEPRECATED]` or `[CAUTION]`.
    > If you have changed anything in `.yaha/memory/`, make a git commit.
<!-- enddefine -->