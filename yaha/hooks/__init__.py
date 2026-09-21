#!/usr/bin/env python3
"""Stage hooks for YAHA agents.

Four hook kinds can be declared as stage properties in ``AgentClass.md``:

* ``preenter`` - runs after a Wait Event is satisfied, before the stage prompt
  is built, so a ``(capture)`` hook can inform the model before it plans.
* ``pretool``  - runs before every tool call (``ask_user`` excluded).
* ``posttool`` - runs after every tool call.
* ``preexit``  - runs before the stage's exit action, including the event set
  performed by ``set_event_success`` / ``set_event_loop``.

Each item is either a bash command or a python block. Python blocks run in one
persistent kernel process per agent, so a later block sees the variables an
earlier block assigned.

``(required)`` turns a hook into a gate; without it, a failure is only logged.
``(capture)`` feeds the hook's output back to the model as an observation.

See ``yaha/docs/hooks.md`` for the full reference, the ordering diagram and the
inheritance rules.
"""

from .grammar import HOOK_NAMES, HookSpecError, parse_stage_hooks  # noqa: F401
from .dom import YahaReadOnlyError, YahaView, build_snapshot  # noqa: F401
from .kernel import KernelResult, PythonKernel  # noqa: F401
from .runner import CAPTURE_LIMIT, HookEngine, HookOutcome, truncate  # noqa: F401

__all__ = [
    "HOOK_NAMES",
    "HookSpecError",
    "parse_stage_hooks",
    "YahaReadOnlyError",
    "YahaView",
    "build_snapshot",
    "KernelResult",
    "PythonKernel",
    "HookEngine",
    "HookOutcome",
    "CAPTURE_LIMIT",
    "truncate",
]
