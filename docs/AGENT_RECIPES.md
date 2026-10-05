# Agent recipes

Short answers to the situations agents hit most. They assume the
[quickstart](../README.md#quickstart-for-coding-agents) and `D="agent-desktop --json"`.
The full contract is in [CLI.md](CLI.md), [INPUT.md](INPUT.md) and
[WINDOWS.md](WINDOWS.md).

## Refs go stale

Refs are `GENERATION:ID` strings copied from results (`result.application.ref`,
`result.windows[].window.ref`). They are valid for one session generation only.

- After `session stop` and `session start`, the new session has a new generation
  and the old application is gone. Every old ref is refused with
  `generation_mismatch` before anything is sent. Launch again and use the new refs.
- A window that closed is no longer in the current observation: its ref gives
  `target_not_found`. Run `$D windows --app APP_REF` and pick the current window.
- Don't cache refs across sessions or pick windows by title. Query again whenever
  you're unsure, it costs about 0.1s.

## Focus before input

`key`, `type` and `click --window` only send input to the active window. If the
window isn't active they fail with `target_lost` (reason `focus_lost`) and send
nothing. Reason `compositor_surface_open` means KWin's window menu is open; see
[below](#tooltips-popovers-and-the-window-menu).

```sh
$D focus --window WIN_REF            # verified: waits until KWin reports it active
$D type --window WIN_REF 'hello'
```

Focus again after anything that can move focus: opening or closing a dialog,
launching an app, or a click on another window.

If focus is lost during a hold or a long `type`, everything held is released and
the request fails with `target_lost`, reporting `strokes_sent`, `strokes_total`,
`key_held` and `focus_rechecks`. Detection is not instant: the window is rechecked
about every 250ms, so typically 0.25–0.35s (at worst about 0.75s) of input,
release included, can reach whatever took focus ([INPUT.md](INPUT.md)). Some of
the `strokes_sent` may have landed in the other window, and an interrupted held
key may already have taken effect. Treat it like `completion_unknown`: before
deciding what to resend, inspect the target (screenshot, title, app state), the
window that took focus, and the reported progress. Don't just refocus and send
the rest. `click` without `--window` uses screen coordinates and checks
nothing about focus. `screenshot --window` doesn't need focus.

## Modal dialogs

A dialog is a separate window of the same application, with its own ref and
title. It shows up in `windows` and is usually the active window. For example,
`ctrl+o` in gnome-text-editor adds:

```json
{"window":{"ref":"GEN:e227aced-…"},"title":"Pick Files","active":true, …}
{"window":{"ref":"GEN:f8c54f64-…"},"title":"New Document (Draft) - Text Editor","active":false, …}
```

What follows from that:

- Input to the main window fails with `target_lost`, because the dialog has focus.
- `focus --window MAIN_REF` times out (`timeout`, phase `focus_wait`) while the
  dialog is open. KWin keeps focus on the modal.
- `focus --app` and `close --app` fail with `target_ambiguous` once the app has
  more than one window. `context.candidates` lists them. Choose by title from
  `windows`, then use `--window`.
- Work with the dialog through its own ref: `focus --window DIALOG_REF`, then
  `type`, `key` or `click`. For example, `key --window DIALOG_REF escape` cancels
  it. Confirm it closed with `wait --for gone --window DIALOG_REF`; the app keeps
  running. After it closes, the main window was active again in our test, but
  `focus --window MAIN_REF` before typing.
- Use `wait --for window --app APP_REF` only for the first window. It is satisfied
  by any existing window, so it can't wait for a dialog to appear. Poll `windows`
  instead.

## Did the app react?

Prefer a bounded wait on something the app reports over taking screenshots in a
loop. Titles often change on save, open, new tab and navigation:

```sh
$D type --window WIN_REF 'hello'
$D wait --for title --window WIN_REF --match 'hello'          # substring, case-sensitive
$D wait --for title --window WIN_REF --regex --match '^New Document'
$D key --window DIALOG_REF escape
$D wait --for gone --window DIALOG_REF                         # dialog closed, app still running
```

- Both return as soon as the condition holds, including on the first poll, and
  time out after `--timeout` (default 10s, at most 60s) with `timeout`. The
  timeout's `context.last_query_artifact` is the last observation it saw.
- `title` fails with `target_lost` if the window disappears while waiting, and
  `target_not_found` if it was already gone. Null or empty titles never match.
- `gone` succeeds immediately with `already_gone: true` if the window was already
  absent, which a mistyped ref would also give. It accepts popups and the window
  menu too, so you can wait for a tooltip or menu to close.
- `--regex` takes a Python `re` pattern. An invalid pattern is
  `invalid_arguments` before anything is sent. A pattern that backtracks for more
  than 100ms of CPU on a title ends the wait with `invalid_arguments`, reason
  `pattern_too_slow`; simplify it rather than retrying. A pattern that can match
  an empty string (`a*`) matches every title. See [Waits](CLI.md#waits).
- A wait occupies the session while it runs: your other commands to that session
  queue behind it. Use screenshots for changes that don't show in titles or
  windows (content, colors, layout).

## Tooltips, popovers and the window menu

`windows` also lists popups, marked by `kind` ([WINDOWS.md](WINDOWS.md#row-kinds-windows-popups-and-compositor-surfaces)):

- `kind: "popup"`: GTK tooltips, popover menus and dropdowns. A tooltip shows up
  as an untitled, inactive row of the app, for example after a click leaves the
  pointer resting on a button. `--app` selection ignores popups, so it stays
  unambiguous. They can't be targeted with `--window` (`unsupported_operation`,
  reason `popup_surface`). Input to the main window works and goes to the app as
  usual: `key --window MAIN_REF escape` closes its open popover.
- `kind: "compositor"`: KWin's own surfaces, such as the window menu that opens
  when you right-click empty space in a client-side-decorated header bar. The row
  has `pid: null` and `app: null`. The app window stays active, but the menu has
  the keyboard and its accelerators act on the window (`c` closes it). So while
  the menu is open, `key`, `type` and `click --window` fail with `target_lost`,
  reason `compositor_surface_open`. If the menu was already open, nothing is sent
  (outcome `not_started`). If it opened during a long `type` or hold, the failure
  comes from a focus recheck after some strokes went out (outcome `unknown`,
  with `strokes_sent` and the rest of the progress). Handle that like
  [focus loss](#focus-before-input): inspect before resending anything. `focus`
  succeeds but doesn't close the menu.

To close the window menu, click a screen point outside the menu, without
`--window`. The menu takes that click, so it doesn't reach the window under it.
Then check that the `compositor` row is gone:

```sh
$D click --x 5 --y 715                       # pick a point outside the menu
$D windows                                   # no row with kind "compositor"
$D key --window WIN_REF ctrl+a               # input works again
```

## Client vs screen coordinates

- `click --window REF --x X --y Y` takes **client-area** pixels, the same space
  as `screenshot --window REF`. A pixel at (x, y) in a window screenshot is the
  same `--x x --y y`, as long as the window is fully on screen (for clipped
  windows, see [Screenshots](CLI.md#screenshots)).
- For client-side-decorated (GTK/libadwaita) apps, the client area **includes the
  header bar**. gnome-text-editor's "New Tab" button is at about client (107, 23).
  Qt/KDE apps get a KWin title bar, which is outside the client area. You can't
  click it with `--window`, and it appears only in full-screen screenshots.
- `click --x X --y Y` without `--window` takes **screen** pixels (0–1279, 0–719,
  as in a full screenshot). There is no window or focus check, so whatever is at
  that point gets the click. The screen point of a client pixel is
  `client.x + x`, `client.y + y`, using `client` from `windows`. With client
  (290, 100), the "New Tab" button above is screen (397, 123).
- Points outside the client area or the screen are refused with
  `invalid_arguments` (reason `outside_window` or `outside_screen`) before anything
  is sent.

## Recovering from `completion_unknown`

The request may have been sent, but the reply was lost (exit 11, outcome
`unknown`). The action may or may not have happened, and nothing is retried
automatically.

1. Run `$D session status`. If the session isn't ready (for example it fails with
   `session_unavailable` and `context.state: failed` after the worker died), run
   `session stop` and `session start`, then launch again. All old refs are stale.
2. If the session is still ready, find out what happened before repeating: check
   `windows` (for example the title), take a `screenshot` or read `logs --app`.
   Repeat the action only if it didn't take effect. Retyping text that did arrive
   doubles it.

## Recovering from `input_uncertain`

The worker couldn't confirm that a key or button was released, so `key`, `type`
and `click` are refused with `input_uncertain` (exit 9) for the rest of the
session. There is no `input reset`.

```sh
$D session stop      # still works; it releases what it can and ends the apps
$D session start     # about 1s; new generation
```

Then launch the app again with new refs. The error's `context` describes the
uncertainty.
