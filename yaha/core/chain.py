#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

"""The prompt-chain control layer for YAHA.

Pure Python (stdlib only): no Rich, no UI, no LLM imports, so the whole module
can be unit-tested in isolation - exactly like ``core/runtime.py``.

Why this module exists
----------------------
The chain controller used to OWN its state by scattering it across
``Agent.iter_run``: the walk index, the exit_to call frame, the runaway
counter, the restart/loop counters and the Max-Loops budget all lived on the
agent as loose attributes, and the status vocabulary was re-derived at every
branch with ``status.startswith(...)`` sniffing. This module is the one place
that owns that state and that vocabulary.

It is a REFACTOR, not a redesign: every guard number, every wire string and
every transition below reproduces the shipped behaviour exactly.
``Agent.iter_run`` still owns the routing DECISIONS - its branches have side
effects and stay where they are - it now asks this layer for the state instead
of holding it.

Three pieces
------------
``StageExit``    the status vocabulary of one stage's outcome.
``ChainWalk``    where the walk is, and where a move takes it.
``ChainBudget``  how many times the chain may still loop or restart.
"""


class StageExit:
    """The status vocabulary a stage returns to the chain driver.

    These are the wire values exchanged between the ReAct loop and
    ``Agent.iter_run``. They are pinned literally: changing one would be a
    protocol change, not a rename.
    """

    DONE = 'DONE'
    LOOP = 'LOOP'
    RESTART = 'RESTART'
    ERROR = 'ERROR'
    # Interrupt outcomes. The two bare ones are produced by the interrupt
    # venue; the jump form carries the target after the colon (see
    # ``core/interrupt_commands.py``).
    INTERRUPT_REWIND = 'INTERRUPT_REWIND'
    INTERRUPT_JUMP = 'INTERRUPT_JUMP'
    # Chain moves, each carrying its payload after the colon.
    EXIT_TO = 'EXIT_TO'
    FAIL = 'FAIL'

    @staticmethod
    def _payload(status, prefix):
        """The text after ``prefix:``, or None when it does not match.

        Anchored at the START and requiring the colon, so a bare ``FAIL`` (the
        interrupt quit outcome) is never mistaken for a ``FAIL:<target>``
        chain move, and ``EXIT_TO`` is never matched inside another word.
        """
        text = str(status or '')
        head = prefix + ':'
        if text.startswith(head):
            return text[len(head):]
        return None

    @classmethod
    def is_interrupt_jump(cls, status):
        return cls._payload(status, cls.INTERRUPT_JUMP) is not None

    @classmethod
    def interrupt_jump_target(cls, status):
        return cls._payload(status, cls.INTERRUPT_JUMP)

    @classmethod
    def is_exit_to(cls, status):
        return cls._payload(status, cls.EXIT_TO) is not None

    @classmethod
    def exit_to_target(cls, status):
        return cls._payload(status, cls.EXIT_TO)

    @classmethod
    def is_fail(cls, status):
        return cls._payload(status, cls.FAIL) is not None

    @classmethod
    def fail_target(cls, status):
        return cls._payload(status, cls.FAIL)


class ChainWalk:
    """Where the walk is, and where a move takes it.

    Owns the walk index, the exit_to call frame, the runaway counter and both
    name lookups. ``Agent.iter_run`` drives it; nothing here has side effects.
    """

    # An exit_to CALL nest may not nest deeper than this.
    CALL_DEPTH_CAP = 16
    # A single chain pass may not enter more stages than this: a jump/call
    # cycle that never terminates must fail LOUDLY instead of spinning.
    RUNAWAY_ENTRIES = 1000

    def __init__(self, chain):
        self.chain = list(chain or [])
        self.stage_count = len(self.chain)
        self.idx = 0
        self.call_stage = None
        self.depth = 0
        self.return_idx = None
        self.walk_entries = 0

    # --- position ---------------------------------------------------------

    @property
    def in_call(self):
        """True while a Detached stage is being run as a CALL."""
        return self.call_stage is not None

    @property
    def stage(self):
        """The stage to run now: the call frame's, else ``chain[idx]``."""
        if self.call_stage is not None:
            return self.call_stage
        if 0 <= self.idx < self.stage_count:
            return self.chain[self.idx]
        return None

    def at_end(self):
        """True when this pass has no stage left to enter."""
        return not (self.idx < self.stage_count or self.call_stage is not None)

    def enter(self):
        """Count one stage entry; return the runaway error string, else None."""
        self.walk_entries += 1
        if self.walk_entries > self.RUNAWAY_ENTRIES:
            return ('Error: the chain walk exceeded %d stage entries in one '
                    'pass (runaway exit_to jump/call cycle); aborting.'
                    % self.RUNAWAY_ENTRIES)
        return None

    # --- moves ------------------------------------------------------------

    def clear_call(self):
        """Forget any call frame (the walk is back in the chain)."""
        self.call_stage = None
        self.depth = 0
        self.return_idx = None

    def advance(self):
        """The next walk index after a stage completes.

        Normally ``idx + 1``. When the stage that just completed was an exit_to
        CALL the nest unwinds instead: because exit_to TERMINATES the calling
        stage, a callee that itself calls another callee never needs to resume
        itself, so the whole nest always returns to the ONE chain index
        recorded when the nest was entered (the caller's next stage).
        """
        if self.call_stage is None:
            return self.idx + 1
        nxt = self.return_idx
        self.clear_call()
        return self.idx if nxt is None else nxt

    def jump_index(self, name):
        """Index of the ATTACHED stage named ``name``, else None.

        Case-insensitive, matching ``target_index``; ``None`` means the name is
        not a chain stage at all (it may still be a Detached one).
        """
        wanted = str(name or '').strip().lower()
        if not wanted:
            return None
        for index, stage in enumerate(self.chain):
            if str((stage or {}).get('name', '')).strip().lower() == wanted:
                return index
        return None

    def target_index(self, name, fallback=None):
        """Index of the stage named ``name``, else ``fallback``.

        Case-insensitive, and the ONE lookup shared by the preenter routing and
        the FAIL routing, so the same target name cannot resolve to different
        stages depending on which code path raised the failure.
        """
        wanted = str(name or '').lower()
        for index, stage in enumerate(self.chain):
            if str((stage or {}).get('name', '')).lower() == wanted:
                return index
        return fallback

    def push_call(self, name, detached_stages):
        """Enter the Detached stage ``name`` as a CALL; return an error, else None.

        The caller's next stage is recorded only ONCE, when the nest was
        entered, so a callee that itself calls another callee still unwinds to
        the original caller's next stage. A nest deeper than
        ``CALL_DEPTH_CAP`` is refused without touching the frame.
        """
        stages = detached_stages or {}
        if self.call_stage is None:
            self.return_idx = self.idx + 1
        self.depth += 1
        if self.depth > self.CALL_DEPTH_CAP:
            return ('Error: exit_to call depth exceeded (%d); aborting '
                    'the chain.' % self.CALL_DEPTH_CAP)
        self.call_stage = dict(stages[name])
        return None


class ChainBudget:
    """How many times the chain may still restart or loop.

    The Max-Loops rule counts ANY operation on the chain: a loop, an exit_to
    jump and an exit_to call each cost one. Restarts are capped separately, at
    a fixed ``RESTART_CAP``.
    """

    RESTART_CAP = 5

    def __init__(self, loop_limit=-1):
        # ``loop_limit <= 0`` means unlimited (the shipped default is -1).
        self.loop_limit = loop_limit
        self.loop_count = 0
        self.restart_count = 0

    # --- pass control -----------------------------------------------------

    def can_start_pass(self):
        """The outer-loop condition: may another whole pass begin?"""
        return (self.restart_count <= self.RESTART_CAP
                and (self.loop_limit <= 0 or self.loop_count <= self.loop_limit))

    def charge_operation(self):
        """Charge one chain operation; return False when the budget is spent.

        The caller decides what to do about a spent budget (the exit_to path
        aborts immediately, the loop path lets the outer-pass condition notice).
        """
        self.loop_count += 1
        return not self.loops_exhausted()

    def note_restart(self):
        self.restart_count += 1

    def loops_exhausted(self):
        return self.loop_limit > 0 and self.loop_count > self.loop_limit

    def loop_cap_label(self):
        """The loop cap as shown to the operator."""
        return str(self.loop_limit) if self.loop_limit > 0 else 'unlimited'

    # --- terminal answers -------------------------------------------------

    def loops_answer(self):
        return 'Error: Maximum chain loops reached (%s).' % self.loop_limit

    def restarts_answer(self):
        return 'Error: Maximum chain restarts reached.'

    def exhausted_answer(self):
        """The answer for a chain that ran out of budget (loops win)."""
        if self.loops_exhausted():
            return self.loops_answer()
        return self.restarts_answer()