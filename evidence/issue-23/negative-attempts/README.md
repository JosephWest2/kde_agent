# Retained negative attempts

These unaltered receipts and artifact copies are diagnostic failures, not selected
passing qualification. Each selection.json hashes every copied raw file.

- `paced-first`: corrected product with a runner that asserted on the first paced
  timeout. Queue delay grew; the incomplete batch does not establish all 30
  outcomes. Lifetime maximum was below 100 ms. Later runners collect every outcome.
- `paced-durable-diagnostics`: synchronous durable evidence writes were still
  enabled. Observed lifetime GLib maximum 218.91 ms fails the qualification target;
  paced requests timed out. The harness also incorrectly demanded script-absence
  evidence for requests that had never spawned. Later receipts state not_spawned.
- `retirement-bracket`: control actions passed, then a native close invalidated an
  in-flight app ownership bracket. Product rejected the whole observation as
  required. The later harness permits a bounded retry only after retained app
  state independently confirms all-exited and cleanup complete.
- `startup-late-publication`: even with nondurable diagnostic receipts, startup
  lifetime GLib gap reached 226.49 ms. Adapter acceptance was before its deadline
  (159807.594315 < 159807.649109), but later publication missed the deadline and
  startup failed with cleanup. This is a failed timing condition and demonstrates
  why accepted_at must not be equated with public request success.

The final selected native and controls runs meet the target. Their small measured
receipt/durable-write durations do not explain these prior pauses. No claim of
causal attribution, universal latency guarantee, or support-policy expansion is
made. Production work/cleanup deadlines and durable writes remained unchanged.
