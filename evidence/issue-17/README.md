# Issue #17 infrastructure evidence

Validated on the development host with distribution Python 3.14 and PyGObject,
against merged #16 baseline `b832fb6`. No native desktop was created or claimed.

- `PYTHONWARNINGS=ignore python -m unittest discover -s tests -q`: **149 tests passed**, 10.298s.
- `git diff --check`: passed.
- Built and installed into `/tmp/kde-agent-issue17-venv` with system site packages;
  two independent installed CLI processes from outside the checkout reached the
  packaged worker, obtained one correlated unsupported JSON response each, and
  produced two accepted terminal records. Installed JSON help passed.
- Worker termination removed its routing pointer while preserving generation
  manifest, request records and logs. Cleanup remains explicitly uncertain until
  production supervisor integration.
- New tests exercise caller/worker cwd separation, protected environment and bus
  grammar, final PATH, repeated request attempts and allocation collisions,
  private permissions/symlink rejection, interrupted atomic writes, sticky
  failure, bounded history, actual final serialization deadline, postaccept
  storage failure, startup failure, caller SIGINT and a real contended-store
  stop with release/cleanup continuing.

Reproduce installation and the retained smoke script:

```sh
python -m venv --system-site-packages /tmp/kde-agent-issue17-venv
/tmp/kde-agent-issue17-venv/bin/python -m pip install --no-deps --no-build-isolation .
python evidence/issue-17/installed_smoke.py
```

The smoke script uses temporary private runtime/artifact directories and removes
its fixture data afterward. Its JSON report is retained in `installed-smoke.json`.
Artifact durability is per-snapshot, not a filesystem/response transaction.
Cancellation measurements assume normal storage; there is no hung-filesystem or
production desktop acceptance claim.

## Fresh-review corrections

The corrected sources address the four reproduced findings from review of
`7642425`: XDG_CONFIG_DIRS list injection, unsynced new directory links, ancestor
substitution during store traversal, and attachment to incomplete stores with
recreated locks. Regression coverage includes the exact host-search value,
parent-fsync ordering and failed-admission effects, a root swapped to a disposable
symlink target, missing directories/logs/lock and malformed same-identity
manifests. Interrupted initialization leaves no attachable manifest. Request
records also retain distinct start and accepted-terminal observation timestamps.

The full 149-test suite, installed smoke and diff checks were rerun after these
corrections. `source-sha256.json` identifies the tested production modules and
new artifact tests; no earlier validation is attributed to changed sources.
