#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

"""Shared fixture for the shipped class template.

Canonical moved to an INCLUDE-BASED template layout: ``templates/AgentClass.md``
is now a small root file whose classes are pulled in with
``<!--include path="..." -->`` directives (const.md, base.md, utils.md,
chat.md, coder.md today). Two consequences for the tests:

* reading the root file RAW only ever sees the stub - the class content lives in
the included files - so a test that needs the CLASS TEXT must load it through
``yaha.core.md_loader`` (includes resolved);
* a temp workspace that copies only the root file leaves the loader with
dangling includes and raises ``MarkdownLoadError``, so every temp ``.yaha/`` the
engine loads needs the whole include CLOSURE.

This module is the single place that knows both rules; the worktree test suite
imports it so a future template split cannot silently stale-out a test again.
"""
import shutil
from pathlib import Path

import yaha
from yaha.core.md_loader import (_INCLUDE_RE, _strip_comments,
                                load_md_with_includes)

TEMPLATE_DIR = Path(yaha.__file__).resolve().parent / "templates"
TEMPLATE = TEMPLATE_DIR / "AgentClass.md"
# The shipped AUTHORING/EVENTS documentation used to be the header of the
# monolithic template; the include refactor moved it to
# `docs/agentclass-authoring-guide.md` (renamed in the open-source cleanup).
AGENTCLASS_DOC = Path(yaha.__file__).resolve().parent / "docs" / "agentclass-authoring-guide.md"


def agentclass_doc_text():
    """The shipped authoring/events documentation (trust model, auto-reset...)."""
    return AGENTCLASS_DOC.read_text(encoding="utf-8")


def include_closure(root=TEMPLATE):
    """Return ``[root, *every included file]`` in include order (deduped)."""
    seen, order, queue = set(), [], [Path(root)]
    while queue:
        path = Path(queue.pop(0)).resolve()
        if path in seen:
            continue
        seen.add(path)
        order.append(path)
        if not path.exists():
            continue
        for match in _INCLUDE_RE.finditer(path.read_text(encoding="utf-8")):
            queue.append(path.parent / match.group(1))
    return order


def strip_comments(text, source="<text>"):
    """Blank every ``<!--comment-->`` region (the loader's own rule)."""
    return _strip_comments(text, source)


def template_text():
    """The class template with every include inlined and comments stripped."""
    return load_md_with_includes(str(TEMPLATE))


def install_template(yaha_dir):
    """Copy the whole template closure flat into ``<yaha_dir>`` (a ``.yaha/``).

    Returns the copied file names. Include paths are plain sibling names, so a
    flat copy resolves exactly like the shipped package directory does.
    """
    dest = Path(yaha_dir)
    dest.mkdir(parents=True, exist_ok=True)
    names = []
    for src in include_closure():
        shutil.copyfile(src, dest / src.name)
        names.append(src.name)
    return names
