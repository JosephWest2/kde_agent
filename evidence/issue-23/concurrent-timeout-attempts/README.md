# Preserved concurrent qualification timeouts

These attempts used corrected source `7e4f8d6` while the parent cleanup matrix
and both discovery/control matrices ran concurrently. Native readiness rejected
a query at its unchanged 500 ms work deadline; the control attempt rejected its
first public query before task creation. Both generations were reclaimed. The
parent cleanup attempt also encountered a readiness timeout. Concurrent workload
is a possible contributor, not a proven cause. These negative receipts are
retained and do not count as successful timing qualification. Selected corrected
source runs are performed serially, without changing any deadline.
