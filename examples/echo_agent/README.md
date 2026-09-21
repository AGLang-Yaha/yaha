# Echo agent demo - two yaha agents talking over a remote endpoint

This directory is a **workspace example**, not a script: it defines two ordinary
yaha agent classes that both inherit `BaseAgent`, and you run them the normal way
with the CLI. One is an echo SERVER that listens on an endpoint; the other is a
CLIENT that opens a conversation with it, asks YOU for a message, and shows the
reply.

* `echo_agent.md` - the two classes, `EchoServer` and `EchoClient`. This file is
the single source of truth: it is what you add to a workspace and what the tests
in `tests/test_echo_demo.py` execute.

## Install the classes into a workspace

In an initialized workspace (`yaha --init`), put the two classes where
`BaseAgent` can be seen, in ONE of these two ways:

**Append them to the workspace class file** (simplest):

```bash
cat /path/to/yaha/examples/echo_agent/echo_agent.md >> .yaha/AgentClass.md
```

**Or keep the file separate and include it** (one line, and inheritance still
resolves because includes are expanded before the file is parsed):

```
<!--include path="echo_agent.md" -->
```

The include path is resolved **relative to the file that contains the line** -
here `.yaha/AgentClass.md` - so copy `echo_agent.md` into `.yaha/` (or write a
path relative to it, e.g. `path="../../examples/echo_agent/echo_agent.md"`). A
path that does not resolve from there fails the load with
`MarkdownLoadError: ... file not found`. The `path=` part is optional:
`<!--include "echo_agent.md" -->` works too.

**Or load it for one run only**, without touching the workspace at all:

```bash
yaha -I /path/to/yaha/examples/echo_agent/echo_agent.md -a Ecl "talk to it"
```

The demo is TWO processes, so pass the SAME `-I` in BOTH terminals (or install
the classes once, as above) - the server process needs `Eco`, the client `Ecl`.

`-I` handles the file as its OWN document: it is parsed on its own and its class
map is then MERGED with the workspace's (a class of the same name defined later
wins, and the clash is reported), with inheritance resolved across both - so
`Ecl`/`Eco` pick up `BaseAgent` exactly as they do when installed. Macros stay
per-file, so an extra file must define its own (install with `<!--include-->` if it
needs the workspace's).

Verify an install without running anything:

```bash
yaha --compile -I /path/to/yaha/examples/echo_agent/echo_agent.md
```

Then confirm both classes are there:

```bash
yaha --list agents      # EchoServer (Eco) and EchoClient (Ecl)
```

## Run the demo (two terminals)

The classes inherit `BaseAgent`, so they run like any other agent. The model comes
from your workspace (`BaseAgent` uses `DefaultModel`, so set that in
`.yaha/Model.md`).

```bash
# terminal 1 - the listener
yaha -a Eco "serve the echo endpoint"

# terminal 2 - the peer (any host that can reach terminal 1)
yaha -a Ecl "talk to http://127.0.0.1:8123/echo/v1"
```

The client asks you for a message, sends it, and prints what comes back:

```
What should the echo server send back? (type quit to end the session)
> a purple elephant
... echo: a purple elephant
```

Type `quit` at the prompt to end the session: the client tells the server it is
done, the server closes the conversation, and both agents finish. Submitting
*repeated* empty input does the same - the framework re-asks a few times and then
reports `[no reply] (user submitted empty input repeatedly)`, which the client
treats as the stop word. A single empty line is not a stop signal.

## What the classes do

`EchoServer` waits on a **service endpoint**:

```
- **Wait Event:** http://127.0.0.1:8123/echo/v1
```

That wait is what installs the listener. When a peer connects, the runtime
creates a **conversation endpoint** (CEP) for that peer and prompts the stage with
its url; the server binds it to `@peer`. The `Echo` stage then waits on `@peer`,
and for every `[REMOTE MESSAGE]` it answers EXPLICITLY with
`send_tool('@peer', 'echo: <text>')`, then `loop_tool`s around for the next
message. `quit` is the stop word: the server closes the conversation and finishes
instead of echoing it.

`EchoClient` opens the conversation with `connect_tool`, binds the CEP it gets
back to `@reply`, and then, in its `Ask` stage, **asks the operator** with
`ask_user` and sends the operator's own words:

```
send_tool('@reply', '<the answer>')
```

`Receive` waits on `@reply`, shows the echo, and returns to `Ask`. Nothing is
hardcoded: the only literal payload the client can send is the placeholder for
your answer, plus the stop word.

## What inheriting `BaseAgent` gives them

Both classes extend `BaseAgent`, so they keep:

* its **System Prompt** (the worker protocol);
* its **Toolset**, extended with `connect_tool`, `send_tool`, `close_tool` and
  `bind_tool` (declared with the `- **Inherited:** All` marker, so the parent's
  tools are kept and these are appended);
* its **Agentset** and skill settings;
* its context-snap policy (`Snap Handler: Compression`), and therefore its
  detached `Compression` stage;
* `DefaultModel`, so the workspace decides which model actually runs.

The **prompt chain is this example's own**: it authors both `Preamble` and
`Aftermath`, plus its own working stages (`Echo` for the server; `Ask` and
`Receive` for the client), so no chain stage comes from the parent. What IS
inherited is the parent's System Prompt, its Toolset (extended with the endpoint
tools), its Agentset, its model, its snap policy and therefore its detached
`Compression` stage.

## The wire protocol

Every request is client-initiated; nothing is pushed. A peer needs no YAHA at
all - plain HTTP is enough:

| Request | Meaning |
| --- | --- |
| `POST <service-ep>` | Open a conversation; the reply carries `{"cep": "<url>"}`. |
| `POST <cep> {"message": ...}` | Put one message in the peer's inbox. |
| `GET <cep>?timeout=N` | Long-poll the peer's outbox; `204` when nothing arrived. |
| `DELETE <cep>` | Close the conversation politely. |

An optional shared token goes in the URL as `?token=` or as
`Authorization: Bearer`; without one, anyone who can reach the port may connect.

## Limits worth knowing

* A conversation's queue is bounded and the number of conversations is capped:
  both limits REPORT an error instead of silently dropping a message.
* A run stays alive while an agent waits on an endpoint a peer could still
  answer - the deadlock detector is DELAYED, not disabled. If nothing answers
  within the idle timeout (60 s by default), the run ends with a deadlock error
  naming the endpoint it waited on.
* Bind a conversation (or any placeholder) BEFORE a stage waits on it: an unbound
  placeholder fails the stage that uses it.
* Each side needs its own run (two processes here, or two runtimes): a
  conversation endpoint name belongs to the side that registered it.
* Round 1 ships HTTP only, no TLS setup, no per-peer identity, no relay for a
  peer that cannot reach the agent directly, and no protocol v2 or endpoint
  discovery.

See `yaha/docs/agentclass-authoring-guide.md` (section "Remote agents (endpoints)") for the full
reference, and `tests/test_echo_demo.py` for the executable proof: the round trip
runs through the real scheduler with the operator's message, plus a raw-HTTP peer
that proves no YAHA is needed on the other side.
