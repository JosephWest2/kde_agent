"""Shared monotonic freshness policy for successful essential observations."""
import math

ESSENTIAL_FRESH_SECONDS = 2.0
# Bus and KWin observations of a ready session: KWin descheduled by host load is
# slow, not hung, so it gets longer than the worker's own observations (#111).
HEALTH_FRESH_SECONDS = 5.0


def fresh(observed_at, now, seconds=ESSENTIAL_FRESH_SECONDS):
    return (type(observed_at) in (int, float)
            and (type(observed_at) is int or math.isfinite(observed_at))
            and now - seconds < observed_at <= now)
