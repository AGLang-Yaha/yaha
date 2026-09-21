#!/usr/bin/env bash
# =============================================================================
# shell-copilot — AI shell error copilot hook (zsh + bash)
#
# Installs a hook into the interactive shell. When a command exits non-zero,
# the hook builds a structured error report and asks a YAHA agent (ShellCopilot)
# to diagnose the failure and suggest a fix.
#
# Usage:
#   source this file from ~/.zshrc or ~/.bashrc
#
# Configuration (set these BEFORE sourcing):
#   SHELL_COPILOT_ENABLED        Set to 0 to disable at runtime      (default 1)
#   SHELL_COPILOT_WORKSPACE      YAHA workspace dir                  (default $HOME/.shell-copilot)
#   SHELL_COPILOT_WORKSPACE_FALLBACK  Writable ws when default is OS-locked (default: project-local ./ws, else $HOME/.cache/shell-copilot/ws)
#   SHELL_COPILOT_AGENT          Agent class name or code            (default ShellCopilot)
#   SHELL_COPILOT_AGENT_FILE     Path to the agent class file        (default: next to this script)
#   SHELL_COPILOT_TIMEOUT        Seconds to wait for yaha            (default 90)
#   SHELL_COPILOT_MAX_HISTORY    Recent history lines in report      (default 5)
#   SHELL_COPILOT_SKIP               Comma list: plain first-word, prefix, or *glob* (default "")
#   SHELL_COPILOT_SKIP_EXIT_CODES    Comma list of exit codes to NEVER trigger on (default 130)
#   SHELL_COPILOT_SKIP_BENIGN        Skip known-benign non-zero exits (queries etc.) (default 1)
#   SHELL_COPILOT_COOLDOWN           Min seconds between invocations          (default 10)
#   SHELL_COPILOT_DEDUPE             Repeat same cmd+rc skip window in secs   (default 0 = off)
#   SHELL_COPILOT_DEBUG              Set 1 to print fire/skip decisions       (default 0)
#   SHELL_COPILOT_CAPTURE_STDERR     Set 1 to tee stderr into a log           (default 0)
#   SHELL_COPILOT_SMART_INVOKE       Catch bare "quoted phrase" -> one-pass ask (default 1)
#   SHELL_COPILOT_ASK_AGENT          One-pass ask agent class                   (default NoAgent)
#   SHELL_COPILOT_ASK_AGENT_FILE     Optional -I include for the ask agent      (default unset)
#   SHELL_COPILOT_ASK_TIMEOUT        Seconds to wait for the ask yaha           (default 300)
# =============================================================================
#
# Smart invoke: type a bare double-quoted phrase, e.g.   "hello there"
# and the hook calls YAHA NoAgent with the inner text as a one-pass prompt.
# =============================================================================

# --- config with defaults ----------------------------------------------------
: "${SHELL_COPILOT_ENABLED:=1}"
: "${SHELL_COPILOT_WORKSPACE:=$HOME/.shell-copilot}"
: "${SHELL_COPILOT_AGENT:=ShellCopilot}"
: "${SHELL_COPILOT_TIMEOUT:=90}"
: "${SHELL_COPILOT_MAX_HISTORY:=5}"
: "${SHELL_COPILOT_SKIP:=}"
: "${SHELL_COPILOT_SKIP_EXIT_CODES:=130}"
: "${SHELL_COPILOT_SKIP_BENIGN:=1}"
: "${SHELL_COPILOT_COOLDOWN:=10}"
: "${SHELL_COPILOT_DEDUPE:=0}"
: "${SHELL_COPILOT_DEBUG:=0}"
: "${SHELL_COPILOT_CAPTURE_STDERR:=0}"
: "${SHELL_COPILOT_SMART_INVOKE:=1}"
: "${SHELL_COPILOT_ASK_AGENT:=NoAgent}"
: "${SHELL_COPILOT_ASK_AGENT_FILE:=}"
: "${SHELL_COPILOT_ASK_TIMEOUT:=300}"

# Locate ShellCopilot.md next to this script (or use SHELL_COPILOT_AGENT_FILE)
if [ -z "${SHELL_COPILOT_AGENT_FILE:-}" ]; then
  if [ -n "${BASH_SOURCE:-}" ] && [ -n "${BASH_SOURCE[0]}" ]; then
    SHELL_COPILOT_AGENT_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ShellCopilot.md"
  elif [ -n "$ZSH_VERSION" ]; then
    SHELL_COPILOT_AGENT_FILE="$(cd "$(dirname "${(%):-%N}")" && pwd)/ShellCopilot.md"
  else
    SHELL_COPILOT_AGENT_FILE="$HOME/.shell-copilot/ShellCopilot.md"
  fi
  export SHELL_COPILOT_AGENT_FILE
fi

# --- state vars ---------------------------------------------------------------
_shell_copilot_last_cmd=""
_shell_copilot_worked=1      # zsh only: 0 means a real command ran since last prompt
_shell_copilot_last_fire=0       # epoch seconds of last invocation (cooldown)
_shell_copilot_last_fire_key="" # "cmd|rc" of last invocation (dedupe)

# --- optional stderr tee (off by default) -------------------------------------
if [ "$SHELL_COPILOT_CAPTURE_STDERR" = "1" ] && [ -t 2 ] && [ -z "$_shell_copilot_stderr_done" ]; then
  _shell_copilot_stderr_log="${SHELL_COPILOT_STDERR_LOG:-$HOME/.cache/shell-copilot/stderr.log}"
  mkdir -p "$(dirname "$_shell_copilot_stderr_log")"
  export _shell_copilot_stderr_log
  exec 2> >(while IFS= read -r line; do
    printf '%s\n' "$line" >> "$_shell_copilot_stderr_log"
    printf '%s\n' "$line" >&2
  done)
  _shell_copilot_stderr_done=1
fi

# --- clean output for display --------------------------------------------------
# The default model may leak its internal <reasoning>...</reasoning> block into
# the -o output file, and may occasionally wrap the whole answer in a code fence.
# Strip both before showing the suggestion above the prompt. Portable (perl w/ awk fallback).
_shell_copilot_clean_output() {
  local f="$1" tmp
  tmp="${f}.clean"
  if command -v perl >/dev/null 2>&1; then
    perl -0777 -pe '
      s{^---\s+[^\r\n]*?---\s*$}{}gm;
      s{\074reasoning\076.*?\074/reasoning\076}{}gs;
      s{^\s*```[^\n]*\n(.*?)\n\s*```\s*$}{$1}s;
      s{^\s+}{};
      s{\s+$}{\n};
    ' "$f" > "$tmp"
  else
    awk '/^---[[:space:]]+[^-]+---[[:space:]]*$/ {next} {print}' "$f" \n      | awk '/[<]reasoning[>]/{inb=1} !inb{print} /[<]\/reasoning[>]/{inb=0}' > "$tmp"
  fi
  if [ -s "$tmp" ]; then
    mv "$tmp" "$f"
  else
    rm -f "$tmp"
  fi
}

# --- workspace health: probe real writability, fall back to a writable ws -----
# The configured YAHA workspace may live inside a protected path (e.g. an
# Obsidian vault on macOS) where `.yaha/logs` cannot be created. Detect that
# at runtime and fall back to a fresh writable workspace.
_shell_copilot_ensure_workspace() {
  # cached healthy workspace for this shell session
  if [ -n "${_shell_copilot_ws:-}" ]; then
    return 0
  fi

  local ws="${SHELL_COPILOT_WORKSPACE:-$HOME/.shell-copilot}"
  local fallback="${SHELL_COPILOT_WORKSPACE_FALLBACK:-}"
  if [ -z "$fallback" ]; then
    local hook_dir="$(dirname "$SHELL_COPILOT_AGENT_FILE" 2>/dev/null)"
    if [ -n "$hook_dir" ] && mkdir -p "$hook_dir/ws" 2>/dev/null; then
      fallback="$hook_dir/ws"
    else
      fallback="${XDG_CACHE_HOME:-$HOME/.cache}/shell-copilot/ws"
    fi
  fi
  local probe="$ws/.yaha/logs/.probe-$$"; rm -rf "$probe"

  if mkdir -p "$probe" 2>/dev/null && touch "$probe/w" 2>/dev/null; then
    rm -rf "$probe"
    _shell_copilot_ws="$ws"
  else
    rm -rf "$probe"
    # Verify the chosen fallback is actually writable before committing to it.
    local fprobe="$fallback/.yaha/logs/.probe-$$"; rm -rf "$fprobe"
    if ! mkdir -p "$fprobe" 2>/dev/null || ! touch "$fprobe/w" 2>/dev/null; then
      rm -rf "$fprobe"
      echo "⚠️  shell-copilot: neither workspace ($ws) nor fallback ($fallback) is writable; copilot disabled for this session" >&2
      _shell_copilot_ws=""
      return 1
    fi
    rm -rf "$fprobe"
    echo "⚠️  shell-copilot: workspace not writable ($ws), using $fallback" >&2
    ws="$fallback"
    if [ ! -f "$ws/.yaha/AgentClass.md" ]; then
      mkdir -p "$ws"
      echo "⚡ shell-copilot: initializing YAHA workspace $ws ..." >&2
      ( cd "$ws" && yaha --init >/dev/null 2>&1 ) || true
    fi
    _shell_copilot_ws="$ws"
  fi
}

# --- the copilot invocation ---------------------------------------------------
_shell_copilot_fire() {
  local cmd="$1" exit_code="$2"
  # record for cooldown + dedupe
  _shell_copilot_last_fire="$(date +%s 2>/dev/null || echo 0)"
  _shell_copilot_last_fire_key="${cmd}|${exit_code}"

  local ws agent agent_file tmp prompt_file out_file log_file
  _shell_copilot_ensure_workspace || return 0
  ws="$_shell_copilot_ws"
  agent="$SHELL_COPILOT_AGENT"
  agent_file="$SHELL_COPILOT_AGENT_FILE"
  tmp="${TMPDIR:-/tmp}"
  prompt_file="$tmp/shell-copilot-prompt-$$.md"
  out_file="$tmp/shell-copilot-out-$$.md"
  log_file="$tmp/shell-copilot-yaha-$$.log"

  # Build the error report
  {
    echo "A shell command just failed. Diagnose the error and suggest a fix."
    echo
    echo "## Error report"
    echo "- **Command:** $cmd"
    echo "- **Exit code:** $exit_code"
    echo "- **Working directory:** $(pwd)"
    echo "- **Shell:** ${SHELL:-unknown}"
    echo "- **OS:** $(uname -srm 2>/dev/null)"
    if command -v sw_vers >/dev/null 2>&1; then
      sw_vers 2>/dev/null | sed 's/^/  /'
    fi
    if [ -f /etc/os-release ]; then
      echo "- **Distro:** $(grep '^PRETTY_NAME=' /etc/os-release | cut -d= -f2- | tr -d '"') "
    fi
    if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      echo "- **Git branch:** $(git branch --show-current 2>/dev/null)"
    fi
    echo "- **Recent history:**"
    fc -l -"$SHELL_COPILOT_MAX_HISTORY" 2>/dev/null | sed 's/^/  /'
    if [ -n "$_shell_copilot_stderr_log" ] && [ -f "$_shell_copilot_stderr_log" ]; then
      echo "- **Recent stderr (last 10 lines):**"
      tail -n 10 "$_shell_copilot_stderr_log" | sed 's/^/  /'
    fi
  } > "$prompt_file"

  printf '\nshell-copilot: analyzing ...\n' >&2
  (
    yaha --yes -f -w "$ws" -a "$agent" -I "$agent_file" -F "$prompt_file" -o "$out_file" </dev/null >"$log_file" 2>&1
  ) &
  local ypid=$!
  ( sleep "$SHELL_COPILOT_TIMEOUT" && kill -9 "$ypid" 2>/dev/null ) >/dev/null 2>&1 &
  local wpid=$!
  wait "$ypid" 2>/dev/null
  local rc=$?
  kill "$wpid" 2>/dev/null
  wait "$wpid" 2>/dev/null  # reap watchdog quietly to avoid `Terminated` job noise

  if [ -s "$out_file" ]; then
    _shell_copilot_clean_output "$out_file"
  fi
  if [ -s "$out_file" ]; then
    printf '\n──────────────────────────────────────────────\n' >&2
    cat "$out_file" >&2
    printf '\n──────────────────────────────────────────────\n' >&2
  else
    printf '\n⚠️  shell-copilot: no suggestion returned (rc=%s). yaha log tail:\n' "$rc" >&2
    tail -n 5 "$log_file" 2>/dev/null | sed 's/^/    /' >&2
  fi

  rm -f "$prompt_file" "$out_file" "$log_file"
}

# --- epoch helper (portable; gdate on macOS if `date` lacks %s) -----------------
_shell_copilot_epoch_now() {
  local e
  e=$(date +%s 2>/dev/null) || e=$(gdate +%s 2>/dev/null) || e=0
  printf '%s' "$e"
}

# --- smart ask: catch a quoted phrase ------------------------------------------
# If the command line is wrapped in double quotes (starts and ends with '"') and the
# text inside is NOT a real command, ask YAHA NoAgent with the unquoted text as a
# one-pass prompt. This handles both simple asks ("hello there") and questions that
# contain nested shell code/quotes ("explain this: wait "$pid" & wait"). Real
# commands keep normal behavior ("ls" runs ls, "git status" runs git status).
_shell_copilot_should_ask() {
  [ "${SHELL_COPILOT_SMART_INVOKE:-1}" = "1" ] || return 1
  local cmd="$1" content
  case "$cmd" in
    '"'*'"') ;;
    *) return 1 ;;
  esac
  # We deliberately do NOT reject inner double quotes here: users often ask about
  # shell syntax that itself contains quotes (e.g. "how do I use wait "$pid"?").
  # A command such as "a" "b" is shell-ambiguous, but if its first word is not a
  # real command the line cannot run anyway; treating it as an ask is more useful.
  content=$(printf '%s' "$cmd" | tr -d '"')
  [ -n "$content" ] || return 1
  # content that looks like a real command must run normally, not be asked
  if command -v "${content%% *}" >/dev/null 2>&1; then
    return 1
  fi
  return 0
}

# Ask YAHA NoAgent with the double-quoted phrase as a one-pass prompt.
# Non-interactive: -o OUT + --yes + stdin from /dev/null; display only the
# cleaned final answer above the next prompt.
_shell_copilot_ask() {
  [ "${SHELL_COPILOT_SMART_INVOKE:-1}" = "1" ] || return 0
  local prompt="$1"
  [ -n "${prompt// /}" ] || return 0

  local ws agent tmp prompt_file out_file log_file
  _shell_copilot_ensure_workspace || return 0
  ws="$_shell_copilot_ws"
  agent="${SHELL_COPILOT_ASK_AGENT:-NoAgent}"
  tmp="${TMPDIR:-/tmp}"
  prompt_file="$tmp/shell-copilot-ask-prompt-$$.md"
  out_file="$tmp/shell-copilot-ask-out-$$.md"
  log_file="$tmp/shell-copilot-ask-yaha-$$.log"

  # Build an ASK prompt (plain-language question), NOT an error-diagnosis report.
  # The quoted phrase is a general question to the agent, so frame it as such.
  {
    printf '%s\n' 'Do NOT treat it an error. Do NOT need a diagnosis.'
    printf '%s\n' 'Answer the following question directly. Keep the answer concise and actionable. If it involves shell/Unix commands, show a short concrete example.'
    printf '%s\n' ''
    printf '%s\n' "Question: $prompt"
  } > "$prompt_file"

  printf '\n⚡ yaha: %s\n' "$prompt" >&2
  (
    if [ -n "${SHELL_COPILOT_ASK_AGENT_FILE:-}" ]; then
      yaha --yes -f -w "$ws" -a "$agent" -I "$SHELL_COPILOT_ASK_AGENT_FILE" -F "$prompt_file" -o "$out_file" </dev/null >"$log_file" 2>&1
    else
      yaha --yes -f -w "$ws" -a "$agent" -F "$prompt_file" -o "$out_file" </dev/null >"$log_file" 2>&1
    fi
  ) &
  local ypid=$!
  ( sleep "${SHELL_COPILOT_ASK_TIMEOUT:-300}" && kill -9 "$ypid" 2>/dev/null ) >/dev/null 2>&1 &
  local wpid=$!

  local rc=0
  # Propagate user Ctrl-C to yaha + watchdog; guarantee prompt returns promptly.
  trap 'kill -9 "$ypid" 2>/dev/null; kill -9 "$wpid" 2>/dev/null; rc=130' INT
  wait "$ypid" 2>/dev/null
  local wr=$?
  trap - INT
  [ "$rc" -eq 130 ] || rc="$wr"

  kill "$wpid" 2>/dev/null; wait "$wpid" 2>/dev/null

  # Clean + display the final answer (like the error copilot)
  if [ -s "$out_file" ]; then
    _shell_copilot_clean_output "$out_file"
  fi
  if [ -s "$out_file" ]; then
    printf '\n──────────────────────────────────────────────\n' >&2
    cat "$out_file" >&2
    printf '\n──────────────────────────────────────────────\n' >&2
  else
    printf '\n⚠️  shell-copilot: no answer returned (rc=%s). yaha log tail:\n' "$rc" >&2
    tail -n 5 "$log_file" 2>/dev/null | sed 's/^/    /' >&2
  fi

  rm -f "$prompt_file" "$out_file" "$log_file"
}

# --- route a failed command: ask or diagnose -----------------------------------
# If a command-not-found line (126/127) looks like a quoted ask, answer it as an
# ask (interception may not have run, e.g. hook sourced without smart-invoke).
# Otherwise fall through to the normal fire/diagnose logic.
_shell_copilot_route_failed() {
  local cmd="$1" exit_code="$2"
  if [ "$exit_code" = "126" ] || [ "$exit_code" = "127" ]; then
    if _shell_copilot_should_ask "$cmd"; then
      local content
      content=$(printf '%s' "$cmd" | tr -d '"')
      _shell_copilot_ask "$content"
      return 0
    fi
  fi
  _shell_copilot_should_fire "$cmd" "$exit_code" || return 0
  _shell_copilot_fire "$cmd" "$exit_code"
}

# --- zsh hooks -----------------------------------------------------------------
_shell_copilot_preexec() {
  _shell_copilot_last_cmd="$1"
  _shell_copilot_worked=0
}

_shell_copilot_precmd_zsh() {
  local exit_code=$?
  [ "$SHELL_COPILOT_ENABLED" = "1" ] || return 0
  [ "$exit_code" -ne 0 ] || return 0
  [ "$_shell_copilot_worked" -eq 0 ] || return 0

  local cmd="$_shell_copilot_last_cmd"
  _shell_copilot_last_cmd=""
  _shell_copilot_worked=1
  [ -n "$cmd" ] || return 0

  _shell_copilot_route_failed "$cmd" "$exit_code"
}

# --- bash hooks ----------------------------------------------------------------
_shell_copilot_precmd_bash() {
  local exit_code=$?
  [ "$SHELL_COPILOT_ENABLED" = "1" ] || return 0
  [ "$exit_code" -ne 0 ] || return 0

  # bash 3.x smart-invoke fallback: the DEBUG trap already handled this line
  # (asked once and marked worked=1); do not re-ask on the resulting 127.
  if [ "${_shell_copilot_debug_trap_active:-0}" = "1" ]; then
    [ "$_shell_copilot_worked" -eq 0 ] || return 0
  fi

  local cmd
  cmd="$(fc -ln -1 2>/dev/null | sed 's/^[[:space:]]*//')"
  [ -n "$cmd" ] || return 0

  _shell_copilot_route_failed "$cmd" "$exit_code"
}

# --- trigger decision ---------------------------------------------------------------
# Decide whether a failed command should trigger the copilot. Returns 0 (fire)
# or 1 (skip). Dependency-free (shell builtins only) because it runs in the
# interactive shell hot path on every failed command.
_shell_copilot_debug() {
  [ "${SHELL_COPILOT_DEBUG:-0}" = "1" ] && echo "shell-copilot: $*" >&2
}

_shell_copilot_in_list() {
  local list="$1" val="$2"
  case ",${list}," in
    *",${val},"*) return 0 ;;
  esac
  return 1
}

# Extended skip-list matcher: comma-separated items.
#   plainword  -> match first word of command (backward compatible)
#   has space  -> prefix match on the whole command line
#   contains * -> shell glob match on the whole command line
_shell_copilot_skip_list_match() {
  local cmd="$1" list="$2" item fw
  fw="${cmd%% *}"
  fw="${fw%%|*}"
  while [ -n "$list" ]; do
    item="${list%%,*}"
    if [ "$item" = "$list" ]; then
      list=""
    else
      list="${list#*,}"
    fi
    [ -z "$item" ] && continue
    case "$item" in
      *\**)
        if [ -n "$ZSH_VERSION" ]; then
          case "$cmd" in
            $~item) return 0 ;;
          esac
        else
          case "$cmd" in
            $item) return 0 ;;
          esac
        fi
        ;;
      *" "*)
        case "$cmd" in
          "$item"*) return 0 ;;
        esac
        ;;
      *)
        [ "$fw" = "$item" ] && return 0
        ;;
    esac
  done
  return 1
}

# The main gate. Layers:
#   1. exit-code hard skips (SHELL_COPILOT_SKIP_EXIT_CODES)
#   2. benign heuristics for query/status commands (SHELL_COPILOT_SKIP_BENIGN)
#   3. user skip list (SHELL_COPILOT_SKIP)
#   4. cooldown (SHELL_COPILOT_COOLDOWN)
#   5. dedupe (SHELL_COPILOT_DEDUPE)
_shell_copilot_should_fire() {
  local cmd="$1" rc="$2" first last now
  [ -n "$cmd" ] || return 1

  # 1. exit-code hard skip
  if _shell_copilot_in_list "${SHELL_COPILOT_SKIP_EXIT_CODES:-130}" "$rc"; then
    _shell_copilot_debug "skip: exit code $rc in SHELL_COPILOT_SKIP_EXIT_CODES"
    return 1
  fi

  # 2. benign heuristics
  if [ "${SHELL_COPILOT_SKIP_BENIGN:-1}" = "1" ]; then
    first="${cmd%% *}"
    case "$first" in
      # grep/ripgrep/ack family: rc=1 means 'no match' (normal), rc=2 = real error
      grep|egrep|fgrep|rg|ack|ag)
        if [ "$rc" = "1" ]; then
          _shell_copilot_debug "skip: $first found nothing (rc=1)"
          return 1
        fi
        ;;
      # diff/comm/cmp: rc=1 means 'inputs differ' (normal output), rc=2 = trouble
      diff|comm|cmp)
        if [ "$rc" = "1" ]; then
          _shell_copilot_debug "skip: $first reports differences (rc=1)"
          return 1
        fi
        ;;
      # lookup commands: rc=1 means 'not found'
      which|whereis|where|whence|type|command|hash|locate)
        if [ "$rc" = "1" ]; then
          _shell_copilot_debug "skip: $first lookup found nothing (rc=1)"
          return 1
        fi
        ;;
      # boolean/conditional expressions: only rc=1 is 'false' (rc=2 = usage error)
      test|\[|\[\[|\(\(|false)
        if [ "$rc" = "1" ]; then
          _shell_copilot_debug "skip: intentional conditional/boolean ($first)"
          return 1
        fi
        ;;
    esac

    # git commands whose rc=1 is normal, not an error.
    case "$cmd" in
      "git diff --check"*) ;;  # whitespace errors SHOULD fire
      "git grep"*)
        if [ "$rc" = "1" ]; then
          _shell_copilot_debug "skip: $cmd no-match (rc=1)"
          return 1
        fi
        ;;
      "git diff"*|"git status"*)
        if [ "$rc" = "1" ]; then
          _shell_copilot_debug "skip: $cmd rc=1 is normal git output"
          return 1
        fi
        ;;
    esac

    # Pipelines exit with the LAST command's code; a trailing query command that
    # exits 1 usually just means "no match".
    case "$cmd" in
      *\|*)
        last="${cmd##*|}"
        # trim leading spaces
        while [ -n "$last" ] && [ "${last# }" != "$last" ]; do last="${last# }"; done
        case "$last" in
          grep*|egrep*|fgrep*|rg*|ack*|ag*|diff*|comm*|cmp*)
            if [ "$rc" = "1" ]; then
              _shell_copilot_debug "skip: pipeline last command $last no-match/diff (rc=1)"
              return 1
            fi
            ;;
        esac
        ;;
    esac
  fi

  # 3. user skip list
  if _shell_copilot_skip_list_match "$cmd" "${SHELL_COPILOT_SKIP:-}"; then
    _shell_copilot_debug "skip: $cmd matched SHELL_COPILOT_SKIP"
    return 1
  fi

  # 4. cooldown
  if [ "${SHELL_COPILOT_COOLDOWN:-10}" -gt 0 ] 2>/dev/null && [ "$_shell_copilot_last_fire" -gt 0 ]; then
    now=$(date +%s 2>/dev/null || echo 0)
    if [ "$now" -gt 0 ] && [ $(( now - _shell_copilot_last_fire )) -lt "${SHELL_COPILOT_COOLDOWN:-10}" ]; then
      _shell_copilot_debug "skip: cooldown (fired $((now - _shell_copilot_last_fire))s ago)"
      return 1
    fi
  fi

  # 5. dedupe: don't re-fire on the exact same cmd+rc within the window
  if [ "${SHELL_COPILOT_DEDUPE:-0}" -gt 0 ] 2>/dev/null && [ -n "$_shell_copilot_last_fire_key" ]; then
    if [ "${cmd}|${rc}" = "$_shell_copilot_last_fire_key" ]; then
      now=$(date +%s 2>/dev/null || echo 0)
      if [ "$now" -gt 0 ] && [ $(( now - _shell_copilot_last_fire )) -lt "${SHELL_COPILOT_DEDUPE:-0}" ]; then
        _shell_copilot_debug "skip: dedupe (same cmd+rc recently fired)"
        return 1
      fi
    fi
  fi

  return 0
}

# --- smart-invoke interception -------------------------------------------------------
# zsh: replace the accept-line widget so a bare double-quoted phrase is asked
# instead of executed. BUFFER is cleared so the phrase is NOT executed.
# bash: >= 4 uses command_not_found_handle (the quoted phrase fails command
# lookup, and the handler answers it silently with exit 0); 3.x falls back to
# a DEBUG trap (see _shell_copilot_bash_debug below).
_shell_copilot_accept_line() {
  local ask_rc=0
  if [ "${SHELL_COPILOT_SMART_INVOKE:-1}" = "1" ]; then
    local line="${BUFFER:-}"
    if _shell_copilot_should_ask "$line"; then
      local content
      content=$(printf '%s' "$line" | tr -d '"')
      BUFFER=""
      _shell_copilot_ask "$content"
      ask_rc=$?
      _shell_copilot_worked=1
      zle -M ""
      # Finish the editing session by accepting the (now empty) buffer so zsh
      # draws a fresh prompt line below the answer. Without this the line was
      # never accepted and the prompt only returns after a manual Enter.
      zle .accept-line
      return 0
    fi
  fi
  zle .accept-line
}

# bash 3.x fallback: DEBUG trap. Catches the quoted line before execution and
# asks once. Without shopt extdebug the `return 1` below cannot cancel the
# pending command, so the line still runs and exits 127 with a "command not
# found" message; the worked-gate in _shell_copilot_precmd_bash prevents the
# second ask that would otherwise follow.
_shell_copilot_bash_debug() {
  [ "$SHELL_COPILOT_ENABLED" = "1" ] || return 0
  [ "${SHELL_COPILOT_SMART_INVOKE:-1}" = "1" ] || return 0
  [ -z "$_shell_copilot_in_ask" ] || return 0
  _shell_copilot_worked=0  # a real top-level command is running
  local line
  line=$(history 1 2>/dev/null | sed 's/^[[:space:]]*[0-9]*[[:space:]]*//')
  _shell_copilot_should_ask "$line" || return 0
  local content
  content=$(printf '%s' "$line" | tr -d '"')
  _shell_copilot_in_ask=1
  _shell_copilot_ask "$content"
  local rc=$?
  unset _shell_copilot_in_ask
  _shell_copilot_worked=1
  return 1  # cancel the quoted line: only honored under `shopt -s extdebug`
}

# --- install -----------------------------------------------------------------------
if [ -n "$ZSH_VERSION" ]; then
  autoload -Uz add-zsh-hook 2>/dev/null || true
  add-zsh-hook preexec _shell_copilot_preexec 2>/dev/null || true
  add-zsh-hook precmd _shell_copilot_precmd_zsh 2>/dev/null || true
  # smart ask: bind the widget overwriting accept-line
  if [ "${SHELL_COPILOT_SMART_INVOKE:-1}" = "1" ]; then
    zle -N accept-line _shell_copilot_accept_line 2>/dev/null || true
  fi
  export SHELL_COPILOT_LOADED=1
  echo "shell-copilot: hook installed (zsh)" >&2
elif [ -n "$BASH_VERSION" ]; then
  PROMPT_COMMAND="_shell_copilot_precmd_bash${PROMPT_COMMAND:+; $PROMPT_COMMAND}"
  # smart ask
  if [ "${SHELL_COPILOT_SMART_INVOKE:-1}" = "1" ]; then
    if [ "${BASH_VERSINFO[0]:-0}" -ge 4 ] 2>/dev/null; then
      # bash >= 4: intercept via command_not_found_handle. Bash calls this
      # handler INSTEAD of printing "command not found" when lookup fails,
      # so a quoted ask is answered once, silently, and the line exits 0 —
      # the precmd hook then has nothing to re-trigger on.
      if [ -z "$_shell_copilot_cnf_installed" ]; then
        # Chain to any pre-existing handler (e.g. Ubuntu's command-not-found).
        if declare -F command_not_found_handle >/dev/null 2>&1; then
          eval "_shell_copilot_prev_cnf_handle() $(declare -f command_not_found_handle | sed 1d)"
        fi
        command_not_found_handle() {
          if [ "$SHELL_COPILOT_ENABLED" = "1" ] && [ "${SHELL_COPILOT_SMART_INVOKE:-1}" = "1" ]; then
            local line content
            line=$(fc -ln -1 2>/dev/null | sed 's/^[[:space:]]*//')
            if _shell_copilot_should_ask "$line"; then
              content=$(printf '%s' "$line" | tr -d '"')
              _shell_copilot_ask "$content"
              return 0
            fi
          fi
          _shell_copilot_cnf_default "$@"
        }
        _shell_copilot_cnf_default() {
          if declare -F _shell_copilot_prev_cnf_handle >/dev/null 2>&1; then
            _shell_copilot_prev_cnf_handle "$@"
            return $?
          fi
          printf 'bash: %s: command not found\n' "$1" >&2
          return 127
        }
        _shell_copilot_cnf_installed=1
      fi
      trap - DEBUG 2>/dev/null || true
    else
      # bash 3.x (macOS stock bash): DEBUG trap catches the line before
      # execution; precmd's worked-gate suppresses the follow-up 127 ask.
      trap - DEBUG 2>/dev/null || true
      trap '_shell_copilot_bash_debug' DEBUG 2>/dev/null || true
      _shell_copilot_debug_trap_active=1
    fi
  else
    trap - DEBUG 2>/dev/null || true
  fi
  export SHELL_COPILOT_LOADED=1
  echo "shell-copilot: hook installed (bash)" >&2
else
  echo "shell-copilot: unsupported shell (zsh and bash supported)" >&2
fi
