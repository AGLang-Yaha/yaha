# AgentClass.md Authoring Guide and Multi-Agent Stage Reference

Comments in AgentClass.md
-------------------------

Author-facing prose, and directives that are parked rather than active,
belong inside a comment block. A comment block opens with an HTML comment
holding the single word `comment` and closes with an HTML comment holding
the single word `endcomment`. Neither marker can be written literally
inside the block itself: the closing one would end the block, so it is
spelled out in words here. Implementation: `_strip_comments` in
`yaha/core/md_loader.py`.

Rules:
- A comment block is stripped while the file is loaded, so its text never
  reaches a system prompt, a stage instruction or any other content.
- Blocks do NOT nest: the first closing marker ends the block.
- Directives parked inside a comment are INERT. A commented-out include,
  define or ref directive is never followed, registered or expanded.
- Malformed comments fail the load: an unterminated block, or a closing
  marker with no matching opener, raises MarkdownLoadError.
- Line numbers after a stripped block are unchanged.

Everything OUTSIDE a comment block must be part of a class: a `## Class`
heading, its `- **Field:**` metadata, its stage blocks and their `>`-quoted
prompt lines. Bare free-style text outside a comment block is not allowed.

The block below documents the multi-agent stage features (patch 1).

Multi-Agent Stages
==========================

Agents in a YAHA run are flat peers that each own a context window; any agent
may start further peers with `spawn_parallel_agent`. A run ends when every
agent has finished. Two optional stage features coordinate them, and
synchronisation happens ONLY at stage boundaries:

- **Wait Event:** <name> - the stage is not entered until the named event is
  set. The scheduler parks the agent and runs other ready agents meanwhile.
  The gate is re-checked on EVERY entry, including a re-entry via `loop_tool`
  / `set_event_loop`: a looped stage parks again exactly like a fresh entry.
- Set events from a stage with the framework event terminators
  `set_event_success` (set the events, then complete the stage, like
  success_tool), `set_event_loop` (set the events, then loop the CURRENT
  stage, like loop_tool) and `set_event_jump` (set the events, then move the
  chain to a target stage, like exit_to). A missing or empty event name is a
  hard error and the stage is NOT completed, so a waiter can never hang
  silently on an event that was never set.
- The event terminators take SEVERAL event names in ONE call:
  `set_event_success('done', 'a', 'b')` sets both `a` and `b`. Their FIRST
  argument is a summary of the stage and becomes that stage's final response;
  nothing is injected into any later stage.
- `loop_tool(summary, modifier)` ends the CURRENT stage and immediately
  re-enters it (the Wait Event gate is re-checked, the prompt is rebuilt).
  `modifier` is a one-shot note appended to the re-entered stage's prompt as
  a `[TASK MODIFIER]: ...` section; it rides that one entry only. To move to
  a DIFFERENT stage, use `exit_to` / `set_event_jump` instead.
- `exit_to` (set the chain position) - NOTE: this bullet list sits inside an
  HTML comment block (stripped at load time), so it is AUTHOR-FACING documentation and is not
  loaded into any prompt. `exit_to("<stage>")` leaves
  the current stage and continues the chain from the named stage. A chain-stage
  name is a positional jump (forward or backward; a JUMP naming the CURRENT
  chain stage is
  REFUSED - call `loop_tool` to re-enter it). A name that is a declared but
  unattached `Detached` stage is a
  CALL: it runs as a full stage, then the chain resumes at the caller 's
  next stage; calls may nest, capped at 16 deep, and naming the CURRENT
  Detached stage is plain recursion (still depth-capped). A blank or unknown name is a
  hard error and the stage is NOT left. An optional `summary` argument
  becomes the stage's final response, exactly as in success_tool. `exit_to`
  is exempt
  from a stage 's Exit Tools list but must be in Allowed Tools, and every
  jump or call costs one chain operation against `Max Loops`. Jumping back to
  the chain's FIRST stage starts a new task round.
  `set_event_jump(summary, target, names...)` is
  the same move with event signalling on top.
- A stage's result is curated into the run's final answer when - and only
  when - the stage completed: DONE (success_tool / plain text /
  set_event_success), LOOP (loop_tool / set_event_loop) or EXIT_TO (exit_to /
  set_event_jump). A stage marked **Final Answer:** True contributes its
  final response to the curated outputs on ANY of these outcomes; FAIL
  (fail_tool / hook failure), RESTART and ERROR outcomes are never curated.
- **Aftermath** is the built-in terminal stage: it has no Fail Target. A
  failure IN Aftermath (fail_tool, or a failed required hook) ends the chain
  normally; the final answer is the curated outputs plus a
  `--- Failure (Aftermath) ---` block carrying the reason.
- Start a parallel peer with `spawn_parallel_agent(agent_class, clone_context,
  prompt)`, which is non-blocking; `clone_context` is `new` (fresh context) or
  `clone` (deep copy of the caller's context at spawn time). The target class
  must be listed in this agent's Agentset, and a peer may itself spawn further
  peers under the same rules. The tool returns the peer's HANDLE NAME only:
  coordinate with it through named events, because a peer sets NO event
  automatically when it finishes and there is no handle-targeted signalling.
  Fan-out is intentionally unbounded - it is limited only by Agentsets and the
  toolset privilege cap.

Every Wait Event name must be produced by some `set_event_success`,
`set_event_loop` or `set_event_jump` call written as a literal in a prompt -
positional
`set_event_success('', 'name')` or by keyword `set_event_success(name='name')` -
otherwise the run refuses to start (this turns a wait-name typo into a clear
error before anything runs).

Events AUTO-RESET after a wait, so a Wait Event blocks on EVERY round: a set
that finds agents parked on the name releases all of them and then re-arms
(clears), so the next round blocks again. A set that finds nobody parked
LATCHES (stays set) and is consumed by the next agent that reaches the wait,
which then proceeds without blocking - so a producer that runs slightly early
cannot deadlock a late consumer. A completed handshake never leaks into a
later wait.

Advertising: a tool must appear in the agent's **Toolset** to be listed in
AVAILABLE TOOLS. `restart` and `loop_tool` are always advertised, and
`success_tool` / `fail_tool` / `set_event_success` / `set_event_loop` /
`set_event_jump` / `exit_to` are also
advertised automatically when any stage names them in its **Exit Tools**.

Gating: the event terminators are exempt from a stage's **Exit Tools** list
(they may end the stage even when that list omits them), but - like every other
tool - they remain subject to a stage's **Allowed Tools** list and to the agent
Toolset for advertising.

Example fragments (illustrative only; not part of any class below):

- **Toolset (Available Tools):**
    - spawn_parallel_agent
    - set_event_success

- **Gather:**
    - **Wait Event:** sources_ready
    - **Exit Tools:**
        - set_event_success

Stage Hooks
===========

A stage can declare up to four hooks. They are ordinary stage properties, so
they sit at the same indentation as `Exit Tools:` and inherit with the stage.

Hooks are CODE, not data. A bash hook runs with `/bin/bash -c` OUTSIDE the
AccessController sandbox that `bash_tool` uses (the workspace boundary does not
apply to it), and a python hook runs in a separate persistent subprocess with the
same privileges as the operator. They are authored by whoever writes this file,
so they carry the same trust level as the prompts: treat an untrusted
AgentClass.md exactly as an untrusted shell script. Hook code is blanked out of
the stage text at load time, so it never reaches the model.

    - **Coding:**
        - **preenter:**
            - bash (capture): echo "entering Coding at $(date)"
            - python (capture):
                ```
                print(yaha.agent.agent_class.short_name)
                ```
        - **pretool:**
            - bash: printf '%s\n' "$YAHA_TOOL" >> /tmp/tool.log
        - **posttool:**
            - python (capture):
                ```
                if not yaha.tool.ok:
                    emit("tool failed: " + str(yaha.tool.error))
                ```
        - **preexit:**
            - python (required):
                ```
                assert yaha.stage.name == 'Coding'
                ```

What each hook type does
------------------------

    preenter   runs AFTER a `Wait Event:` has been satisfied and BEFORE the
               stage prompt is built, so a (capture) hook can inform the model
               before it plans. A failing (required) preenter blocks entry.
    pretool    runs before every tool call in the stage (one per call).
    posttool   runs after every tool call, and can see whether it failed.
    preexit    runs once before the stage's exit action - including the event
               set performed by set_event_success / set_event_loop /
               set_event_jump. A failing
               (required) preexit blocks the exit AND the event signal, so a
               peer can never be released by a stage that failed its own gate.
               A failing (required) posttool abandons a pending event signal
               the same way, and both are reported to the runtime's deadlock
               diagnostic as a blocked signal. That list (visible to hooks as
               yaha.runtime.blocked_signals) is scoped to signals that never
               happened: once an event is actually signalled, its entries are
               dropped, so a recovered handshake cannot be blamed for a later,
               unrelated deadlock.

The whole order is:

    stage boundary -> Wait Event satisfied -> preenter -> prompt
    each turn:      LLM -> pretool -> TOOL -> posttool -> LLM ...
    exit:           pretool -> tool -> posttool -> preexit -> exit action

Hooks run on every tool call, including the framework exit tools
(success_tool, loop_tool, fail_tool, set_event_success, set_event_loop,
set_event_jump) and
spawn_parallel_agent. `ask_user` is excluded from pretool/posttool only:
hooking an open interactive prompt would interleave hook output into the
question. preenter/preexit are stage-level and always run, so an `ask_user`
listed as an Exit Tool still goes through its preexit gate.

An event terminator (set_event_success / set_event_loop / set_event_jump)
signals its event at
exactly ONE place - the exit action, after preexit - whether or not the stage
declares a preexit hook. A posttool hook therefore always sees the event as
still unset, and a failing (required) posttool OR preexit abandons the signal.

Items, and the two modifiers
----------------------------

Every item is either

    - bash [(required)][, (capture)]: <shell command>
    - python [(required)][, (capture)]:
        ```
        <python>
        ```

Modifiers are optional, case-insensitive and order-free.

* Without (required) a failing hook is only reported: the TUI shows it and the
  session error log records it (category `hook`), and the run continues.
* With (required) the failure becomes the stage's failure, routed exactly like
  any other stage failure. A failing `pretool (required)` aborts THAT tool call
  before it runs and hands the agent a normal failed tool result.
* With (capture) the hook's output is injected into the conversation as an
  observation attributed to the hook, e.g.

      [HOOK Coding/preenter[1]]
      <output>

  It is a user-side observation, never a fake assistant turn, and it is capped
  (4 KB, then `[...truncated]`). Without (capture) the output goes to the TUI
  and the log only, and the conversation is untouched.

bash hooks
----------

Run with `/bin/bash -c`, in the workspace directory, with the inherited
environment plus:

    YAHA_AGENT  YAHA_AGENT_CLASS  YAHA_AGENT_ID  YAHA_STAGE  YAHA_TURN
    YAHA_WORKSPACE  YAHA_SESSION_DIR  YAHA_TOOL  YAHA_TOOL_ARGS

stdout and stderr are both captured; a non-zero exit code is the failure.

A command may continue on deeper indented lines, and a YAML-style literal block
scalar header as the WHOLE inline text works as expected - the indicator is
layout, never part of the command:

    - bash (required): |
        set -eu
        test -f out.txt

The folded form (`>`) and explicit indentation indicators (`|2`) are rejected
with `HookSpecError`: folding is not implemented, and silently keeping the
indicator would hand bash a command it cannot parse.

python hooks
------------

All python hooks of one agent run in ONE persistent kernel process, so a later
block sees the variables an earlier block assigned:

    - python: x = 21
    - python (capture): print(x * 2)      # -> 42

The kernel lives for the whole agent (every stage, and across `loop_tool`
re-entries), and each `spawn_parallel_agent` peer gets its own. `print`
is captured per block; `emit(value)` is always available as the explicit way to
hand structured output back.

An exception is a hook failure, and the namespace is NOT rolled back: a failed
block keeps whatever it already mutated (Jupyter-like). A block that never
returns is killed by the per-block timeout, and the kernel restarts with an
EMPTY namespace. The timeout is resolved in this precedence order:
`YAHA_HOOK_TIMEOUT` (process environment) > `**Hook Timeout:**` (this class,
inherited) > the 300 s default; `0` disables it, and an empty or unparsable
`YAHA_HOOK_TIMEOUT` is ignored so the class value applies (never fatal). The same
value is the subprocess timeout of a bash hook. If the kernel dies unexpectedly
the next python hook starts a fresh one and the reset is reported. Nothing here
needs a TTY, so hooks work unattended too.

The `yaha` object (read-only)
----------------------------

Every python hook can read the run through `yaha`, with no import:

    yaha.agent.name / agent_id / turn / message_count / model
    yaha.agent.agent_class.short_name / lineage / inherits(name) / stages
    yaha.agent.kernel.pid / running / resets / last_error
    yaha.stage.name / index / chain_len / tools.allowed|required|exit
    yaha.stage.wait_event / hooks
    yaha.hook.name / kind / required / capture / index / label
    yaha.tool.name / args / ok / error / duration_ms   (pretool/posttool only)
    yaha.runtime.live / ready / waiting / events / active / blocked_signals
    yaha.workspace.root / session_dir / log_dir / cwd
    yaha.log.error_log_path / agent_log_path

yaha.hook is a closed view: `name` is an alias of `label`, and the hook's own
source text is NOT exposed. yaha.tool is bound only in pretool/posttool; inside
a pretool hook its ok/error/duration_ms are necessarily `None` because the call
has not happened yet, and they are populated in posttool (ok=False plus the
error text for a failed tool).

posttool's `ok` comes from the tool-result string convention: failure is text
starting with Error/ERROR (case-insensitive, leading whitespace ignored). A
custom tool that reports failure in another wording is seen as ok=True, so a hook
that must be exact should inspect yaha.tool.name/args. The agent's own stage
control keeps its stricter check, so this observation never changes whether a
stage completes.

It is strictly read-only: assigning to any attribute raises
`YahaReadOnlyError`. Because the code runs out of process, the view is a
snapshot taken immediately before the block runs - values are current as of
block start (nothing can change while the hook runs, because the agent is
blocked inside it). Mutating hooks (signalling events, injecting messages,
spawning) are deliberately not available in this patch.

Inheritance
-----------

Hooks inherit exactly like the other stage fields, and the rule is uniform:
`Inherited: All` means keep and append, anything else means replace.

* A stage-level `- **Inherited:** All` inherits every field the child did not
  author - the prompt, scalars such as `Fail Target`, the tool lists AND hooks.
* A per-block marker inside the tool list or the hook block keeps the parent's
  entries and appends this stage's after them (ancestor-first):

      - **preenter:**
          - **Inherited:** All
          - bash: echo mine >&2        # runs AFTER the parent's preenter hooks

  It works on its own: you do not also need the stage-level flag.
* Without a marker an authored block REPLACES the parent's list for that field.

So a subclass can extend a parent's hooks by adding the marker, and can take a
field over completely by simply authoring it.

Context compression
-------------------

Long-horizon runs compress their context in one of two ways: a class-level Snap
policy (`Snap Handler` / `Snap Threshold` / `Snap To`) whose detached handler
produces the summary, or the built-in L2 reflection when no handler exists. Both
rebuild the context from three parts, in order:

1. the freshly built system prompt (the MEMORY INDEX is re-read),
2. a compact summary of the prior conversation (asked to fit in fewer than 100
   lines, hard-capped at 100), with sections GOAL / KEY LEARNINGS / DESIGN &
   ARCHITECTURE DECISIONS / PENDING / KEY FILES / ERRORS & LESSONS,
3. the most recent ReAct steps - a step is one tool_call-result pair.

`Compression Keep Steps` sets how many recent ReAct steps survive a compression
(the default is 10). It is a class-level scalar and inherits like the other Snap
fields:

      - **Compression Keep Steps:** 10

The retained steps are NOT exempt from the L1 rule: an oversized TOOL RESULT
inside the retained window is still replaced by the standard compressed
placeholder.

Where classes come from
-----------------------

A workspace's `.yaha/AgentClass.md` is the primary class file, and it may pull in
more text with `<!--include path="..." -->` (the include path resolves relative
to the file that contains the line). An include is MACRO PROCESSING: the included
text is inlined into the document BEFORE it is parsed, so the two files become ONE
document that shares one macro tree.

Classes can also come from EXTRA files named on the command line with
`-I`/`--include`. Those are handled DIFFERENTLY, on purpose. Each file is its own
DOCUMENT: it is loaded and macro-expanded on its own (its own macro store, its own
`<!--include-->`s resolved relative to itself) and parsed into its own class map,
and only then are the class maps MERGED - the primary first, each `-I` file after
it, in the order given. Inheritance is resolved once over the merged map, so a
class in an extra file can inherit from the workspace's classes and from classes
in earlier `-I` files.

When the same class name is defined more than once - twice inside one document, or
by two different documents - the LAST definition wins WHOLESALE: there is no
field-level merge across documents. The clash is not silent: it is REPORTED, with
the class name, how many times it was defined and by which files. A class whose
`Inherited:` names a class that does not exist is reported too (it would otherwise
quietly become a root class).

Because each `-I` file is its own document, macros stay per-file for it: an extra
file must define the macros it uses, and a file that needs the workspace's macro
tree should be installed with `<!--include-->` instead.

Check any of this without running an agent:

```bash
yaha --compile                 # the workspace's own classes
yaha --compile -I extra.md     # ...plus one or more extra class files
```

`--compile` loads everything and reports. ERRORS (exit non-zero): no classes were
loaded, a class names a parent that is not defined, a Wait Event has no producer.
WARNINGS (exit 0): a class name defined more than once, a model that `Model.md`
does not define, a class with an empty `Code`.

Runtime interaction
-------------------

A failing `preexit (required)` in front of `set_event_success` / `set_event_loop`
means the event is never signalled. If another agent was waiting on it the run
ends through the multi-agency deadlock detector, and that diagnostic names the
agent, the hook (`Preamble/preexit[1]`) and the failure - not just the parked
peer. This is deliberate: a gate that cannot pass must not be able to release a
peer.


Class identity (SOUL.md)
------------------------

A class's `System Prompt` states its ROLE: the protocol, its constraints, its
stage chain. Its IDENTITY - name, gender, tone of voice - lives in the
workspace `SOUL.md` instead.

`<workspace>/.yaha/SOUL.md` may carry a DEFAULT identity (everything before the
first `## ` heading) plus one section per class:

    # SOUL

    You are {agent_name}, a helpful and efficient agent.

    ## ChatAgent
    You are Mina, a warm, curious woman...

A class is matched to a section by its NAME, then by its short **Code**
(case-insensitively). A class with no section uses the DEFAULT identity, and a
file with no `## ` heading at all is the identity of EVERY class - which is why
workspaces that predate the feature are unaffected.

Author guide: `yaha/docs/soul.md`.

Late binding of events
======================

Every event name above can be written literally, or LATE-BOUND through a
placeholder. A placeholder is an event name beginning with `@`, and it is
resolved at runtime by its own tool:

    bind_tool(placeholder, real_event)

Rules:

- the FIRST argument must carry the `@` prefix (for example `@gate`);
- the SECOND argument must be a REAL event name. `bind_tool('@a', '@b')` is
  refused: chaining placeholders has no defined resolution order;
- the binding is RUN-GLOBAL across the multi-agency runtime: every agent on
  that run (the root and its flat `spawn_parallel_agent` peers) resolves the
  same mapping, so one actor can bind a gate another actor is already waiting
  on. A nested `yaha_spawn_agent` sub-agent has no runtime, so it can neither
  bind nor wait;
- the mapping is strictly 1:1, and REBINDING IS ALLOWED - the LAST bind wins, so
  re-binding identically is a harmless no-op. Bind a placeholder BEFORE any
  actor waits on or signals it, and do NOT re-point one that is already in use:
  a signal latched under the old target is not moved by a rebind, so a later
  waiter would resolve the NEW target and park even though the handshake already
  happened;
- malformed arguments come back as an ordinary `Error: ...` tool result and do
  NOT end the stage.

A BOUND placeholder is legal in both positions:

- a stage's **Wait Event:** `@name`;
- the event terminators: `set_event_success('', '@name')`, `set_event_loop` and
  `set_event_jump`.

An UNBOUND placeholder is an ERROR, not a park. Nothing can ever satisfy it, so
the stage is not entered (wait side) or does not complete (signal side), and the
chain continues at that stage's **Fail Target** exactly as a `fail_tool` failure
would, with Aftermath as the terminal fallback. The message names the
placeholder - so bind it before the wait is reached.

Startup validation with placeholders
------------------------------------

`validate_wait_events` still refuses to start when a Wait Event has no known
producer, but two cases are exempt because a binding can supply the name:

- a Wait Event that is itself a placeholder;
- any Wait Event, when SOME prompt in the workspace produces a placeholder.

A workspace that never mentions `@` therefore behaves exactly as before.

Exposing `bind_tool`
-------------------

`bind_tool` is declared in `tool-schema.md` and must be listed in a class's
**Toolset** to be usable. It is deliberately NOT auto-advertised, and NOT one of
the framework tools that bypass the Toolset, so the Agentset / toolset privilege
cap keeps applying to sub-agents: a child cannot bind gates unless its parent
could.

Remote agents (endpoints)
=========================

A remote agent talks to another agent through an ENDPOINT (EP). An EP is
addressed by a URL of the form `http(s)://host[:port]/path/version`, for example
`http://localhost/crew/reviewer/v1`. The trailing `/v1` is the protocol version
and is required. One EP plays BOTH roles under that one name: it is an event name
(a stage can wait on it) and a message queue.

There are two kinds of EP.

Service endpoint (SEP)
----------------------

The LISTENER side. A stage that waits on a service EP is what installs the
listener in the agent's own runtime:

    - **Wait Event:** http://127.0.0.1:8123/crew/reviewer/v1

When a remote client connects, YAHA creates a CONVERSATION endpoint (CEP) for
that client and prompts the stage with `[REMOTE CONNECTION]: <CEP>`. The stage
is expected to bind it and let a later stage do the waiting:

    bind_tool('@channel', 'http://127.0.0.1:8123/crew/reviewer/cep/1/v1')

and then

    - **Wait Event:** @channel

A service EP serves MANY clients at once, each with its own CEP.

Conversation endpoint (CEP)
---------------------------

One conversation. A CEP is a LEVEL signal over a FIFO queue: it is SET while
messages are queued and UNSET once the queue is drained. It is deliberately NOT
an ordinary event - a wait READS it and takes ONE message; there is no one-shot
pulse, no latch and no auto-reset. A message that arrives while nobody is waiting
is BUFFERED and delivered to the next wait, so an early message is never lost.

When the queue has a message the waiting stage is prompted with
`[REMOTE MESSAGE]: <text>`, and its reply is EXPLICIT: the stage calls
`send_tool` itself. That is what lets an operator gate replying per stage or per
Toolset.

The client side is symmetric: `connect_tool` hands back a CEP, and the client can
bind it and wait on it exactly as the server does. From the agent's point of view
it cannot tell which side initiated a message.

Tools
-----

- `connect_tool(service_ep)` - open a conversation; returns its CEP.
- `send_tool(cep, message)` - send one message. The cep argument may be a bound
  placeholder (`send_tool('@channel', ...)`).
- `close_tool(cep)` - close politely. Optional: YAHA reclaims every listener and
  conversation when the run ends.

All three are declared in `tool-schema.md` and must be listed in a class's
**Toolset** to be usable, so the Agentset / toolset privilege cap keeps applying
to sub-agents.

Transport and limits
--------------------

HTTP with long-poll and no in-memory tricks: every request is client-initiated,
a message the server initiates is queued and delivered when the client polls, and
the polling works only client -> server, as in any ordinary web application.

A service may require a shared bearer token, given as `?token=` in the URL or as
an `Authorization: Bearer` header; without a token anyone who can reach the port
may connect. `https://` works if you front the listener yourself - round 1 ships
no TLS setup, certificates or per-peer identity.

Each CEP queue is bounded and the number of concurrent conversations is capped.
Both limits REPORT an error to the caller instead of silently dropping messages.

A run stays alive while an agent waits on an endpoint that a peer could still
answer - the multi-agency deadlock detector does not fire just because nothing is
locally runnable. It is delayed, not disabled: if no endpoint answers within the
idle timeout (60 s by default), the run ends with a deadlock error naming the
endpoints it was waiting on, so a peer that never arrives fails loudly instead of
hanging.

Not in round 1: multi-message streaming beyond explicit sends, a relay process
for peers that cannot reach an agent directly, TLS/identity/ACLs, protocol v2 and
endpoint discovery.
