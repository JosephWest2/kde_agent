# Implementation milestone review

**Final decision: APPROVED** (see re-review below).

Initial decision: CHANGES REQUESTED.

Reviewed `REQUIREMENTS.md`, `ARCHITECTURE.md`, and `planning/milestones.json`
before actionable subissues are drafted. This is a planning review, not a
platform compatibility or implementation validation.

## Blocking correction

1. **Make the real test fixture available before its consumers (M1, M4–M7).**
   M1 requires acknowledged input; M4 requires real custom-rendered windows,
   dialogs, and descendant tests; M5 requires an instrumented application; M6
   requires acknowledged rendering changes. However, construction of the
   deterministic fixture is assigned to M7, which depends on those milestones.
   The declared dependency graph is acyclic, but its acceptance prerequisites
   imply a backwards dependency. Assign a minimal native Wayland fixture with
   input acknowledgment and observable rendering to M1, extend its window,
   dialog, and descendant controls in M4 and input/failure controls in M5, and
   make M7 responsible for assembling and completing the integration suite.
   Alternatively, identify an existing suitable fixture and its reproducible
   setup in the earlier milestone scopes. Ensure M6's visual acceptance has an
   earlier provider. Each milestone must be independently verifiable using
   artifacts available from itself or its declared predecessors.

## Assessment

- **Coverage:** Every MUST and SHOULD ID, REQ-001 through REQ-046, is mapped.
  The substantive scopes and acceptance criteria cover the required behavior;
  M8 also requires evidence for every MUST and documented exceptions and
  consequences for SHOULD requirements. Optional features remain deferred.
- **Ordering:** Platform feasibility precedes production integration, contracts
  precede lifecycle and actions, and integration precedes release qualification.
  Declared dependencies exist and follow a valid topological order. The fixture
  prerequisite above is the one blocking sequencing problem found.
- **Acceptance:** Real-compositor checks, measured deadlines, independent
  essential-process failures, retained artifacts, and the twenty-consecutive-run
  gate are verifiable. M3 explicitly identifies preliminary hooks and M7 requires
  their production replacement, avoiding an early support claim.
- **Scope and architecture:** The plan preserves the private systemd-owned
  KWin session, pinned kdotool, small project-owned libei/capture adapters,
  generation checks, responsive control handling, and CLI-first contract. It
  explicitly excludes ydotool, kwin-mcp, MCP delivery, and unnecessary backends.
- **Risks:** M1 appropriately gates uncertain native bindings, transport polling,
  capture isolation, and event-loop integration. Later milestones cover process
  ownership, stale cleanup, uncertain input release, cancellation under slow
  calls, and dependency-change requalification. No additional blocking risk
  omission was identified at milestone granularity.

## Optional observations for subissue drafting

- State how the one-active-application limit is enforced, including whether
  surviving descendants prevent another launch; add an acceptance case for a
  second launch while the first application remains active.
- Name the planned device-lifecycle fault-injection mechanism early in M1/M5.
  Preserve M7's rule that unavailable real lifecycle evidence blocks support,
  rather than allowing a mocked test to silently substitute for it.
- Identify an owner for each cross-cutting acceptance item when splitting
  milestones into subissues, especially the transition from M3's preliminary
  readiness/shutdown hooks to M7's production integrations.

## Final re-review — 2026-09-15

**APPROVED for actionable subissue drafting. No remaining blockers.**

Re-read the corrected `planning/milestones.json`. M1 now creates the reusable
native Wayland custom-rendered fixture with input acknowledgments and
controllable visual state. M4 supplies its own dialog, duplicate-title,
window-delay, exit, and descendant extensions; M5 supplies its own focus,
input-acknowledgment, and device-fault scenarios. M6 can use its declared M1
dependency for acknowledged visual changes. M7 assembles those existing
fixtures and explicitly prohibits deferring adapter-critical fixtures to it.

This resolves the backwards acceptance dependency without adding a dependency
cycle or weakening any acceptance gate. Requirement coverage, architecture
constraints, failure validation, and release qualification remain intact.
The optional observations above remain guidance for subissue drafting, not
conditions of milestone approval.
