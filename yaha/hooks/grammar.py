#!/usr/bin/env python3
"""Stage-hook grammar for AgentClass.md.

Hooks are declared as *stage properties*, exactly like ``Allowed Tools:``::

    - **Coding:**
        - **preenter:**
            - bash (required, capture): git status --porcelain
            - python (capture):
                ```
                print(yaha.agent.agent_class.short_name)
                ```
        - **preexit:**
            - python (required):
                ```
                assert yaha.stage.name == 'Coding'
                ```

Rules
-----
* A hook block is introduced by a bullet whose whole text is
  ``- **<name>:**`` (case-insensitive) where ``name`` is one of
  ``preenter`` / ``pretool`` / ``posttool`` / ``preexit``.
* The block owns every following line that is more indented than its banner,
  up to the first non-blank line at the same or lower indentation.
* Items inside a block are bullets ``- bash ...`` / ``- python ...`` at one
  common indentation, in declaration order.
* ``(required)`` / ``(capture)`` modifiers are optional, case-insensitive and
  order-free; anything else is an error, because silently ignoring a typo would
  leave a hook looking armed when it is not.
* A ``python`` item needs a fenced code block (at least three backticks or
  tildes); the fence body is dedented. An inline body is an error.
* A ``bash`` item takes an inline command, optionally continued on deeper
  indented lines. A YAML-style literal block scalar header (``|``, ``|-``,
  ``|+``) as the whole inline text is layout: the body is the command, dedented
  as a block. The folded form (``>``) and explicit indentation indicators
  (``|2``) are rejected with ``HookSpecError`` instead of being silently kept.
* ``- **Inherited:** All`` inside a hook block means "keep the ancestor hooks
  and append mine after them"; its absence means "my hooks replace the
  ancestor's".
* Hook regions are removed from the returned ``cleaned`` text, so hook code can
  never leak into a stage prompt or into the stage's declared-field detection.
* A malformed hook block raises :class:`HookSpecError` at load time.
"""

import re
import textwrap


class HookSpecError(Exception):
    """Raised when a stage hook block in AgentClass.md is malformed."""


HOOK_NAMES = ("preenter", "pretool", "posttool", "preexit")

_INHERIT_LINE_RE = re.compile(
    r'^\s*-\s*\*\*\s*Inherited\s*:\*\*\s*(?P<value>[A-Za-z]+)\s*$',
    re.IGNORECASE,
)

_HOOK_BANNER_RE = re.compile(
    r'^(?P<indent>[ \t]*)-[ \t]*\*\*[ \t]*(?P<name>'
    + "|".join(HOOK_NAMES)
    + r')[ \t]*:\*\*[ \t]*$',
    re.IGNORECASE,
)

_ITEM_RE = re.compile(
    r'^(?P<indent>[ \t]*)-[ \t]*(?P<kind>bash|python)'
    r'(?P<mods>\s*\([^)]*\))?[ \t]*:(?P<rest>.*)$',
    re.IGNORECASE,
)

# A whole-line YAML-style block scalar header: `|`, `|-`, `|+`, `>`, `>2`, ...
# Only an entire `rest` counts, so `> out.txt`, `|tee log` and `|| true` remain
# ordinary shell text.
_BLOCK_SCALAR_RE = re.compile(
    r'^(?P<indicator>[|>])(?P<chomp>[+-]?)(?P<indent>[0-9]?)$'
)

_FENCE_RE = re.compile(r'^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>.*)$')


def inherit_marker_value(line):
    """Return True/False when ``line`` is an ``Inherited: All|None`` marker.

    Returns ``None`` when the line is not such a marker. Used both for the
    stage-level flag and for the per-block flags, so the two can never drift.
    """
    m = _INHERIT_LINE_RE.match(line)
    if not m:
        return None
    value = m.group('value').strip().lower()
    if value == "all":
        return True
    if value == "none":
        return False
    raise HookSpecError(
        "unknown Inherited value %r (expected All or None)" % value
    )


def _indent_of(line):
    return len(line) - len(line.lstrip(" \t"))


def _parse_modifiers(raw, where):
    if raw is None:
        return set()
    text = raw.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    mods = [m.strip().lower() for m in re.split(r"[,\s]+", text) if m.strip()]
    allowed = ("required", "capture")
    for mod in mods:
        if mod not in allowed:
            raise HookSpecError(
                "%s: unknown hook modifier %r (allowed: %s)"
                % (where, mod, ", ".join(allowed))
            )
    if len(mods) != len(set(mods)):
        raise HookSpecError(
            "%s: duplicate hook modifier in (%s)" % (where, raw.strip())
        )
    return set(mods)


def _extract_python_fence(body, where):
    """Return the code inside the first fenced block of ``body``."""
    open_index = None
    fence = None
    for i, line in enumerate(body):
        m = _FENCE_RE.match(line)
        if m:
            open_index = i
            fence = m.group("fence")
            info = (m.group("info") or "").strip()
            if info and info.lower() not in ("python", "py"):
                raise HookSpecError(
                    "%s: python hook fence has an unexpected info string %r"
                    % (where, info)
                )
            break
    if open_index is None:
        raise HookSpecError(
            "%s: a python hook needs a fenced code block (``` ... ```)" % where
        )
    close_index = None
    for j in range(open_index + 1, len(body)):
        m = _FENCE_RE.match(body[j])
        if not m:
            continue
        token = m.group("fence")
        if token[0] == fence[0] and len(token) >= len(fence) \
                and not (m.group("info") or "").strip():
            close_index = j
            break
    if close_index is None:
        raise HookSpecError(
            "%s: the python hook code block is not terminated" % where
        )
    inner = body[open_index + 1:close_index]
    code = textwrap.dedent("\n".join(inner)).strip("\n")
    if not code.strip():
        raise HookSpecError("%s: the python hook code block is empty" % where)
    return code


def _dedent_line(line, item_indent):
    if len(line) >= item_indent and line[:item_indent].strip() == "":
        return line[item_indent:]
    return line.lstrip()


def _bash_code(rest, body, item_indent, where):
    """The effective shell command of one ``- bash:`` item.

    A YAML-style block scalar header written as the WHOLE inline text is layout,
    not command text: ``- bash: |`` takes the following (deeper indented) lines
    as the command and dedent them as a block, so the ``|`` can never reach
    ``/bin/bash``. The folded form ``>`` is rejected instead of being silently
    reinterpreted (folding turns newlines into spaces, which this grammar does
    not implement), and so is an explicit indentation indicator such as ``|2``.

    Anything else on the line is an ordinary command: ``> out.txt``, ``|tee log``
    and ``|| true`` are left exactly as written.
    """
    lines = [_dedent_line(nxt, item_indent) for nxt in body]
    header = _BLOCK_SCALAR_RE.match(rest)
    if header:
        if header.group("indicator") == ">":
            raise HookSpecError(
                "%s: folded block scalars ('>') are not supported; use '|' to "
                "keep the body verbatim, or write the command inline" % where
            )
        if header.group("indent"):
            raise HookSpecError(
                "%s: explicit block-scalar indentation indicators are not "
                "supported; indent the body deeper than the item instead"
                % where
            )
        code = textwrap.dedent("\n".join(lines)).strip("\n")
        if not code.strip():
            raise HookSpecError(
                "%s: the bash hook has an empty command" % where
            )
        return code
    parts = []
    if rest:
        parts.append(rest)
    parts.extend(lines)
    while parts and not parts[-1].strip():
        parts.pop()
    code = "\n".join(parts).strip()
    if not code:
        raise HookSpecError("%s: the bash hook has an empty command" % where)
    return code


def _parse_items(region, hook_name, where):
    """Parse one hook block region into hook specs, plus its inherit flag."""
    specs = []
    inherit_all = False
    index = 0
    total = len(region)

    while index < total and not region[index].strip():
        index += 1
    if index >= total:
        raise HookSpecError("%s: the hook block declares no hooks" % where)
    item_indent = _indent_of(region[index])

    while index < total:
        line = region[index]
        if not line.strip():
            index += 1
            continue
        marker = inherit_marker_value(line)
        if marker is not None:
            inherit_all = marker
            index += 1
            continue
        if _indent_of(line) < item_indent:
            raise HookSpecError(
                "%s: unexpected dedent inside the hook block: %r"
                % (where, line.strip())
            )
        if _indent_of(line) != item_indent:
            raise HookSpecError(
                "%s: hook items must share one indentation; got %r"
                % (where, line.strip())
            )
        m = _ITEM_RE.match(line)
        if not m:
            raise HookSpecError(
                "%s: hook items must be '- bash ...' or '- python ...', got %r"
                % (where, line.strip())
            )
        number = len(specs) + 1
        item_where = "%s item %d" % (where, number)
        kind = m.group("kind").lower()
        mods = _parse_modifiers(m.group("mods"), item_where)
        rest = m.group("rest").strip()

        # A following line belongs to this item while it is indented deeper or
        # is not itself the start of a sibling bullet.
        body = []
        j = index + 1
        while j < total:
            nxt = region[j]
            if nxt.strip():
                if _indent_of(nxt) < item_indent:
                    break
                if _indent_of(nxt) == item_indent and nxt.lstrip().startswith("-"):
                    break
            body.append(nxt)
            j += 1

        if kind == "python":
            if rest:
                raise HookSpecError(
                    "%s: a python hook takes no inline code; use a fenced block"
                    % item_where
                )
            code = _extract_python_fence(body, item_where)
        else:
            code = _bash_code(rest, body, item_indent, item_where)

        specs.append({
            "kind": kind,
            "required": "required" in mods,
            "capture": "capture" in mods,
            "code": code,
            "index": number,
            "label": "%s[%d]" % (hook_name, number),
        })
        index = j

    return specs, inherit_all


def parse_stage_hooks(block, stage_label="stage"):
    """Extract the hook blocks of one stage block.

    Returns ``(hooks, hook_inherit, declared, cleaned)`` where

    * ``hooks`` maps each hook name to its ordered list of hook specs,
    * ``hook_inherit`` maps each hook name to its per-block ``Inherited: All``
      flag (False when absent or explicit ``None``),
    * ``declared`` is the set of hook names whose banner really appeared, so the
      caller can tell "not written" from "written but empty",
    * ``cleaned`` is the block with every hook region blanked out, so hook code
      can never reach the stage prompt or the field detection.
    """
    hooks = {name: [] for name in HOOK_NAMES}
    hook_inherit = {name: False for name in HOOK_NAMES}
    declared = set()

    lines = block.split("\n")
    if not any(_HOOK_BANNER_RE.match(line) for line in lines):
        return hooks, hook_inherit, declared, block

    blanked = list(lines)
    i = 0
    while i < len(lines):
        banner = _HOOK_BANNER_RE.match(lines[i])
        if not banner:
            i += 1
            continue
        name = banner.group("name").lower()
        banner_indent = _indent_of(lines[i])
        j = i + 1
        while j < len(lines):
            line = lines[j]
            if line.strip() and _indent_of(line) <= banner_indent:
                break
            j += 1
        where = "%s: hook %s" % (stage_label, name)
        specs, inherit_all = _parse_items(lines[i + 1:j], name, where)
        hooks[name] = specs
        hook_inherit[name] = inherit_all
        declared.add(name)
        for k in range(i, j):
            blanked[k] = ""
        i = j

    return hooks, hook_inherit, declared, "\n".join(blanked)


def relabel_hooks(hooks):
    """Re-number hook labels after a merge so diagnostics point at the
    effective list (1-based, in run order)."""
    relabelled = {}
    for name, specs in (hooks or {}).items():
        fresh = []
        for position, spec in enumerate(specs or [], start=1):
            item = dict(spec)
            item["index"] = position
            item["label"] = "%s[%d]" % (name, position)
            fresh.append(item)
        relabelled[name] = fresh
    return relabelled
