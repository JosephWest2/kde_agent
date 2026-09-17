# Fresh-review correction: sample the capture deadline at acceptance

Fresh PR review found that `Capture.tick()` reused a monotonic timestamp sampled
before checking child completion and publication. A delayed publication stat
could cross the deadline and still be accepted with that stale time. A local
regression using a real exited child and a 50ms delayed publication check with
20ms remaining reproduces the failure; a second check proves the original
acceptance timestamp preceded the publication observation. Both failures are
preserved in [before-fix-tests.log](before-fix-tests.log).

The corrected path samples monotonic time **after** child/process and publication
checks, immediately before accepting success. Expiry takes the existing timeout,
artifact removal and conservative failed-session path; rejected results contain
no accepted timestamp. An on-time result records that final sample as both its
acceptance time and latency basis.

The corrected `tools/capture_probe.py` SHA256 is
`3e6a2dc2b1d80684f2716e77d7d9b2168faacac2c5a0a2037aeee2ca9307ac45`.
No codec, native input, query, harness, bound or architecture policy changed.
All **60 tests passed in 1.989s**; see [unit-tests.log](unit-tests.log).

After independent review desktop probes finished, three focused private runs
were performed sequentially against this corrected source. [runs.json](runs.json)
indexes their raw receipts, PNGs, original/normalized hashes and observed cleanup;
[summary.json](summary.json) preserves all samples and source hashes.

| Scenario | Actual result | Complete service shutdown |
| --- | --- | ---: |
| functional | Ten complete captures, 360 exact pixel checks, freshness and rendered-input sequence | 161ms |
| invalid-screen | Safe exact rejection and one subsequent complete capture | 160ms |
| responsive-pipe | Real Shift+W cancellation/release while capture child is stopped; expected capture timeout, local cleanup and failed-session stop | 148ms |

All observed cgroups were empty. Eleven accepted PNGs had maximum actual
acceptance latency 121.222ms; local abort cleanup max 5.282ms, GLib gap max
5.641ms, cancellation dispatch 0.579ms and fixture release 2.451ms. The stalled
request's timeout was detected at 3.004051s on the existing 5ms timer; complete
failed capture admission-through-empty-cgroup took 3.222s. Timely acceptance
remains strictly before the 3s work deadline. The conservative aggregate sequence
including ten captures/input took 1.770s; it does not implement production readiness.

The original eleven-run matrix, 32 PNGs, summary, run index, all-attempts index
and 58-test log remain unchanged in the parent evidence directory and retain
their original source hashes. They are **historical pre-correction evidence**,
not reruns of the new code. Their successful ordinary timings did not exercise
the delayed-publication race. This separate correction record prevents those
samples from being relabeled and preserves the failure that fresh review found.
