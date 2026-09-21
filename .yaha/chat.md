<!--comment-->
- SPDX-License-Identifier: Apache-2.0
- SPDX-FileCopyrightText: 2026 TAN Kun
<!--endcomment-->

## ChatAgent
- **Inherited:** BaseReflector
- **Code:** Cha
- **Description:** A persistent chat agent that executes a task, then uses the Query_user stage and exit_to to prompt for more tasks in a continuous loop.
- **System Prompt:**
    - **Inherited:** All
    > You are a persistent chat and execution assistant. You need to work in two stages alternatively.
    > In the Premable-and-task stage, you focus on executing the given task perfectly using your available tools.
    > In Query_usr stage, you ask for further tasks and jump back to the earlier stage to execute the new task.

- **Preamble:**
    - **Inherited:** All
    - **Snap:** enabled
    - **Fail Target:** Query_user

- **Query_user:**
    - **Inherited:** None
    - **Allowed Tools:**
        - ask_user
        - exit_to
        - success_tool
    - **Required Tools:**
        - ask_user
    - **Exit Tools:**
        - exit_to
        - success_tool
    <!--ref name="query_protocol" -->

### Toolset (Available Tools)
	- **Inherited:** All
