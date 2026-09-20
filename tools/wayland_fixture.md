# Native Wayland fixture

`private_harness.build()` builds `wayland_fixture.c` with the generated stable
xdg-shell and presentation-time protocols, `wayland-client`, and `xkbcommon`.
The existing `tools/private_harness.py run` smoke exercises the default
stdin-driven primary surface. No AT-SPI integration is used.

The default remains a 640×360 primary toplevel titled `KDE Agent Native Fixture`
with app ID `org.kde_agent.fixture`. It requires the private Wayland and D-Bus
environment and a 32-character lowercase hexadecimal `HARNESS_GENERATION`.
`--autonomous` ignores stdin and permits the existing all-zero generation fallback
when the environment does not provide one. Existing `--window-delay-ms`,
`--exit-after-ms`, `--exit-code`, and sleeping `--descendant-ms` modes are retained.

For structured-window evidence, add any of:

| Option | Effect |
| --- | --- |
| `--sibling` | Independent 480×300 toplevel labeled `sibling` |
| `--dialog` | 320×180 toplevel labeled `dialog`, with its xdg parent set to `primary` |
| `--child-window-ms N` | One child process creates a 400×240 toplevel labeled `child`, then exits after N ms |
| `--title-mode normal\|empty\|omitted` | Set the normal title, explicitly set an empty title, or omit `set_title` |
| `--app-id-mode normal\|empty\|omitted` | Set the normal app ID, explicitly set an empty app ID, or omit `set_app_id` |

All surfaces share the chosen metadata policy; default title/app ID are
intentionally identical across the primary, sibling, dialog, and child. There
are three fixed surface slots in the parent and at most one window child. Slots
cannot be reused after closure. Each surface owns its configure state, rendering
state, presentation feedback, and independent shared-memory buffer.

For example, a public launch can pass these fixture arguments:

```text
--autonomous --sibling --dialog --child-window-ms 12000 --exit-after-ms 15000
```

The window child inherits the caller's cgroup and standard output, closes its
inherited Wayland descriptor, and execs the same binary to obtain a new Wayland
connection. Its internal `--child-surface` option requires autonomous timed mode
and prevents recursive child-window spawning. A `window_child_spawned` receipt
records the child PID. Parent and child sequence numbers are independent; key
receipts by `(fixture_pid, seq)`. The child can outlive the parent until its own
timer expires, as with the existing sleeping descendant mode. Native evidence
must independently read each PID's birth identity and cgroup while it is alive;
a printed PID alone is not association proof.

Stdin still accepts `state VALUE CONTROL_ID` for the primary and `close` for all
surfaces in that process. New commands are `open sibling`, `open dialog`, and
`close LABEL`, where LABEL identifies an open surface in that process. Opening a
dialog requires an open primary. Closing one surface leaves other surfaces
running; closing the last surface exits the process. Autonomous mode does not
read these commands.

`surface_created`, `configure`, `map`, `committed`, `presented`, `discarded`, and
`close` receipts include the fixture-local `surface` label. `surface_created`
records parent, requested dimensions, and metadata modes. `configure` records
the acknowledged serial and current client size. `map` means the first buffer
commit (`receipt: first_buffer_commit`); only `presented` proves compositor
presentation. Dimensions in configure/commit receipts describe content size,
not frame position or decoration size. `close` records `control`, `compositor`,
or `shutdown` as its source.

Empty and omitted metadata do not promise that KWin reports JSON null: record
its actual caption/class values, including defaults or empty strings. Native
Wayland also does not reliably force missing PID or client bounds; null decoder
coverage belongs in fixed synthetic tests. An unassociated evidence window can
use the same fixture binary launched as service infrastructure outside the
application subgroup; the fixture does not create or move cgroups.
