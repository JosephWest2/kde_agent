# Issue #22 installed launch evidence

Selected implementation: `6eff7aa507081a4119c9114a19664cf572b90cad`.
The installed wheel's 32 Python module hashes match the source hashes in
[launch/launch.json](launch/launch.json). Evidence used a separate virtual
environment with system site packages, clean caller environments and different
caller/worker working directories. The native fixture was rebuilt from the
committed C source with `-Wall -Wextra -Werror`.

[installed_launch.py](installed_launch.py) exercises the installed public CLI:

- Native autonomous Wayland fixture: delayed initial window, a committed frame,
  timed exit 9, original-process birth identity and durable logs.
- Absolute, slash-relative, final PATH and empty-PATH-segment executable lookup;
  spaces, shell metacharacters and empty argv elements; allowed environment
  override; private runtime/Wayland/bus endpoints; durable stdout/stderr.
- Protected endpoint overrides and `--wait-window` reject before spawning.
- Original fixture exit before a double-forked setsid descendant: second launch
  returns `application_active` without allocating another application; after
  recursive populated=0, a sequential launch succeeds.
- Actual installed LaunchTask followed by a Python-only injected wait: wait
  failure, explicit protocol cancellation and an actual client socket disconnect
  preserve the application/process/log references in real terminal request
  records and leave the authorized target alive. No launch is retried.
- The cancellation case includes 64 living descendants. Measured cancellation
  dispatch was 4.883 ms, below the existing 100 ms target. Each generation's stop
  independently verifies entire service cgroup emptiness and durable logs remain.

The wait injection subclasses the real installed launch task after it completes;
it neither fabricates a launch result nor bypasses containment/readiness. It is
not real compositor window-timeout acceptance, which belongs to issue #24.
Provisional desktop readiness remains `release_qualified: false`, replacement #35.

Effective service properties are `Delegate=yes`, empty `DelegateControllers` and
`DelegateSubgroup=supervisor`; main-process membership was independently checked.
The toolkit contains no writes to `cgroup.subtree_control`, and the observed
service value was empty in this run. This does not claim host-inherited controllers
are always absent.

The [complete 21-case shutdown requalification](CLEANUP.md) passed on the same
implementation, including worker kill/freeze, essential-service failures, blocked
hooks, retained prior failures and stale-generation replay. It verifies the new
exact supervisor/control-subgroup layout without widening the 15-second bound.

[tests.txt](tests.txt) records the complete suite; focused coverage includes gate
EOF/expiry, before-release persistence failure, post-release protocol death,
retained scheduler failures, descriptor closure, PID reuse/foreign membership,
high-numbered pidfds, missing membership input, and finite traversal limits.

Reproduce in a newly installed environment, with no live generations from an
older build:

```sh
.local/issue22-launch-venv/bin/python -I evidence/issue-22/installed_launch.py \
  .local/issue22-launch-new .local/dependencies
```

`launch/` contains unaltered selected receipts and generation artifacts. Absolute
paths name their original qualification location; artifacts were copied here
without rewriting receipts. Failed harness-development runs are not selected
qualification evidence.
