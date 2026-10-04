# Setup

One command prepares a checkout: `tools/setup.sh`. It builds the pinned
dependencies, installs `agent-desktop` into a project venv and runs
`agent-desktop doctor`. It never uses sudo or installs system packages.

## Supported host

- Arch Linux, x86_64.
- KDE Plasma 6: KWin 6.x. KWin 6.7.5 and libei 1.6.0 are the tested versions;
  `doctor` warns on others, and then you should run the smoke test
  ([TESTING.md](TESTING.md)).
- A systemd user session with cgroup v2 (a normal Plasma login provides it).
  Sessions run as transient user services; your own desktop is never touched.

## Prerequisites

The Arch packages are listed in [dependencies.json](../dependencies.json)
(`arch_packages`):

```sh
sudo pacman -S --needed kwin plasma-workspace systemd dbus libei python \
  python-gobject python-dbus python-pillow glib2 gobject-introspection-runtime \
  libffi libjpeg-turbo zlib wayland wayland-protocols libxkbcommon gcc pkgconf \
  git rustup
```

kdotool is built with rustup's stable toolchain, which must support Rust
edition 2024:

```sh
rustup toolchain install stable
```

The first run needs network access: it fetches the pinned kdotool source and its
crates, and pip fetches the setuptools build backend. The smoke test also uses
`gnome-text-editor` (or pass `--no-editor`).

## Run it

From the checkout:

```sh
tools/setup.sh
```

It does four things, in order:

1. **Checks prerequisites.** It runs `pacman -T` on the package list and checks
   the rustup stable toolchain.
2. **Builds or verifies the pinned dependencies.** It runs
   `tools/dependencies.py setup`, which works only in `.local/dependencies/`:
   - creates `venv/` with `--system-site-packages`, so PyGObject, dbus-python and
     Pillow come from the distribution;
   - fetches kdotool at the pinned revision and builds it with Cargo's locked
     resolution into `bin/kdotool`, recording a build receipt (`build.json`);
   - on later runs, verifies that receipt instead of rebuilding.
3. **Installs the package** into that venv with `pip install`. The session
   service runs the installed package, using the interpreter it was installed
   into, so this is what makes `session start` work.
4. **Runs `agent-desktop doctor`** and prints any untested-version warnings.

On success it ends with `setup: OK: agent-desktop is installed and doctor passed.`
and the next commands to run. The CLI is
`.local/dependencies/venv/bin/agent-desktop`; put `.local/dependencies/venv/bin`
on your PATH to call it as `agent-desktop`. `doctor` and `session start` look for
`.local/dependencies` relative to the current directory, so from anywhere else
pass `--dependency-root /path/to/checkout/.local/dependencies`
([CLI.md](CLI.md#commands-flags-and-defaults)).

The first run takes about half a minute on a typical machine, mostly the
kdotool build.

## Rerunning

Rerunning is safe and is a no-op when nothing changed (about a second). The build
receipt is verified, not rebuilt. pip is skipped only while the stamp in
`venv/.agent-desktop-install.sha256` matches (a hash of `pyproject.toml` and
`src/`, plus the venv interpreter's version and path) and the package still
imports from the venv. After pulling new code, rerun it to reinstall. After an
Arch Python upgrade, it recreates just the venv (never the kdotool build) and
reinstalls.

pip runs with every `PIP_*` setting and pip config file ignored, so nothing can
redirect the install, and setup checks afterwards that `agent_desktop` imports from
the venv.

Setup refuses to reinstall or recreate the venv while an `agent-desktop-*` session
service is running, because a live session's stop hooks run the installed code.
Stop the sessions first. It asks the same user manager the session code uses
(`/run/user/UID/bus`) and also refuses if that query fails. Nothing locks setup
against a `session start` during the install itself. Setup checks again
afterwards, and if a session appeared, it exits 1 and asks you to stop it and
rerun, so don't start sessions while setup runs.

To start over, delete `.local/dependencies` and rerun.

## When a prerequisite is missing

Setup prints what is missing and exactly what to install, then exits 1 without
changing anything outside `.local/`. For example:

```text
setup: FAILED: missing Arch packages: libei python-pillow
Install them with:
    sudo pacman -S --needed libei python-pillow
then rerun tools/setup.sh.
```

A missing rustup toolchain prints `rustup toolchain install stable`. A failed
dependency build or `doctor` check prints each failed item with its repair hint.
Exit status is 0 on success, 1 for a missing or broken prerequisite, and 2 for
bad arguments. `AGENT_DESKTOP_SETUP_PACMAN` replaces `/usr/bin/pacman` for the
package check, which is only useful to test these messages.

## The dependency tool

`tools/setup.sh` drives `tools/dependencies.py`, which you can also run directly.
It prints one JSON object on stdout for every outcome, including failure:

```sh
/usr/bin/python -I tools/dependencies.py setup           # venv + pinned kdotool build
/usr/bin/python -I tools/dependencies.py report          # versions and checks, as JSON
/usr/bin/python -I tools/dependencies.py check-kdotool   # receipt + script-generation check
```

`--root PATH` selects another work directory instead of `.local/dependencies`.
Run Python with `-I` as shown to exclude caller Python startup settings. Exit
status is 0 for success, 1 for failed or missing prerequisites and 2 for invalid
arguments. No network is needed for reporting a completed build.

The report records the required Arch package versions, the complete installed
package inventory (as an inventory, not a dependency claim), selected
pkg-config versions, the Python binding versions and their distribution origin,
libei's loaded SONAME/file and the Rust toolchain. The tool does not update the
system or choose a different Rust toolchain for you. Arch mirrors move:
recreating an exact recorded environment needs matching cached packages or the
[Arch Linux Archive](https://archive.archlinux.org/), and this tool is a
source/build lock and version recorder, not a filesystem image.

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
These are prerequisite-tool bounds only; desktop command bounds are in
[CLI.md](CLI.md#commands-flags-and-defaults).

## When dependencies change

The machine-readable rerun policy is in `dependencies.json`:

| Changed dependency | Required compatibility checks |
| --- | --- |
| KWin/KDE, systemd, D-Bus, Wayland | Environment; #10 private harness; affected #11 windows, #12 input, #13 capture/control loop. KWin changes require all three adapters. |
| kdotool revision/patch, Cargo graph, Rust | Rebuild/provenance and script generation; #11 queries/focus/timeout/cleanup and timing measurements. |
| Python, PyGObject, dbus-python, GLib, libffi | Binding inventory; #10 lifecycle; #11 orchestration; #12 device loop; #13 capture/control responsiveness. |
| libei, headers or native compiler | Environment; #12 ABI/header, readiness, events, input and cancellation; #13 responsiveness. |
| Pillow or image libraries | Environment and #13 metadata, PNG and pixel checks. |

The issue numbers name the original validation areas (#10 private harness, #11
windows, #12 input, #13 capture). Today the end-to-end smoke test and the
failure-path tests cover them: after any of these changes, rerun `tools/setup.sh`,
the unit tests and both integration suites ([TESTING.md](TESTING.md)). No
package-version check replaces running them.

`ydotool` and `kwin-mcp` are excluded from build and runtime dependencies: do not
install, invoke, vendor or patch them for this project. No MCP SDK is added.
See the [recorded prerequisite evidence](https://github.com/JosephWest2/kde_agent/blob/d1efe95b/evidence/issue-9/README.md).
