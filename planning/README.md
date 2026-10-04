# First-version implementation plan

> **Superseded (2026-10-03):** the remaining milestones (M5–M8) were replaced by [REVISED_PLAN.md](REVISED_PLAN.md) and issues #62–#68. This file is kept as history.


This plan implements the contracts in [REQUIREMENTS.md](../REQUIREMENTS.md) and
[ARCHITECTURE.md](../ARCHITECTURE.md), baseline `9e49b06e651764e33ee94aab67db304856401170`.
The repository contained those two documents and no implementation or existing
GitHub issues when planning began. Milestones are parent tracking issues; their
acceptance is complete only when their children and milestone criteria pass.

## Delivery order

1. **M1 — Platform evidence:** establish reproducible dependencies, an early
   acknowledged Wayland fixture, and evidence for window/input/capture adapters.
2. **M2 — Command foundation:** CLI, protocol, identity, deadlines and records.
3. **M3 — Session ownership:** private environments, supervision and cleanup.
4. **M4 — Applications and windows:** launch, ownership, targeting and closure.
5. **M5 — Input:** event lifecycle, mappings, targeting, cancellation and reset.
6. **M6 — Capture:** complete fresh PNGs and durable artifact access. This can
   proceed alongside M4/M5 after M3.
7. **M7 — Integration:** production readiness/shutdown, real workflow and faults.
8. **M8 — Qualification:** twenty consecutive runs, reproducible docs and evidence.

Dependencies mean completion gates. Children depend on specific prerequisite
children or earlier milestones, never on completion of their own parent. Within
a milestone, work without dependency edges may proceed independently. Fixture
development and meaningful validation accompany each feature. M7 assembles
the full cross-component suite; it is not the first point at which testing occurs.

M1 is an evidence gate, not a supported prototype release. Record measured
finite timing limits and justified architecture decisions there; M7 validates
those limits with production adapters. Known correctness blockers must be
resolved before dependent integration or support claims. No numeric performance
promise or delivery date is invented before measurement.

## Scope and evidence

All 40 MUST and six SHOULD requirements are mapped in [COVERAGE.md](COVERAGE.md).
SHOULD exceptions require a recorded reason and consequences. REQ-047–050 stay
deferred: live viewing, XWayland, richer inputs/output layouts/multiple active
apps, and MCP. The first release supports one native Wayland application and its
dialogs on the recorded Arch/KDE environment, at 1280×720 and scale 1.

No personal-desktop fallback, Plasma shell, sandbox claim, concurrent-client
support, plugin framework, ydotool, kwin-mcp, or MCP SDK is introduced. Trusted
builds remain in the ordinary project environment. Support for any particular
application needs its own successful recorded run.

## Drafts and reviews

- [INDEX.md](INDEX.md) lists every milestone and actionable issue with links.
- `milestones.json` and `subissues.json` contain the issue specifications.
- `drafts/` contains rendered issue bodies ready for GitHub.
- `reviews/` preserves the independent milestone and subissue reviews and revisions.
- `published.json` records created GitHub issue identities after publication.
- Run `python planning/render_plan.py` to validate references, dependency cycles,
  and requirement coverage and regenerate bodies. Actual GitHub issue numbers
  replace planning identifiers when a publication map exists.
- `publish_plan.py` publishes through authenticated GitHub CLI calls, journals
  issue identities, creates native parent/subissue relationships, and verifies
  issue bodies and links. `publication-verification.json` records verification
  counts and hashes of the published specifications.

The user authorized publication after both review stages pass. Draft review
occurs locally before creating any issues; no implementation work is performed
as part of this planning task.

Both review stages passed on 2026-09-15 using separate GPT-6 Astra subagents at
high reasoning effort. The milestone review moved the reusable acknowledged
fixture into M1 and assigned incremental extensions to its feature consumers.
The subissue review separated early simulated launch-record retention checks
from production window-wait acceptance in M4.3, and clarified the early provider
for delayed capture/control tests. Original findings and final approvals are
preserved in the review reports.
