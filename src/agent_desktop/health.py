"""Shared monotonic freshness policy for successful essential observations."""
import math

ESSENTIAL_FRESH_SECONDS = 2.0


def fresh(observed_at, now):
    return (type(observed_at) in (int, float)
            and (type(observed_at) is int or math.isfinite(observed_at))
            and now - ESSENTIAL_FRESH_SECONDS < observed_at <= now)
