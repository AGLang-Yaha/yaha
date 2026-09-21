# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

"""Per-class identities inside the workspace SOUL.md.

A workspace has ONE ``.yaha/SOUL.md``. It may carry a DEFAULT identity
(everything before the first ``## `` heading) followed by per-class IDENTITY
SECTIONS::

    # SOUL

    You are {agent_name}, a helpful and efficient agent.

    ## ChatAgent
    You are Mina, a warm, curious woman.

    ## CodeAgent
    You are Kris, a blunt, precise man.

A class whose canonical name (or short code) matches a heading uses that
section as its soul; every other class falls back to the DEFAULT identity.

The format is deliberately additive: a SOUL.md with NO ``## `` heading is the
identity of EVERY class, exactly as before, so a workspace that never opts in
keeps byte-identical prompts.

How a section is delimited
--------------------------
A section starts at a line beginning with two hashes followed by whitespace.
Heading matches are used as SPANS (never as a consuming split), so the
``## Name`` line stays INSIDE the section it introduces.
"""

import re

# A section heading: two hashes plus whitespace at the start of a line; the rest
# of the line is the class key. ``### x`` is NOT a heading, because a third hash
# is not whitespace.
_HEADING_RE = re.compile(r"^##\s+(?P<key>.*)$", re.MULTILINE)


def _key(text):
    """Normalise a heading or a class identity to one comparison key."""
    if text is None:
        return ""
    return str(text).strip().lower()


def parse_soul_sections(text):
    """Split a SOUL.md into ``(default_text, {key: section_text})``.

    ``default_text`` is everything before the first heading (the DEFAULT
    identity). Each section text INCLUDES its ``## Name`` heading line and runs
    up to the next heading. Keys are heading texts, stripped and lower-cased.
    Two headings normalising to the same key keep the LAST one.

    A file with no heading yields ``(text, {})``: callers detect that case and
    use the text verbatim (see ``soul_for_class``). Any non-string input yields
    ``("", {})``. This function never raises.
    """
    if not isinstance(text, str):
        return "", {}

    headings = list(_HEADING_RE.finditer(text))
    if not headings:
        return text, {}

    sections = {}
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        key = _key(match.group("key"))
        if key:
            sections[key] = text[match.start():end]

    return text[:headings[0].start()], sections


def soul_for_class(text, agent_class, class_code=None):
    """Return the SOUL identity text one agent class should be given.

    Resolution order:

    1. No heading in the file at all -> the WHOLE text, untouched. This is the
       backward-compatibility guarantee: an existing single-identity SOUL.md
       behaves exactly as before.
    2. A section whose key equals the class's canonical name (case-insensitive).
    3. A section whose key equals the class's short code (case-insensitive).
    4. The DEFAULT identity (trimmed), or ``""`` when the file has sections but
       no default preamble. Another class's section is NEVER substituted.

    Section and default text is trimmed; the sectionless path (1) is not, so it
    stays byte-identical. Never raises.
    """
    if not isinstance(text, str) or not text:
        return ""

    default, sections = parse_soul_sections(text)
    if not sections:
        return text

    for candidate in (agent_class, class_code):
        key = _key(candidate)
        if key and key in sections:
            return sections[key].strip()

    return default.strip()
