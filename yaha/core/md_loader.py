# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

import ast
import json
import os
import re


class MarkdownLoadError(Exception):
    """Raised when a markdown load directive (include/ref/define/comment) fails.

    The message is a visible HTML-style comment so callers that relay it
    verbatim keep a marker where the failed directive lived.
    """


# ---------------------------------------------------------------------------
# Directive patterns
# ---------------------------------------------------------------------------

_INCLUDE_RE = re.compile(r'<!--\s*include\s+(?:path=)?["\']([^"\']+)["\']\s*-->')

# `<!-- define name="x", params=[...] -->\n...body...\n<!-- enddefine -->`
_DEFINE_RE = re.compile(
    r'<!--\s*define\s+name\s*=\s*["\']([^"\']*)["\']'
    r'(?:\s*,\s*params\s*=\s*(\[[^\]]*\]))?\s*-->\s*?(.*?)'
    r'<!--\s*enddefine\s*-->',
    re.IGNORECASE | re.DOTALL,
)

# `<!--ref name="x", params=[...] -->`
_REF_RE = re.compile(
    r'<!--\s*ref(?:\s+name\s*=\s*)?["\']([^"\']*)["\']'
    r'(?:\s*,\s*params\s*=\s*(\[[^\]]*\]))?\s*-->',
    re.IGNORECASE | re.DOTALL,
)

_MAX_EXPANSION_DEPTH = 16


class _MacroStore:
    """Ordered map of macro name -> (params list, body, defining file)."""

    def __init__(self):
        self._macros = {}

    def add(self, name, params, body, source):
        if not name:
            raise MarkdownLoadError(f"<!-- Macro error: empty/unnamed macro definition in {source} -->")
        if name in self._macros:
            prev = self._macros[name][2]
            raise MarkdownLoadError(
                f"<!-- Macro error: duplicate macro '{name}' defined in {prev} and {source} -->"
            )
        self._macros[name] = (params, body.strip("\n"), source)

    def get(self, name):
        return self._macros.get(name)

    def names(self):
        return sorted(self._macros.keys())


# ---------------------------------------------------------------------------
# Parsing / expansion helpers
# ---------------------------------------------------------------------------


_BLOCKQUOTE_LINE_RE = re.compile(r'^\s*>')


def _parse_params(raw):
    if raw is None:
        return []
    text = raw.strip()
    try:
        val = json.loads(text) if text else []
    except (ValueError, TypeError):
        try:
            val = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            raise MarkdownLoadError(f"<!-- Macro error: invalid params list: {raw!r} -->")
    if not isinstance(val, list):
        raise MarkdownLoadError(f"<!-- Macro error: params must be a list: {raw!r} -->")
    return [str(v) for v in val]


def _find_line_prefix(content, pos):
    """Return the text between line start and pos (the existing indentation
    and any `>` blockquote marker), or empty string."""
    ls = content.rfind("\n", 0, pos) + 1
    return content[ls:pos]


def _register_defines(content, store, source):
    """Register every define block in content (does not mutate content)."""
    for m in _DEFINE_RE.finditer(content):
        store.add(m.group(1), _parse_params(m.group(2)), m.group(3), source)


def _strip_defines(content):
    """Remove define/enddefine blocks; return cleaned text."""
    return _DEFINE_RE.sub("", content)


# `<!--comment--> ... <!--endcomment-->` - author-facing only; stripped at read
# time and never part of the loaded output.
_COMMENT_START_RE = re.compile(r'<!-- *comment *-->', re.IGNORECASE)
_COMMENT_END_RE = re.compile(r'<!-- *endcomment *-->', re.IGNORECASE)


def _strip_comments(content, source):
    """Remove every `<!--comment--> ... <!--endcomment-->` block.

    Comment blocks are author-facing only: they are stripped before define
    registration, include inlining and macro expansion, so a directive parked
    inside a comment (`include`, `define`, `ref`) is inert. Blocks do not nest;
    the first `<!--endcomment-->` closes the block.

    The original line numbering is preserved: a removed block is replaced by as
    many newlines as it spanned, so a following line keeps its line number and
    an inline comment does not glue its neighbours together. An unterminated
    block or a stray closing marker raises MarkdownLoadError (fail-fast).
    """
    if not _COMMENT_START_RE.search(content) and not _COMMENT_END_RE.search(content):
        return content  # fast path: files without comment markers are untouched

    out = []
    pos = 0
    while True:
        start = _COMMENT_START_RE.search(content, pos)
        close = _COMMENT_END_RE.search(content, pos)
        if close is not None and (start is None or close.start() < start.start()):
            raise MarkdownLoadError(
                f"<!-- Comment error: '<!--endcomment-->' without a matching "
                f"'<!--comment-->' in {source} -->"
            )
        if start is None:
            out.append(content[pos:])
            break
        out.append(content[pos:start.start()])
        end = _COMMENT_END_RE.search(content, start.end())
        if end is None:
            raise MarkdownLoadError(
                f"<!-- Comment error: unterminated '<!--comment-->' block "
                f"in {source} -->"
            )
        out.append("\n" * content[start.start():end.end()].count("\n"))
        pos = end.end()
    return "".join(out)


def _apply_blockquote_continuation(replace, line_prefix):
    """When a ref occurs inside a markdown blockquote, ensure every line of
    the expanded body is preceded by `>` so it stays inside the quote."""
    if not line_prefix or _BLOCKQUOTE_LINE_RE.match(line_prefix) is None:
        return replace
    marker = "\n" + line_prefix
    replace = replace.rstrip("\n")
    head, sep, tail = replace.partition("\n")
    if not sep:
        return replace
    return head + marker + tail.replace("\n", marker)


def _expand_once(content, store, source):
    """Replace one layer of `<!--ref ... -->` directives.

    A macro expanded inside a blockquote gets `>` markers on every added
    line so it remains part of the quote for the AgentClass.md parser.
    """

    def repl(m):
        name = m.group(1)
        params = _parse_params(m.group(2))
        meta = store.get(name)
        if meta is None:
            known = ", ".join(store.names())
            raise MarkdownLoadError(
                f"<!-- Macro error: unknown macro '{name}' referenced in {source}; "
                f"known macros: {known} -->"
            )
        declared, body, def_source = meta
        if declared and len(params) != len(declared):
            raise MarkdownLoadError(
                f"<!-- Macro error: macro '{name}' expects {len(declared)} params, "
                f"got {len(params)} in {source} -->"
            )
        for i, _ in enumerate(declared, start=1):
            value = params[i - 1]
            body = re.sub(r"\{\{\s*" + str(i) + r"\s*\}\}", lambda _m, _v=value: _v, body)

        prefix = _find_line_prefix(content, m.start())
        return _apply_blockquote_continuation(body, prefix)

    return _REF_RE.sub(repl, content)


def _expand_all_refs(content, store, source, depth=0):
    """Expand refs recursively (nested macros allowed) with a depth cap."""
    if depth > _MAX_EXPANSION_DEPTH:
        raise MarkdownLoadError(
            f"<!-- Macro error: exceeded maximum expansion depth ({_MAX_EXPANSION_DEPTH}) in {source} -->"
        )
    if not _REF_RE.search(content):
        return content
    expanded = _expand_once(content, store, source)
    if expanded == content:
        return content  # no progress guard
    return _expand_all_refs(expanded, store, source, depth + 1)


# ---------------------------------------------------------------------------
# Include tree walking
# ---------------------------------------------------------------------------


def _collect_macros(file_path, store, chain):
    """Recursively collect macros from a file and every include it references."""
    file_path = os.path.abspath(file_path)
    if file_path in chain:
        raise MarkdownLoadError(f"<!-- Include error: circular include detected at {file_path} -->")
    if not os.path.exists(file_path):
        raise MarkdownLoadError(f"<!-- Include error: file not found: {file_path} -->")
    chain = chain | {file_path}

    with open(file_path, "r", encoding="utf-8") as f:
        content = _strip_comments(f.read(), file_path)

    _register_defines(content, store, file_path)

    def recurse(m):
        inc = m.group(1).strip()
        return _collect_macros(os.path.join(os.path.dirname(file_path), inc), store, chain)

    _INCLUDE_RE.sub(recurse, content)


def _build_tree_text(file_path, seen):
    """Inline all includes and strip every define block; return plain text."""
    file_path = os.path.abspath(file_path)
    if file_path in seen:
        raise MarkdownLoadError(f"<!-- Include error: circular include detected at {file_path} -->")
    if not os.path.exists(file_path):
        raise MarkdownLoadError(f"<!-- Include error: file not found: {file_path} -->")
    seen = seen | {file_path}

    with open(file_path, "r", encoding="utf-8") as f:
        content = _strip_comments(f.read(), file_path)

    def repl(m):
        inc = m.group(1).strip()
        return _build_tree_text(os.path.join(os.path.dirname(file_path), inc), seen)

    return _INCLUDE_RE.sub(repl, content)


def load_md_with_includes(file_path, seen=None):
    """Load a markdown file with `<!-- include ... -->` expansion.

    `<!--comment--> ... <!--endcomment-->` blocks are stripped first, so a
    commented-out include is inert; `<!-- define -->` and `<!--ref ... -->`
    directives are intentionally left untouched (this loader is macro-inert).

    Missing, circular, or duplicate includes raise MarkdownLoadError
    (strict/fail-fast); the signature is unchanged for existing callers.
    """
    if seen is None:
        seen = set()
    return _build_tree_text(file_path, frozenset(seen))


def load_md_with_macros(file_path, seen=None):
    """Load markdown with includes and `<!--ref ... -->` macro expansion.

    Macro bodies may be defined in the root file or in any file it includes.
    All macro references are expanded after the whole tree is collected;
    define blocks never appear in the result. Unknown macros and missing
    files raise MarkdownLoadError (fail-fast).

    `<!--comment--> ... <!--endcomment-->` blocks are stripped while the tree is
    read - before define registration, include inlining and ref expansion - so a
    directive parked inside a comment is inert and comment text never reaches the
    loaded content. Malformed comment markers raise MarkdownLoadError.
    """
    if seen is None:
        seen = set()
    seen = frozenset(seen)

    store = _MacroStore()
    _collect_macros(file_path, store, seen)
    flat = _strip_defines(_build_tree_text(file_path, seen))
    return _expand_all_refs(flat, store, os.path.abspath(file_path))
