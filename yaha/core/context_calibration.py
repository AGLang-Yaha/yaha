"""Characters-to-tokens calibration for the context-occupancy instrument.

YAHA used to treat a single hard-coded constant (4 characters per token, see
``tools/output_guard.py``) as a measurement of the real prompt. For code- and
JSON-heavy histories that constant under-counts the provider's own
``prompt_tokens`` by roughly a quarter, which made a compression pass look like a
large saving while the real window had barely moved, and it biased every
escalation decision built on the same number.

This module keeps the conversion honest in two ways:

* ``4`` characters per token stays the documented COLD START
  (``DEFAULT_TOKENS_PER_CHAR``), and it is only used until a provider count is
  available.
* every pair of observations (history characters, provider ``prompt_tokens``)
  refines the relation ``tokens ~= a * chars + b``. The intercept ``b`` matters:
  a constant per-request framing term (role markers, special tokens) is NOT
  proportional to history characters, so folding it into a density coefficient
  would mis-model it (review finding F4).

The rate is clamped to a physically plausible band so a pathological observation
cannot poison every later prediction; the window keeps the fit local, since the
conversion depends on what the recent content happens to be.
"""

# 4 characters per token: the documented YAHA cold start.
DEFAULT_TOKENS_PER_CHAR = 0.25

# ~6.7 characters per token; below this a fit is not believable for text or code.
MIN_TOKENS_PER_CHAR = 0.15

# ~1.7 characters per token; above this a fit is not believable either.
MAX_TOKENS_PER_CHAR = 0.60

# How many recent observations feed the fit.
DEFAULT_WINDOW = 8


class ContextCalibration:
    """Running ``tokens ~= a * chars + b`` model, fed by provider usage reports."""

    def __init__(self, window=DEFAULT_WINDOW):
        try:
            self.window = max(2, int(window))
        except (TypeError, ValueError):
            self.window = DEFAULT_WINDOW
        # oldest first: list of (chars, tokens)
        self.observations = []

    # -- writing -----------------------------------------------------------

    def observe(self, chars, tokens):
        """Record one (history characters, provider prompt tokens) pair.

        Ignored when the pair cannot inform the model (non-numeric, empty
        history, negative count) so bad data cannot poison the fit.
        """
        try:
            chars = float(chars)
            tokens = float(tokens)
        except (TypeError, ValueError):
            return
        if chars <= 0 or tokens < 0:
            return
        self.observations.append((chars, tokens))
        if len(self.observations) > self.window:
            del self.observations[0:len(self.observations) - self.window]

    # -- reading -----------------------------------------------------------

    def tokens_per_char(self):
        """The current slope ``a`` (tokens per character)."""
        return self._fit()[0]

    def offset(self):
        """The current intercept ``b`` (tokens that do not scale with content)."""
        return self._fit()[1]

    def predict(self, chars):
        """Predicted provider tokens for ``chars`` characters of history."""
        try:
            chars = float(chars)
        except (TypeError, ValueError):
            return 0
        if chars <= 0:
            return 0
        a, b = self._fit()
        return int(max(0.0, a * chars + b))

    # -- internals ---------------------------------------------------------

    def _clamp(self, rate):
        if rate is None:
            return DEFAULT_TOKENS_PER_CHAR
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            return DEFAULT_TOKENS_PER_CHAR
        return min(MAX_TOKENS_PER_CHAR, max(MIN_TOKENS_PER_CHAR, rate))

    def _fit(self):
        """Least-squares ``(a, b)`` over the window, with a cold-start fallback."""
        points = self.observations
        if not points:
            return (DEFAULT_TOKENS_PER_CHAR, 0.0)
        if len(points) == 1:
            chars, tokens = points[0]
            rate = tokens / chars if chars else DEFAULT_TOKENS_PER_CHAR
            return (self._clamp(rate), 0.0)

        count = len(points)
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        mean_x = sum(xs) / count
        mean_y = sum(ys) / count
        sxx = sum((x - mean_x) ** 2 for x in xs)
        if sxx <= 0:
            # No spread to separate slope from intercept; the mean rate is the
            # best available reading and the intercept stays neutral.
            rate = mean_y / mean_x if mean_x else DEFAULT_TOKENS_PER_CHAR
            return (self._clamp(rate), 0.0)

        sxy = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(count))
        rate = self._clamp(sxy / sxx)
        # Recompute the intercept against the clamped slope so the line still
        # passes through the observed mean, and keep it non-negative.
        intercept = mean_y - rate * mean_x
        if intercept < 0:
            intercept = 0.0
        return (rate, intercept)
