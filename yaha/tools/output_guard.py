"""Shared output-size safety guard for the model-facing tools.

A single tool result can otherwise consume the whole remaining context window:
``read_file`` over a wide line range and ``bash`` on a chatty command both hand
back their payload in full. This module bounds ONE tool result against HALF of
the window that is still free at call time and - when it truncates - tells the
model exactly how much it is *not* seeing (actual bytes read, bytes truncated).

Unit conventions are taken from the rest of YAHA rather than invented:

* ``Agent.context_size`` and ``Agent._last_prompt_tokens`` are counted in
  TOKENS, and the occupancy feeding the budget below comes from
  ``Agent._context_usage_ratio()`` (a provider anchor plus a calibrated
  characters delta, never a bare characters/4 reading);
* the characters-to-tokens conversion lives in ``core/context_calibration.py``
  (``ContextCalibration``). ``DEFAULT_TOKENS_PER_CHAR`` mirrors its cold-start
  default of 4 characters per token, which is the assumption the byte budget
  below still makes - exact only until real observations refine the rate;
* a tool result is measured in UTF-8 BYTES - ``Agent`` already prints
  ``len(tool_result.encode("utf-8")) / 1024`` as the output size.

The byte budget is therefore ``remaining_tokens * CHARS_PER_TOKEN / 2``.
Applying the characters-per-token factor to *bytes* is deliberately
conservative for non-ASCII text: a multi-byte character consumes several budget
units, so the guard may under-fill the window but can never over-fill it.

Degradation rules (the guard is never silently unbounded):

* ``context_size`` unknown/falsy, or the window already exhausted
  (``remaining <= 0``) -> ``MIN_OUTPUT_BUDGET_BYTES``;
* ``max_bytes is None`` -> the caller opted out (a direct caller outside the
  agent tool dispatch); the text is returned untouched. The agent always passes
  a real integer, so every model-visible tool result is guarded;
* an output that fits exactly in the budget is NOT truncated and gets no
  marker - the returned object is the very same string.
"""

# Characters-per-token COLD START, mirroring core/context_calibration.py
# (DEFAULT_TOKENS_PER_CHAR = 0.25). Applied to BYTES here; the agent passes its
# calibrated density instead of relying on this nominal value.
CHARS_PER_TOKEN = 4

# Cap: one half of the remaining window.
TRUNCATION_DIVISOR = 2

# Smallest budget the guard will ever apply. Used when the window cannot be
# measured (unknown) or is already exhausted, so that "cannot measure" degrades
# to "small but bounded", never to "unbounded".
MIN_OUTPUT_BUDGET_BYTES = 2048

# Marker opening, exposed so callers/tests can recognise a guarded result
# without re-implementing the footer format.
TRUNCATION_MARKER_PREFIX = "[... OUTPUT TRUNCATED"

_LIMIT_PERCENT = int(100 / TRUNCATION_DIVISOR)

_FOOTER_TEMPLATE = (
    "\n\n[... OUTPUT TRUNCATED by the YAHA output guard: read {shown} bytes of "
    "{original} bytes; truncated {dropped} bytes (limit = {percent}% of the "
    "remaining context window = {limit} bytes). Re-read a narrower range "
    "(smaller end_line, or head/tail via bash) if you need the omitted "
    "content. ...]"
)


def compute_output_budget(context_size, used_tokens, chars_per_token=CHARS_PER_TOKEN):
    """Byte cap for one tool result: half of the remaining context window.

    ``context_size`` is a token count (``Agent.context_size``), ``used_tokens`` the
    occupancy reported by the agent's own instrument, and ``chars_per_token`` the density
    used to convert. The agent passes its CALIBRATED density
    (``core/context_calibration.py``) so the half-window bound holds at the real rate
    (~3.2 chars/token for code-heavy history) instead of at the nominal cold start; the
    default keeps the historical behaviour for direct callers and tests.

    Returns an integer byte budget, floored at ``MIN_OUTPUT_BUDGET_BYTES`` when the window
    is unknown or exhausted. Rounding: ``remaining_tokens * chars_per_token //
    TRUNCATION_DIVISOR``, i.e. floor of half the remaining bytes, then the floor constant.
    The result is monotonically non-increasing in ``used_tokens``. A non-positive or
    non-numeric ``chars_per_token`` is ignored in favour of the default: a nonsense
    density must not produce a nonsense budget.
    """
    if not context_size:
        return MIN_OUTPUT_BUDGET_BYTES
    remaining_tokens = context_size - (used_tokens or 0)
    if remaining_tokens <= 0:
        return MIN_OUTPUT_BUDGET_BYTES
    try:
        density = float(chars_per_token)
    except (TypeError, ValueError):
        density = float(CHARS_PER_TOKEN)
    if density <= 0:
        density = float(CHARS_PER_TOKEN)
    budget = int((remaining_tokens * density) // TRUNCATION_DIVISOR)
    return max(budget, MIN_OUTPUT_BUDGET_BYTES)


def _cut_utf8_safe(raw, limit):
    """The longest prefix of ``raw`` that fits in ``limit`` bytes.

    Never splits a UTF-8 sequence (a trailing partial character is dropped) and
    prefers to end on a line boundary, so the model keeps whole lines when the
    payload is line-oriented.
    """
    if limit <= 0:
        return b""
    if len(raw) <= limit:
        return raw
    head = raw[:limit]
    newline = head.rfind(b"\n")
    if newline >= 0:
        head = head[:newline + 1]
    return head.decode("utf-8", errors="ignore").encode("utf-8")


def _footer(shown, original, limit):
    """The truncation marker for ``shown`` kept bytes out of ``original``."""
    return _FOOTER_TEMPLATE.format(
        shown=shown,
        original=original,
        dropped=original - shown,
        limit=limit,
        percent=_LIMIT_PERCENT,
    )


def guard_output(text, max_bytes):
    """Cap ``text`` to ``max_bytes`` UTF-8 bytes, marking any truncation.

    Returns ``text`` unchanged (the identical object) when it fits, so callers
    that produce small outputs keep their exact previous behaviour. Otherwise
    returns the head of the payload plus a footer that reports the bytes the
    model actually received, the bytes that were dropped, the original size and
    the limit that was applied.

    The returned string - content plus footer - fits in ``max_bytes`` whenever
    the budget can hold the footer at all. With a pathological budget smaller
    than the footer itself the footer alone is returned, so the truncation is
    still visible instead of the call returning nothing.
    """
    if max_bytes is None or not text:
        return text
    raw = text.encode("utf-8")
    original = len(raw)
    if original <= max_bytes:
        return text

    # The footer's own length depends on the numbers it reports, so reserve room
    # for the widest form first and then re-fit for the real counts (the digit
    # count only shrinks, so this converges immediately).
    footer = _footer(original, original, max_bytes)
    kept = _cut_utf8_safe(raw, max(0, max_bytes - len(footer.encode("utf-8"))))
    for _ in range(4):
        footer = _footer(len(kept), original, max_bytes)
        room = max(0, max_bytes - len(footer.encode("utf-8")))
        if len(kept) <= room:
            break
        kept = _cut_utf8_safe(raw, room)
    else:
        kept = b""
        footer = _footer(0, original, max_bytes)
    return kept.decode("utf-8") + footer