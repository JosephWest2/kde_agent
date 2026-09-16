# Milestone 1 dependency setup

This utility records prerequisites for the first Arch Linux / KDE 6 target. A
successful report does **not** establish private-desktop compatibility or product
support. Real harness, window, input and screenshot evidence belongs to issues
#10–#13. It never starts KWin or connects to a desktop.

From the checkout, run:

```sh
/usr/bin/python -I tools/dependencies.py setup
/usr/bin/python -I tools/dependencies.py report > environment.json
/usr/bin/python -I tools/dependencies.py check-kdotool
/usr/bin/python -m unittest discover -s tests -v
```

Setup installs only into `.local/dependencies`, creates a Python venv with
`--system-site-packages`, fetches the exact source revision in
[dependencies.json](../dependencies.json), and builds kdotool with Cargo's locked
resolution. It does not change global packages. `--root PATH` selects an alternate
work directory, resolved from the caller's working directory. For example, from
another directory use an absolute path to the utility and an explicit absolute
`--root`. Missing/incompatible prerequisites yield repair instructions in JSON.

Each operation returns one JSON object on stdout, including failure. Exit status
is 0 for successful prerequisite evaluation, 1 for failed/missing prerequisites,
and 2 for invalid arguments. `--help` displays usage. No network is needed for
reporting a completed build; initial setup needs public upstream Git and crates.io
access. Run Python with `-I` as shown to exclude caller Python startup settings.

The required Arch package names are in `dependencies.json`. The recorded target
already provided them; if a report identifies a missing package, install it using
your normal Arch package workflow and rerun setup. The tool does not invoke sudo,
update a rolling distribution, or choose a different Rust toolchain for you.
The `rustup` stable toolchain must already be installed and support the pinned
candidate (Rust edition 2024). Its actual compiler and Cargo releases are recorded.

Python, PyGObject, dbus-python and Pillow come from the distribution. The isolated
venv has deliberate access to system site packages; no pip runtime packages are
installed. Reports include the complete visible Python name/version inventory,
actual binding versions and distribution-origin checks, GLib's loaded version,
and libei's loaded SONAME/file. Names and versions of all installed native
packages are included as an **inventory**, not as a claim that every installed
package is a toolkit dependency. Selected pkg-config versions and the explicit
required package set identify the relevant platform dependencies. Installed
unrelated packages are never invoked as dependencies by this utility.

Arch mirrors move. The evidence records exact package versions; recreating that
historical native environment requires matching cached packages or the
[Arch Linux Archive](https://archive.archlinux.org/), not installing today's latest
packages and assuming compatibility. Maintain the machine through normal Arch
upgrade practices. This utility is a source/build lock and version recorder, not
a filesystem image or a promise of byte-identical native packages/toolchains.

## Build provenance and configuration

The candidate is kdotool `be03ce90c09350898556436bac74ed35fe928617`, release `0.3.0`.
Its reviewed `Cargo.lock` SHA-256 is pinned in policy. A completed build receipt
records source revision/cleanliness, lock digest, executable digest, exact Rust
releases, complete Cargo lock packages with checksums, resolved dependency nodes,
and all maintained patches (currently `[]`). Repeated setup verifies an existing
receipt. Changed source, lock or binary fails explicitly; use a fresh `--root`
after reviewing the change. A failed build creates no success receipt. Generated
sources, caches, venv, binaries and receipts remain ignored; recorded evidence
includes sanitized provenance.

Subprocesses receive a constructed environment with a fixed `/usr/bin:/bin` PATH,
locale and temporary private HOME/XDG/TMP settings. They do not inherit display,
D-Bus, accessibility, Python, dynamic-loader, Cargo, Git, proxy or credential
variables. Git's system/global configuration and interactive credential prompts
are disabled. Cargo runs in a temporary directory outside the checkout using a
project-local Cargo home and explicit installed Rust compiler; unexpected Cargo
configuration in that directory's ancestry or credentials/configuration in the
project Cargo home are rejected. The only host Rust state read is the explicitly
selected installed stable toolchain. If network access requires credentials or
special proxy settings, the isolated build fails; it does not copy secrets.

Candidate `--help`/`--version` do not open a bus, but its `--dry-run` does. The
custom-script check creates and destroys an owner-private D-Bus daemon with an
explicit temporary socket and supplies `KDE_SESSION_VERSION=6` and only that
private bus. It validates generated custom JavaScript and its completion marker;
it never executes a KWin script. The report labels real desktop compatibility
`untested`. Successful script generation is not window transport evidence.

## Enforced bounds

| Operation | Work deadline |
| --- | --- |
| Report | 120 seconds total; ordinary subprocess at most 5 seconds |
| Setup | 900 seconds total |
| Git fetch | 120 seconds within setup |
| Cargo build | 600 seconds within setup |
| Locked Cargo metadata / Python venv creation | 60 seconds each within setup |
| Custom-script generation check | 10 seconds, including private-bus readiness |

Each step uses the shorter of its own bound and the operation's remaining time.
Process-group termination and direct-child reaping have an additional finite
2-second allowance per cleanup. A failing command and its private-bus owner can
require two cleanups, so a custom-script check has at most 4 seconds of cleanup
allowance. Children write to temporary files so a descendant retaining stdout or
stderr cannot prevent completion. Each returned stream is limited to 8 MiB;
failed-command diagnostics contain only its last 2,000 stderr characters.
The runner kills remaining group members even after the main command exits.
Timing evidence below is prerequisite timing only; desktop action/cancellation
bounds will be measured separately.

## When dependencies change

The machine-readable rerun policy is in `dependencies.json`:

| Changed dependency | Required compatibility checks |
| --- | --- |
| KWin/KDE, systemd, D-Bus, Wayland | Environment; #10 private harness; affected #11 windows, #12 input, #13 capture/control loop. KWin changes require all three adapters. |
| kdotool revision/patch, Cargo graph, Rust | Rebuild/provenance and script generation; #11 queries/focus/timeout/cleanup and timing measurements. |
| Python, PyGObject, dbus-python, GLib, libffi | Binding inventory; #10 lifecycle; #11 orchestration; #12 device loop; #13 capture/control responsiveness. |
| libei, headers or native compiler | Environment; #12 ABI/header, readiness, events, input and cancellation; #13 responsiveness. |
| Pillow or image libraries | Environment and #13 metadata, PNG and pixel checks. |

Combined milestone checks must run again before milestone acceptance. Later
production acceptance additionally requires REQ-037–REQ-039, including twenty
consecutive complete workflows. No package-version check replaces that evidence.

`ydotool` and `kwin-mcp` are excluded from build and runtime dependencies: do not
install, invoke, vendor or patch them for this project. No MCP SDK is added.
See [recorded prerequisite evidence](../evidence/issue-9/README.md).
