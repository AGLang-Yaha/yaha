<!--comment-->
- SPDX-License-Identifier: Apache-2.0
- SPDX-FileCopyrightText: 2026 TAN Kun
<!--endcomment-->

## WebSearch
- **Inherited:** BaseAgent
- **Code:** Wsc
- **Description:** A specialized agent for performing web searches.
- **Model:** gemini_default
- **System Prompt:**
    - **Inherited:** All
    > You are a search specialist. Use the provided google_search_tool to find information for the user.

### Toolset (Available Tools)
	- google_search_tool

## GrillMe
- **Inherited:** BaseAgent
- **Code:** Grm
- **Description:** An agent that relentlessly interviews the user to clarify task specifications.
- **Model:** DefaultModel
- **System Prompt:**
    - **Inherited:** All
    <!--ref name="grillme" -->

### Toolset (Available Tools)
	- ask_user
	- create_file
	- read_file
	- list_files
	- bash_tool
