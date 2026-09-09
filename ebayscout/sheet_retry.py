"""Surviving the Sheets per-minute write quota.

Pure logic, no gspread — main.py owns the API calls and the sleeping.

Why this exists
---------------
Sheets allows 60 write requests per minute per user.  The inventory write spends
two of them per button: the ``update_cell`` that moves the count and the
``append_rows`` that records it on the Bot Writes tab.  A Gemini-auto lot
confirms a dozen buttons in a couple of seconds, and several lots can land back
to back, so a big run goes over the limit in bursts even though its average rate
is nowhere near it.

Measured on the 2026-09-05 run: 353 buttons in 40 minutes — an average of about
18 writes a minute — produced ten ``[429] Quota exceeded`` failures, and five
buttons never reached the sheet.  Worse, the failures are not evenly damaging:
the same quota also throttles the audit append, so some writes landed with no
Bot Writes row and some failures had no row either.  The sheet and its own audit
log degrade *independently*, which is what made that run hard to reconstruct.

A 429 is the one Sheets error worth retrying: it means "not now", not "no".
Everything else — a bad range, a deleted tab, a revoked token — will fail again
just as fast, so it is raised immediately rather than slept on.
"""

# Waits between attempts, in seconds.  Three retries over ~12s: the quota is a
# per-MINUTE budget, so a burst that trips it usually clears well inside that.
# Deliberately not jittered — the caller retries while holding the sheet write
# lock, so the writers are already serialized and cannot stampede each other.
RETRY_DELAYS = (1.0, 3.0, 8.0)


def _status_code(exc):
    """The HTTP status on a gspread APIError, or None if it carries none."""
    for attr in ("response", "resp"):
        resp = getattr(exc, attr, None)
        code = getattr(resp, "status_code", None) or getattr(resp, "status", None)
        if isinstance(code, int):
            return code
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


def is_rate_limited(exc):
    """True when ``exc`` is Sheets saying "too many writes this minute".

    Matched on the status code when the exception carries one, and otherwise on
    the message text, because gspread has moved its error classes around across
    versions and the text is the one thing that has stayed stable.  Both spellings
    Sheets uses are covered: the ``[429]`` prefix gspread renders, and the
    ``RESOURCE_EXHAUSTED`` / ``Quota exceeded`` wording of the underlying error.
    """
    if _status_code(exc) == 429:
        return True
    text = str(exc).lower()
    return ("[429]" in text
            or "quota exceeded" in text
            or "resource_exhausted" in text
            or "rate_limit_exceeded" in text)


def retry_delays(delays=RETRY_DELAYS):
    """How long to wait after each failed attempt; ``None`` means give up.

    Yields one value per attempt, so a caller can write the whole retry loop as
    ``for delay in retry_delays()`` and treat ``None`` as "re-raise".  With the
    default schedule that is four attempts.
    """
    for d in delays:
        yield d
    yield None
