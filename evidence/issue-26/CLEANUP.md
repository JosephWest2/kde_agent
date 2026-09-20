# Explicit termination: installed lifecycle regression

All **21 scenarios passed** against corrected revision
`52dbeae70e663bcc56cd945dc77fc496ab332145`. The isolated noneditable build
came from a clean archive of that revision. All **36 Python module hashes and
one packaged JavaScript resource hash** match the committed source exactly.
The [complete receipt](cleanup/cleanup.json) and
[unaltered artifact inventory](cleanup/selection.json) retain provenance and
observations. No fallback cleanup was needed. The
[installation receipt](cleanup-installation/receipt.json),
[build/install log](cleanup-installation/build-install.log) and
[exact installer](cleanup-installation/install-archive.py) retain archive and
wheel SHA-256, build provenance and installed-module verification. Pip's
ephemeral wheel cache is not retained; its exact wheel hash is recorded by the
build/install log.

The longest fault-to-empty/finalization interval was **7.640 seconds**
(`blocked-release`), within the original 15-second limit. Coverage includes
normal/manager stop, failed startup/exec, bus/compositor/worker death and freezes,
held locks, disconnect, missing sockets, blocked hooks/release, prior-failure
preservation and stale-generation replay. Each case independently checks the
whole-service cgroup and ordinary descendants.

The deliberately blocked finalizer remains a negative recording case: service
termination passed, while complete artifact finalization required the recorded
explicit reconciliation. Other cases require both final recording and cgroup
emptiness within the original fault deadline; transient empty containment before
ExecStopPost starts is insufficient. Empty-controller delegation, `supervisor`
infrastructure, application subgroups and exact `.control` hooks are retained.

Reproduce with an isolated committed installation:

```sh
/path/to/venv/bin/python -I evidence/issue-21/installed_cleanup.py \
  /absolute/new/output-directory /absolute/dependency-root
```

This matrix exercises existing lifecycle fixture hooks and regression-checks
shared Registry lifetime ownership. Separate native #26 evidence qualifies
explicit application termination and the affected launch/wait/close paths.
Systemd service cleanup is not counted as successful application kill.
Production shutdown integration and release qualification remain under #35.
These receipts do not establish broad response latency; recorded gaps beyond
the unchanged 100 ms target remain negative evidence. Normal local storage and
ordinarily killable process assumptions still apply.
