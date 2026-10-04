# Revised plan (2026-10-03, rev 2)

Replaces the remaining M5.2–M8.3 issues. This is a personal tool that should still
be solid: correct cleanup, input that never gets stuck, and honest results. It
doesn't need archived per-issue evidence or a formal release qualification.

Rev 2 includes an independent review. The changes:
- The kwin-mcp go/no-go and the target-app check now come first.
- Phase 0 is smaller.
- Removing `evidence/` and renaming the provisional-provider fields each need
  lockstep code changes first.
- Phase 2 is split into keyboard and screenshot (2a) and pointer (2b).
- Basic guarantees against stuck input move into 2a.
- Both binary-hash pins (libei and kdotool) are relaxed. KWin is tracked as the
  real fragility.

## Decisions

- **Evidence:** `git rm` the `evidence/` tree and ignore it from now on. Keep a
  one-table summary in `docs/`. History is not rewritten.
- **Definition of done for each issue:** unit tests pass, the relevant real-desktop
  integration test passes, and the docs are updated. No receipts, provenance
  archives or timing certifications.
- **Release bar:** an automated end-to-end smoke test (about a minute) plus a small
  set of failure-path tests. A `--loop N` mode exists for soak runs but isn't a gate.
- **Targets:** the project's Wayland fixture (input acknowledgments) and
  gnome-text-editor (a real application), subject to Step 0.
- **Order:** build a working vertical slice first, then harden it.
- **GitHub:** close #5–#8 and #28–#41 with a pointer here; open the issues below.
  Drafts are reviewed before anything is posted.
- **Safety focus:** a stuck key can only affect the private compositor and goes
  away on `stop`. So the safety budget goes mainly to leaked processes and units,
  and to keeping input from wedging *within* a session.

## Step 0: go/no-go checks (about 2 hours, before any churn)

1. **kwin-mcp comparison.** Run kwin-mcp (isac322/kwin-mcp) once against the
   Wayland fixture and gnome-text-editor. Check:
   - cleanup after a kill;
   - behavior of held keys on error;
   - how isolated the session is (HOME, buses, process ownership);
   - how recently it's been maintained.

   Write the result into ARCHITECTURE.md §dependencies, replacing the unexplained
   exclusion at `ARCHITECTURE.md:304`.
   - **Accessibility (AT-SPI):** this project deliberately disables accessibility
     (`environment.py:13`), while kwin-mcp's main advantage is reading the
     widget tree. Record whether that matters for the intended use. If it does,
     add it as a deferred MAY item (a private accessibility bus inside the
     session) rather than dropping isolation.
   - **Outcome:** continue as planned, or stop and discuss adopting or
     contributing to kwin-mcp.
2. **Target-app check.** Launch gnome-text-editor with the current build and run
   launch, windows, focus, close and stop several times in a row. Watch for:
   - single-instance GApplication behavior across launches;
   - client-side decoration geometry (does `client` include the header bar?);
   - startup delays or warnings with no portal, dconf or accessibility available.

   If it misbehaves, switch to another native Wayland app now (Qt or a simple
   game) instead of finding out in Phase 2.

### Step 0 results (2026-10-03)

**kwin-mcp** (isac322/kwin-mcp v0.10.0, MIT, last commit 2026-09-28, about 10k
lines of source, 33 MCP tools, plus a pipe/REPL CLI):

- **It works now.** Pipe-mode CLI: start, wait_for_element, keyboard_type,
  screenshot, stop took about 1.3s, and the screenshot showed the typed text in
  gnome-text-editor. It covers click, drag, scroll, touch, clipboard and the
  accessibility tree.
- **Crash cleanup is weak (verified).** After SIGKILL of its controller, the
  following all stayed alive or on disk until removed by hand: the private
  `kwin_wayland`, the app, the AT-SPI bus and launcher, a `dbus-run-session`
  helper shell, the temp HOME and the Wayland sockets. It owns processes by
  process group (`start_new_session=True`), not cgroup.
- **Weak defaults:**
  - `isolate_home=False` by default, so apps use the real HOME and config
    unless asked otherwise.
  - Screenshots are deleted on stop unless `keep_screenshots=true`.
  - It has an opt-in mode that drives the personal desktop.
- **Input:** modifier and button releases in `mouse_click` are not in
  `try/finally` (`input.py:1048-1066`).
- **No session across separate shell commands:** the session lives inside one
  MCP-server or REPL process.
- **No built-in readiness wait:** typing before the window was ready lost
  input (only "p" arrived on the first run).

**agent-desktop, same tests:**

- **SIGKILL of the worker:** systemd removed the whole cgroup (compositor, bus,
  app). `status` reported `failed` with `cleanup: complete`, and the logs and
  manifest were kept. The only leftover was a failed-unit entry (Phase 1.6).
- **Target app:** five consecutive cycles of start, launch gnome-text-editor,
  windows, focus, close (exited=true), stop all passed, at about 2.2s per cycle.
  No leftover units or processes.
- **Single-instance:** no GApplication problem across fresh sessions.
- **Log noise:** only harmless warnings (spell plugins; the AT-SPI bus is absent
  by design).
- **Geometry:** for this client-side-decorated app, `client == frame`
  (700x520). So "client content" includes the GTK header bar. Phase 2b
  documents this as the click coordinate contract.
- **Logs:** app logs keep their `.partial` names after exit. Finishing them
  belongs to Phase 5 (M6.3 scope).

**Outcome:** the projects have different strengths. kwin-mcp is ahead on
features. agent-desktop is ahead on ownership, crash cleanup, default
isolation, persistence across CLI calls and durable artifacts.

**Recommendation:** continue, and use kwin-mcp's MIT code as a *reference*
(keymap tables, pointer and absolute-motion libei usage, wait-for-element
ideas) to shrink 2a and 2b. Attribute any copied code. AT-SPI stays deferred.
Decision recorded in ARCHITECTURE.md once confirmed.

## Phase 0: repo cleanup (kept small)

1. Move `evidence/issue-9/environment.json` to `tests/data/issue9-environment.json`,
   or drop the checks that read it, and update its readers:
   `tests/test_dependencies.py:108`, `tools/kdotool_probe.py:122`,
   `tools/private_harness.py:518`. Update the comment at `prerequisites.py:27`.
   Run the tests.
2. `git rm -r evidence/` and add `evidence/` to `.gitignore`. Write
   `docs/VALIDATION.md` as a single table: issue, headline result, and the last
   commit SHA where the raw evidence can still be found.
3. Repoint or delete the `evidence/` links in ARCHITECTURE.md and docs/*.md.
4. Add a short note at the top of REQUIREMENTS.md: REQ-036 exact pinning, REQ-038,
   REQ-039 and REQ-040 are superseded by this plan's release bar. (No per-ID
   retirement work.)
5. Commit `planning/`, which was never tracked, including this plan. The old
   milestone files are marked as superseded.
6. Local only, with confirmation: prune the `.local/issue*` folders (about 1.4 GB)
   and keep `.local/dependencies`.

## Phase 1: consistency and robustness fixes

1. **One list of supported operations.** A single constant feeds `--help`,
   `doctor`, `session status` and readiness. Remove the "not wired yet" text
   (`cli.py:32`, `__init__.py:1`).
2. **Retire the provisional-provider fields in lockstep.** Replace or remove
   `provider: m1-provisional`, `release_qualified`, `replacement_issue` and
   `desktop_operations_supported` everywhere at once:
   - `readiness.py:21`
   - `prerequisites.py` (`PROVISIONAL`)
   - `lifecycle.py:316-317`, whose start validation otherwise rejects the change
   - `shutdown.py:11,42`
   - their tests

   Bump `schema_version` if the result shape changes.
3. **Ready-to-use refs.** Every app and window handle in output also carries
   `"ref": "<generation>:<id>"`. Document the string and object forms in CLI.md
   (`contracts.handle()` already accepts both).
4. **Dependency checks that survive updates.**
   - **libei:** first make setup failure safe by construction. If EIS setup
     fails, leave the FD open instead of calling `os.close` when ownership is
     ambiguous (`input_connection.py:150-153`). The session fails anyway, and
     systemd reclaims it. Then replace the SHA-256 check (`libei_binding.py:49`,
     `prerequisites.py:34`) with a soname check plus the `getattr` symbol
     resolution already done in `load()`. Optionally add a version floor.
   - **kdotool:** pin the source revision and the `Cargo.lock` hash only. Drop the
     fixed binary-hash set (`prerequisites.py:28,176`). Still check that the
     installed binary matches its own build receipt.
   - **KWin:** record the tested KWin version (currently 6.7.5) in `doctor`
     output and the manifest. A different version gives a warning, not a
     failure, plus the advice to rerun the smoke test.
5. **doctor checks the worker.** Confirm that the interpreter the service will
   use can import `agent_desktop.worker`. This is the PYTHONPATH trap.
6. **Clean up failed units.** After reconciling a failed generation, run
   `systemctl --user reset-failed` on its unit.
7. **Python 3.14 deprecation.** Use `GLibUnix.signal_add` when available and fall
   back to `GLib.unix_signal_add` (`worker.py:269`).

## Phase 2a: keyboard and screenshot

Mostly wiring: `Input.press/release`, the held/retired ledger and the
uncertain-input block already exist (`input_connection.py:299-356`). The capture
child is complete (`provisional_capture.py`).

- **Keymap.** Documented physical key names, including common game keys (WASD,
  arrows, space, shift and others).
- **`key CHORD [--hold S]`:** parse and validate the whole chord before sending.
- **`type TEXT`:** a US-layout character map that inserts generated Shift
  presses; reject the whole request on any unsupported character.
- **Focus check:** every input action verifies the window exists and is focused
  first.
- **Timeout budget:** document the longest text that fits in `type`'s timeout,
  or raise the ceiling (`contracts.py:28`, currently 3s).
- **`screenshot`:** promote the capture child to a production adapter. Each
  capture gets a unique, complete PNG plus metadata.
- **Production readiness:** readiness uses the production adapters, and the
  provisional provider goes away.
- **Guarantees against stuck input, required in 2a:**
  - The worker owns hold and release timing, so a client disconnect or Ctrl-C
    never skips a release.
  - No new input is accepted while release is uncertain (existing block,
    surfaced as a clear error code).
  - `session stop` works while input is uncertain.
- **Done when:** the fixture acknowledges key order, modifiers, text and hold
  duration. Against the target app, launch → type → screenshot shows the typed
  text, and a key held across a client kill is released.

## Phase 3: end-to-end smoke test

- `tests/integration/smoke.py` drives the installed CLI through separate
  invocations: start, launch, windows, focus, key, type, screenshot, close, stop.
  It confirms no owned processes or units are left over and that the artifacts
  survived. It prints the KWin, libei and kdotool versions it ran against.
- Add `--loop N` and a short `docs/TESTING.md`. Rerun it after system updates.

## Phase 2b: pointer and click

New binding work: today only the keyboard capability (4) is bound
(`libei_binding.py:44`, `input_connection.py:250`), and exactly one resumed
device is assumed (`input_connection.py:69`).

- Bind libei's absolute pointer and button capabilities, add the extra symbols
  (absolute motion, button, region queries), and handle separate keyboard and
  pointer devices.
- Add buttons to the release ledger.
- **`click --x --y [--button]`:** client-content coordinates, bounds checked,
  translated to global coordinates from the current geometry. Use what Step 0
  found to decide and document whether a client-side-decorated header bar counts
  as "content".
- Add `click` to the smoke test.

## Phase 4: input hardening and failure paths

Trimmed M5.4, M5.5, M7.3 and M7.4.

- Bounded cancellation in the middle of a sequence during holds and long typing.
- Focus is rechecked during held or repeated input; losing focus cancels and
  releases.
- `input reset`, which replaces the connection after an uncertain release.
- Integration tests:
  - focus loss;
  - cancel during a hold;
  - generation mismatch;
  - compositor or bus death leading to failed state and cleanup;
  - stop with an unresponsive worker;
  - no leftover units or processes after each test.

## Phase 5: logs, artifacts and docs

Trimmed M6.3 and M8.1.

- `logs` command and a complete session manifest (launch args, dependency and
  KWin versions, outcome).
- Finalize application log artifacts. `.stdout.partial` and `.stderr.partial`
  currently remain after the app exits and the session stops.
- README quickstart written for coding agents: the typical command sequence, the
  ref format, error codes, and what "dispatched" does and doesn't guarantee.

## Proposed GitHub issues

1. Go/no-go: kwin-mcp comparison and target-app check (Step 0)
2. Repo cleanup and plan adoption (Phase 0)
3. Contract consistency, refs and dependency robustness (Phase 1)
4. Keyboard input and screenshots (Phase 2a)
5. End-to-end smoke test (Phase 3)
6. Pointer input and click (Phase 2b)
7. Input hardening and failure-path tests (Phase 4)
8. Logs, manifest and agent-facing docs (Phase 5)

## Deferred

- GLib main-loop stalls over 100 ms caused by synchronous artifact writes
  (observed during #27). Fix only if it causes real misses.
- An accessibility tree via a private AT-SPI bus, if Step 0 shows it matters.
- Live viewing, XWayland, MCP wrapper (REQ-047/048/050).
