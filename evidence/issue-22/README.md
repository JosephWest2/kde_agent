# Issue #22 installed launch evidence

Selected corrected implementation: `98745c82d15f8058f37a0c4b213a69343388bfe1`.
The installed wheel's 32 Python module hashes match the source hashes in
[launch-review/launch.json](launch-review/launch.json). Evidence used a separate virtual
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
  dispatch was 4.803 ms, below the existing 100 ms target. Each generation's stop
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

[review-tests.txt](review-tests.txt) records all 306 passing tests; focused coverage includes gate
EOF/expiry, before-release persistence failure, post-release protocol death,
retained scheduler failures, descriptor closure, PID reuse/foreign membership,
high-numbered pidfds, missing membership input, and finite traversal limits.

Reproduce in a newly installed environment, with no live generations from an
older build:

```sh
.local/issue22-launch-venv/bin/python -I evidence/issue-22/installed_launch.py \
  .local/issue22-launch-new .local/dependencies
```

`launch-review/` contains the current unaltered selected receipts and generation artifacts.
`launch/` and `tests.txt` retain the historical pre-review qualification on `6eff7aa`.
The corrected source makes retained completed exit observations idempotent and
shares the registry turn deadline across scanning, reaping and publication,
retaining deferred process batches for later turns. Ten new regressions cover
completion and budget exhaustion during iteration, acquisition, reaping and writes,
including sharing the remaining tick budget after lifetime observation. Absolute
paths name their original qualification location; artifacts were copied here
without rewriting receipts. Failed harness-development runs are not selected
qualification evidence.
