# Issue #9 prerequisite evidence

Recorded on the target Arch Linux machine on 2026-09-16. This is prerequisite and
custom-script **generation** evidence. No compositor, desktop adapter or supported
application was evaluated; desktop compatibility remains `untested`.

[environment.json](environment.json) is the actual successful JSON report. It
contains exact required Arch/native versions, a name/version inventory of 1,664
installed native packages, 138 resolved Python distributions, distribution binding
origins, libei SONAME `/usr/lib/libei.so.1.6.0`, build tool versions, and the full
117-package Cargo lock graph with checksums and resolved nodes. The installed
inventory is broader than this toolkit's dependencies: for example, the target
already had ydotool installed. It was only listed by the package inventory; this
work did not install, invoke, patch or vendor it. No excluded tool or MCP SDK was
added to the project.

The evaluated candidate is `be03ce90c09350898556436bac74ed35fe928617` (kdotool
`0.3.0`). Cargo.lock SHA-256 is
`d6beea15d1a9254586d71ac1c5c55d088d9dc3c9a8e980c6af7c2d8ee8f25edc`.
The recorded executable SHA-256 is
`62e7ee53096d933ec29e8e5d439b895590f851a40f0dcd87b87db9a6dc4749de`.
Build tools were rustc `1.95.0 (59807616e 2026-04-14)` and cargo
`1.95.0 (f2d3ce0bd 2026-03-21)`. Maintained patches are explicitly empty.

Commands were run from the repository unless otherwise specified:

```sh
/usr/bin/python -I tools/dependencies.py setup --root .local/issue9-clean
/usr/bin/python -I tools/dependencies.py setup --root .local/issue9-clean
/usr/bin/python -I tools/dependencies.py report --root .local/issue9-clean
/usr/bin/python -m unittest discover -s tests -v
```

The first command used a previously absent work root and completed in **16.237
seconds**, including source fetch, locked release build, venv creation, native
binding imports and private-bus custom-script generation. Repeating setup against
that root passed in **0.441 seconds**. A report invoked from `/tmp`, using absolute
utility/root paths, passed in **0.232 seconds**. The checked-in final report passed
in **0.214 seconds**, with custom-script generation in **0.019 seconds** and its
private daemon reaped. Timing excludes the external shell that redirected JSON.
The report contains no local checkout/HOME paths, inherited environment values,
credentials or Python direct-source URLs; portable source placeholders replace
Cargo's local package IDs.

Two independent build directories produced passing builds with the same source,
lock and toolchain. Their executable hashes differed; hashes identify each actual
artifact, and this work does not claim byte-identical builds across directories.
The setup above and report refer consistently to the second, clean build.

During development, an initial build succeeded but `cargo metadata --offline`
failed because it also needed non-host-target packages absent from that fresh
cache. The final setup obtains the complete graph using bounded `cargo metadata
--locked`; it succeeded from the clean root. No success receipt was written for
the failed attempt.

All **12 tests passed** (0.817 seconds), including a CLI regression added after PR review for null/non-mapping nested build receipts. They exercised injected environment and
Python startup settings, ignored host Git configuration, rejected project and
ancestor Cargo configuration, actionable missing tools/bindings, finite timeout
with a descendant retaining output handles, private-bus cleanup after successful
and failed generation, incomplete/tampered receipt/binary/source/lock rejection,
absence of a receipt after a failed build, and JSON-only failure when reporting a
missing root from another directory. Timeout testing observed no running owned
descendant after cleanup. `git diff --check` also passed.

Remaining compatibility evidence is explicitly assigned to #10 (private harness
and fixture), #11 (real kdotool transport/focus), #12 (libei ABI/device/input), and
#13 (fresh capture and control-loop integration). Dependency changes must rerun
the checks in [the setup policy](../../docs/SETUP.md#when-dependencies-change).
