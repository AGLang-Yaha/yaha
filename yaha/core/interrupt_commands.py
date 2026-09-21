#!/usr/bin/env python3
"""Declarative slash-command vocabulary for the interrupt diagnose venue.

The interrupt stage is a diagnostic venue: any free text that is NOT a slash
command is a question to the model, and only a leading-slash line changes the
execution flow (prompt chaining). This module is the single source of truth for

- the command words,
- their argument arity,
- whether they loop inside the venue or leave it (kind),
- the chain-move status each final command produces,
- the per-command policy when the venue was opened from ask_user,
- the usage and help text.

It is deliberately pure: no UI, no prompt_toolkit, no Rich, no Agent import.
"""

from dataclasses import dataclass

ARG_NONE = 'none'
ARG_REQUIRED = 'required'
ARG_OPTIONAL = 'optional'

KIND_LOOP = 'loop'
KIND_FINAL = 'final'

STATUS_RESUME = 'RESUME_CONTINUE'
STATUS_REWIND = 'INTERRUPT_REWIND'
STATUS_RESTART = 'RESTART'
STATUS_JUMP = 'INTERRUPT_JUMP'
STATUS_FAIL = 'FAIL'

ASK_LOOP = 'loop'
ASK_REASK = 'reask'
ASK_ANSWER = 'answer'
ASK_DEFER = 'defer'

PREFIX = '/'


@dataclass(frozen=True)
class InterruptCommand:
    """One entry of the venue vocabulary."""

    name: str
    arg: str
    kind: str
    status: str
    help: str
    ask_user: str

    def is_final(self):
        return self.kind == KIND_FINAL

    def usage(self):
        hint = _ARG_HINTS.get(self.name)
        if hint:
            return PREFIX + self.name + ' ' + hint
        return PREFIX + self.name


_ARG_HINTS = {
    'jump': '<stage>',
    'call': '<detached-stage>',
    'spawn': '<AgentClass> <task>',
    'resume': '[<text>]',
}

INTERRUPT_COMMANDS = (
    InterruptCommand('help', ARG_NONE, KIND_LOOP, '', 'show this command menu', ASK_LOOP),
    InterruptCommand('status', ARG_NONE, KIND_LOOP, '', 'show the live runtime state', ASK_LOOP),
    InterruptCommand('call', ARG_REQUIRED, KIND_LOOP, '', 'run a detached stage and show its output', ASK_LOOP),
    InterruptCommand('spawn', ARG_REQUIRED, KIND_LOOP, '', 'spawn a sub-agent and show its output', ASK_LOOP),
    InterruptCommand('resume', ARG_OPTIONAL, KIND_FINAL, STATUS_RESUME, 'resume the interrupted stage (optional steer text)', ASK_ANSWER),
    InterruptCommand('rewind', ARG_NONE, KIND_FINAL, STATUS_REWIND, 're-enter the current stage', ASK_DEFER),
    InterruptCommand('restart', ARG_NONE, KIND_FINAL, STATUS_RESTART, 'restart the whole chain', ASK_DEFER),
    InterruptCommand('jump', ARG_REQUIRED, KIND_FINAL, STATUS_JUMP, 'jump to an attached stage', ASK_DEFER),
    InterruptCommand('quit', ARG_NONE, KIND_FINAL, STATUS_FAIL, 'route to the fail target and finish', ASK_DEFER),
)

CANONICAL_ORDER = tuple(cmd.name for cmd in INTERRUPT_COMMANDS)

_BY_NAME = {cmd.name: cmd for cmd in INTERRUPT_COMMANDS}


# --- lookups ---------------------------------------------------------------

def is_command(raw):
    """True when the input is an explicit slash command (not a question)."""
    if raw is None:
        return False
    return str(raw).lstrip().startswith(PREFIX)


def command_token(raw):
    """Return the bare command word of a slash line, or None."""
    if not is_command(raw):
        return None
    text = str(raw).lstrip()[len(PREFIX):].strip()
    if not text:
        return ''
    return text.split(None, 1)[0].lower()


def lookup(token):
    """Resolve a command word (with or without the slash) to its spec."""
    if token is None:
        return None
    word = str(token).strip().lower()
    if word.startswith(PREFIX):
        word = word[len(PREFIX):]
    return _BY_NAME.get(word)


def parse_interrupt_line(raw):
    """Split a venue input line into (spec, arg).

    - bare text and empty input return (None, None): the caller treats bare
      text as a model question and empty input as a no-op re-prompt;
    - an unknown slash command returns (None, arg) so the caller can warn and
      point at /help.
    """
    if not is_command(raw):
        return (None, None)
    body = str(raw).lstrip()[len(PREFIX):].strip()
    if not body:
        return (None, None)
    parts = body.split(None, 1)
    word = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else None
    if arg == '':
        arg = None
    return (_BY_NAME.get(word), arg)


# --- rendering -------------------------------------------------------------

def usage_of(spec):
    """The canonical usage line for a command spec."""
    return spec.usage()


def render_help():
    """The command menu, generated from the table (never hand-maintained)."""
    width = max(len(cmd.usage()) for cmd in INTERRUPT_COMMANDS)
    lines = ['Venue commands:']
    for cmd in INTERRUPT_COMMANDS:
        lines.append('  ' + cmd.usage().ljust(width) + '   ' + cmd.help)
    lines.append('')
    lines.append('Anything without a leading slash is a question to the model.')
    return '\n'.join(lines)


def venue_banner(stage_label):
    """One-line banner shown once when the venue opens."""
    where = stage_label or 'the current stage'
    return (
        'Interrupt venue - paused at ' + str(where) + '. '
        'Ask a question, or type /help for commands (/resume to continue).'
    )


def label_for_status(status):
    """Map a chain-move status back to the command word the user typed."""
    text = str(status or '')
    if text == STATUS_RESUME:
        return 'resume'
    if text == STATUS_REWIND:
        return 'rewind'
    if text == STATUS_RESTART:
        return 'restart'
    if text.startswith(STATUS_JUMP):
        return 'jump'
    if text.startswith(STATUS_FAIL):
        return 'quit'
    return ''