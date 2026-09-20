# Issue #19: installed private desktop and application environment

Final validation used a fresh non-editable wheel in `/tmp/issue19-venv`, with
system distribution bindings, from source following main `6ac423b`. All **181
unit/process tests passed** (15.589s). The [summary](summary.json) records the
actual installed Python module hashes, native versions, fixture build provenance,
three final service generations, environment/output observations and cleanup.
An earlier exploratory run also passed; the retained evidence is the final-source
run, not an aggregate support qualification.

| Case | Generation | Observed result |
| --- | --- | --- |
| Default packaged worker | `3edfecbd3aca4fc5a2cfc647ccf47086` | Private bus/KWin sockets, independent installed CLI status and stop, settings removed and durable logs retained |
| Application/adapter boundary | `cc3642e5d12c437381cf12fb44133849` | Native one-output 1280×720 scale-1 observation; both actual children read project README from explicit project cwd; stop/reconciliation 89.6ms |
| Private-bus death | `b39012c998fc412eb8b952ba67dd8173` | Essential exit propagates from GLib callback to failed worker; owned worker/bus/KWin lifetimes exit; later stop/reconciliation 67.6ms, failed outcome preserved |

The last two cases use a test-only Python observer around the installed production
Desktop owner and launch method. That observer supplies no bus/compositor backend
and no alternate environment implementation. It deliberately contaminates worker
HOME/XDG/display/accessibility/session variables and a secret marker, then launches
real application and adapter-style Python reporters and the compiled native
Wayland fixture. Both reporters see the private endpoints/settings and fixed
policy, no host marker, no inherited secret, and no compositor permission knob.
Allowed PATH and APP_FLAG overrides survive. All protected override attempts fail
before the marker process can launch. The native fixture independently sees the
fixed output; its output observation is preserved in the artifact manifest.

The actual bus environment is readable and recorded. This platform denies reading
KWin's `/proc/PID/environ`; that limitation is explicit in the summary. The exact
constructed environment passed to its observed Popen is recorded separately:
ScreenShot2 permission is present only there. No EIS permission adjustment, global
KDE write, host clipboard/input/capture operation, Plasma shell, or XWayland is
used. Read-only hashes of the user's existing relevant KDE configuration files
match before and after all three runs.

Private roots/directories, generated files and sockets were observed owner-only;
control/runtime root checks use the existing runtime policy. Bus and Wayland
socket modes are also verified inside both real children. The service is the
cgroup ownership boundary, and independent lifetime/cgroup checks precede claims
of completed cleanup. Durable [per-generation logs](runs/) remain after removal
of the disposable settings tree. The bus-death worker traceback is the expected
failure evidence, not a successful desktop request.

Public `session start` remains rejected and `desktop_ready` remains false. The
packaged worker's socket construction is narrower than #20's query/input/capture
readiness checks. This issue's settings removal follows explicit lifecycle
reconciliation; it does not claim autonomous crash-record/settings cleanup before
#21. Trusted project/network access is preserved; this is not a security sandbox.

Reproduce from the checkout (choose fresh paths):

```sh
python -W ignore -m unittest discover -s tests -q
python -m venv --system-site-packages /tmp/issue19-venv
/tmp/issue19-venv/bin/python -m pip install --no-deps --no-build-isolation .
/tmp/issue19-venv/bin/python -I evidence/issue-19/installed_smoke.py \
  /absolute/durable/issue19-run
```

The smoke builds only the existing native Wayland test fixture, starts owned
private transient services, and writes receipts/logs to the supplied durable run
directory. It requires the recorded native prerequisites and available user
systemd manager. This machine's unrelated missing `openai-chatgpt` package-database
warning appears on native inventory; the requested installed package versions
were returned successfully.
