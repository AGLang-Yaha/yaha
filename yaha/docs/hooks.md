# Stage hooks

Four hook kinds can be declared as **stage properties** in `AgentClass.md`:
`preenter`, `pretool`, `posttool` and `preexit`. Each item is a shell command or
a python block. Python blocks run in one persistent kernel process per agent,
so a later block sees the variables an earlier block assigned.

## Trust model

Hooks are **code, not data**. They are read from `AgentClass.md` and run with the
operator's own privileges:

* a `bash` hook runs through `/bin/bash -c` **outside** the `AccessController`
  sandbox that `bash_tool` goes through. The workspace boundary does not apply to
  it: it can read and write whatever the operator can.
* a `python` hook runs in a separate, persistent subprocess with the same
  privileges and can import anything on the interpreter's path.

This is deliberate - whoever authors the class file also writes the stage prompts,
and a prompt can already ask for arbitrary tool calls, so a hook is the same trust
level expressed as code. The consequence is the one that matters operationally:
treat an untrusted `AgentClass.md` exactly as you would treat an untrusted shell
script.

Hook code never becomes part of the model's context: hook regions are blanked out
of the stage text at load time, so they cannot leak into a stage prompt.

```markdown
- **Coding:**
    - **Inherited:** All
    - **Allowed Tools:**
        - **Inherited:** All
        - read_file
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
    > Coding stage prompt
```

## Execution order

```
stage boundary -> Wait Event satisfied -> preenter -> build+append prompt
per ReAct turn : LLM -> pretool -> TOOL -> posttool -> LLM ...
exit           : pretool -> tool -> posttool -> preexit -> exit action
```

* `preenter` runs after a `Wait Event:` has been satisfied and before the stage
  prompt is appended, so a `(capture)` hook can inform the model before it
  plans.
* Hooks run on every tool call, including the framework terminators
  (`success_tool`, `loop_tool`, `fail_tool`, `set_event_success`, `set_event_loop`)
  and `spawn_parallel_agent`.
* `ask_user` is excluded from **`pretool`/`posttool`** only: hooking an open
  interactive prompt would interleave hook output into the question.
  `preenter`/`preexit` are stage-level and always run, so an `ask_user` that a
  class lists as an **Exit Tool** still goes through its `preexit` gate - a
  stage gate must not be bypassable by choosing a different exit tool.
* `exit_to` is a chain move like the event terminators: its jump/call happens at the **exit action**, after `posttool` and `preexit`, so a failing `(required)` `posttool`/`preexit` in front of it abandons the move and the stage FAILS THROUGH ITS FAIL TARGET, exactly like the event terminators.
* An event terminator (`set_event_success` / `set_event_loop`) signals its event at
  exactly one point: the **exit action**, after `preexit`. This holds whether or
  not the stage declares a `preexit` hook, so a `posttool` hook always observes
  the event as still unset. A failing `(required)` `posttool` or `preexit`
  therefore still abandons the signal (see the multi-agency section below).
* Hooks re-run on every ReAct iteration and on each `loop_tool` stage re-entry.
* Hooks never run while an agent is parked on a `Wait Event`.

## Items and modifiers

```
- bash [(required)][, (capture)]: <shell command>
- python [(required)][, (capture)]:
    ```
    <python>
    ```
```

Modifiers are optional, case-insensitive and order-free. A malformed hook block
(unknown modifier, a python item with no fenced block, an unterminated fence, an
empty bash command, a hook item that is neither `bash` nor `python`) raises
`HookSpecError` at load time: a silently ignored hook would leave an operator
believing a gate is armed when it is not.

* **Without `(required)`** a failing hook is reported in the TUI and written to
  the session error log (`.yaha/logs/<session>/error.log`, `category=hook`) and
  the run continues.
* **With `(required)`** the failure becomes a stage failure.
* **With `(capture)`** the hook's output is injected into the conversation as an
  observation attributed to the hook, e.g.

  ```
  [HOOK Coding/preenter[1]]
  <output>
  ```

  It is a user-side observation, never a fake assistant turn, and it is capped
  at 4 KB with an explicit `[...truncated]` tail. Without `(capture)` the output
  goes to the TUI and the log only.

A `bash` command may continue on deeper indented lines, and a YAML-style **literal
block scalar** header as the whole inline text works as expected - the indicator
is layout, never part of the command:

```markdown
- **preexit:**
    - bash (required): |
        set -eu
        test -f out.txt
```

The folded form (`>`) and explicit indentation indicators (`|2`) are rejected
with `HookSpecError`: this grammar does not implement folding, and silently
keeping the indicator would hand `/bin/bash` a command it cannot parse.

## Failure routing

| Hook | `(required)` failure | without `(required)` |
|---|---|---|
| `preenter` | stage is not entered; routed to the stage's `Fail Target` (default Aftermath). Aftermath is a built-in stage: undeclared, it has no prompt and the chain terminates immediately with all final answers collected so far (never a fall-back to the last stage). Aborts if the resolved target is the stage itself, and after 5 re-routings in one chain pass. | logged; stage proceeds |
| `pretool` | the tool is **not** executed; the agent receives a normal failed tool result | logged; tool runs |
| `posttool` | stage failure (the tool already ran; no undo) | logged |
| `preexit` | exit action **not** performed; for `set_event_success`/`set_event_loop` the event is **not** signalled | logged; exit proceeds |

Fail-fast: a hook list stops at the first failing `(required)` hook, because
later hooks must not run against an already-invalid stage.

## bash hooks

Run with `/bin/bash -c` in the workspace directory with the inherited
environment plus:

```
YAHA_AGENT  YAHA_AGENT_CLASS  YAHA_AGENT_ID  YAHA_STAGE  YAHA_TURN
YAHA_WORKSPACE  YAHA_SESSION_DIR  YAHA_TOOL  YAHA_TOOL_ARGS
```

stdout and stderr are captured; a non-zero exit code is the failure.

## python hooks and the kernel

* One persistent kernel subprocess per agent, started lazily on the first python
  hook, alive across every stage and every `loop_tool` loop, terminated when the
  agent dies. Each `spawn_parallel_agent` peer gets its own; nothing is shared.
* `print` is captured per block. `emit(value)` is always available for explicit
  or structured output.
* An exception is a hook failure and the namespace is **not** rolled back: a
  failed block keeps whatever it already mutated (Jupyter-like).
* Per-block timeout, in precedence order: `YAHA_HOOK_TIMEOUT` (process
  environment) > `- **Hook Timeout:** <seconds>` (class, inherited) > the 300 s
  default. `0` disables the timeout entirely, and an empty or unparsable
  `YAHA_HOOK_TIMEOUT` is ignored so the class value applies (it is never fatal).
  The same value is the `subprocess` timeout of a bash hook. On timeout the
  python kernel is killed and restarted with an EMPTY namespace, and the block
  counts as failed.
* If the kernel dies unexpectedly, the next python hook starts a fresh one; the
  reset is logged and reported in the hook output.
* No TTY is involved, so hooks behave identically in unattended runs.

## The `yaha` object (read-only)

Pre-bound in every python hook:

```
yaha.agent.name / agent_id / turn / attempt / message_count / model / provider
yaha.agent.agent_class.short_name / name / lineage / inherits(name) / stages
yaha.agent.kernel.pid / running / resets / last_error
yaha.stage.name / index / chain_len / tools.allowed|required|exit
yaha.stage.wait_event / hooks
yaha.hook.name / kind / required / capture / index / label
yaha.tool.name / args / ok / error / duration_ms / stage_attempt
yaha.runtime.live / ready / waiting / events / max_agents / active / blocked_signals
yaha.workspace.root / session_dir / log_dir / cwd
yaha.log.error_log_path / agent_log_path
```

`yaha.hook` exposes a closed view: `name` is an alias of `label` (the label shown
in hook output and diagnostics), and the hook's own source text is **not**
exposed - a hook has no reason to read itself, so the readable surface stays
minimal.

`yaha.tool` is bound **only** inside `pretool`/`posttool` (it is `None` in
`preenter`/`preexit`). Inside a `pretool` hook `ok`, `error` and `duration_ms`
are necessarily `None`: the call has not happened yet. They are populated in
`posttool`, where `ok` is `False` and `error` carries the failure text for a
failed tool. Attribute assignment raises `YahaReadOnlyError`.

`posttool`'s `ok` is derived from the tool-result **string convention**: a tool
reports failure by returning text that starts with `Error`/`ERROR`
(case-insensitive, leading whitespace ignored). That is the only success/failure
channel the tool layer exposes, so a custom tool that reports failure with some
other wording is seen as `ok=True`; a hook that must be exact can inspect
`yaha.tool.name`/`args` instead. (The agent's own stage control keeps its stricter
existing check, so this observation never changes whether a stage completes.)

Because the code runs out of process, the view is a snapshot materialised
immediately before the block runs: values are current as of block start, and
nothing can change while the hook runs because the agent owns execution and is
blocked inside it. A live-proxy `get` channel, and a controlled mutation API
(`set_event`, `inject`, `kill`), are deliberately left to a later patch.

## Inheritance

The rule is uniform - `Inherited: All` keeps and appends, anything else
replaces:

* A **stage-level** `- **Inherited:** All` inherits every field the child did
  not author: the prompt (concatenated), scalars (`Max Turns`, ...), the tool
  lists and the hooks.
* A **per-block** `- **Inherited:** All`, written inside a tool list or a hook
  block, keeps the parent's entries and appends this stage's after them
  (ancestor-first). It is self-sufficient: the stage-level flag is not needed.
* Without a marker, an authored block **replaces** the parent's list.

So a subclass extends a parent's hooks with the marker and takes a field over
completely by simply authoring it. Hook labels are renumbered after the merge
(`preexit[1]`, `preexit[2]`, ...) so a diagnostic always points at the effective
list.

## Interaction with multi-agency events

An event terminator (`set_event_success` / `set_event_loop`) has its event
signalled at the exit action, after `posttool` and `preexit`. A failing
`(required)` `posttool` **or** `preexit` in front of such a terminator means the
event is never signalled. If a peer was waiting on it the run ends through the
runtime's deadlock detector, and that message names the agent, the hook
(`Preamble/preexit[1]`) and the failure - not merely the parked peer:

```
Multi-agency deadlock: no agent is runnable and no event can be set.
Waiting: cns waiting on 'go'.
Blocked signals: Preamble/preexit[1] (abcd1234) FAILED: bash hook exited with
code 1 -> the event 'go' was never set
```

This is deliberate: a gate that cannot pass must not be able to release a peer.

The blocked-signal list (`yaha.runtime.blocked_signals`) is scoped to signals that
**never happened**: once an event is actually signalled, the entries recorded for
it are dropped, so a handshake that failed in one round and succeeded in the next
cannot be blamed for a later, unrelated deadlock. Entries for events that were
never signalled survive, which is what keeps the diagnostic above honest.

## Not in this patch

Live-proxy DOM reads, Jupyter-style `Out[]` repr capture, hook interception or
tool-argument rewriting, hooks on `ask_user`, hooks on the blocking
`yaha_spawn_agent`, per-hook `Inherited` markers, and any mutation API.
