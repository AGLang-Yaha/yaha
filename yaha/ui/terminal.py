# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

from rich.console import Console
from rich.panel import Panel
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text
from rich.box import ROUNDED, MINIMAL
from rich.syntax import Syntax
from rich.markdown import Markdown
from rich.rule import Rule
import json
import os
import sys
import time
import unicodedata

console = Console()

# Truncation marker appended to a clipped line. The marker text consumes
# terminal cells itself, so YAHAUI._clip_line budgets it INSIDE the caller's
# limit - otherwise the "clipped" line would still be wider than the console.
_CLIP_MARKER = "…[+{} chars]"

# Multi-agency status shared by every agent's YAHAUI instance: all instances
# draw to the same module-level console, so the live-agent count and the
# active agent live in one place and stay consistent whichever agent prints.
_MULTI_STATE = {"live": 0, "active": None, "stage": None}


def set_multi_agent_state(live=None, active=None, stage=None):
    # Publish the live-agent count and the active agent for section headers.
    if live is not None:
        _MULTI_STATE["live"] = int(live)
    if active is not None:
        _MULTI_STATE["active"] = active
    if stage is not None:
        _MULTI_STATE["stage"] = stage


def multi_agent_suffix():
    # Section-bar suffix; empty unless more than one agent is live.
    if _MULTI_STATE["live"] <= 1:
        return ""
    active = _MULTI_STATE["active"] or "?"
    stage = _MULTI_STATE["stage"]
    where = (" (%s)" % stage) if stage else ""
    return "agents %d | active: %s%s" % (_MULTI_STATE["live"], active, where)

def reset_multi_agent_state():
    # Clear the multi-agent header state (e.g. when a run finishes) so a
    # later single-agent run cannot show a stale suffix.
    _MULTI_STATE["live"] = 0
    _MULTI_STATE["active"] = None
    _MULTI_STATE["stage"] = None

# Sentinel returned by the ask_user editor when the operator asks to open the
# interrupt diagnose venue from inside a prompt. Two triggers map to it:
#   * pressing Ctrl+I (note: Ctrl+I == Tab / 0x09 in a terminal), or
#   * submitting exactly "/interrupt" or "/int" as the answer.
ASK_INTERRUPT_SENTINEL = "\x00__YAHA_ASK_INTERRUPT__\x00"
_ASK_INTERRUPT_COMMANDS = ("/interrupt", "/int")


def _ask_interrupt_text(text):
    """Return True when a submitted answer is an interrupt slash-command."""
    return str(text).strip().lower() in _ASK_INTERRUPT_COMMANDS


def _multiline_editor(default=""):
    """Low-level prompt_toolkit multi-line editor (chat-style submit).

    Contract:
    - ``Enter`` (carriage return) submits the whole buffer, so one-line or
      pasted multi-line answers need no Alt/Option/Meta modifier to send.
    - Text pasted with newlines is inserted into the buffer (it does not submit).
    - Optional, never-required line-break helpers:
        * Control+J (linefeed) inserts a newline (always available);
        * Escape then Enter (two literal keys) inserts a newline (always
          available);
        * Option+Enter inserts a newline only when the terminal forwards
          Option/Meta as an Escape prefix (e.g. iTerm2 with "Option as Meta
          +Esc"). The default macOS Terminal sends plain Return for
          Option+Enter, so it is indistinguishable from Enter and submits.

    Raises EOFError/KeyboardInterrupt on cancellation; the caller maps empty
    input and cancellation to its fallback value.
    """
    # The multiline editor does not need prompt_toolkit's cursor-position probe.
    # Some terminals report stdout.isatty() but never answer ESC[6n, which makes
    # prompt_toolkit print the "terminal doesn't support CPR" warning and redraw
    # via run_in_terminal(); that redraw race can swallow the operator's first
    # typed character. Tell prompt_toolkit up front that CPR is unavailable so
    # it never sends the probe.
    os.environ["PROMPT_TOOLKIT_NO_CPR"] = "1"
    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding import KeyBindings

    kb = KeyBindings()

    @kb.add("enter")
    def _accept(event):
        event.current_buffer.validate_and_handle()

    @kb.add("c-j")
    def _linefeed(event):
        event.current_buffer.insert_text("\n")

    # Terminal-dependent convenience: fires when the terminal precedes Enter
    # with ESC (e.g. iTerm2's "Option as Meta +Esc"). On default macOS
    # Terminal, Option+Enter sends only CR, so it is indistinguishable from
    # Enter and cannot be bound differently.
    @kb.add("escape", "enter")
    def _option_newline(event):
        event.current_buffer.insert_text("\n")

    # Ctrl+I (the same control code as Tab, 0x09) opens the interrupt diagnose
    # venue instead of answering. Terminals that swallow Ctrl+I can use the
    # typed commands "/interrupt" or "/int" instead.
    @kb.add("c-i")
    def _interrupt(event):
        event.app.exit(result=ASK_INTERRUPT_SENTINEL)

    session = PromptSession(key_bindings=kb)
    answer = session.prompt("> ", multiline=True, default=default)
    text = answer if isinstance(answer, str) else str(answer)
    if text == ASK_INTERRUPT_SENTINEL or _ask_interrupt_text(text):
        return ASK_INTERRUPT_SENTINEL
    return text



class YAHAUI:
    def __init__(self, verbose=False):
        self.console = console
        self.verbose = verbose
        # Optional live provider of context-window usage shown on section bars.
        # Must be a zero-arg callable returning a short string such as
        # "Ctx usage: 45% (58K/128K)", or None to disable the suffix.
        self._ctx_provider = None
        # Optional active stage name included in agent section headers.
        self._stage_name = None
        # Short agent tag (e.g. 'main', 'agent-1'); shown only in multi-agent
        # runs so single-agent output is byte-identical to before.
        self._agent_tag = None

    def set_agent_tag(self, tag):
        # Label this agent's output lines in a multi-agent run.
        self._agent_tag = tag

    def set_active_agent(self, name, tag, stage, live_count):
        # Publish runtime state for the section-bar suffix (best-effort).
        set_multi_agent_state(live=live_count, active=name, stage=stage)

    # --- Context usage section bars ----------------------------------------
    def set_ctx_provider(self, provider):
        """Register a zero-arg callback that reports current context usage.

        The callback result (if any) is appended to every section-rule title,
        e.g. ``──── Agent (id) : Question ──── Ctx usage: 45% (58K/128K) ─────``.
        Pass ``None`` to disable the suffix again.
        """
        self._ctx_provider = provider if callable(provider) else None

    def _ctx_text(self):
        """Return the current context-usage suffix text, or '' when unavailable."""
        try:
            if self._ctx_provider is None:
                return ""
            text = self._ctx_provider()
            return str(text) if text else ""
        except Exception:
            return ""

    def set_stage_name(self, stage_name):
        """Set the active stage shown in agent section headers.

        Pass ``None`` to clear the stage context again.
        """
        self._stage_name = stage_name

    def _subject_label(self, agent_name, agent_id, action):
        """Return ``Agent (id) [: Stage] : Action`` subject text.

        Stage-transition bars pass the same value for stage and action; that case
        stays as ``Agent (id) : Stage`` instead of duplicating the stage name.
        """
        base = f"{agent_name} ({agent_id})"
        tag = getattr(self, "_agent_tag", None)
        if tag and _MULTI_STATE["live"] > 1:
            base = f"[{tag}] {base}"
        stage = self._stage_name
        if not stage:
            return f"{base} : {action}"
        if action and action.lower() == stage.lower():
            return f"{base} : {stage}"
        return f"{base} : {stage} : {action}"

    def _section_rule(self, markup_title, style):
        """Draw a rule (section bar) and append the live ctx/multi-agent suffix."""
        title = markup_title
        suffix = []
        ctx = self._ctx_text()
        if ctx:
            suffix.append(ctx)
        multi = multi_agent_suffix()
        if multi:
            suffix.append(multi)
        if suffix:
            title = f"{title} [dim]{' | '.join(suffix)}[/dim]"
        self.console.rule(title, style=style)

    # --- TUI long-line safety helpers --------------------------------------
    def _console_width(self):
        """Best-effort visible width of the terminal/console."""
        try:
            w = getattr(self.console, "width", None)
            if isinstance(w, int) and w > 20:
                return w
        except Exception:
            pass
        try:
            import shutil
            w = shutil.get_terminal_size((80, 24)).columns
            if w > 20:
                return w
        except Exception:
            pass
        return 80

    def _char_cell_width(self, ch):
        """Approximate terminal cells consumed by a character (CJK wide = 2)."""
        try:
            if unicodedata.combining(ch):
                return 0
            return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        except Exception:
            return 1

    def _line_width(self, line):
        return sum(self._char_cell_width(ch) for ch in line)

    def _hard_clip(self, line, limit):
        """Clip ``line`` to at most ``limit`` terminal cells, without a marker.

        The last-resort fallback: when the budget is too small to also hold the
        truncation marker, drop the marker rather than overflow the display.
        """
        limit = int(limit)
        if not line or limit <= 0:
            return ""
        if self._line_width(line) <= limit:
            return line
        used = 0
        out = []
        for ch in line:
            w = self._char_cell_width(ch)
            if used + w > limit:
                break
            out.append(ch)
            used += w
        return "".join(out)

    def _clip_line(self, line, limit):
        """Clip a single source line so the WHOLE line fits the console width.

        The truncation marker occupies terminal cells, so it is budgeted INSIDE
        ``limit`` (plus one spare cell of slack) instead of being appended after
        the content was already clipped to ``limit``. Appending it afterwards -
        the previous behaviour - produced strings up to
        ``limit + marker_width`` cells wide, i.e. exactly the over-wide repaint
        this helper exists to prevent. When even the marker cannot fit, fall
        back to a marker-free truncation rather than ever overflowing.
        """
        limit = int(limit)
        if not line or limit <= 0:
            return ""
        if self._line_width(line) <= limit:
            return line
        # Reserve room for the widest marker this line could produce: the count
        # of dropped characters is largest at the FIRST clip point, so reserving
        # against ``len(line)`` is a safe upper bound for the actual marker.
        reserve = self._line_width(_CLIP_MARKER.format(len(line)))
        budget = limit - reserve - 1  # -1: conservative spare cell
        if budget >= 1:
            used = 0
            out = []
            for idx, ch in enumerate(line):
                w = self._char_cell_width(ch)
                if used + w > budget:
                    out.append(_CLIP_MARKER.format(len(line) - idx))
                    break
                out.append(ch)
                used += w
            clipped = "".join(out)
            # Defensive: the reserve above should make this unreachable.
            if self._line_width(clipped) <= limit:
                return clipped
        # No room for the marker (tiny limit) or a measurement surprise: degrade
        # to a marker-free truncation instead of overflowing the display.
        return self._hard_clip(line, limit)

    def _fit_text(self, text, max_lines=None, tail_lines=0):
        """Make arbitrary UI text safe for the TUI:

        - clip any individual line whose display width exceeds the console
        - optionally cap the number of lines shown (keeps giant outputs usable)
        - optionally keep the last `tail_lines` lines (head+tail display) so
          trailing context such as a stack trace's final line is not lost

        Prose and Markdown are left to Rich when Rich already wraps safely; this
        helper is used for raw/preview text that may end up inside renderers that
        do not wrap (Syntax, tables) or that could otherwise flood the screen.
        """
        if text is None:
            return text
        text_str = str(text)
        width = self._console_width()
        # Leave a little slack so nested/bordered renderables stay on screen,
        # but never let the budget reach or exceed the console width: a hard
        # floor (the old `max(width - 4, 40)`) ignored narrow terminals and let
        # an emitted line be wider than the display.
        content_width = max(min(width - 4, width - 1), 1)
        clipped_lines = []
        for line in text_str.splitlines():
            clipped_lines.append(self._clip_line(line, content_width))

        def truncation_marker(count):
            # Route the marker through the same clip so it cannot itself exceed
            # a narrow console; on normal widths it is returned unchanged.
            return self._clip_line(
                f"... [{count} more lines truncated]", content_width
            )

        if max_lines and len(clipped_lines) > max_lines:
            dropped = len(clipped_lines)
            if tail_lines > 0 and max_lines > tail_lines + 1:
                keep_head = max_lines - tail_lines - 1
                head = clipped_lines[:keep_head]
                tail = clipped_lines[-tail_lines:]
                dropped = len(clipped_lines) - keep_head - tail_lines
                clipped_lines = head + [truncation_marker(dropped)] + tail
            else:
                dropped = len(clipped_lines) - max_lines
                clipped_lines = clipped_lines[:max_lines] + [truncation_marker(dropped)]
        return "\n".join(clipped_lines)

    def display_thought(self, agent_name, agent_id, thought):
        if not self.verbose:
            return
            
        self.console.print()
        self._section_rule(f"[cyan]{self._subject_label(agent_name, agent_id, 'Thought')}[/cyan]", style="cyan")
        self.console.print(Markdown(thought))
        self.console.print()

    def display_tool_call(self, agent_name, agent_id, tool_name, args):
        if self.verbose:
            self.console.print()
            
            # Truncate large arguments for UI display to prevent hanging/crashes
            display_args = {}
            for k, v in args.items():
                if isinstance(v, str) and len(v) > 1000:
                    display_args[k] = v[:1000] + f"\n... ({len(v)-1000} more chars truncated in UI)"
                else:
                    display_args[k] = v

            args_str = json.dumps(display_args, indent=2)
            args_syntax = Syntax(args_str, "json", background_color="default", theme="monokai", word_wrap=True)
            
            self._section_rule(f"[yellow]{self._subject_label(agent_name, agent_id, f'Tool Call ({tool_name})')}[/yellow]", style="yellow")
            self.console.print(args_syntax)
            self.console.print()
        else:
            # Concise mode: Agent: name (id) \n tool_name [arg_summary]
            prefix = Text(f"{agent_name} ({agent_id})", style="bold blue")
            self.console.print(prefix)
            
            summary = ""
            if tool_name == "bash_tool":
                summary = args.get("cmdline", "")
            elif tool_name in ["create_file", "read_file", "edit_file", "write_file", "append_file", "search_files", "list_files"]:
                summary = args.get("path", "")
            elif tool_name == "yaha_spawn_agent":
                summary = f"{args.get('agent_class', 'Inherited')}"
            elif tool_name == "google_search_tool":
                summary = args.get("query", "")
            elif tool_name == "load_skill":
                summary = args.get("name", "")
            elif tool_name == "async_bash":
                summary = args.get("cmdline", "")
            elif tool_name == "poll_async":
                summary = args.get("job_id", "")

            prefix = f"  {tool_name} "
            tool_line = Text(prefix, style="yellow")
            # Clip over-length arguments so one huge line cannot flood the TUI.
            # Budget against the prefix's CELL width (not its codepoint count)
            # and leave slack; no hard floor, since a floor can itself exceed a
            # narrow terminal and push the assembled line past the display width.
            available = max(self._console_width() - self._line_width(prefix) - 2, 1)
            summary = self._clip_line(str(summary), available)
            tool_line.append(summary, style="white")
            self.console.print(tool_line)

    def display_tool_result(self, agent_name, agent_id, tool_name, result):
        if not self.verbose:
            return

        self.console.print()
        # Normalize/clip first so giant or width-unfriendly results are safe.
        # Syntax previews are further given word_wrap=True: Rich's default is
        # to crop over-long lines and silently drop their tails from the screen.
        if not isinstance(result, str):
            result = str(result)
        preview = self._fit_text(result, max_lines=15)

        # Determine if we should use syntax highlighting.
        # Note: at this point `preview` is the display-safe, line-capped text;
        # a long/truncated preview may no longer be *valid* JSON, so we try a
        # full parse and only pretty-print when it succeeds. That is fine: the
        # preview is capped first for UI safety, and re-indentation is a bonus.
        display_content = preview
        try:
            # Check if it's a unified diff or a JSON-ish payload.
            if preview.startswith("--- a/") or preview.startswith("--- a"):
                display_content = Syntax(preview, "diff", background_color="default", theme="monokai", word_wrap=True)
            elif preview.strip().startswith(("{", "[")):
                try:
                    parsed = json.loads(preview)   # only when preview is whole/valid
                    preview = json.dumps(parsed, indent=2)  # re-indent for readability
                except Exception:
                    pass
                display_content = Syntax(preview, "json", background_color="default", word_wrap=True)
        except Exception:
            pass
        self._section_rule(f"[green]{self._subject_label(agent_name, agent_id, f'Tool Result ({tool_name})')}[/green]", style="green")
        self.console.print(display_content)
        self.console.print()

    def display_error(self, agent_name, agent_id, error):
        self.console.print()
        self._section_rule(f"[bold red]{self._subject_label(agent_name, agent_id, 'Error')}[/bold red]", style="bold red")
        # Errors often put the actionable line at the bottom (stack trace final
        # frame, summary); keep both the head and the tail when truncating.
        self.console.print(self._fit_text(error, max_lines=20, tail_lines=5), style="red")
        self.console.print()

    def display_warning(self, agent_name, agent_id, warning):
        self.console.print()
        self._section_rule(f"[orange3]{self._subject_label(agent_name, agent_id, 'Warning')}[/orange3]", style="orange3")
        # Warnings can also carry long payloads; same guard as errors.
        self.console.print(self._fit_text(warning, max_lines=20), style="orange3")
        self.console.print()

    def display_progress(self, agent_name, agent_id, content):
        self.console.print()
        self._section_rule(f"[cyan]{self._subject_label(agent_name, agent_id, 'Progress')}[/cyan]", style="cyan")
        self.console.print(Markdown(content))
        self.console.print()

    def display_hook(self, agent_name, agent_id, label, output, status="ok"):
        """Render one stage-hook execution.

        Hook output is diagnostic material, not a conversation turn: it gets its
        own section carrying the hook label (`Coding/preexit[2]`) and the agent
        tag, and a failing hook is coloured like a warning. Nothing printed here
        reaches the model unless the hook declares (capture), which is injected
        separately as an attributed observation.
        """
        failed = status == "error"
        style = "bold red" if failed else "deep_sky_blue3"
        colour = "red" if failed else "grey62"
        title = self._subject_label(agent_name, agent_id, f"hook {label}")
        self.console.print()
        self._section_rule(f"[{style}]{title}[/{style}]", style=style)
        text = (output or "").strip()
        if text:
            # Hook output can be an entire traceback: keep the head and the tail.
            self.console.print(self._fit_text(text, max_lines=15, tail_lines=3), style=colour)
        self.console.print()

    def display_final_answer(self, agent_name, agent_id, answer):
        self.console.print()
        self._section_rule(f"[magenta]{self._subject_label(agent_name, agent_id, 'Final Answer')}[/magenta]", style="magenta")
        self.console.print(Markdown(answer))
        self.console.print()

    def display_compression(self, agent_name, agent_id, before_tokens, after_tokens, level, count):
        self.console.print()
        self._section_rule(f"[bold yellow]{self._subject_label(agent_name, agent_id, f'Context Compressed ({level}, #{count})')}[/bold yellow]", style="bold yellow")
        # Both figures come from ONE instrument (the corrected occupancy). The " (est.)"
        # marker is deliberately NOT repeated on this line: the section rule above already
        # carries it via _ctx_display_text, which is the single place that knows whether the
        # reading is a prediction, so printing it here again would double it per banner.
        self.console.print(f"Context usage: ~{before_tokens} -> ~{after_tokens} tokens", style="yellow")
        self.console.print()

    def display_stage_transition(self, agent_name, agent_id, stage_name):
        self.console.print()
        self._section_rule(f"[bold green]{self._subject_label(agent_name, agent_id, stage_name)}[/bold green]", style="bold green")

    def prompt_user(self, agent_name, agent_id, question, default="", timeout=None, multiline=False):
        self.console.print()
        self._section_rule(f"[bold blue]{self._subject_label(agent_name, agent_id, 'Question')}[/bold blue]", style="bold blue")
        
        # If the question looks like it has Markdown (contains code blocks or bold), render it
        if "```" in question or "**" in question:
            self.console.print(Markdown(question))
        else:
            self.console.print(question)
            
        self.console.print()
        
        # Display the prompt label and default value separately to ensure the actual input line is short and stable
        self.console.print(f"[bold green]Your answer[/bold green] [dim](default: {default})[/dim]:")

        if timeout is None:
            # Chat-style multi-line mode (ask_user): on a real TTY use an editor
            # where Enter always submits the whole buffer, pasted text keeps its
            # newlines, and Control+J / Esc+Enter optionally add a new line.
            # Option+Enter also works when the terminal forwards Option as
            # Escape/Meta; otherwise it is indistinguishable from Enter.
            if multiline and sys.stdin.isatty():
                self.console.print(
                    "[dim](Enter sends · paste keeps multi-line text · "
                    "Control+J or Esc+Enter adds a line · "
                    "Ctrl+I or /interrupt opens the interrupt venue: ask questions, "
                    "/help for commands, /resume to continue)[/dim]"
                )
                return self._prompt_multiline(default)

            # Pass the visible prompt to input("> ") instead of pre-printing it.
            # When the prompt is printed manually and input() is called with an empty
            # prompt, the line editor (readline/libedit) does not know the prompt
            # occupies columns on screen, so the user can Backspace past the start of
            # the buffer and overwrite the '>' prompt. Letting readline own the
            # prompt protects it. Keep the prompt plain text: ANSI escapes embedded
            # in the input() prompt cause readline width/redraw miscalculation.
            try:
                ans = input("> ").strip()
                if _ask_interrupt_text(ans):
                    return ASK_INTERRUPT_SENTINEL
                return ans if ans else default
            except (EOFError, KeyboardInterrupt):
                return default

        # Timed read: used when the caller wants to keep retrying even if the
        # operator is away. Returns None when no reply arrives within the
        # timeout (or when there is no interactive stdin at all), so the caller
        # can distinguish "absent user" from a typed wait/break answer.
        self.console.print("> ", end="")
        try:
            import select
            if not sys.stdin.isatty():
                # Unattended / redirect / EOF: model an absent operator with a
                # cooldown equal to the requested timeout, then signal no reply.
                time.sleep(timeout)
                return None
            ready, _, _ = select.select([sys.stdin], [], [], timeout)
            if not ready:
                return None
            ans = sys.stdin.readline()
            if not ans:
                return None
            ans = ans.strip()
            return ans if ans else default
        except EOFError:
            return None
        except KeyboardInterrupt:
            raise
        except Exception:
            # Non-Unix / select unsupported on this stdin: fall back to an
            # absent-operator cooldown so the retry still waits before resuming.
            time.sleep(timeout)
            return None

    def _prompt_multiline(self, default=""):
        """Read an editable answer that may span multiple lines (TTY only).

        Chat-style: Enter always submits. Control+J and the literal two-key
        sequence Escape then Enter insert a new line; Option+Enter only inserts
        a new line when the terminal forwards Option/Meta as Escape (otherwise
        it is indistinguishable from Enter and submits). Pasted multi-line
        text is kept. Empty accepted input and cancellation fall back to
        `default`.
        """
        try:
            answer = _multiline_editor(default)
        except (EOFError, KeyboardInterrupt):
            return default
        except Exception:
            # prompt_toolkit unavailable / terminal interaction failed: fall
            # back to builtin input, same semantics as the single-line path.
            try:
                answer = input("> ")
            except (EOFError, KeyboardInterrupt):
                return default
        # An interrupt request must survive the empty-default fallback below.
        if str(answer).strip() == ASK_INTERRUPT_SENTINEL or _ask_interrupt_text(answer):
            return ASK_INTERRUPT_SENTINEL
        answer = str(answer).strip()
        return answer if answer else default

    # --- Interrupt diagnose venue -----------------------------------------
    def _interrupt_rule(self, agent_name, agent_id, label, style):
        self.console.print()
        self._section_rule(
            f"[{style}]{self._subject_label(agent_name, agent_id, label)}[/{style}]",
            style=style,
        )

    def interrupt_banner(self, agent_name, agent_id, text):
        """One-time header shown when the diagnose venue opens."""
        self._interrupt_rule(agent_name, agent_id, 'Interrupt', 'bold red')
        self.console.print(str(text))
        self.console.print()

    def interrupt_read(self, prompt="! "):
        """Read one venue line.

        Returns None on EOF / Ctrl-D / Ctrl-C so the venue can leave by
        resuming instead of blocking. Raw text is returned unstripped; the
        caller normalizes it. The prompt is plain text so readline (which owns
        the cursor) can measure it correctly.

        The venue uses a `!` prompt char (default `"! "`) so it is
        visually distinct from the normal `"> "` ask_user prompt.
        """
        try:
            return input(prompt)
        except (EOFError, KeyboardInterrupt):
            return None

    def interrupt_answer(self, agent_name, agent_id, text):
        """Print a model answer (or a /call or /spawn digest) in the venue."""
        self._interrupt_rule(agent_name, agent_id, 'Interrupt Answer', 'cyan')
        self.console.print(Markdown(str(text)))
        self.console.print()

    def interrupt_help(self, agent_name, agent_id, text):
        """Print the command menu, generated from the command table."""
        self._interrupt_rule(agent_name, agent_id, 'Interrupt Help', 'bold blue')
        self.console.print(str(text))
        self.console.print()

    def interrupt_status(self, agent_name, agent_id, text):
        """Print the ground-truth runtime dump for /status."""
        self._interrupt_rule(agent_name, agent_id, 'Interrupt Status', 'bold blue')
        self.console.print(str(text))
        self.console.print()

    def interrupt_info(self, agent_name, agent_id, title, text):
        """Print a titled read-only info panel (venue /history, /memory, ...)."""
        self._interrupt_rule(agent_name, agent_id, str(title), 'bold blue')
        self.console.print(str(text))
        self.console.print()

    def interrupt_list(self, agent_name, agent_id, title, items, columns=None):
        """Print a titled read-only table under an interrupt rule (venue /list)."""
        self._interrupt_rule(agent_name, agent_id, str(title), 'bold blue')
        rows = list(items or [])
        if not rows:
            self.console.print('(none)', style='yellow')
            self.console.print()
            return
        from rich.table import Table
        table = Table(box=MINIMAL, expand=True)
        for col in (columns or ['Name']):
            table.add_column(str(col), style='cyan')
        for row in rows:
            cells = row if isinstance(row, (list, tuple)) else [row]
            table.add_row(*[self._fit_text(str(c), max_lines=3) for c in cells])
        self.console.print(table)
        self.console.print()

    def interrupt_note(self, agent_name, agent_id, text):
        """Print a plain one-line note, e.g. the /resume confirmation."""
        self.console.print(str(text))
        self.console.print()

    def spinner(self, message):
        return Live(Spinner("dots", text=message), refresh_per_second=10, transient=True)

    def display_list(self, category, items, columns=None):
        if not items:
            self.console.print(f"No {category} found.", style="yellow")
            return
        
        from rich.table import Table
        # Use MINIMAL box for the table to avoid too many lines
        table = Table(title=f"Available {category.capitalize()}", box=MINIMAL, expand=True)
        
        if columns:
            for col in columns:
                table.add_column(col, style="cyan")
        else:
            table.add_column("Name", style="cyan")
        
        def clip_cell(value):
            # Guard against one huge cell value flooding/spreading the table.
            return self._fit_text(str(value), max_lines=3)

        for item in items:
            if isinstance(item, (list, tuple)):
                table.add_row(*[clip_cell(x) for x in item])
            else:
                table.add_row(clip_cell(item))
        
        self.console.print(table)
