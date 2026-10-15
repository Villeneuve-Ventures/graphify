# Local resource retention

This rule governs Graphify development resources: local branches and worktrees,
investigation outputs, preservation archives, delivery copies, and related
runtime sessions. It does not change retention for product-managed workspace
data. Apply it when creating outputs and at task closeout.

Explicit task boundaries, ownership, and existing acceptance contracts still
govern. This document grants no deletion, workflow cancellation, GitHub delivery,
or scheduled cleanup authority. Preserve active, paused, occupied, and concurrently
created resources. Keep unknown items until the missing evidence is resolved.

## Keep set and release conditions

| Resource | Keep | Release condition |
| --- | --- | --- |
| Unique source, history, local edits, or irreplaceable research data | A verified recoverable copy, its provenance, and restore instructions | Equivalent recovery is verified elsewhere, or the operator explicitly decides to discard the unique material |
| Unresolved reproduction | The smallest complete reproducer, required inputs, failure output, and environment description | Resolution is verified and the final receipt preserves the diagnosis and disposition |
| Final receipt | Source identity, checks, results, unresolved findings, and artifact references | Retain as the compact historical record; append corrections instead of rewriting history |
| Contract-required evidence | Original approvals, validation receipts, inventories, and other artifacts required by the governing contract | That contract permits release, or its authorized owner explicitly changes the requirement |
| Active or paused work | Workspace, state, inputs, and evidence required to resume | Proven owner releases the resource, pending work is resolved, and all other keep conditions are satisfied |
| Rebuildable environment, cache, completed temporary fixture, or duplicate copy | Only what another keep condition requires | Fresh checks establish no unique content, no consumer or retained dependency, and authority for the exact removal |

Age, merged PR status, a clean checkout, an idle process, differing commit hashes,
or missing function/test names alone do not establish a release condition. Check
whether logic moved, tests were renamed or folded together, or contracts changed.
Use review dates to revisit unknown items; no resource expires solely by age.

## Local retention index

Use one index in an operator-selected Graphify evidence root outside the Git
checkout. Report its absolute path at closeout; keep machine-local paths out of
committed policy. A local index is discovery guidance, not proof of current state
or a prerequisite for another machine to review this policy.

For each retained packet or separately classified component, record:

- Stable identifier, actual path or ref, observation date, and repository identity.
- Proven owner, or `unconfirmed`; identify who can decide its disposition.
- Classification, reason to keep, supporting receipt, and unresolved uncertainty.
- Canonical recovery location, hashes or manifest, and restore prerequisites.
- Dependencies in either direction, release condition, and next review date or
  event. A review date schedules nothing and authorizes no deletion.

Group related files only when they share a retention decision. Mixed archives
need component decisions: copied source, environments, research inputs, and
validation evidence can have different release conditions. Avoid additional
working copies when a canonical recovery packet already serves the purpose.

## Compact final receipt

Keep one final receipt per completed task or accepted candidate. Include:

- Repository, full base/HEAD identities, and preserved local edits or their
  canonical patch location and digest when HEAD does not describe all inputs.
- Task outcome, governing contract, owner, and disposition authority.
- Commands, relevant arguments and environment, results, and validation limits.
  Record shard/coverage identities when needed to support an aggregate result.
- Unresolved findings, retained artifact paths and hashes, recovery dependencies,
  and exact cleanup outcomes or unavailable operations.

Keep raw output when it contains unique failure evidence, explains a disputed
result, supports an unresolved reproduction, or is contract-required. Other
successful verbose output may be released after the receipt is complete and
release checks pass. A hash cannot replace evidence that still needs inspection.
Historical passing evidence does not establish current readiness.

## Task closeout

1. Identify the exact repository and resources created or used by the task. Read
   applicable instructions and the governing evidence requirements. Record the
   current branch/HEAD, dirty and untracked state, and relevant live PR disposition
   when it affects release. Do not start tests, gates, or monitoring for retention.
2. Classify each component and preserve the keep set before proposing removal.
   For Git resources, check ancestry, unique commits, local and ignored content,
   owners, processes, sessions, gates, and dependencies. For sessions, inspect
   attachment, pane commands, descendants, activity, pending work, and canonical
   workflow state; detachment alone is insufficient.
3. Inspect archives without running archived scripts or extracting through unsafe
   paths. Compare source bytes with current and relevant landed revisions. Check
   bundle heads, prerequisite history, shared Git stores, and restore instructions.
   Verify any replacement recovery packet before releasing its predecessor.
4. Write the final receipt and update the local index. If exact removal is
   authorized, revalidate target identity and ownership immediately before using
   the supported removal interface. Use documented interfaces for app-managed
   artifacts; record unavailable operations and unconfirmed queued results.
5. Verify authorized removals and preservation of retained resources. Leave new,
   changed, occupied, or uncertain targets untouched and report the gap. Do not
   rewrite original receipts or clear workflow state to simplify cleanup.

For report-only tasks, stop at the report and proposed dispositions. Future audits
must refresh evidence; prior receipts, this index, and this policy are not standing
authorization to prune resources.
