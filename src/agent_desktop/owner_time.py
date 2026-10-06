"""Budgets that the owner's own stalls do not use up (#80).

The GLib owner thread waits on KWin and the private bus with bounded budgets.
When the owner itself is held up (a slow fsync, a long turn), it can't dispatch a
reply that has already arrived or start the next step, and a plain monotonic
deadline then blames KWin or the bus for the owner's delay. A Budget moves its
deadline later by the owner stall time measured since it began, by at most the
budget again. A hung peer is still detected within twice the budget, and never
after the limit the Budget was given (a request's or startup's own deadline).
Without an OwnerClock a Budget is a plain deadline. A Budget reads the time from
its OwnerClock unless it is given its own `now`.
"""
from __future__ import annotations

import time


def _monotonic():
    return time.monotonic()  # Looked up per call, so a patched clock applies.


# Owner turns are 5ms apart; a gap between turn starts up to this long is normal scheduling.
TOLERANCE = .010


class OwnerClock:
    """Owner stall time: gaps between owner turn starts beyond TOLERANCE, and the current turn's overrun."""

    def __init__(self, now=_monotonic):
        self.now = now
        self.turn_start = None
        self.total = 0.0

    def turn(self):
        """Called at the start of every owner turn."""
        now = self.now()
        if self.turn_start is not None:
            self.total += max(0.0, now - self.turn_start - TOLERANCE)
        self.turn_start = now

    def stalled(self, now):
        if self.turn_start is None:
            return self.total
        return self.total + max(0.0, now - self.turn_start - TOLERANCE)


class Budget:
    """seconds of time the owner was free to act, within at most 2 * seconds of wall time and before limit."""

    def __init__(self, seconds, *, limit=None, clock=None, now=None):
        if now is None:
            now = clock.now if clock is not None else _monotonic
        self.clock, self.now = clock, now
        start = now()
        self.base = start + seconds
        self.hard = start + 2 * seconds if clock is not None else self.base
        if limit is not None:
            self.base, self.hard = min(self.base, limit), min(self.hard, limit)
        self.origin = clock.stalled(start) if clock is not None else 0.0

    def at(self, now=None):
        """The absolute monotonic deadline as of now."""
        if self.clock is None:
            return self.base
        now = self.now() if now is None else now
        return min(self.hard, self.base + self.clock.stalled(now) - self.origin)

    def expired(self, now=None):
        now = self.now() if now is None else now
        return now >= self.at(now)


def expired(deadline, now):
    """A float deadline or a Budget."""
    return deadline.expired(now) if isinstance(deadline, Budget) else now >= deadline


def hard(deadline):
    """The latest the deadline can be: what an external timeout (a D-Bus call's) must allow."""
    return deadline.hard if isinstance(deadline, Budget) else deadline
