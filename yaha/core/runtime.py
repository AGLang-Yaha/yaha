#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

"""Cooperative multi-agent runtime for YAHA.

Pure Python (stdlib only): no Rich, no UI, no LLM imports, so the whole module
can be unit-tested in isolation.

Model (patch 1)
---------------
* Agents are flat peers. There is no parent/child lifetime coupling: spawning
  only shares an agent class (and optionally a snapshot of the parent's
  context) at the moment of creation. The runtime ends only when every agent
  has died: its chain reached Aftermath, or it quit explicitly.
* Execution is cooperative and NON-PREEMPTIVE. An agent owns the execution
  right for a whole stage and yields control only at a stage boundary:
    - ("enter", stage_name): the agent is about to run one whole stage; the
      scheduler may round-robin to another ready agent here;
    - ("wait", event_name): the stage is gated by a Wait Event that is not set
      yet; the agent is parked until some agent sets that event.
  Because a stage (including any interactive prompt inside it) runs
  synchronously to completion, the single-threaded scheduler gives the TUI a
  global input lock for free.
* Synchronisation happens ONLY at stage boundaries, through named Events in a
  runtime-global registry that is created on first use and never deleted.
* Any agent on the runtime may start further peers with
  spawn_parallel_agent, subject to its own Agentset authorization and
  the toolset privilege cap.
* Fan-out is intentionally UNBOUNDED: the runtime applies no hard cap on how
  many agents may exist. In practice it is bounded by each class's Agentset
  authorization and the toolset privilege cap, and ``max_agents`` records the
  high-water mark for observation only (it is a record, not a limit).
* Events AUTO-RESET after a wait, so a Wait Event blocks on EVERY round: a
  set that finds agents parked on the name releases ALL of them and then
  re-arms (clears), so the next round blocks again instead of passing
  straight through. A set that finds nobody parked LATCHES (stays set) and
  is consumed by the next waiter, so a producer that runs slightly early
  cannot deadlock a late consumer. A completed handshake never leaks into a
  later wait.
"""

import re
import threading
import time
from collections import deque

from .endpoints import (ENDPOINT_IDLE_TIMEOUT_S, EndpointRegistry, is_ep)

# --- agent lifecycle states -------------------------------------------------

READY = "ready"
RUNNING = "running"
WAITING = "waiting"
DEAD = "dead"


class RuntimeDeadlockError(RuntimeError):
    """Raised when no agent is runnable and no waiting event can ever be set."""


class Event:
    """A named boolean flag.

    On its own this is a plain latch: ``set()`` sets it and it stays set until
    ``clear()`` is called. The MULTI-AGENT WAIT POLICY lives in
    ``AgentRuntime``: a stage-boundary wait CONSUMES the flag
    (``consume_event``), and ``set_event`` re-arms it after releasing the
    parked agents, so a Wait Event blocks again on the next round. Treat
    ``Event`` as the storage, not as the synchronisation semantics.
    """

    __slots__ = ("name", "_set")

    def __init__(self, name):
        self.name = name
        self._set = False

    def set(self):
        self._set = True
        return True

    def clear(self):
        self._set = False
        return False

    def is_set(self):
        return self._set

    def __bool__(self):
        return self._set

    def __repr__(self):
        return "<Event %s %s>" % (self.name, "set" if self._set else "unset")


class EventRegistry:
    """Runtime-global named events, created on first use and never deleted.

    Every agent in a run shares one registry, so a name is enough to
    coordinate agents that were spawned independently.
    """

    def __init__(self):
        self._events = {}

    def get(self, name):
        key = str(name)
        ev = self._events.get(key)
        if ev is None:
            ev = Event(key)
            self._events[key] = ev
        return ev

    def set(self, name):
        return self.get(name).set()

    def clear(self, name):
        return self.get(name).clear()

    def is_set(self, name):
        return self.get(name).is_set()

    def names(self):
        return sorted(self._events)

    def __contains__(self, name):
        return str(name) in self._events


# A late-binding PLACEHOLDER: an event name beginning with '@' that is bound to
# a REAL event name at runtime by `bind_tool`. One definition, shared by the
# registry, the agent's resolution helper and the startup validator.
PLACEHOLDER_PREFIX = "@"


def is_placeholder(name):
    """True when an event name is a late-binding placeholder (starts with '@')."""
    return str(name or "").strip().startswith(PLACEHOLDER_PREFIX)


class BindingRegistry:
    """Runtime-global placeholder -> real event name map (strictly 1:1).

    A placeholder (``@name``) lets a stage declare a wait - or signal an event -
    before the concrete event name is known. Binding is run-global for the same
    reason ``EventRegistry`` is: every agent in a run must see ONE mapping, so a
    crew can bind a gate another agent is already parked on.

    REBINDING IS ALLOWED: the LAST bind wins, so re-binding identically is a
    harmless no-op. This class never raises - ``bind_tool`` validates the
    argument SHAPE, and an UNBOUND placeholder is reported by the caller (as a
    recoverable stage failure), not by this storage.
    """

    def __init__(self):
        self._bindings = {}

    def bind(self, placeholder, real_event):
        """Bind (or re-bind) and return the stored real event name."""
        key = str(placeholder).strip()
        value = str(real_event).strip()
        self._bindings[key] = value
        return value

    def resolve(self, placeholder):
        """The real event name bound to a placeholder, or None."""
        return self._bindings.get(str(placeholder).strip())

    def names(self):
        return sorted(self._bindings)

    def __contains__(self, placeholder):
        return str(placeholder).strip() in self._bindings


class AgentHandle:
    """Bookkeeping for one live agent in the runtime."""

    __slots__ = ("name", "tag", "agent", "generator", "status",
                 "wait_event", "result", "entered")

    def __init__(self, name, tag, agent, generator=None):
        self.name = name
        self.tag = tag or name
        self.agent = agent
        self.generator = generator
        self.status = READY
        self.wait_event = None
        self.result = None
        self.entered = []

    def __repr__(self):
        return "<AgentHandle %s %s>" % (self.name, self.status)


class AgentRuntime:
    """Single-threaded, non-preemptive cooperative scheduler.

    ``ready`` is a FIFO of runnable handles; ``waiting`` maps an event name to
    the handles parked on it; ``pool`` tracks every live handle by name.
    """

    def __init__(self, ui=None, endpoint_idle_timeout=ENDPOINT_IDLE_TIMEOUT_S):
        self.ui = ui
        self.events = EventRegistry()
        # ENDPOINT WAKE MAILBOX. With endpoints a wake arrives from an HTTP
        # handler thread or a client poller thread, and those must NEVER touch
        # the scheduler state (`waiting` / `ready`) directly. They post a
        # notification here and the scheduler drains it on its own thread. The
        # notification is DURABLE (a queued item, not a momentary state change)
        # and the Event stays set until drained, so a message landing in the
        # take-then-park window cannot be lost.
        self._endpoint_lock = threading.Lock()
        self._endpoint_pending = deque()
        self._endpoint_event = threading.Event()
        self._endpoint_activity = time.monotonic()
        # Bounded: a peer that never arrives must end the run LOUDLY.
        self.endpoint_idle_timeout = float(endpoint_idle_timeout)
        # Late-binding placeholders (`@name`): one registry per run, exactly
        # like the events, so every agent resolves the same mapping.
        self.bindings = BindingRegistry()
        # Remote endpoints (service EPs and CEPs): one registry per run, exactly
        # like the events and the bindings, so every agent on the runtime sees
        # the same conversations. `wake` lets an arriving message release an
        # agent parked on that endpoint.
        self.endpoints = EndpointRegistry(wake=self._notify_endpoint)
        self.ready = deque()
        self.waiting = {}
        self.pool = {}
        self._counter = 0
        self.agent_count = 0
        # High-water mark of concurrent live agents. Recorded for observation
        # only: the runtime enforces no fan-out cap (see the module docstring).
        self.max_agents = 0
        # Stage hooks that REFUSED to let an agent signal an event (a failing
        # `preexit (required)` / `posttool (required)` in front of
        # set_event_success / set_event_loop). Recorded so a resulting deadlock
        # can name the real culprit instead of only naming the agents parked on
        # the event. Scoped to signals that never happened: `set_event` drops the
        # entries of an event once it is signalled (see _forget_blocked_signals),
        # so a recovered handshake cannot be blamed for a later deadlock.
        self.blocked_signals = []

    def record_blocked_signal(self, agent_name, hook_label, detail, event=None):
        """Remember that a required hook blocked an event signal."""
        try:
            self.blocked_signals.append({
                "agent": str(agent_name),
                "hook": str(hook_label),
                "detail": str(detail or ""),
                "event": str(event) if event else None,
            })
        except Exception:
            pass

    def active_name(self):
        """Name of the agent currently holding the execution right, if any."""
        for handle in self.pool.values():
            if handle.status == RUNNING:
                return handle.name
        return None

    # --- naming -----------------------------------------------------------

    def next_name(self, prefix="agent"):
        self._counter += 1
        return "%s-%d" % (prefix, self._counter)

    # --- registration -----------------------------------------------------

    def spawn(self, agent, task_prompt, name=None, tag=None, seed_history=None):
        """Register and schedule a new agent; returns its handle.

        Non-blocking: the child is appended to the ready queue and executes
        only when the runtime next gives it the CPU (i.e. when the spawning
        agent blocks at a stage boundary).

        No fan-out cap is applied here; authorization and the toolset
        privilege cap are enforced by the caller (YAHAEngine).
        """
        if name is None:
            name = self.next_name()
        handle = AgentHandle(name, tag, agent)
        agent.runtime = self
        # Tag the agent's own UI so its output lines are labelled in the TUI.
        ui = getattr(agent, "ui", None)
        tagger = getattr(ui, "set_agent_tag", None)
        if tagger is not None:
            try:
                tagger(handle.tag)
            except Exception:
                pass
        handle.generator = agent.iter_run(task_prompt, seed_history=seed_history)
        self.pool[handle.name] = handle
        self.ready.append(handle)
        self.agent_count += 1
        self.max_agents = max(self.max_agents, self.agent_count)
        return handle

    # --- event plumbing ---------------------------------------------------

    def bind_event(self, placeholder, real_event):
        """Late-bind a placeholder to a REAL event name (the LAST bind wins)."""
        return self.bindings.bind(placeholder, real_event)

    def resolve_event(self, name):
        """Resolve an event name through the run-global binding registry.

        A plain name is returned unchanged. A placeholder (``@name``) returns
        its bound real event name, or None when it is UNBOUND - the caller
        turns that into a recoverable stage failure.
        """
        if not is_placeholder(name):
            return str(name or "")
        return self.bindings.resolve(name)

    def set_event(self, name):
        """Signal a named event: release every parked agent, then re-arm.

        A set is a ONE-SHOT PULSE. If agents are parked on the name they are
        all released and the flag is cleared (re-armed), so the NEXT round
        blocks again instead of passing straight through. If nobody is parked
        the flag LATCHES, so a producer that runs slightly early does not
        deadlock a consumer that arrives later; that consumer takes the latch
        via ``consume_event`` and proceeds without blocking.
        """
        ev = self.events.get(name)
        ev.set()
        released = self._wake(str(name))
        if released:
            # The pulse was consumed by the current generation of waiters.
            ev.clear()
        # The signal did happen, so any recorded "this hook blocked it" entry
        # for this event is stale: drop it, or a later, unrelated deadlock would
        # cite a handshake that actually recovered.
        self._forget_blocked_signals(name)
        return ev

    def _notify_endpoint(self, name):
        """Thread-safe: queue an endpoint wake for the scheduler thread.

        Called from an HTTP handler thread or a client poller thread, so it must
        NOT touch ``self.waiting`` or ``self.ready``. It records the name and sets
        the Event; the scheduler's own loop drains it and calls ``_wake``.
        """
        with self._endpoint_lock:
            self._endpoint_pending.append(str(name))
        self._endpoint_event.set()
        return 1

    def _drain_endpoint_notifications(self):
        """Turn queued endpoint wakes into ready handles (scheduler thread)."""
        with self._endpoint_lock:
            pending = list(self._endpoint_pending)
            self._endpoint_pending.clear()
        if not pending:
            return 0
        self._endpoint_event.clear()
        self._endpoint_activity = time.monotonic()
        released = 0
        for name in pending:
            released += self._wake(name)
        return released

    def _endpoint_wait_live(self):
        """True while a parked endpoint wait could still be answered by a peer.

        An endpoint's producer is an EXTERNAL peer, not a handle in the pool, so
        "nothing is runnable" does not mean "no progress is possible" for these
        waits. The scheduler consults this only for endpoint names, so local
        waits keep the original deadlock semantics exactly.
        """
        for name in list(self.waiting):
            if is_ep(name) and self.endpoints.is_live(name):
                return True
        return False

    def _wake(self, name):
        """Release every live agent parked on ``name``; return how many."""
        waiters = self.waiting.pop(name, None)
        if not waiters:
            return 0
        released = 0
        for handle in waiters:
            if handle.status == DEAD:
                continue
            handle.status = READY
            handle.wait_event = None
            self.ready.append(handle)
            released += 1
        return released

    def consume_event(self, name):
        """Consume an event at a stage-boundary wait (the auto-reset read).

        Returns True when the flag was set - it is cleared and the caller may
        proceed WITHOUT blocking (the producer ran early and latched). Returns
        False when the caller must park; the runtime re-queues a parked handle
        only from ``set_event``, so a resume means the wait was satisfied.

        """
        ev = self.events.get(name)
        if ev.is_set():
            ev.clear()
            return True
        return False

    def event_is_set(self, name):
        return self.events.is_set(name)

    # --- scheduler --------------------------------------------------------

    def run(self, root_handle=None):
        """Drive the scheduler until every agent has died.

        Returns the root handle's result when a root handle is given.
        Raises RuntimeDeadlockError when nothing is runnable and no waiting
        event can ever be set.
        """
        while self.pool:
            # Endpoint wakes arrive from foreign threads; turn them into ready
            # handles HERE, on the scheduler thread, before deciding anything.
            self._drain_endpoint_notifications()
            if not self.ready:
                if self._endpoint_wait_live():
                    # An endpoint's producer is an EXTERNAL peer, so "nothing is
                    # runnable" is NOT "no progress is possible": wait for
                    # endpoint activity instead of declaring a deadlock. The
                    # bounded idle timeout keeps this honest when the peer never
                    # arrives, so the detector is delayed, never disabled.
                    if (time.monotonic() - self._endpoint_activity
                            > self.endpoint_idle_timeout):
                        message = ("Multi-agency deadlock: no agent is runnable "
                                   "and no remote endpoint answered within "
                                   "%gs. Waiting on: %s"
                                   % (self.endpoint_idle_timeout,
                                      ", ".join(sorted(self.waiting))))
                        self._abort_pool()
                        self.endpoints.reclaim()
                        raise RuntimeDeadlockError(message)
                    self._endpoint_event.wait(0.05)
                    continue
                # Build the diagnostic FIRST: it describes the agents that are
                # still parked, and reaping the pool renames every one of them
                # as dead (the message would degrade to "no live agents",
                # losing exactly the information this detector exists for).
                message = self.deadlock_message()
                # An aborted run never steps a parked agent again, so release
                # their stage-hook kernels here instead of leaking a subprocess
                # per parked agent in a long-lived process (ChatAgent loop).
                self._abort_pool()
                # Close every listener/poller too: an aborted run must not leak
                # a bound port into a long-lived process.
                self.endpoints.reclaim()
                raise RuntimeDeadlockError(message)
            handle = self.ready.popleft()
            self._step(handle)
        # The run is over: reclaim every conversation and listener (`close_tool`
        # is a courtesy for the peer, never a requirement for us).
        self.endpoints.reclaim()
        if root_handle is not None:
            return root_handle.result
        return None

    def _step(self, handle):
        """Advance one agent up to its next stage boundary."""
        handle.status = RUNNING
        # Publish before stepping so the header names the agent that is
        # really producing output during this step.
        stage_now = handle.entered[-1] if handle.entered else None
        self._publish(handle, stage_now)
        try:
            token = next(handle.generator)
        except StopIteration as stop:
            self._kill(handle, stop.value)
            return
        if token is None:
            # Defensive: never stall on an empty token.
            handle.status = READY
            self.ready.append(handle)
            return
        kind = token[0]
        if kind == "wait":
            name = str(token[1])
            handle.status = WAITING
            handle.wait_event = name
            self.waiting.setdefault(name, []).append(handle)
            if is_ep(name):
                # A fresh endpoint wait gets a FULL idle budget measured from
                # here, so a long-lived run is not punished for its earlier
                # waits; `_endpoint_wait_live` decides whether we may keep
                # waiting at all.
                self._endpoint_activity = time.monotonic()
        elif kind == "enter":
            stage_name = token[1]
            handle.entered.append(stage_name)
            handle.status = READY
            # Round-robin: give every other ready agent a turn before this one
            # runs its stage.
            self.ready.append(handle)
        else:
            handle.status = READY
            self.ready.append(handle)

    def _publish(self, handle, stage_name):
        """Tell the UI which agent/stage is active (best-effort, never fatal)."""
        ui = self.ui
        if ui is None:
            return
        publish = getattr(ui, "set_active_agent", None)
        if publish is None:
            return
        try:
            publish(handle.name, handle.tag, stage_name, len(self.pool))
        except Exception:
            pass

    def _kill(self, handle, result):
        handle.status = DEAD
        handle.result = result
        self.pool.pop(handle.name, None)
        self.agent_count = max(0, self.agent_count - 1)
        # An agent that reached Aftermath (or quit, or whose run was aborted by a
        # deadlock) owns no resources any more: release its stage-hook python
        # kernel here instead of leaking a subprocess per dead agent. This is
        # the ONE release point for a handle - the normal death path (`_step`)
        # and the abort path (`_abort_pool`) both go through it, so they cannot
        # drift apart.
        shutdown = getattr(handle.agent, "_shutdown_hooks", None)
        if shutdown is not None:
            try:
                shutdown()
            except Exception:
                pass
        for waiters in self.waiting.values():
            try:
                waiters.remove(handle)
            except ValueError:
                pass

    def _abort_pool(self):
        """Release every still-pooled agent when the run is aborted.

        Called only by `run()` on a deadlock, where the parked agents will never
        be stepped again: their stage-hook python kernels must not outlive the
        aborted run, and the runtime must not keep reporting them as live. A
        no-op when the pool is already empty (every agent died normally).
        """
        for handle in list(self.pool.values()):
            self._kill(handle, handle.result)

    def _forget_blocked_signals(self, name):
        """Drop blocked-signal records for an event that has now been set.

        `blocked_signals` is a DEADLOCK diagnostic, not a history: an entry
        whose event was eventually signalled (a later attempt succeeded) must
        not be cited by an unrelated future deadlock. Entries for events that
        were never signalled survive, so the detector keeps naming the real
        culprit.
        """
        try:
            target = str(name)
            self.blocked_signals = [
                entry for entry in self.blocked_signals
                if str(entry.get("event") or "") != target
            ]
        except Exception:
            pass

    def deadlock_message(self):
        parts = []
        for handle in self.pool.values():
            if handle.status == WAITING:
                parts.append("%s waiting on '%s'" % (handle.name, handle.wait_event))
        detail = "; ".join(sorted(parts)) or "no live agents"
        message = (
            "Multi-agency deadlock: no agent is runnable and no event can be "
            "set. Waiting: " + detail
        )
        blocked = list(getattr(self, "blocked_signals", []) or [])
        if blocked:
            lines = []
            for entry in blocked:
                lines.append(
                    "%s (%s) FAILED: %s%s" % (
                        entry.get("hook"), entry.get("agent"),
                        " ".join(str(entry.get("detail") or "").split())[:400],
                        " -> the event '%s' was never set" % entry.get("event")
                        if entry.get("event") else "",
                    )
                )
            message += ". Blocked signals: " + " | ".join(lines)
        return message

    # --- diagnostics ------------------------------------------------------

    def snapshot(self):
        """A small, test-friendly description of the runtime state."""
        return {
            "live": sorted(self.pool),
            "ready": [h.name for h in self.ready],
            "waiting": {k: [h.name for h in v] for k, v in self.waiting.items()},
            "events": self.events.names(),
            "max_agents": self.max_agents,
            "blocked_signals": [dict(entry) for entry in
                                list(getattr(self, "blocked_signals", []) or [])],
        }


# --- static validation of Wait Event producers ------------------------------

# A producer is a TOOL CALL written as TEXT in a stage prompt, so this matching
# is deliberately lexical: it is never evaluated. Heuristic by design - a
# non-literal name is simply not detected.
_EVENT_CALL_RE = re.compile(
    r"set_event_(?:success|loop|jump)\s*\(([^)]*)\)")
_NAMES_KW_RE = re.compile(r"\bnames\s*=\s*(\[[^\]]*\]|['\"][^'\"]*['\"])")
_NUMBERED_NAME_KW_RE = re.compile(r"\bname(\d+)\s*=\s*['\"]([^'\"]*)['\"]")
_NAME_KW_RE = re.compile(r"\bname\s*=\s*['\"]([^'\"]*)['\"]")
_QUOTED_RE = re.compile(r"['\"]([^'\"]*)['\"]")
# Every keyword argument a producer accepts, removed before the positional
# (summary, name1, name2, ...) form is read off the remainder.
_KW_ARG_RE = re.compile(
    r"\b(?:summary|modifier|target|names|name\d*)\s*=\s*"
    r"(?:\[[^\]]*\]|['\"][^'\"]*['\"]|[^,)]*)")


def _quoted_literals(text):
    """Every non-empty single- or double-quoted literal in ``text``."""
    return [lit for lit in _QUOTED_RE.findall(text or "") if lit]


def _event_names_in_call(arg_text):
    """Event names declared by ONE set_event_success / loop / jump call.

    The documented canonical form is ``summary`` plus ``names`` (a list). The
    tolerated forms are numbered keys (``name1``, ``name2``, ...) and a bare
    ``name``. With no keyword form at all the arguments are positional, i.e.
    ``(summary, name1, name2, ...)``, so every literal AFTER the first one is
    an event name. A lone literal names NOTHING - which is exactly the
    mistake the startup gate must keep reporting.
    """
    arg_text = arg_text or ""
    names = []
    listed = _NAMES_KW_RE.search(arg_text)
    if listed:
        names.extend(_quoted_literals(listed.group(1)))
    numbered = _NUMBERED_NAME_KW_RE.findall(arg_text)
    if numbered:
        for _index, value in sorted(numbered, key=lambda pair: int(pair[0])):
            if value:
                names.append(value)
    if not listed and not numbered:
        single = _NAME_KW_RE.search(arg_text)
        if single and single.group(1):
            names.append(single.group(1))
    if names:
        return names
    positional = _KW_ARG_RE.sub("", arg_text)
    for segment in [s.strip() for s in positional.split(",")][1:]:
        names.extend(_quoted_literals(segment))
    return names


def stage_wait_event(stage):
    """Return the Wait Event name declared by a stage, or None."""
    if not stage:
        return None
    value = stage.get("wait_event")
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def collect_wait_events(agent_classes):
    """Map every Wait Event name to the agent classes/stages that wait on it."""
    waits = {}
    for name, cfg in (agent_classes or {}).items():
        for stage in getattr(cfg, "prompt_chain", []) or []:
            event = stage_wait_event(stage)
            if event:
                waits.setdefault(event, []).append("%s/%s" % (name, stage.get("name")))
    return waits


def collect_producer_events(agent_classes):
    """Collect every event name a set_event_success / loop / jump call names.

    Producers are tool calls made by the model, not a declarative field, so the
    only static evidence available is the literal text written in a stage's
    prompt. The accepted spellings are the ones the tool itself accepts - the
    positional ``set_event_success('a summary', 'a', 'b')``, ``names=[...]``,
    ``name1=...`` and ``name=...`` (see ``_event_names_in_call``); a producer
    call with no event name contributes nothing, so a stale single-argument call
    surfaces at startup instead of hanging later. This stays intentionally
    heuristic: a non-literal name is not detected and is reported as missing.
    """
    producers = set()
    for cfg in (agent_classes or {}).values():
        texts = [getattr(cfg, "system_prompt", "") or ""]
        for stage in getattr(cfg, "prompt_chain", []) or []:
            texts.append(stage.get("content", "") or "")
        for text in texts:
            for arg_text in _EVENT_CALL_RE.findall(text):
                producers.update(_event_names_in_call(arg_text))
    return producers


def validate_wait_events(agent_classes):
    """Return a list of Wait Event names with no known producer.

    Empty list means every waited-on event is at least mentioned as a
    ``set_event_*(...)`` literal somewhere, so a typo in a wait name is
    reported instead of hanging the run.

    LATE BINDING relaxes this in exactly two places: a PLACEHOLDER wait
    (``@name``) is exempt, because a ``bind_tool`` call satisfies it at runtime
    (it is not a prompt literal); and a wait with no literal producer is
    accepted when ANY prompt produces a placeholder, because a binding could map
    that placeholder onto this name. A workspace that never mentions the
    placeholder prefix keeps the old behaviour byte for byte.
    """
    waits = collect_wait_events(agent_classes)
    producers = collect_producer_events(agent_classes)
    late_binding_possible = any(is_placeholder(name) for name in producers)

    missing = []
    for name in waits:
        if name in producers:
            continue
        if is_placeholder(name):
            continue
        if is_ep(name):
            # A REMOTE endpoint is produced by its peer: a service EP by an
            # inbound connect, a CEP by its conversation. Neither is a
            # `set_event_*` prompt literal, so the local producer rule cannot
            # apply to it.
            continue
        if late_binding_possible:
            continue
        missing.append(name)
    return sorted(missing)
