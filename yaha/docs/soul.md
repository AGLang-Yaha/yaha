# SOUL.md: Workspace and Per-Class Identity

`SOUL.md` is the identity document of a YAHA workspace. It holds the persona
text injected at the very top of every agent's system prompt, ahead of the
class's own `System Prompt`.

One file, one workspace
-----------------------

There is exactly ONE `<workspace>/.yaha/SOUL.md`. `YAHAEngine` reads it once at
startup and hands each agent the identity belonging to its class. The file may
hold two kinds of content:

- a **DEFAULT** identity: everything BEFORE the first `## ` heading;
- one or more **class sections**: a `## ` heading followed by that class's
  persona.

    # SOUL

    You are {agent_name}, a helpful and efficient agent.

    ## ChatAgent
    You are Mina, a warm, curious woman in her thirties...

    ## CodeAgent
    You are Kris, a blunt, precise man...

The sections shipped in `yaha/templates/SOUL.md` deliberately use headings that
name NO real class (`## ExampleClass`, `## AnotherClass`), so a freshly
initialised workspace is never silently re-identified; rename a heading to one
of your own classes to put it to work.

Which identity a class receives
-------------------------------

1. a section headed by the class NAME exactly as written in `AgentClass.md`;
2. otherwise a section headed by the class's short **Code** (for example
   `## Cha` for `ChatAgent`);
3. otherwise the DEFAULT identity;
4. a file with NO `## ` heading at all is the identity of EVERY class.

Matching is case-insensitive. If two headings differ only in case, the LAST one
wins. The chosen text is injected as-is apart from surrounding whitespace: its
`## Name` heading and interior lines survive verbatim, but the section's leading
and trailing blanks are trimmed.

If the file HAS sections but no DEFAULT preamble, a class with no matching
section receives an EMPTY soul - another class's persona is never substituted.
Keep a DEFAULT identity unless you are certain every class has a section.

Why per-class identity?
-----------------------

A YAHA run is multi-agent. One workspace can define many classes (`ChatAgent`,
`CodeAgent`, `WebSearch`, the Crew classes, ...), and they cooperate inside a
single run: as nested sub-agents started with `yaha_spawn_agent`, as flat peers
started with `spawn_parallel_agent`, or as a crew whose members synchronise
through named events. `docs/agentclass-authoring-guide.md` is the reference for that stage
machinery (Wait Events, the event terminators, `spawn_parallel_agent`).

All of those agents read the SAME `SOUL.md`. Without sections they would all
speak with one voice, which is wrong for a crew whose members have different
jobs. Class sections let one file describe many distinct personalities while
keeping a single place to edit them.

Backward compatibility
----------------------

A `SOUL.md` with no `## ` heading is used verbatim for every class, so a
workspace that predates this feature behaves exactly as before. `yaha --upgrade`
never overwrites `SOUL.md`, so your existing file is not rewritten - add
sections whenever you want them.

Roles belong elsewhere
----------------------

`SOUL.md` is for IDENTITY: name, gender, tone and style of conversation. The
operational role - what the agent must do, its protocol, its stage chain -
belongs in the class's `System Prompt` inside `AgentClass.md`. Keeping them
apart keeps both editable.

Writing a persona
-----------------

- Keep it short: a few sentences of voice beat a page of adjectives.
- Describe who the agent IS, not what it must do.
- Write class sections literally. `{agent_name}` is substituted ONCE, at
  `yaha --init [AGENT_NAME]`, over the whole file.
- The sections shipped in the template are examples: rename them to your own
  classes, rewrite the personas, or delete them.
