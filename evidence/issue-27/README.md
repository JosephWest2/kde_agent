# Persistent production libei connection qualification

Final evidence and measured results are added after installed native validation.

Reproduce from a clean committed source, in fresh directories, serially:

```sh
python -I evidence/issue-24/installed_targeting.py prepare /new/prepared --commit HEAD
/new/prepared/venv/bin/python -I /new/prepared/source/evidence/issue-27/audit.py /new/audit
python -I /new/prepared/source/tools/private_harness.py run --artifacts /new/runs -- \
  /new/prepared/venv/bin/python -I /new/prepared/source/evidence/issue-27/installed_input.py \
  /absolute/dependencies/bin/kdotool input
```

Repeat `input`, `cancel`, `removal`, `disconnect`, `focus-loss`, `slow-query`,
`reset-failure`, and `pause`. For `pause`, pass the locally built, audited M1
`--eis-fault-plugin /absolute/issue12_eis_fault.so` before `--`.
The native matrix generally takes under two minutes; each harness generation
retains its existing finite deadlines and independent owned-service cleanup.

The runner reuses the M1 fixture and scenario observations, but replaces its
Input class with the installed production owner and uses production asynchronous
PrivateBus/EIS negotiation. Reset between evidence actions includes neutral
fixture observations; it does not implement or certify public issue #31 reset.
Source and installed hashes are checked before scenarios; raw logs record actual
emission/fixture timestamps and real lifecycle events. No public input operation
is enabled by this PR. The full public input workflow and twenty-run support
qualification remain subsequent milestones.
