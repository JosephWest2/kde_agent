# PR #45 correction: failed libei FD setup ownership

Fresh review of commit `4f42108` found an error in the initial ownership audit:
the installed libei 1.6.0 does **not** close the supplied FD when the initial
epoll-add operation fails. A regular-file FD reproduces `-EPERM`; epoll watch
exhaustion can also affect a valid socket. The original negative-result path
therefore leaked that descriptor. The initial successful 24-run matrix never
exercised this native failure.

The fix closes the known caller-owned FD immediately after the negative return,
before logging, context destruction or any descriptor reuse. It performs no
probe-and-close operation on an old integer. Successful setup remains a single
transfer to libei, with no extra caller close.

This rule is specific to the audited binary. The binding now loads the explicit
resolved `/usr/lib/libei.so.1` path only on x86_64 and only when it matches the
already recorded libei 1.6.0 SHA256
`93897fc311319920c1c25e9422db62ebe8324d54c5e0c3d4a9f15a0a6cac2501`.
Changed native builds are rejected before obtaining an EIS FD; a fresh ABI and
failure-ownership audit is required before updating the tested hash. No native
library patch, global update or new compositor mechanism was needed.

## Exact source basis

Release 1.6.0, commit `8a46bf3d4b6af7f25a61be53386cf618218114ef`:

- `src/libei-fd.c:71–81` delegates to ei_set_socket.
- `src/libei.c:988–1005` returns negative only after sink_add_source fails; it
  never sets ei->source in that branch and drops the temporary source reference.
- `src/util-sources.c:189–199` returns the epoll_ctl error without activating or
  removing the source.
- `src/util-sources.c:91–112` closes at destruction only for CLOSE_FD_ON_DESTROY,
  while source_new defaults to CLOSE_FD_ON_REMOVE. Failed-add cleanup and later
  context unref therefore leave the supplied descriptor open.

[summary.json](summary.json) records exact source-file, library and corrected-code
hashes. The general header ownership wording alone is insufficient for this
failure branch; the original docs have been corrected explicitly.

## Regression and targeted verification

All **35 tests passed in 1.090s**, including four new checks against the installed
library/implementation gate. See [unit-tests.log](unit-tests.log).

1. An actual regular-file FD returns `-EPERM` and remains open until its caller
   closes it, confirming the native failure contract.
2. Eight actual failed `Input.setup` calls verify the FD is already closed inside
   the first setup-log callback. The callback immediately reuses that exact
   integer with dup2 before context disposal. Both the reused FD and a separate
   unrelated FD survive disposal; total FD count is unchanged after all cycles.
3. A real nonblocking socketpair returns success, retains its FD until ei_unref,
   then closes it exactly once.
4. A mismatched native binary hash is rejected before ctypes loads it.

These native checks use no desktop connection; the regular-FD failure is an
immediate kernel epoll rejection, and the socketpair is explicitly nonblocking.
The regression has eight fixed iterations, no retry/wait loop, and releases each
context/owned descriptor. The compiler audit retains its 10s compiler deadline
plus bounded owned-group cleanup; its fresh [audit.json](audit.json) passes all
24 signatures/constants/sizes and records the corrected binding hash.

Only relevant real desktop scenarios were rerun after the fix:

| Scenario | Generation | Result | Complete shutdown |
| --- | --- | --- | ---: |
| input | `5d32fd52b4e84123bdccbc6403621f09` | Pass; successful setup, chord/releases | 170ms |
| pause | `cb53fa02db1e41b8b30b8b0766bd8631` | Pass; real pause, uncertainty, connection replacement, neutral recovery | 218ms |
| reset-failure | `69ce453fce5540ffac14c2f6eb0a30d2` | Pass; failed reset gates input, later recovery | 172ms |

Each scenario directory preserves its raw receipts, lifecycle timeline, corrected
code hashes, manifest and observed empty cgroup. The existing private-harness
95s lifetime and 15s cleanup bounds are unchanged. The plugin was unchanged and
not rebuilt for this fix. No unexpected failure occurred in these targeted runs.

The original 24-run evidence, old audit and 31-test log are preserved as historical
pre-fix evidence; all **342 original raw-file hashes** still match runs.json.
This new directory documents the correction without silently replacing them.
The ownership fix does not change the original timing measurements or Python/GLib
boundary decision, but the original successful runs alone must not be cited as
coverage for failed native FD setup.
