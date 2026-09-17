# Oversized-status supervisor cleanup correction

The original local transport regression failed against capture_probe.py SHA256
`23d2e8ae6d3e7da9672744890481d112c17056c5f3c79934c70e846cc8d6a08f`. This source passed the initial eleven-run desktop matrix,
which did not exercise oversized status. The failing test log is preserved in
`status-cap-test.log`.

Repeated parsing after status-cap abort skipped kill/reap finalization. The fix
stops reading/parsing status after abort starts and continues bounded reap and
artifact cleanup. The final source hash is recorded in ../summary.json; all eight
transport tests pass, including the actual complete-byte-count-without-EOF case.
All eleven selected desktop scenarios were rerun against that final source.
Earlier desktop attempts remain separately indexed in ../all-attempts.json.
