#!/usr/bin/env python3
"""Read-only DOM for python hooks.

A python hook runs in a separate persistent kernel process, so it cannot touch
the agent's live objects. Instead the parent builds a JSON-able *snapshot*
immediately before each hook block runs and the kernel binds it as ``yaha``::

    yaha.agent.agent_class.short_name        # 'CrewCda'
    yaha.agent.agent_class.inherits('CodeAgent')
    yaha.stage.name / yaha.stage.tools.allowed
    yaha.hook.name / kind / required / capture / index / label
                                             # the hook that is running now
                                             # (name is an alias of label;
                                             #  the hook's own `code` is not exposed)
    yaha.tool.name / args / ok / error / duration_ms
                                             # only inside pretool/posttool; in
                                             # pretool ok/error/duration_ms are None
                                             # because the call has not happened yet
    yaha.runtime.waiting                     # who is parked on which event
    yaha.workspace.root

Honest limitation: because the code runs out of process, the view is a snapshot
taken at block start, not a live RPC proxy. Nothing can change while a hook runs
(the agent owns execution and is blocked inside the hook), so the values are
current for the whole block. A live-proxy ``get`` channel is the upgrade path.

The view is strictly read-only in this patch: assigning to a public attribute
raises :class:`YahaReadOnlyError` rather than silently succeeding.
"""


class YahaReadOnlyError(AttributeError):
    """Raised when a hook tries to mutate the read-only ``yaha`` view."""


class _Node:
    """A read-only attribute tree over a plain dict/list snapshot."""

    __slots__ = ("_data",)

    def __init__(self, data):
        object.__setattr__(self, "_data", dict(data or {}))

    # -- read ------------------------------------------------------------
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        data = object.__getattribute__(self, "_data")
        if name not in data:
            known = ", ".join(sorted(data)) or "(none)"
            raise AttributeError(
                "yaha has no attribute %r here; available: %s" % (name, known)
            )
        return _wrap(data[name])

    def __getitem__(self, key):
        return _wrap(object.__getattribute__(self, "_data")[key])

    def get(self, key, default=None):
        data = object.__getattribute__(self, "_data")
        return _wrap(data[key]) if key in data else default

    def keys(self):
        return list(object.__getattribute__(self, "_data").keys())

    def items(self):
        return [(k, _wrap(v)) for k, v in object.__getattribute__(self, "_data").items()]

    def __contains__(self, key):
        return key in object.__getattribute__(self, "_data")

    def __iter__(self):
        return iter(object.__getattribute__(self, "_data"))

    def __len__(self):
        return len(object.__getattribute__(self, "_data"))

    def __eq__(self, other):
        if isinstance(other, _Node):
            return object.__getattribute__(self, "_data") == object.__getattribute__(other, "_data")
        return NotImplemented

    def __repr__(self):
        return "yaha(%r)" % (object.__getattribute__(self, "_data"),)

    # -- write -----------------------------------------------------------
    def __setattr__(self, name, value):
        raise YahaReadOnlyError(
            "yaha.%s is read-only: hooks may observe the agent but not mutate it"
            % name
        )

    def __delattr__(self, name):
        raise YahaReadOnlyError("yaha.%s is read-only" % name)


class _ClassNode(_Node):
    """``yaha.agent.agent_class`` - adds ``inherits()`` over the lineage."""

    __slots__ = ()

    def inherits(self, class_name):
        target = str(class_name or "").strip().lower()
        lineage = object.__getattribute__(self, "_data").get("lineage") or []
        return any(str(entry).strip().lower() == target for entry in lineage)


def _wrap(value):
    if isinstance(value, _Node):
        return value
    if isinstance(value, dict):
        if value.get("__t") == "class":
            return _ClassNode(value)
        return _Node(value)
    if isinstance(value, (list, tuple)):
        return [_wrap(item) for item in value]
    return value


class YahaView(_Node):
    """The ``yaha`` object bound in every python hook namespace."""

    __slots__ = ()

    @classmethod
    def from_snapshot(cls, snapshot):
        return _wrap(dict(snapshot or {}))


# ---------------------------------------------------------------------------
# Parent-side snapshot builder
# ---------------------------------------------------------------------------

def _safe(getter, default=None):
    """Call ``getter`` and fall back to ``default`` - a snapshot must never
    be able to break a hook."""
    try:
        value = getter()
    except Exception:
        return default
    return default if value is None else value


def class_view(config, agent_classes=None):
    """Build the JSON-able ``agent_class`` node for a resolved class config."""
    classes = agent_classes or {}
    lineage = []
    seen = set()
    current = config
    while current is not None:
        name = getattr(current, "name", None)
        if name is None or name in seen:
            break
        seen.add(name)
        lineage.append(name)
        parent_name = getattr(current, "inherited", None)
        if not parent_name or parent_name == "None":
            current = None
        else:
            current = classes.get(parent_name)
    lineage.reverse()

    stages = {}
    for stage in _safe(lambda: list(getattr(config, "prompt_chain", []) or []), []):
        try:
            stages[str(stage.get("name"))] = {
                "name": stage.get("name"),
                "wait_event": stage.get("wait_event"),
                "allowed_tools": list(stage.get("allowed_tools") or []),
                "required_tools": list(stage.get("required_tools") or []),
                "exit_tools": list(stage.get("exit_tools") or []),
                "hooks": {
                    name: [spec.get("label") for spec in specs]
                    for name, specs in (stage.get("hooks") or {}).items() if specs
                },
            }
        except Exception:
            continue

    return {
        "__t": "class",
        "name": getattr(config, "name", None),
        "short_name": getattr(config, "code", None),
        "lineage": lineage,
        "model": getattr(config, "model", None),
        "stages": stages,
    }


def _hook_view(spec):
    """The readable ``yaha.hook`` node for one hook spec - a CLOSED view.

    Only the keys documented in ``yaha/docs/hooks.md`` are exposed. ``name`` is
    an alias of the spec's ``label`` (the label is what appears in hook output
    and logs). The hook's own source text (``code``) is deliberately NOT part of
    the view: a hook gains nothing from reading itself, and keeping hook source
    out of the DOM keeps the readable surface minimal and stable.
    """
    spec = spec or {}
    label = spec.get("label")
    return {
        "name": label,
        "label": label,
        "kind": spec.get("kind"),
        "required": bool(spec.get("required")),
        "capture": bool(spec.get("capture")),
        "index": spec.get("index"),
    }


def build_snapshot(agent, stage=None, hook=None, tool=None):
    """Read the live agent into a JSON-able read-only snapshot."""
    stage = stage or {}
    config = getattr(agent, "config", None)

    kernel_view = None
    engine = getattr(agent, "_hook_engine_obj", None)
    if engine is not None:
        kernel_view = engine.kernel_view()

    runtime_view = None
    runtime = getattr(agent, "runtime", None)
    if runtime is not None:
        runtime_view = _safe(lambda: dict(runtime.snapshot()), {})
        runtime_view["blocked_signals"] = _safe(
            lambda: [dict(entry) for entry in runtime.blocked_signals], []
        )
        runtime_view["active"] = _safe(lambda: runtime.active_name(), None)

    session = getattr(agent, "session", None)
    workspace_root = _safe(lambda: session.workspace_dir, None)

    tool_view = None
    if tool is not None:
        tool_view = {
            "name": tool.get("name"),
            "args": tool.get("args"),
            "ok": tool.get("ok"),
            "error": tool.get("error"),
            "duration_ms": tool.get("duration_ms"),
            "stage_attempt": tool.get("stage_attempt"),
        }

    return {
        "agent": {
            "name": _safe(lambda: config.name),
            "agent_id": _safe(lambda: agent.agent_id),
            "agent_class": class_view(config, getattr(agent, "agent_classes", None)) if config else None,
            "stage": stage.get("name"),
            "turn": _safe(lambda: agent._current_turn, 0),
            "attempt": _safe(lambda: agent._last_stage_turn, 0),
            "model": _safe(lambda: agent.model_cfg.model),
            "provider": _safe(lambda: agent.model_cfg.provider),
            "message_count": _safe(lambda: len(agent.history), 0),
            "context_size": _safe(lambda: agent.context_size),
            # Publish the SAME instrument the TUI shows, not the raw provider anchor:
            # the anchor is deliberately no longer updated by a compression, so a
            # consumer reading it alone would see a stale, unmarked pre-compression
            # value while the section bar says (est.).
            "prompt_tokens": _safe(
                lambda: int(agent._context_usage_ratio() * (agent.context_size or 0)), 0),
            "usage_is_predicted": _safe(lambda: bool(agent._usage_is_predicted), False),
            "kernel": kernel_view,
        },
        "stage": {
            "name": stage.get("name"),
            "index": _safe(lambda: agent._chain_index, 0),
            "chain_len": _safe(lambda: agent._chain_len, 0),
            "tools": {
                "allowed": list(stage.get("allowed_tools") or []),
                "required": list(stage.get("required_tools") or []),
                "exit": list(stage.get("exit_tools") or []),
            },
            "wait_event": stage.get("wait_event"),
            "hooks": {
                name: [spec.get("label") for spec in specs]
                for name, specs in (stage.get("hooks") or {}).items() if specs
            },
        },
        "hook": _hook_view(hook) if hook else None,
        "tool": tool_view,
        "runtime": runtime_view,
        "workspace": {
            "root": workspace_root,
            "session_dir": _safe(lambda: session.folder_name),
            "log_dir": _safe(lambda: session.log_dir),
            "cwd": workspace_root,
        },
        "log": {
            "error_log_path": _safe(lambda: session.get_error_log_path()),
            "agent_log_path": _safe(lambda: agent.logger.log_path),
        },
    }
