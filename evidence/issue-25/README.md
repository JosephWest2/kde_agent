# Issue 25 — graceful selected-window close

Implements [#25](https://github.com/JosephWest2/kde_agent/issues/25), parent #4.
Product source is `9584243e6ab7fef57c9b263f4cef440dc9e8e4de`.
The approved plan and fresh plan review select owned-application exit observation:
explicit UUID close of a real unassociated surface is unsupported before dispatch;
explicit focus remains available for that surface.

## Implementation and unit/process checks

One fresh selected window receives one native `windowclose {uuid}` request.
Completed transport is distinct from application acknowledgment. Success requires
positively observed whole-application exit within the admission deadline. Window
loss after dispatch and root exit with descendants alive do not satisfy it.
Confirmation, refusal, timeout and cancellation never cause implicit application
signals or dialog interaction. Exact references and typed bounded lifetime state
remain in partial/durable records.

The independent close owner underlies the public task and a bounded reusable hook.
Production shutdown policy/wiring remains #35. Superseded read-only queries keep
one existing cleanup reserve; success still requires acceptance before the work
deadline. Actual scheduler/shutdown deadlines hard-clip that same reserve.

The final integrated unit/process suite passed **383 tests in 22.172 seconds** at
`57d644a2`. The later hook-only correction reports unresolved foreign adapter
ownership as unconfirmed without touching that owner; all 21 affected close tests
passed. Focused close tests include the five reviewed supersession
boundaries, a real Query cleanup/reuse check, and retained terminal identity after
disconnect. Application tests cover missing root metadata, empty-before-reap,
pending publication, historical completion and constant-size 4096-identity reporting.
Native-adapter process tests exercise actual child cleanup and a hook whose owner
pumps child reaping. The fixture compiled with
`-std=c11 -O2 -Wall -Wextra -Werror`.

The baseline source `3ec00739` passed the complete installed **21-case cleanup
regression**, with all 35 Python modules plus packaged JS matching the archive,
no import fallback, and a maximum observed cleanup duration of 7.632 seconds.
Final corrected-source installed qualification will be recorded before review.

## Installation and native qualification

Native evidence is being prepared; no native acceptance claim is made by this
intermediate record. The runner uses a clean committed source archive, a
noneditable isolated wheel install, and installed/source Python and packaged-JS
hash comparisons. It operates only on generation-private endpoints, records
bounded traces and independent fixture receipts, and preserves failed runs.
Native suites and the complete installed cleanup regression run one at a time.

## Scope and limitations

Functional acceptance is separate from latency qualification. Inherited #24
lifetime GLib gaps of 424.188 ms, 132.647 ms and 136.902 ms exceed the unchanged
100 ms target and remain negative evidence. New runs must retain every measured
outlier. Only measured passing controls can qualify their selected scopes; none
of this establishes broad latency or release readiness.

Confirmation persistence/discoverability is the close acceptance criterion.
Public key/click confirmation input remains #28/#29. A separately labeled private
fixture input control, if used, is evidence-only. These cases do not qualify
representative third-party applications, production lifecycle wiring, or the
future twenty-run release acceptance.
