# AGLang Programming Guide

A practical guide to writing programs in **AGLang** — the agent-oriented language
in which a program is a set of Markdown agent classes — and running them with
**YAHA** (Yet Another HArness), its open-source runtime.

This guide complements the reference documents:

- [`AOP-White-Paper.md`](AOP-White-Paper.md) — the design rationale.
- [`agentclass-authoring-guide.md`](agentclass-authoring-guide.md) — the full
  class/stage reference (events, hooks, endpoints, inheritance edge cases).
- [`hooks.md`](hooks.md), [`soul.md`](soul.md), [`crew-environment.md`](crew-environment.md).

---

## 1. The mental model

AGLang inverts the usual framework relationship. In LangChain-style frameworks
the agent is an extension of a host language; in AGLang **the agent is the
program**. You do not write Python that orchestrates an LLM — you write
structured natural language that *is* the program, and YAHA interprets it.

Concretely:

- **A program is a Markdown file.** Agent classes live in
  `<workspace>/.yaha/AgentClass.md` (and any files it includes).
- **A class defines an agent**: its identity, model, tools, workflow and memory
  policy. Classes are inheritable and stateful.
- **A workflow is a prompt chain.** A class declares ordered **stages**
  (`Preamble`, your own stages, `Aftermath`). Each stage has its own
  instructions, tool whitelist and entry/exit gates. All stages share one
  conversation history.
- **Execution is a ReAct loop.** Within a stage the model alternates between
  reasoning and tool calls until it ends the stage with a control tool
  (`success_tool`, `fail_tool`, `loop_tool`, `exit_to`, `restart`) or an event
  terminator (`set_event_success`, `set_event_loop`, `set_event_jump`).
- **Agents compose.** An agent can spawn blocking sub-agents
  (`yaha_spawn_agent`), non-blocking parallel peers (`spawn_parallel_agent`),
  and remote agents over HTTP endpoints (`connect_tool` / `send_tool`).

Because it is interpreted Markdown, an AGLang program is portable: it runs
wherever a YAHA interpreter is installed, against any configured LLM backend.

---

## 2. Installation and workspace setup

```bash
git clone https://github.com/AGLang-Yaha/yaha.git
cd yaha
pip install -e .
```

Create and initialize a workspace:

```bash
mkdir my-project && cd my-project
yaha --init            # optionally: yaha --init "Ada" to name your agent
```

`--init` creates the `.yaha/` configuration ecosystem:

```
.yaha/
├── AgentClass.md     # your agent classes (includes const/base/utils/chat/coder templates)
├── Model.md          # LLM provider configurations
├── SOUL.md           # agent identity (default + per-class personas)
├── tool-schema.md    # JSON catalogue of available tools
├── layout.md         # workspace-layout reference
├── ext/              # workspace-specific agent extensions (git-controlled)
├── memory/           # persistent agent memory (git-controlled, auto-indexed)
├── logs/             # execution trajectories (a git repo, contents ignored by default)
├── skills/           # installed expert skills
├── resources/        # skill/class resources
└── shellcopilot/     # the shell-copilot hook (see README)
```

Sanity-check a workspace without running anything:

```bash
yaha --compile      # exit 0 = OK; errors (no classes, missing parent,
                    # Wait Event without producer) exit non-zero
```

Run a task:

```bash
yaha "Summarize this directory"
yaha -a CodeAgent "fix the failing test"    # by name or 3-letter code: -a Cda
yaha -y -f -o answer.md "nightly report"    # unattended: yes-to-all + force + output file
```

With no `-a`, `yaha` runs the class named after the workspace folder if one
exists, otherwise `BaseAgent`.

---

## 3. Hello World: your first agent

Append this to `.yaha/AgentClass.md` (or a file it includes):

```markdown
## HelloAgent
- **Inherited:** BaseAgent
- **Code:** Hel
- **Description:** Greets the user and stops.

- **Preamble:**
    - **Exit Tools:**
        - success_tool
    > Greet the user warmly, mention one interesting fact about their
    > workspace folder name, then complete the stage with success_tool.
```

Run it:

```bash
yaha -a Hel "say hello"
```

That is a complete AGLang program: one class, one stage, one instruction.
Everything else in this guide adds structure on top of this skeleton.

---

## 4. Anatomy of an agent class

### 4.1 Class declaration

A class is a `## ` heading followed by `- **Field:** value` metadata:

```markdown
## CodeAgent
- **Inherited:** BaseReflector
- **Code:** Cda
- **Description:** A lifecycled code-change agent ...
- **Model:** DefaultModel
- **Max Loops:** 60
```

| Field | Meaning |
|---|---|
| `Inherited` | Parent class name, or `None`. The subclass reuses everything it does not re-author. |
| `Code` | Three-letter short code for `-a` (e.g. `yaha -a Cda`). |
| `Description` | Human/agent-readable summary; shown in `yaha --list agents`. |
| `Model` | A model name defined in `Model.md`. |
| `Max Loops` | Chain-operation budget per stage traversal; `-1` = unlimited. |
| `Snap Handler` / `Snap Threshold` / `Snap To` / `Compression Keep Steps` | Context-compression policy (§10). |
| `Hook Timeout` | Per-hook timeout in seconds (see §9). |

### 4.2 Stages

Stages are bullet fields of the class. `Preamble` is the entry stage and
`Aftermath` the built-in terminal stage; anything in between is yours:

```markdown
- **Plan:**
    - **Allowed Tools:**
        - read_file
        - grep_files
    - **Required Tools:**
        - read_file
    - **Exit Tools:**
        - success_tool
        - fail_tool
    - **Fail Target:** Aftermath
    - **Final Answer:** True
    > Read the relevant source files and produce a step-by-step plan.
    > End with success_tool when the plan is complete.
```

The blockquoted lines (`> ...`) are the stage's **prompt** — the instructions
the model sees on entering the stage.

Stage properties:

| Property | Meaning |
|---|---|
| `Allowed Tools` | Whitelist of tools usable in this stage (subject to the class Toolset). |
| `Required Tools` | Tools the model *must* call before the stage may complete. |
| `Exit Tools` | Tools allowed to *end* the stage. The event terminators and `exit_to` are exempt from this list but still need `Allowed Tools` / Toolset. |
| `Wait Event` | Do not enter the stage until the named event is set (§7). |
| `Fail Target` | Stage to route to on `fail_tool` or a failed required hook. `Aftermath` has no Fail Target — a failure there ends the chain with a `--- Failure (Aftermath) ---` block. |
| `Final Answer: True` | This stage's final response is curated into the run's final answer (only on DONE / LOOP / EXIT_TO outcomes, never on FAIL/RESTART/ERROR). |
| `Detached: True` | The stage is not in the chain; it is *called* via `exit_to('name')`, runs fully, then the chain resumes after the caller. Calls nest up to 16 deep. |
| `Snap: enabled` | Allow the compression snapshot at this stage boundary. |
| `Inherited` | Per-stage: `All` keeps the parent's version of every field not re-authored. |

### 4.3 System prompt and chain order

```markdown
- **System Prompt:**
    > WORKER PROTOCOL:
    > 1. Think, then act with exactly one tool per turn.
    > ...
```

The **chain order** is the textual order of the stages in the class:
`Preamble` first, `Aftermath` last. A fresh run starts at `Preamble`; jumping
back to the first chain stage starts a new task round.

### 4.4 Toolset, Skillset, Agentset

After the stages, three sections bound what the agent can reach:

```markdown
### Toolset (Available Tools)
	- create_file
	- edit_file
	- bash_tool

### Skillset (Available Skills)
	**Mode:** Auto

### Agentset
	- WebSearch
	- SleepAgent
```

- **Toolset** — only listed tools are advertised to the model. `restart` and
  `loop_tool` are always advertised; the other control tools are advertised
  automatically when a stage names them in `Exit Tools`.
- **Skillset** — skills the agent may `load_skill`; `Mode: Auto` lets the
  runtime offer relevant skills.
- **Agentset** — classes this agent may spawn. Together with the toolset it
  forms the **privilege cap**: a sub-agent gets at most its parent's tools and
  agents, so a child can never exceed its parent's authority.

A per-class inline `- **Toolset:**` field with `- **Inherited:** All` plus
appended names also works (used by the endpoint classes in §8).

### 4.5 Inheritance

`- **Inherited:** <Parent>` makes the child reuse the parent wholesale, with
three granularities of override:

1. **Class level** — any field the child authors replaces the parent's.
2. **Stage level** — `- **Inherited:** All` inside a stage keeps the parent's
   version of every field the stage does not re-author; without it, an
   authored stage replaces the parent's stage.
3. **List level** — inside a tool list or hook block,
   `- **Inherited:** All` keeps the parent's entries and appends yours
   (ancestor-first); without it your list *replaces* the parent's.

```markdown
## CodeAgent
- **Inherited:** BaseReflector        # reuse everything ...

- **Preamble:**
    - **Inherited:** All              # ... keep the parent's Preamble ...
    - **Snap:** enabled               # ... but enable snap
    - **Fail Target:** Query_user     # ... and reroute failures
```

Inheritance is resolved once over the merged class map. Naming a parent that
does not exist is reported (and is a `--compile` error).

### 4.6 Comments, includes, macros

**Comments** — author-facing prose lives in comment blocks, stripped at load
time so it never reaches a prompt. Blocks do not nest; malformed blocks fail
the load; directives inside are inert:

```markdown
<!--comment-->
This text never reaches any prompt. Parked directives here are inert.
<!--endcomment-->
```

Everything outside a comment block must belong to a class — bare free-style
text is not allowed.

**Includes** — `<!--include path="utils.md" -->` (also `<!--include "utils.md" -->`)
inlines the file *before parsing*: macro expansion, one document, one class
map. The path resolves relative to the including file. The default
`AgentClass.md` is just five includes: `const.md`, `base.md`, `utils.md`,
`chat.md`, `coder.md`.

**Macros** — define once, reference anywhere *in the same document*:

```markdown
<!-- define name="snap", params=[] -->
    > Distill Critical Wisdom into .yaha/memory/ ...
<!-- enddefine -->

... later, inside a stage:
    <!--ref name="snap" -->
```

**Extra class files** — `-I extra.md` (repeatable) loads additional class
files. Unlike `<!--include-->`, each `-I` file is its *own document*: own
macro store, own includes, parsed separately, then class maps merged in order
(primary first). A class in a later file can inherit from earlier ones; if the
same class name is defined twice, the **last definition wins wholesale** (the
clash is reported). Use `<!--include-->` when you need the workspace's macros;
use `-I` for self-contained extension packs:

```bash
yaha -I /path/to/echo_agent.md -a Ecl "talk to it"
```

---

## 5. The execution model

### 5.1 The ReAct loop and `<tool_code>`

Inside a stage, the model reasons and emits exactly one tool call per turn:

```
<tool_code>{"tool": "bash_tool", "args": {"cmdline": "ls -la"}}</tool_code>
```

YAHA validates every response; malformed output is rejected and retried
automatically. The loop continues until the model calls a control tool.

### 5.2 Control tools (traversing the chain)

| Tool | Effect |
|---|---|
| `success_tool(summary)` | Complete the stage, advance to the next chain stage. |
| `fail_tool(reason)` | Fail the stage; route to its `Fail Target` (recovery path). |
| `loop_tool(summary, modifier)` | Re-enter the *current* stage: Wait Event re-checked, prompt rebuilt; `modifier` rides one entry as a `[TASK MODIFIER]: ...` section. |
| `exit_to(name, summary)` | Jump to a chain stage (forward or backward; naming the current stage is refused — use `loop_tool`), or **call** a `Detached` stage and resume after the caller. Costs one chain operation against `Max Loops`. Jumping to the first chain stage starts a new task round. |
| `restart` | Restart the whole chain from `Preamble`. |
| `set_event_success(summary, names...)` | Set events, then complete (event + `success_tool`). |
| `set_event_loop(summary, names...)` | Set events, then loop (event + `loop_tool`). |
| `set_event_jump(summary, target, names...)` | Set events, then jump (event + `exit_to`). |

The event terminators take several event names in one call; their first
argument is the stage summary and becomes its final response. A missing/empty
event name is a hard error — a waiter can never hang on an event that was
never set.

### 5.3 A typical lifecycle: CodeAgent

The shipped `CodeAgent` (`-a Cda`, in `coder.md`) shows idiomatic chain
design:

```
Preamble -> Plan -> Approve -> Coding -> Summary -> Aftermath
```

- `Preamble` grills the user for a clear task (with the `grillme` macro).
- `Plan` drafts; `Approve` asks the user for approval and uses
  `exit_to('Plan')` when rejected.
- `Coding` implements; `Summary` reports and jumps back to `Preamble` for the
  next task.

Pattern to copy: **one job per stage, gates on the boundaries, loops via
control tools.**

---

## 6. Tools and skills

### 6.1 The tool catalogue

`tool-schema.md` is a single JSON array of tool schemas. The default catalogue:

**Files** — `create_file(path, content)`, `edit_file(path, search_text?,
replace_text?, start_line?, end_line?)`, `read_file(path, start_line,
end_line)`, `write_file(path, content, start_line)`, `append_file(path,
content)`, `search_files(path, search_text)` (regex in one file),
`grep_files(path, search_text, recursive?, glob_pattern?, max_results?)`,
`list_files(path?, pattern?)`.

**Shell & web** — `bash_tool(cmdline)`, `async_bash(cmdline)` (background job;
returns a waitable job id `async-N` usable as an event name),
`poll_async(job_id)`, `web_fetch_tool(url, prompt)`,
`google_search_tool(query)` (Gemini grounding), `tavily_search_tool(query,
max_results?)` (needs `TAVILY_API_KEY`).

**Agents & skills** — `yaha_spawn_agent(agent_class, system, instruction)`
(blocking; returns the sub-agent's final answer),
`spawn_parallel_agent(agent_class, clone_context?, prompt?)` (non-blocking;
returns the peer handle), `load_skill(name)`, `ask_user(question)`.

**Chain control** — `restart`, `loop_tool`, `success_tool`, `fail_tool`,
`exit_to` (§5.2).

**Events** — `set_event_success`, `set_event_loop`, `set_event_jump`,
`bind_tool(placeholder, real_event)` (§7).

**Endpoints** — `connect_tool(service_ep)`, `send_tool(cep, message)`,
`close_tool(cep)` (§8).

Register your own tools by adding schemas to `tool-schema.md`; disable one by
removing it. A tool is usable by an agent only when listed in its class
Toolset.

### 6.2 Skills

A skill is a folder under `.yaha/skills/` with a `SKILL.md`:

```markdown
---
name: pdf2markdown
description: Use this skill to convert a PDF file to a Markdown file.
resources:
  - scripts/pdf2markdown-text.py
---

# Instructions
1. Run: python3 <skill_location>/scripts/pdf2markdown-text.py <in>.pdf -o <out>.md
2. ...

# Rubric
Use `ls -l` to check the converted file exists.
```

Frontmatter fields: `name`, `description`, `resources` (files shipped with the
skill). The body is natural-language instructions, optionally with `# Rubric`
and `# Example` sections. Agents load one with `load_skill('pdf2markdown')`.
Scaffold a new skill with `yaha --create_skill myskill`; search extra folders
with `--sk-dir`. Shipped examples: `arxiv-dl`, `pdf2markdown`, `yigua`,
`python-expert`.

---

## 7. Multi-agent programming

### 7.1 Blocking sub-agents

`yaha_spawn_agent(agent_class, system, instruction)` runs a child to
completion and returns its final answer — a synchronous function call, agent
style. The child must be in the caller's Agentset and inherits the privilege
cap. The shipped helpers are built this way: `BaseReflector` (distills wisdom
into memory after a task), `SleepAgent` (memory consolidation), `WebSearch`,
`GrillMe` (specification interviewer), `ChatAgent`.

### 7.2 Parallel peers and named events

`spawn_parallel_agent(agent_class, clone_context, prompt)` starts a **flat
peer** that returns immediately with a handle name; `clone_context` is `new`
(fresh context) or `clone` (deep copy of the caller's). Peers have isolated
context windows but **share the workspace and memory**. A run ends when every
agent has finished.

Peers coordinate through **named events** — synchronization happens only at
stage boundaries:

```markdown
- **Gather:**
    - **Wait Event:** sources_ready
    - **Exit Tools:**
        - set_event_success
    > Combine the fetched sources, then
    > set_event_success('gathering done', 'gather_done')
```

- A stage with `Wait Event: name` is not entered until `name` is set; the
  scheduler parks that agent and runs others. The gate is re-checked on every
  entry, including loops.
- Events **auto-reset** after a wait: a set that finds parked agents releases
  them all and re-arms, so the next round blocks again. A set that finds
  nobody parked **latches** and is consumed by the next waiter — an early
  producer cannot deadlock a late consumer, and a completed handshake never
  leaks into a later wait.
- A peer sets **no** event automatically when it finishes, and there is no
  handle-targeted signalling — always coordinate through named events.
- Startup validation: every `Wait Event` name must have a literal producer
  (`set_event_*(..., 'name')`) somewhere in the workspace prompts, or the run
  refuses to start. A typo becomes a clear error before anything runs.

### 7.3 Late binding with `@placeholders`

Event names can be resolved at runtime:

```
bind_tool('@gate', 'review_round_7_done')
```

- The first argument must carry the `@` prefix; the second must be a real
  event name (no `@a` → `@b` chaining).
- The binding is **run-global**: every agent in the run resolves the same
  mapping, so one actor can bind a gate another is already waiting on. It is
  strictly 1:1; rebinding is allowed and the last bind wins — but do not
  re-point a placeholder already in use (a latch under the old target is not
  moved).
- A bound placeholder works in `Wait Event:` and in all three event
  terminators. An **unbound** placeholder is an error, not a park: the stage
  routes to its `Fail Target` with a message naming the placeholder.
- `bind_tool` must be listed in the class Toolset; it is deliberately not
  auto-advertised, so the privilege cap applies to sub-agents.

---

## 8. Distributed agents: endpoints

An **endpoint (EP)** is a `http(s)://host[:port]/path/version` URL — e.g.
`http://127.0.0.1:8123/echo/v1` (the trailing `/v1` protocol version is
required). One EP is both an event name and a message queue. Two kinds:

- **Service endpoint (SEP)** — the listener. A stage that waits on a service
  EP installs the listener. When a client connects, YAHA creates a
  **conversation endpoint (CEP)** and prompts the stage with
  `[REMOTE CONNECTION]: <cep>`. A SEP serves many clients, each with its own
  CEP.
- **Conversation endpoint (CEP)** — one conversation: a level signal over a
  FIFO queue. A wait takes one message; an early message is buffered, never
  lost. The waiting stage is prompted with `[REMOTE MESSAGE]: <text>` and
  replies **explicitly** by calling `send_tool` — which lets an operator gate
  replying per stage or per Toolset.

Tools: `connect_tool(service_ep)` → returns a CEP; `send_tool(cep, message)`
(cep may be a bound placeholder); `close_tool(cep)` (optional — everything is
reclaimed at run end). Transport is plain HTTP long-poll; optional shared
bearer token via `?token=` or `Authorization: Bearer`. A run stays alive while
an agent waits on an endpoint a peer could still answer; if nothing answers
within the idle timeout (60 s default) the run ends with a deadlock error
naming the endpoints.

The shipped demo ([`examples/echo_agent/`](../../examples/echo_agent/README.md))
is the canonical pattern — a server waiting on its SEP, binding each CEP to
`@peer`, and looping on it:

```markdown
## EchoServer
- **Inherited:** BaseAgent
- **Code:** Eco
- **Toolset:**
    - **Inherited:** All
    - bind_tool
    - send_tool
    - close_tool

- **Preamble:**
    - **Wait Event:** http://127.0.0.1:8123/echo/v1
    > ... bind_tool('@peer', '<cep>') ... then success_tool.

- **Echo:**
    - **Wait Event:** @peer
    > Reply with send_tool('@peer', 'echo: <text>'), then loop_tool for the
    > next message. On exactly 'quit': close_tool('@peer') + success_tool.
```

The client is symmetric: `connect_tool` → bind the returned CEP to `@reply` →
`send_tool` a message → `Wait Event: @reply` for the echo → `exit_to('Ask')`
for the next round. Run it in two terminals:

```bash
yaha -a Eco "serve the echo endpoint"    # terminal 1
yaha -a Ecl "talk to it"                 # terminal 2
```

Because endpoints are URLs, the two agents can just as well run on different
machines.

---

## 9. Stage hooks: embedding Bash and Python

Hooks attach code to stage boundaries. They are ordinary stage properties
(same indentation as `Exit Tools:`) and inherit like any stage field:

```markdown
- **Coding:**
    - **preenter:**
        - bash (capture): echo "entering Coding at $(date)"
    - pretool:
        - bash: printf '%s\n' "$YAHA_TOOL" >> /tmp/tool.log
    - posttool:
        - python (capture):
            ```
            if not yaha.tool.ok:
                emit("tool failed: " + str(yaha.tool.error))
            ```
    - preexit:
        - python (required):
            ```
            assert yaha.stage.name == 'Coding'
            ```
```

The four hook points:

```
stage boundary -> Wait Event satisfied -> preenter -> prompt
each turn:      LLM -> pretool -> TOOL -> posttool -> LLM ...
exit:           pretool -> tool -> posttool -> preexit -> exit action
```

Each item is `bash` or `python` with two optional, case-insensitive modifiers:

- `(required)` — a failure becomes the stage's failure (routed like
  `fail_tool`). A failing required `pretool` aborts that one tool call; a
  failing required `preexit` blocks the exit **and** the event signal — a
  stage that failed its own gate can never release a peer. Without it, a
  failure is only reported (TUI + error log) and the run continues.
- `(capture)` — the hook's output is injected into the conversation as a
  user-side observation `[HOOK <stage>/<hook>[i]]` (capped at 4 KB). Without
  it, output goes to the TUI and log only.

Rules of thumb:

- **bash hooks** run with `/bin/bash -c` in the workspace, with extra env vars
  `YAHA_AGENT`, `YAHA_AGENT_CLASS`, `YAHA_AGENT_ID`, `YAHA_STAGE`,
  `YAHA_TURN`, `YAHA_WORKSPACE`, `YAHA_SESSION_DIR`, `YAHA_TOOL`,
  `YAHA_TOOL_ARGS`. Non-zero exit = failure. Multi-line commands: use a
  YAML-style literal block `- bash (required): |` with indented lines (the
  folded form `>` is rejected).
- **python hooks** share one persistent kernel per agent (variables survive
  across blocks, stages and loops; each parallel peer gets its own). An
  exception is a failure and the namespace is *not* rolled back. Timeout:
  `YAHA_HOOK_TIMEOUT` env > class `Hook Timeout` > 300 s default; `0`
  disables.
- Every python hook reads the run through a read-only `yaha` object:
  `yaha.agent.*`, `yaha.stage.*`, `yaha.hook.*`, `yaha.tool.*` (pretool/
  posttool only), `yaha.runtime.*` (including `events` and
  `blocked_signals`), `yaha.workspace.*`, `yaha.log.*`. Assignment raises
  `YahaReadOnlyError`; `emit(value)` returns structured output.
- Hooks fire on framework exit tools too; `ask_user` is excluded from
  pretool/posttool only.
- **Security:** hooks run *outside* the tool sandbox with the operator's
  privileges. Treat an untrusted `AgentClass.md` exactly like an untrusted
  shell script.

---

## 10. Memory and context

### 10.1 Durable memory

`.yaha/memory/` is a git-backed folder of Markdown wisdom files, fully managed
by the agents. Its index is rebuilt and injected into the system prompt at
startup (and after every compression), so agents read memory on demand.
`BaseReflector` writes lessons into memory after a task; `SleepAgent` (`-a
Slp`) periodically consolidates, verifies and deduplicates it (budget: ≤10
files, ≤100 lines/file, ≤100 words/line). After a run, the CLI auto-commits
`memory/` and `ext/`, and triggers `SleepAgent` when memory exceeds 1500
lines.

### 10.2 Context compression

Long-horizon runs compress context in one of two ways: a class-level **Snap
policy** whose detached handler stage writes the summary, or the built-in L2
reflection when no handler exists:

```markdown
- **Snap Handler:** Compression      # a Detached stage that summarizes
- **Snap Threshold:** 80%            # compress when context is 80% full
- **Snap To:** 40%                   # target fill after compression
- **Compression Keep Steps:** 10     # recent ReAct steps that survive
```

Compression rebuilds the context from three parts: the freshly built system
prompt (memory index re-read), a structured summary (GOAL / KEY LEARNINGS /
DESIGN & ARCHITECTURE DECISIONS / PENDING / KEY FILES / ERRORS & LESSONS,
under 100 lines), and the most recent ReAct steps (one step = one
tool_call–result pair). Because compression is a *stage*, you can write your
own handler class and control exactly what survives.

---

## 11. Identity and models

### 11.1 SOUL.md — who the agent is

`SOUL.md` holds persona text injected at the very top of every system prompt.
Everything before the first `## ` heading is the DEFAULT identity; `##
ClassName` sections give per-class personas (matched by name, then by short
code, case-insensitively; last duplicate heading wins):

```markdown
# SOUL

You are Ada, a helpful and efficient agent.

## CodeAgent
You are Kris, a blunt, precise engineer. You never apologize for deleting code.
```

Keep identity (name, tone, voice) here; keep the operational *role* (protocol,
stages) in the class's `System Prompt`. A file with no `## ` heading applies
to every class. `yaha --upgrade` never overwrites `SOUL.md`; `{agent_name}` is
substituted once at `--init`.

### 11.2 Model.md — what the agent thinks with

Each model is a `## name` heading with provider fields:

```markdown
## gemini_default
- **provider:** gemini
- **model:** gemini-3-flash-preview
- **context_size:** 1000000

## deepseek_reasoner
- **provider:** deepseek
- **model:** deepseek-reasoner
- **max_tokens:** 8192
- **context_size:** 128000
```

Providers shipped: **Gemini**, **DeepSeek** (V4 Flash/Pro/Reasoner), and local
models via **Ollama**. A class picks its model with `- **Model:** <name>`;
different classes in one run can use different backends. `yaha --compile`
warns when a class names a model `Model.md` does not define.

---

## 12. CLI reference

| Command | Effect |
|---|---|
| `yaha "task"` | Run the default agent (workspace-named class, else `BaseAgent`). Stdin is appended when piped. |
| `yaha -a Cda "task"` | Run a specific class by name or 3-letter code. |
| `yaha -F spec.md "task"` | Append a file's contents to the prompt. |
| `yaha -w /path "task"` | Use (creating if needed) another workspace. |
| `yaha -I extra.md ...` | Load extra class files for this run (repeatable). |
| `yaha --init [NAME]` | Initialize `.yaha/`; personalize SOUL.md with NAME. |
| `yaha --upgrade` | Sync latest framework configs/skills; never clobbers SOUL.md. |
| `yaha --compile [-I ...]` | Validate classes without running. Errors: no classes, missing parent, Wait Event without producer. Warnings: duplicate class names, undefined model, empty Code. |
| `yaha --list agents/skills/tools/sessions` | Inventory the workspace. |
| `yaha --create_skill name` | Scaffold a new skill. |
| `yaha --sk-dir DIR` | Add skill search folders (repeatable). |
| `yaha -y -f -o out.md "task"` | Unattended mode: yes-to-all, force, save the raw answer. Auto-enabled when stdin is not a TTY. |
| `yaha --ide` | Open the workspace as an Obsidian vault (with the bundled YAHA Agents plugin). |
| `yaha -v`, `-V` | Verbose TUI; print version. |

---

## 13. Security: the Access Control System

- **Filesystem sandbox** — file tools enforce the workspace root strictly,
  with granular protection of `.yaha` internals. On macOS, `sandbox-exec`
  wraps shell commands natively.
- **Three-tier shell security** — a whitelist for common commands, heuristic
  anomaly checks, and human-in-the-loop confirmation for dangerous ones.
  `-y` pre-grants confirmations; unattended mode refuses non-whitelisted
  commands that reach outside the workspace.
- **Privilege cap** — a sub-agent's Toolset/Agentset can only ever be a
  subset of its parent's.
- **Hooks are trusted code** (§9) — they deliberately run outside the
  sandbox.

---

## 14. Observability and debugging

- `.yaha/logs/` holds a hierarchical trajectory for every execution attempt,
  plus a structured error log (hook failures are category `hook`).
- `yaha --list sessions` lists recorded runs.
- The interrupt venue's `status` dump reports the resolved workspace and log
  paths.
- `yaha --compile` is your static checker — run it after every edit to class
  files.
- Multi-agent deadlock detection names the agent, the hook, and the endpoints
  involved instead of hanging silently.

A typical authoring loop:

```bash
$EDITOR .yaha/AgentClass.md
yaha --compile && yaha -a Mya "small smoke test"
yaha --list sessions          # find the trajectory
```

---

## 15. A complete worked example

A two-stage reviewer that reads a diff, summarizes it, and — only when the
diff touches tests — asks the user whether to run them:

```markdown
<!--comment-->
DiffTriage: a minimal two-stage agent demonstrating Allowed/Required/Exit
tools, a Fail Target, a conditional jump, and a Final Answer stage.
<!--endcomment-->

## DiffTriage
- **Inherited:** BaseAgent
- **Code:** Dft
- **Description:** Reads a unified diff, triages it, and conditionally runs the tests.
- **Model:** DefaultModel
- **Max Loops:** 40

- **Preamble:**
    - **Allowed Tools:**
        - read_file
        - bash_tool
    - **Required Tools:**
        - read_file
    - **Exit Tools:**
        - exit_to
        - fail_tool
    - **Fail Target:** Aftermath
    > Read the diff at the path the user gave. Note every file it touches.
    > If any touched path matches test patterns, exit_to('AskRunTests');
    > otherwise exit_to('Report').

- **AskRunTests:**
    - **Allowed Tools:**
        - ask_user
        - bash_tool
    - **Exit Tools:**
        - exit_to
    > ask_user whether to run the tests. If yes, run them with bash_tool
    > and keep the output. Either way, exit_to('Report').

- **Report:**
    - **Final Answer:** True
    - **Exit Tools:**
        - success_tool
    > Write the triage: files touched, risk hotspots, and the test outcome
    > if tests ran. Complete with success_tool.

- **Aftermath:**
    > The triage is finished or was abandoned; state which.
```

Run it:

```bash
yaha --compile && yaha -a Dft "triage /tmp/round-3.patch"
```

From here the natural next steps are: give it a `Snap` policy for long diffs,
spawn a `CodeReviewer` sub-agent from `Report` for a second opinion, or turn
`AskRunTests` into an unattended gate with a `preexit (required)` python hook
that asserts the test command exited zero.

---

## 16. Authoring checklist

1. **One job per stage.** Move a second job into its own stage rather than
   growing a prompt.
2. **Always set a Fail Target** (or accept `Aftermath`) — unstructured failure
   is the enemy of long runs.
3. **Whitelist per stage.** `Allowed Tools` is cheaper than prompt-policing.
4. **Every Wait Event needs a literal producer** — `--compile` enforces it;
   keep it green.
5. **Bind `@placeholders` before anyone waits on them**, and never re-point
   one in use.
6. **Keep prompts imperative and short**; put identity in `SOUL.md`,
   procedures in skills, code in hooks.
7. **Treat hooks as code**: `(required)` for gates, `(capture)` only when the
   model must see the output.
8. **Let memory work**: inherit `BaseReflector` so lessons persist, and run
   `SleepAgent` periodically.
9. **`--compile` after every edit.** It catches missing parents, dead waits
   and duplicate classes before they cost you a run.
10. **Design the stop rule first**: which stage, which tool, which event ends
    the program — for single agents and crews alike.
