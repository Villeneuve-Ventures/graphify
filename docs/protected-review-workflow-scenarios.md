# Protected review workflow integration scenarios

This is an evidence-backed guidance matrix for the checkpoints in
[the protected-change policy](protected-change-review.md), not a new approval
mechanism. The policy and authority pinned for an attempt govern that attempt.

## Ownership and overlap assessment

Inspected on September 12, 2026, at local base
`81ef9c71943e1ec7a75481a110fb57aa005b397c` in an initially clean isolated worktree.
[PR124](https://github.com/Villeneuve-Ventures/graphify/pull/124), merged September
10 as `5a5c0bd553d255f117dbac3ee80448f9e11f16e1`, already added policy section 10,
`graphify/protected_change_equivalence.py`, and its tests. Its comparator explicitly
does not authenticate reviewers, establish the complete check set, or authorize
approval transfer. Those responsibilities remain with policy orchestration.

The live open-PR listing contained #126, #127, #128, and #129. The inspected
[PR127 diff](https://github.com/Villeneuve-Ventures/graphify/pull/127/files) only
adds a README commands-by-purpose table. The other listed PRs concern semantic
incremental updates or Gemini defaults; none is a separately listed adoption or
reviewer-recovery repair. This is a dated overlap observation, not a continuing
claim about GitHub state. No reviews or CI were monitored.

The durable repository owner is `docs/protected-change-review.md`, reached from
`AGENTS.md`. The missing early decision and continuation fields belong there.
No repository implementation of `collaboration.list_agents` or
`collaboration.followup_task` was found; their runtime behavior is externally
owned. Rebuilding the comparator or patching graph construction would not repair
these integration gaps. No code changed, so graph refresh is not required.

## Scenario walkthrough

Read each row as input evidence, required action, and expected disposition.
The test names refer to existing comparator coverage; the owner/API rows are
manual contract walkthroughs, not claims of a live platform regression test.

| Case and evidence | Required workflow result | Verification basis |
| --- | --- | --- |
| Explicit prospective owner opt-in; authorized exact staging before freeze; one unchanged commit; complete immutable manifests, authenticated approvals, pinned runtime and fresh contexts | Section 10 comparison may link predecessor approvals only after all remaining checks pass; keep original approvals and separate delivery authority | `test_real_git_staged_commit_equivalence` checks the transition and `approval_granted=false`; policy supplies the external prerequisites |
| Missing, declined, or deferred opt-in; later request to commit | Record ordinary review before staging/freeze; later delivery authority does not retrofit eligibility | Section 1 checkpoint and section 10; comparator sees acceptance digests, not the owner's opt-in semantics |
| Opt-in exists but staging is unauthorized or incomplete at freeze | Do not stage without authority or retrofit the frozen candidate; ordinary re-freeze/re-review | `test_invalid_transition[partial-stage]` plus the pre-freeze checkpoint |
| Dirty resulting status, additional staging, or raw tracked/ignored path, type, mode or byte drift | Reject equivalence; ordinary review and affected validation remain required, subject to existing scope/repair limits | `test_invalid_transition` dirty, extra-stage, ignored and path cases; `test_raw_content_drift` |
| Acceptance, policy, or base changes | Do not carry approval through equivalence; obtain required owner approval/new freeze | `test_contract_drift` |
| Expired/failed receipts or changed inputs, environment, runtime, command or receipt | Reject reuse; do not copy the old context or extend expiry to force equality; ordinary review with required fresh proof | `test_validation_cannot_transfer` |
| Required HEAD-sensitive check exists | Exclude it from reused contexts, run it freshly against committed identity; no delivery while missing or failed | `test_validation_cannot_transfer[head]` rejects reuse; sections 1, 9 and 10 require separate proof |
| Inventory omits known reviewer, direct followup succeeds | Resume the recorded target before replacements; verify identity/independence and await an actual candidate-bound response | Supplied PR129 recovery sequence; section 3 continuation procedure |
| Inventory omits reviewer, direct followup returns explicit unknown/unavailable | Retain failed-target evidence; assign an independent replacement with complete packet and supported dispositions; obtain its own review | Manual unavailable-result branch against section 3 and current tool schema; not observed as PR129's final outcome |
| Direct followup returns capacity exhaustion | Do not infer absence or spawn backup lanes; wait or safely recover authorized capacity, retry original target only after a capacity change | Supplied PR129 capacity sequence; section 3 capacity branch |
| API unavailable or failure ambiguous | Record recovery gap; no blind retries or claimed continuity; any replacement needs explicit rationale and full independent review basis | Manual missing-API branch against section 3; required review remains a delivery blocker |
| Resumed reviewer has supported prior dispositions and a new candidate digest | Preserve dispositions and review repair/affected invariants; broaden only for a recorded section 3 trigger; bind verdict to new digest | Existing PR124 bounded-review contract plus continuation record |
| Ordinary committed reapproval; source/runtime/environment/command unchanged; authentic passing receipts still fresh and not HEAD-sensitive | Record reuse before dispatch; obtain ordinary committed review without automatically repeating the suite | Section 9 pre-dispatch decision and section 10; manual guidance scenario, not approval transfer |
| Only one required check depends on HEAD | Run that check freshly and bind it to the committed identity; retain other eligible receipts | Per-check decision separates HEAD sensitivity from unrelated test inputs |
| Source or test dependency changes for a subset of checks | Mark affected checks run fresh with the changed dependency; retain only proven unaffected evidence | Section 9 input-binding rule; shared dependencies may invalidate the full suite |
| Receipt authenticity, input binding or freshness cannot be established | Do bounded inspection; otherwise run the affected check or report unavailable required proof as blocked | Unknown is not reuse; a blocked disposition does not satisfy delivery |
| Reviewer changes but validation inputs remain unchanged | Recover reviewer under section 3; assess receipts separately, with no automatic test restart | Reviewer identity is not itself a validation-input change |
| Repository explicitly requires a fresh full suite at the delivery identity | Record the exact requirement and run the full suite even when source bytes match | Mandatory freshness overrides reuse; elapsed time is not a waiver |

These walkthroughs check that the early decision routes to section 10 without
weakening it, and that inventory discovery cannot replace a direct reachability
attempt. They do not certify an actual protected candidate, receipt authenticity,
immutable runtime, or future host behavior. Ordinary review is a fallback process,
not a waiver of missing proof or an automatic new repair attempt.

## Historical PR129 boundary

The task supplied a read-only external evidence root, referred to here as
`EVIDENCE_ROOT`. Inputs inspected were `pr129-review/plan.md`,
`acceptance-008.json`, `acceptance-009.json`,
`pr129-review/push129-review-packet.json`,
`pr129-review/push129-stable-correctness.json`, and
`pr129-review/push129-stable-architecture.json`.

Acceptance 008 and 009 both set `commit_equivalence=false`. The task reports
unstaged content at freeze. The packet records commit
`91f6df823efa11aab4159c8b23c1b00c5fda1977`, equal raw worktree inventory, and ordinary
committed reapproval from candidate
`87d49d2a56cace291ff962a13dcd0d730ed1a2aadedad4cc893689c49a195e7d`
to `6ca0e3368dee995e710d16e1195724cdb80211ec373f7d2b06f3d40531b4d5d9`.
Equal raw content did not make this attempt eligible. The two stable review files
are explicitly leader summaries; they name `/root/correctness_review` and
`/root/repair_architect` and report approval of the latter digest, with committed
validation pending at those observations. They are historical corroboration,
not independently authenticated approvals or current delivery evidence here.
No historical input or PR129 state is changed by this guidance.

## Minimal external inventory bug handoff

Owner: the host's native collaboration inventory/resumption implementation.
Status: user-reported observation, locally corroborated by the stable-review
summaries above; underlying cause unproven. No platform implementation fix or
fresh reproduction is claimed.

Reported sequence to investigate:

1. After continuation, prior reviewer identities remained in supplied context,
   but `collaboration.list_agents` returned only `/root`.
2. The leader inferred absence and created two backup reviewers.
3. Direct `collaboration.followup_task` to `/root/repair_architect` resumed the
   established reviewer despite its inventory omission.
4. Followup to `/root/correctness_review` initially returned capacity exhaustion
   while backups occupied capacity; a later followup succeeded. Stable reviewers
   approved; one backup completed a redundant review and the other was interrupted.

Expected investigation: determine whether inventory intentionally scopes out
resumable identities or loses them across continuation; document that contract
or fix the owning implementation. Capture host/runtime version, parent session,
exact inventory response, target IDs, followup results and capacity transitions
in an owner-controlled reproduction. The supplied artifacts do not establish
that internal cause. Do not rerun against the historical reviewers or alter their
tasks merely to reproduce it.

Repository omission: no reachable recovery procedure or early equivalence
decision in the execution packet. Agent execution mistake: treating inventory
omission as unavailability before trying known IDs. Possible tool defect:
inventory/resumption mismatch, pending the owner's contract and reproduction.
Only the first two are addressed by this repository guidance.
