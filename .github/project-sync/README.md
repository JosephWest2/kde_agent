# GitHub project Status synchronization

Issues and pull requests are the source of truth. This automation only adds
project membership and changes the project's **Status** field. It does not
edit, close, label or assign issues, merge PRs, or run an agent.

## Placement

The first matching rule wins:

1. Closed issue or PR → **Done**.
2. `blocked` → **Blocked**.
3. `in-progress` → **In progress**.
4. `review` → **Review**.
5. `backlog` → **Backlog**.
6. Unlabelled open PR → **In progress** if draft, otherwise **Review**.
7. Other open issue → **Todo**.

Labels unrelated to progress are ignored. Prefer one progress label; if several
are present, the order above resolves the conflict. Remove old progress labels
when changing stages. Removing every progress label or reopening an issue
recomputes its placement from its current state; an old event cannot restore an
old status. Moving a card manually does not change the issue, and the next
reconciliation will restore the label-derived Status.

Issue creation, closing, reopening, adding a label and removing a label trigger
a synchronization after the Actions runner starts. An hourly reconciliation
also imports new open issues/PRs and repairs missed events, including PR label
and draft changes. There is deliberately no privileged `pull_request_target`
workflow. PR merges/closures may also move cards to Done through the project's
built-in workflows. Scheduled Actions can be delayed by GitHub.

The full scan covers currently open issues/PRs and existing project items from
this repository. It does not backfill every historical closed issue, touch
other repositories' items, or change/unarchive archived cards. A single-issue
run can add a recently closed issue if its creation event was missed.

## Credential and operation

Set the repository Actions secret **`PROJECT_SYNC_TOKEN`** to a dedicated token
with access to this repository and write access to the linked GitHub project.
For a classic personal access token, GitHub documents `repo` and `project`
scopes for this use. `repo` includes broad private-repository access; use an
expiration, keep the token separate from interactive CLI credentials, and rotate
it before expiry. No token belongs in code, workflow inputs, or logs. A GitHub
App credential is an alternative if token-generation wiring is added later.
The normal repository `GITHUB_TOKEN` cannot access user/organization Projects.

The project ID is set explicitly in `.github/workflows/project-sync.yml`.
The workflow runs secret-free unit tests on its changes. The write job runs
only on the default branch and checks out only that branch. It fails visibly
if the credential is missing or lacks access; it never silently reports sync
success. Each changed item is separately read back to verify its project,
source issue/PR, and Status. Only one synchronization job runs per repository
at a time; the hourly scan repairs events replaced in GitHub's pending queue.

Use **Actions → project status sync → Run workflow** on `main` to reconcile
all items, or enter an issue/PR number to update one item. The job summary shows
processed, added, changed, unchanged and skipped counts.

For a local preview using an already-authorized `gh` login:

```sh
python3 .github/project-sync/sync.py --repo OWNER/REPO --project-id PVT_NODE_ID --dry-run
python3 -m unittest discover -s .github/project-sync -p 'test_*.py' -v
```

Workflow configuration and source: `.github/workflows/project-sync.yml` and
`sync.py`. GitHub's authentication guidance:
https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/automating-projects-using-actions
