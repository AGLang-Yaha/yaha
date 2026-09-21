# Crew environment configuration

The Crew agents (`Crewlda`, `CrewCda`, `CrewRva`) do not hard-wire a workspace layout. They read
their environment from a project-level descriptor that the lead resolves ONCE and then treats as
fixed:

- `.crew/environment.md` - the descriptor (the ONLY place the environment is defined).
- `.crew/protocol.md` - the coordination contract between the peers.
- `<Exchange Folder>` - per descriptor: `<id>.patch`, `<id>.review.md`, `crew.status`.
- `<Merge Record Dir>` - per descriptor (default `.crew/merge/`): `<id>.merge.md`.

## Descriptor schema

A flat `- **Key:** value` bullet block, so it is greppable by both agents and tests. Keys, in
order:

`Name`, `Workflow` (`local-git` | `external-canonical`), `Main Repo`, `Canonical Root`, `Remote`,
`Base Branch`, `Worktree Root`, `Exchange Folder`, `Merge Record Dir`, `Code Folder`,
`Docs Folder`, `Plans Folder`, `Tests Folder`, `Test Command`, `VCS Backend`, `Git Dir`,
`Merge Target`, `Issue/PR Policy`, `Protocol`.

The coder's `Plans Folder` and `Docs Folder` override the inherited default plan location. The
coder must never touch the main or canonical repository; the lead owns it.

## Bootstrap rules (`Crewlda.Bootstrap`)

1. **Present** -> read `.crew/environment.md` and freeze it. Never rewrite and never switch it.
2. **Absent** -> derive it from the layout that actually exists and write it exactly once:
   - a git repo -> `local-git`;
   - an existing mirror/scaffold -> `external-canonical`;
   - a plain project layout -> adopt it as is.
3. **Empty project** -> scaffold `.crew/` plus `project/{code,docs,plans,tests,worktree}` and write
   the descriptor and the protocol.

Then write `.crew/protocol.md` if missing, make sure the exchange folder and the merge-record
directory exist, and write `crew.status` = `IN_PROGRESS` so a previous run's `DONE` can never be
read as this run's stop signal.

## The two workflows

**`local-git`** - the main git repository is the workspace or a folder inside it. The coder and the
reviewer work in a SEPARATE working copy (`Worktree Root`), never the main checkout. The lead
guards the repository and merges into `Base Branch` (plus a push or a PR only when `Issue/PR
Policy` allows it).

**`external-canonical`** - the canonical tree lives OUTSIDE the workspace and is read-only to the
agents. The coder fetches or mirrors it into `Worktree Root` and publishes a unified diff; the lead
writes a merge record and emits the exact host commands, because it cannot write to the canonical
tree itself.

## The gated commit (`Crewlda.Commit`, `Wait Event: commit_ready`)

The lead is the ONLY agent that touches the main or canonical repository, and only after the user
explicitly confirms. `Commit` reads the descriptor and the newest `<id>.patch` plus its
`<id>.review.md`, refuses to proceed when the verdict is `REQUEST_CHANGES` or there is no patch,
then `ask_user` presenting the patch id, the files it touches, the test evidence and the exact
action it intends to run. Only on approval does it act; it records the outcome in `Merge Record
Dir` as `<id>.merge.md`, and if the command is denied or fails it reports the EXACT command for the
user to run on the host instead of forcing anything.

Exit: mid-run -> `set_event_success('', 'commit_done')`, which wakes the parked coder; `crew.status` exactly
`DONE` (the final pass) -> `exit_to('Aftermath')`. The lead NEVER writes `crew.status`: writing
`DONE` while the coder is alive would strand it. `Rearm` immediately calls `exit_to('Commit')` so
the lead re-parks for the next request.

## The TUI input-lock guard (`CrewCda.CommitWait` + `Crewlda.Rearm`)

The runtime gives ONE global input lock per interactive prompt, and an interactive stage runs to
completion before another agent may take it. If the coder, after requesting a commit, looped
straight back to its interactive `Preamble`, it would hold that lock while the lead needs it for the
merge confirmation. So:

- the coder's chain is `Preamble -> Plan -> Approve -> Coding -> Summary -> CommitWait -> Aftermath`.
  `CommitWait` (`Wait Event: commit_done`, exit tool `loop_tool` ONLY) PARKS the coder and releases
  the lock; when woken it loops back to the interactive `Preamble`;
- the lead's chain is `Preamble -> Bootstrap -> Fan-out -> Commit -> Rearm -> Aftermath`, with
  `Rearm` (exit tool `exit_to` ONLY) re-parking the lead. No new tool is needed: `set_event_success` is
  the signal and `exit_to` closes the loop.

## Event map

| event | direction | producer | waiter |
|---|---|---|---|
| `code_ready` | coder -> reviewer | `CrewCda.Coding` (every round), `CrewCda.Aftermath` (shutdown) | `CrewRva.Review` |
| `review_done` | reviewer -> coder | `CrewRva.Review` | `CrewCda.Summary` |
| `commit_ready` | coder/reviewer -> lead | `CrewCda.Summary`, `CrewRva.Review` (on exactly `DONE`) | `Crewlda.Commit` |
| `commit_done` | lead -> coder | `Crewlda.Commit` | `CrewCda.CommitWait` |

A wait event with no producer aborts the run at startup, so every event above has a prompt-literal
producer. A `set_event` that finds nobody parked LATCHES and is consumed by the next waiter, so the
lead cannot lose the coder's wake-up even if it finishes its commit first.

## Sandbox `.git` limitation

`core/access.py::AccessController` emits a blanket `(deny file* (regex #"/\.git(/|$)") )` into
the sandbox profile, but then RE-ALLOWS every `.git` path INSIDE the workspace:

```
(deny  file* (regex #"/\.git(/|$)"))
(allow file* (regex #"^{workspace_root}/(.*/)?\.git(/|$)"))
```

Within the `file*` class SBPL is last-match-wins, so the re-allow decides. Reproduced in this
workspace (throwaway dir under `selfcoding/tmp/`):

- `git init` WORKS -> `Initialized empty Git repository in .../gitprobe/.git/` (rc=0);
- `git worktree add wt` WORKS -> `Preparing worktree (new branch 'wt')` (rc=0);
- `git rev-parse`, `git commit` and the rest of the porcelain work in any repository under the
  workspace, including the workspace root repo.

Outside the workspace the denial is real: `mkdir /Users/kuntan/code/claud/agents/<x>` fails with
`Operation not permitted`, and the unattended tier of `AccessController.authorize_bash` refuses every
non-whitelisted command that reaches outside the workspace with
`Access Denied: Command requires user approval.` - the canonical tree is exactly such a path.

So a `local-git` workflow - whose `Main Repo` is "the workspace or a folder inside it" - needs NO
workaround: drive it with ordinary `git` in `Worktree Root`.

The `Git Dir` key and the `GIT_DIR`/`GIT_WORK_TREE` workaround are for a host whose profile lacks the
re-allow rule, and for repositories OUTSIDE the workspace - and they only work if `GIT_DIR` itself is
**WRITABLE**: inside the workspace, or listed in `~/.yaha/security/fs_whitelist.md`. The profile puts
`(deny file-write* (subpath "/"))` before every other `file-write*` rule and `file-write*` is
last-match-wins, so a `GIT_DIR` outside the workspace is denied before the `.git`-name regex can
matter (`GIT_DIR=/Users/kuntan/code/claud/yaha-lite/tmp/<x> ... git init` -> `Operation not
permitted`). Verified working variant - the bookkeeping dir INSIDE the workspace, the tree anywhere:

```bash
GIT_DIR=<writable dir not named .git> GIT_WORK_TREE=<tree> git init
GIT_DIR=<writable dir not named .git> GIT_WORK_TREE=<tree> git add -A
GIT_DIR=<writable dir not named .git> GIT_WORK_TREE=<tree> git commit -m "message"
GIT_DIR=<writable dir not named .git> GIT_WORK_TREE=<tree> git status
```

That makes `init`/`add`/`commit`/`status`/`log` work there too. When NO VCS step is possible at all -
no writable metadata dir, no re-allow rule, a `.git` that cannot be named around - the lead must
degrade gracefully: report the EXACT host commands and record them in the merge record.

## Stop rule

The only clean stop is the user answering quit/stop/exit at the coder's interactive `Preamble`. That
routes to the coder's `Aftermath`, which writes `crew.status` = `DONE` and signals `code_ready` one
last time so the parked reviewer wakes, relays the finish to the lead with
`set_event_success('', 'commit_ready')` and exits. The lead then runs the final gate and `exit_to`s its
`Aftermath`. `IN_PROGRESS`, any other text, or no marker at all always means a new round.
