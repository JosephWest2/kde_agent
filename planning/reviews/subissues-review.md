# Actionable subissue review

**Final decision: APPROVED** — 2026-09-15 (see re-review below).

Initial decision: CHANGES REQUESTED.

Independently reviewed all 33 items in `planning/subissues.json` against
`REQUIREMENTS.md`, `ARCHITECTURE.md`, the approved `planning/milestones.json`,
`planning/README.md`, and the milestone review and re-review. This is a review
of implementation planning, not evidence that the platform or implementation
already satisfies the contracts. No GitHub issues were created.

## Blocking change

1. **Remove the backwards window-wait acceptance prerequisite in M4.1.**
   M4.1 requires that a "Window timeout" retain the application/process handle,
   but production window discovery is implemented in M4.2 and bounded window
   waits in M4.3. Both depend on M4.1. As written, an implementer cannot run that
   production acceptance case using only M4.1 and its declared predecessors.
   Keep application-record allocation and retention on launch cancellation or
   disconnect in M4.1. Either explicitly validate record retention after a
   *simulated* post-launch wait failure there, with the real window-timeout case
   owned by M4.3, or move the window-timeout acceptance entirely to M4.3.
   M4.3 already requires retained handles on window timeout, so this correction
   need not add an issue or change the graph. Do not add an M4.3 dependency to
   M4.1, which would create a cycle.

## Assessment

- **Implementability and cohesion:** The split generally gives each issue one
  implementable responsibility and an observable result: adapter evidence,
  protocol, lifecycle, application records, window operations, input behavior,
  capture, integration, or qualification. The larger M1.2 and M3.4 items remain
  cohesive because their harness and cleanup responsibilities must be verified
  together. Implementation choices such as the reliable descendant-ownership
  mechanism are explicitly assigned to an owner rather than silently assumed.
- **Coverage:** All 40 MUST and six SHOULD IDs are assigned to children, and
  their substantive behavior is covered. M8.3 requires implementation and
  validation evidence for every MUST and a reason and consequences for any
  SHOULD exception. Representative-application validation cannot waive the
  real fixture workflow or twenty-consecutive-run gate.
- **Architecture:** The children preserve the Python CLI/worker, private
  versioned Unix socket, generation checks before side effects, transient user
  service, generation-bound cleanup, `KillMode=control-group`, finite stop
  deadline, and no automatic restart. They preserve the pinned kdotool fixed
  query, explicit private endpoints, header-checked limited ctypes/libei
  adapter, single GLib input owner, asynchronous queries, direct ScreenShot2
  pipe handling, Pillow encoding, and complete-file publication. The excluded
  packages, MCP SDK, viewer, extra backends, sandbox claims, and additional
  first-version workloads remain excluded.
- **Dependencies:** The declared graph is acyclic, including milestone
  completion edges through children. The earlier fixture blocker is resolved:
  M1 supplies input acknowledgments and observable rendering before M4–M6
  consume them. The M4.1 acceptance prerequisite above is the remaining
  blocking sequencing ambiguity found in this review.
- **Cross-cutting ownership:** M2 defines transport, deadlines, cancellation
  routing, records, and errors. M3 owns supervision and independent cleanup;
  M4 owns process/window behavior; M5 owns real input cancellation and recovery;
  M6 owns capture publication and artifact discovery. M7.1 explicitly replaces
  preliminary readiness/shutdown hooks and completes command, doctor, and
  manifest integration. M7.3/M7.4 own the complete failure matrix. This prevents
  provisional probes or partially connected manifests from passing release
  qualification.
- **Acceptance realism:** Feature-level real-compositor checks precede full
  integration. Header audits, device lifecycle reproduction, acknowledged
  input/rendering, independently failed essential processes, slow calls, partial
  images, uncertain release, and generation/PID safety have explicit evidence
  requirements. M1 must establish the real device-fault procedure; inability to
  obtain the required evidence remains a blocker rather than a skipped pass.
  Timing bounds are measured and later enforced, without an invented numeric
  performance promise.

## Optional observations

- M5.4 exercises cancellation while capture is slow but can complete before
  M6. To make its independent execution clearer, name the M1 capture harness
  or a controlled slow-work double as the provider for that check, and keep
  production capture/control responsiveness in M7.4. Tests should also explain
  which work is active and which is queued, since ordinary screenshot and input
  actions serialize.
- When implementing the early children, keep their validation boundaries
  explicit: M3.1 can establish service/worker persistence before desktop
  readiness exists, M3.3 invokes the cleanup interface that M3.4 completes,
  and M5.3 owns target-loss cancellation before M5.4 audits every exit path.
  These are sensible incremental boundaries and should not become premature
  claims of full lifecycle or input compliance.

## Validation performed

`python planning/render_plan.py` passed and rendered **8 milestones and 33
subissues**, validating references, dependency cycles, and ID coverage. Manual
review supplied the semantic acceptance-prerequisite check above, which the
renderer does not perform. Re-review the corrected M4.1/M4.3 wording before
publication; no other blocking change was identified.

## Final re-review — 2026-09-15

**APPROVED for publication. No remaining blockers.**

Re-read the corrected M4.1 and M5.4 together with their downstream validation
owners, M4.3 and M7.4. M4.1 now tests record retention with a simulated
post-launch wait failure and cancellation/disconnection, and exposes retention
hooks for M4.3. The real compositor window-timeout case explicitly belongs to
M4.3, after discovery and waits exist. This resolves the blocking acceptance
prerequisite without adding a cycle or weakening the actual requirement.

M5.4 now identifies the M1 capture harness or a controlled delay double for
early responsiveness checks, leaves production capture validation to M7.4,
and separates active-input and active-capture scenarios while preserving
ordinary-action serialization and queued cancellation. This also resolves the
optional capture-test clarification.

`python planning/render_plan.py` passed again for **8 milestones and 33
subissues**. Coverage, architecture boundaries, integration ownership, and the
release evidence gates remain intact. Approval applies to the issue plan;
implementation and platform qualification remain the work described by it.
