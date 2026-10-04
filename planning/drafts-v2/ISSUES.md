# Draft GitHub issues (revised plan)

Not posted. Source: `planning/REVISED_PLAN.md`. Each issue is done when unit tests
pass, its real-desktop check passes and the docs are updated. No evidence
archives.

---

## Comment for closing #5–#8 and #28–#41

> Superseded by the revised plan in `planning/REVISED_PLAN.md` (personal-tool
> scope: smoke test plus failure-path tests replace per-issue evidence and the
> 20-run qualification). Scope from this issue moves to #NEW. Closing as not
> planned.

Mapping: M5.2 and M5.3 → Keyboard; M6.1, M6.2 and M7.1 → Keyboard;
M5.3 (pointer) → Pointer; M5.4, M5.5, M7.3 and M7.4 → Hardening; M7.2 and
M8.2 → Smoke test; M6.3, M8.1 and M8.3 → Logs and docs. Milestones #5–#8 close
together with their children.

---

## 1. Go/no-go: kwin-mcp comparison and target-app check

**Status:** done 2026-10-03. Create and close it as a record.

Results are in `planning/REVISED_PLAN.md`, under "Step 0 results":
- kwin-mcp has more features. agent-desktop has stronger ownership and crash
  cleanup (verified with a SIGKILL test on both).
- gnome-text-editor passes 5 consecutive full cycles. Its client geometry
  includes the CSD header bar.

**Decision:** continue, using kwin-mcp (MIT) as a reference implementation.

---

## 2. Repo cleanup and plan adoption

- [ ] Move `evidence/issue-9/environment.json` → `tests/data/issue9-environment.json`.
      Update `tests/test_dependencies.py:108`, `tools/kdotool_probe.py:122`,
      `tools/private_harness.py:518` and the comment at `prerequisites.py:27`.
      Tests pass.
- [ ] `git rm -r evidence/`; add `evidence/` to `.gitignore`.
- [ ] `docs/VALIDATION.md`: one table (issue, headline result, last commit with raw
      evidence).
- [ ] Repoint or delete the `evidence/` links in ARCHITECTURE.md and docs/*.md.
- [ ] REQUIREMENTS.md note: REQ-036 exact pinning and REQ-038–040 are superseded
      by the revised release bar.
- [ ] ARCHITECTURE.md: replace the unexplained kwin-mcp exclusion (`:304`) with
      the Step 0 comparison and reference-use policy.
- [ ] Commit `planning/`.
- [ ] (Local) prune `.local/issue*`; keep `.local/dependencies`.

---

## 3. Contract consistency, refs and dependency robustness

- [ ] One supported-operations constant feeds `--help`, `doctor`, `status` and
      readiness. Remove the "not wired yet" text (`cli.py:32`, `__init__.py:1`).
- [ ] Retire `m1-provisional`, `release_qualified`, `replacement_issue` and
      `desktop_operations_supported` in lockstep: `readiness.py`,
      `prerequisites.py`, `lifecycle.py:316-317`, `shutdown.py:11,42` and their
      tests. Bump `schema_version` if the result shape changes.
- [ ] Add `"ref": "<generation>:<id>"` to every app and window handle in
      output, and document it in CLI.md.
- [ ] libei:
  - [ ] On ambiguous setup failure, leave the FD open instead of closing it
        (`input_connection.py:150-153`).
  - [ ] Replace the SHA-256 gate with a soname and required-symbol check.
- [ ] kdotool: pin the revision and `Cargo.lock` only; drop the fixed
      binary-hash set (`prerequisites.py:28,176`).
- [ ] Record the KWin version in `doctor` and the manifest. Warn (don't fail)
      when it differs from the tested version.
- [ ] `doctor` checks that the service interpreter can import
      `agent_desktop.worker`.
- [ ] `systemctl --user reset-failed` for reconciled failed generations.
- [ ] Use `GLibUnix.signal_add` with a fallback (`worker.py:269`).

**Done when:** `doctor`, `--help` and `status` agree, and a session starts
after the changes.

---

## 4. Keyboard input and screenshots

Builds on `Input.press/release` and the ledger (`input_connection.py:299-356`)
and on the capture child (`provisional_capture.py`). kwin-mcp's keymap is a
reference.

- [ ] Documented physical key names, including game keys. `key CHORD
      [--hold S]` with full prevalidation.
- [ ] `type TEXT`: US-layout map with generated Shift; reject the whole request
      on any unsupported character. Document the longest text that fits the
      timeout, or raise the ceiling (`contracts.py:28`).
- [ ] Every input action checks that the window exists and is focused first.
- [ ] The worker owns hold and release timing. Client disconnect or Ctrl-C
      never skips a release.
- [ ] Input is refused while release is uncertain (clear error code). `stop`
      still works in that state.
- [ ] Production `screenshot`: unique, complete PNG plus metadata.
- [ ] Readiness uses the production adapters; the provisional provider is
      removed.

**Done when:**
- The fixture acknowledges key order, modifiers, text and hold duration.
- Against gnome-text-editor, launch → type → screenshot shows the typed text.
- A key held across a client kill is released.

---

## 5. End-to-end smoke test

- [ ] `tests/integration/smoke.py` drives the installed CLI through separate
      invocations: start, launch, windows, focus, key, type, screenshot, close,
      stop. Add click once issue 6 lands.
- [ ] Afterwards: no owned processes or units remain, and the artifacts exist.
      Print the KWin, libei and kdotool versions.
- [ ] Add `--loop N`, and `docs/TESTING.md` (run it after system updates).

**Done when:** it passes in under about a minute, and `--loop 5` passes.

---

## 6. Pointer input and click

- [ ] Bind the absolute pointer and button capabilities, with the extra libei
      symbols (absolute motion, button, regions). Support separate keyboard and
      pointer devices (today only cap 4 is bound and one device is assumed:
      `libei_binding.py:44`, `input_connection.py:69,250`).
- [ ] Buttons go into the release ledger.
- [ ] `click --x --y [--button]`: client-content coordinates (which include CSD
      header bars, per Step 0), bounds checked, mapped to global coordinates from
      current geometry.
- [ ] Add click to the smoke test.

**Done when:** the fixture acknowledges click position and button, and clicking
the gnome-text-editor "new tab" button changes the screenshot.

---

## 7. Input hardening and failure-path tests

- [ ] Bounded cancellation in the middle of a sequence during holds and long
      typing.
- [ ] Focus is rechecked during held or repeated input; losing focus cancels and
      releases.
- [ ] `input reset` replaces the connection after an uncertain release.
- [ ] Integration tests:
  - [ ] focus loss
  - [ ] cancel during a hold
  - [ ] generation mismatch
  - [ ] compositor death
  - [ ] bus death
  - [ ] worker SIGKILL
  - [ ] stop with an unresponsive worker
  - [ ] each test checks that no units or processes are left over

---

## 8. Logs, manifest and agent-facing docs

- [ ] `logs` command.
- [ ] Complete manifest: launch args, dependency and KWin versions, outcome.
- [ ] Finalize app log artifacts; `.partial` names remain after exit today.
- [ ] README quickstart for coding agents: command sequence, ref format, error
      codes, and what "dispatched" does and doesn't guarantee.
