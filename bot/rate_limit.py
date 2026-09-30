"""
rate_limit.py
~~~~~~~~~~~~~
How often one person may do something costly: here, have a resume read.
`intern_ui.upload_limiter` is the one instance, five reads an hour, which is
generous for a person and useless for a script. Every read spends a worker
process (`resume_worker`), so without it one person could keep both slots busy.

A rolling window per person: each attempt `take` allows is remembered for
`window` seconds, and a person holding `limit` of them waits until the oldest
ages out. A refused attempt is not remembered, so being refused never extends
the wait. `refund` gives back the last slot, for an attempt that was allowed and
then never ran (the worker was busy, or Discord's download failed).

In memory, so it forgets on restart. The cost of forgetting is a handful of
extra reads after a restart; the cost of a table is a migration and a file to
back up, for a rule nobody tunes.

Pure: no discord, no database, nothing read at import.
"""

import time
from collections import defaultdict

#: Five an hour, what `intern_ui` uses.
DEFAULT_LIMIT = 5
DEFAULT_WINDOW_S = 60 * 60


class RateLimiter:
    """`limit` attempts per person in any `window` seconds."""

    def __init__(self, limit: int = DEFAULT_LIMIT, window: float = DEFAULT_WINDOW_S) -> None:
        if limit < 1 or window <= 0:
            raise ValueError(f"a rate limit needs limit >= 1 and window > 0, not {limit}, {window}")
        self._limit = limit
        self._window = window
        self._taken: defaultdict[int, list[float]] = defaultdict(list)

    def _recent(self, user_id: int, moment: float) -> list[float]:
        recent = [at for at in self._taken[user_id] if moment - at < self._window]
        self._taken[user_id] = recent
        return recent

    @staticmethod
    def _moment(now: float | None) -> float:
        return time.monotonic() if now is None else now

    def take(self, user_id: int, now: float | None = None) -> bool:
        """True when this attempt is allowed, and counts it. False when it is not."""
        moment = self._moment(now)
        recent = self._recent(user_id, moment)
        if len(recent) >= self._limit:
            return False
        recent.append(moment)
        return True

    def refund(self, user_id: int) -> None:
        """
        Gives back the most recent slot `take` counted, for an attempt that was
        allowed and then never ran. Pops the last one rather than clearing the
        list: someone who spent two and had the third refunded still holds two.
        """
        taken = self._taken.get(user_id)
        if taken:
            taken.pop()

    def used(self, user_id: int, now: float | None = None) -> int:
        """How many slots this person holds in the window right now."""
        return len(self._recent(user_id, self._moment(now)))

    def opens_in(self, user_id: int, now: float | None = None) -> int:
        """Seconds until this person may try again. Zero when they may now."""
        moment = self._moment(now)
        recent = self._recent(user_id, moment)
        if len(recent) < self._limit:
            return 0
        return max(0, int(self._window - (moment - min(recent))) + 1)
