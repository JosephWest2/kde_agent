# Delegated application layout: installed cleanup regression

All **21 scenarios passed** against implementation `6eff7aa507081a4119c9114a19664cf572b90cad`.
All 32 installed Python module hashes exactly match that source.
The [complete receipt](cleanup/cleanup.json) and [unaltered artifact inventory](cleanup/selection.json)
retain the observed results. No fallback cleanup was needed.

The longest fault-to-empty/observation interval was **7.733 seconds**
(`blocked-release`), below the existing 15-second shutdown bound. The matrix covers normal
and manager stop, failed startup/exec, bus/compositor/worker death and freezes,
held record/generation locks, client disconnect, missing sockets, blocked hooks,
blocked release, preservation of earlier failures, and stale-generation replay.

Live properties confirm `Delegate=yes`, empty `DelegateControllers`, and
`DelegateSubgroup=supervisor`. All observed worker/infrastructure/fixture processes
were in the exact generation `supervisor` group. The instrumented blocked-hook
cases confirm helpers run in the exact `.control` group. Every case independently
observed the whole service cgroup empty and ordinary descendants absent.

The deliberately blocked finalizer remains a negative recorder case: service
termination succeeded, while complete artifact finalization required the
separately recorded explicit reconciliation. All other ordinary failure/stop
paths preserved autonomous terminal artifacts.

This requalifies the changed systemd layout using the existing lifecycle fixture
and installed product cleanup paths. Its injected task/hooks are not evidence for
the production application-launch registry; the separate #22 launch evidence
covers that behavior. Readiness input/capture providers remain provisional and
release qualification remains deferred. Bounds retain the normal local storage
and ordinarily killable process assumptions.

Reproduce with a committed build installed non-editably in an isolated venv:

```sh
.local/issue22-cleanup-venv/bin/python -I evidence/issue-21/installed_cleanup.py \
  /absolute/new/output-directory /absolute/dependency-root
```

The runner rejects source/installed hash mismatch and records effective service
properties, exact process identities, timings, receipts and artifact hashes.
The selected raw output is copied without altering its observations or paths.
