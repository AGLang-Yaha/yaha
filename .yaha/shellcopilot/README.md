# shell-copilot

An embedded AI shell copilot for **zsh** and **bash** on macOS/Linux. It hooks into your interactive shell and:

1. **Diagnoses failed commands** — when a command exits non-zero, it builds a structured error report and asks a YAHA `ShellCopilot` agent for a concrete fix.
2. **Answers bare quoted phrases** — type a double-quoted question such as `"how do I rebase?"` and it runs a one-pass YAHA `NoAgent` ask (Smart Invoke).

It ships as an example YAHA tool in the `yaha-harness` package under `yaha/templates/shellcopilot/`.

---

## Requirements

- [yaha](https://github.com/cohen-tan/yaha-lite) installed and available on `PATH` (`pip install yaha-harness` or run from source)
- zsh or bash (macOS or Linux)

## Files

| File | Purpose |
| --- | --- |
| `copilot-hook.sh` | The shell hook. Source this from your shell rc file. |
| `ShellCopilot.md` | The YAHA agent class file used for the error-diagnosis agent. |
| `README.md` | This file. |

`copilot-hook.sh` looks for `ShellCopilot.md` next to itself automatically, so keep the files together.

---

## Installation (manual)

### Quick start — copy-paste one block

Run this from the directory that contains `copilot-hook.sh` and `ShellCopilot.md`.
It creates the install folder, copies the files, and appends a guarded sourcing
line to the correct rc file for your current shell:

```sh
# Quick start: run from the folder that contains copilot-hook.sh + ShellCopilot.md
INSTALL_DIR="${SHELL_COPILOT_INSTALL_DIR:-$HOME/.local/share/yaha/shell-copilot}"
mkdir -p "$INSTALL_DIR"
cp copilot-hook.sh ShellCopilot.md "$INSTALL_DIR/"

# Pick the rc file for the current shell
if [ -n "$ZSH_VERSION" ]; then
  RC_FILE="$HOME/.zshrc"
elif [ -n "$BASH_VERSION" ]; then
  RC_FILE="$HOME/.bashrc"
else
  RC_FILE=""
fi

if [ -n "$RC_FILE" ]; then
  LINE="[ -f '$INSTALL_DIR/copilot-hook.sh' ] && source '$INSTALL_DIR/copilot-hook.sh'"
  if [ -f "$RC_FILE" ] && grep -qF "$INSTALL_DIR/copilot-hook.sh" "$RC_FILE"; then
    echo "Already installed in $RC_FILE"
  else
    touch "$RC_FILE"
    printf '\n# yaha shell-copilot\n%s\n' "$LINE" >> "$RC_FILE"
    echo "Installed into $RC_FILE"
  fi
else
  echo "Unsupported shell — source $INSTALL_DIR/copilot-hook.sh manually." >&2
fi

echo "Start a new shell to activate, or run: source $INSTALL_DIR/copilot-hook.sh"
```

Notes:
- Change `INSTALL_DIR` to any stable location (e.g. `~/.config/yaha/shell-copilot`) if you prefer; the sourcing line uses the same path.
- The script is idempotent: running it twice will not append a duplicate sourcing line.
- `copilot-hook.sh` looks for `ShellCopilot.md` **next to itself**, so both files must be copied into `INSTALL_DIR`.

### Step-by-step

1. **Copy the files to a stable location**, e.g.:

   ```sh
   mkdir -p "$HOME/.local/share/yaha/shell-copilot"
   cp copilot-hook.sh ShellCopilot.md "$HOME/.local/share/yaha/shell-copilot/"
   ```

2. **Source the hook from your shell rc file.**

   For zsh (`~/.zshrc`):

   ```sh
   echo '[ -f "$HOME/.local/share/yaha/shell-copilot/copilot-hook.sh" ] && source "$HOME/.local/share/yaha/shell-copilot/copilot-hook.sh"' >> ~/.zshrc
   ```

   For bash (`~/.bashrc`):

   ```sh
   echo '[ -f "$HOME/.local/share/yaha/shell-copilot/copilot-hook.sh" ] && source "$HOME/.local/share/yaha/shell-copilot/copilot-hook.sh"' >> ~/.bashrc
   ```

3. **Start a new shell** (or `source ~/.zshrc` / `source ~/.bashrc`).

> **Tip:** To remove it later, delete the sourcing line you appended (plus the `# yaha shell-copilot` comment above it), then remove the directory `$HOME/.local/share/yaha/shell-copilot`.

---

## Usage

### 1. Error copilot

Just use your shell normally. When a command fails (non-zero exit), the hook intercepts the prompt and shows the ShellCopilot diagnosis + fix:

```
$ git push
fatal: The current branch has no upstream branch.

⚡ yaha: git push failed (exit 1)
──────────────────────────────────────────────
Diagnosis: The branch has no upstream remote set.
Fix:
  git push --set-upstream origin <branch>
──────────────────────────────────────────────
$ _
```

The hook presents the report, waits for the agent answer, displays it above the next prompt, then continues normally.

### 2. Smart invoke (ask anything with quotes)

Type a bare double-quoted phrase whose first word is **not** a real command:

```
$ "how do I squash the last 3 commits?"
⚡ yaha: how do I squash the last 3 commits?
──────────────────────────────────────────────
(one-pass NoAgent answer)
──────────────────────────────────────────────
$ _
```

Real commands keep normal behavior: `"ls"` still runs `ls`; `"git" status` still runs `git status`.

Set `SHELL_COPILOT_SMART_INVOKE=0` before sourcing the hook to disable the quoted-phrase ask.

---

## Configuration

Set these environment variables **before** sourcing `copilot-hook.sh`. All have defaults.

| Variable | Default | Description |
| --- | --- | --- |
| `SHELL_COPILOT_ENABLED` | `1` | Set `0` to disable at runtime. |
| `SHELL_COPILOT_WORKSPACE` | `$HOME/.shell-copilot` | YAHA workspace dir. |
| `SHELL_COPILOT_WORKSPACE_FALLBACK` | project-local `./ws`, else `$HOME/.cache/shell-copilot/ws` | Writable ws when the default workspace is OS-locked (e.g. macOS EPERM). |
| `SHELL_COPILOT_AGENT` | `ShellCopilot` | Agent class name or code. |
| `SHELL_COPILOT_AGENT_FILE` | next to this script | Path to the agent class `.md` file. |
| `SHELL_COPILOT_TIMEOUT` | `90` | Seconds to wait for the error-copilot yaha call. |
| `SHELL_COPILOT_ASK_TIMEOUT` | `300` | Seconds to wait for the Smart Invoke yaha call. |
| `SHELL_COPILOT_MAX_HISTORY` | `5` | Recent history lines included in the error report. |
| `SHELL_COPILOT_SKIP` | *(empty)* | Comma list to never trigger on: plain first-word, prefix, or `*glob*`. |
| `SHELL_COPILOT_SKIP_EXIT_CODES` | `130` | Comma list of exit codes that never trigger. |
| `SHELL_COPILOT_SKIP_BENIGN` | `1` | Skip known-benign non-zero exits (e.g. queries). |
| `SHELL_COPILOT_COOLDOWN` | `10` | Min seconds between invocations. |
| `SHELL_COPILOT_DEDUPE` | `0` | Suppress the same cmd+rc for this many seconds (0 = off). |
| `SHELL_COPILOT_DEBUG` | `0` | Set `1` to print fire/skip decisions. |
| `SHELL_COPILOT_CAPTURE_STDERR` | `0` | Set `1` to tee stderr into a log. |
| `SHELL_COPILOT_SMART_INVOKE` | `1` | Set `0` to disable the bare-quoted-phrase ask. |
| `SHELL_COPILOT_ASK_AGENT` | `NoAgent` | One-pass ask agent class (Smart Invoke). |
| `SHELL_COPILOT_ASK_AGENT_FILE` | *(unset)* | Optional `-I` include file for the ask agent. |

### Example: custom workspace + longer timeout

```sh
export SHELL_COPILOT_WORKSPACE="$HOME/.config/yaha/shell-copilot-ws"
export SHELL_COPILOT_TIMEOUT=120
export SHELL_COPILOT_AGENT_FILE="$HOME/.config/yaha/shell-copilot/ShellCopilot.md"
# then source copilot-hook.sh
```

---

## Anatomy: how this example YAHA tool works

This directory is a complete, body of an example YAHA tool:

1. **`copilot-hook.sh`** — the thin shell glue that detects an event in the interactive shell (failed command, or bare quoted phrase).
2. On trigger, it invokes a non-interactive YAHA call:
   ```sh
   yaha --yes -f -w "$SHELL_COPILOT_WORKSPACE" -a "$SHELL_COPILOT_AGENT" -I "$SHELL_COPILOT_AGENT_FILE" -F "$REPORT" -o "$OUT"
   ```
3. **`ShellCopilot.md`** — the agent personality: system prompt + response format for the fix.
4. **Result** — the hook reads the `-o` answer file and prints a cleaned answer above the next prompt.

Build your own YAHA tool by copying this skeleton: change the trigger in the hook and swap `ShellCopilot.md` for your agent's class file.

---

## Notes

- The hook is **read-only** with respect to your commands; it never modifies command history.
- Smart Invoke uses YAHA's `NoAgent` class by default. To use a custom lightweight agent, set `SHELL_COPILOT_ASK_AGENT` and optionally `SHELL_COPILOT_ASK_AGENT_FILE`.
- On bash ≥ 4, Smart Invoke hooks `command_not_found_handle`, so a quoted ask never prints a spurious `command not found` and is answered exactly once. Any pre-existing handler (e.g. Ubuntu's command-not-found suggestions) is preserved and chained for real typos. On bash 3.x (macOS stock bash), a DEBUG-trap fallback is used instead — there the failed quoted line may still print one `command not found` line, but it is only answered once.
- If yaha is slow or unavailable, the hook falls back silently to normal shell behavior (bounded by the timeout).