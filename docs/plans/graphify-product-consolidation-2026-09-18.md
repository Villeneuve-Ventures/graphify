# Graphify capability consolidation into v8

> Publication note (2026-09-19): this is a portable snapshot of the completed
> bounded design task and its macOS context note. Status and inspection claims
> below are historical to that task. Separate S1 implementation now has
> [PR #145](https://github.com/Villeneuve-Ventures/graphify/pull/145); its code,
> validation and later local tracker updates are outside this publication.
> The local tracker is preserved. Session-local input reports are identified
> for provenance only; the design states its proposal and pinned source evidence
> here without requiring access to those local files.

Created 2026-09-18. Revised to reflect the user's clarified priority: resolve v8/workspace-v1 divergence before repository adoption. Status: historical planning snapshot; no implementation or delivery authority.

## Objective

Consolidate the selected capabilities into **one development line based on v8**, preserving useful workspace/v1 behavior and v8's existing improvements. Graphify remains intended for all of the user's repositories and the eventual trading suite. Team Research was a test case, not a product boundary. Aletheia is the first later adoption priority, not a prerequisite for this reconciliation.

The comparison audit (session-local input; not included in this publication) records source snapshots, differences, source commits and candidate checks. Its workspace `01ad5a3c` / v8 `9896fa94` findings are static evidence, not fresh test results. The original global plan (session-local input; not included in this publication) supplies intended capabilities and the one-engine principle; historical phase statuses and engine pins require current reconciliation.

## Four steps

| Step | Work | Done when | Status |
|---|---|---|---|
| 1. Decide capability disposition | Map the meaningful differences and original requirements to existing v8 behavior. Classify each as already covered, integrate into v8, defer, or intentionally omit, with reasons. | Every audited capability is accounted for; unresolved decisions are explicit. No absence of current adoption is used to dismiss intended shared capabilities. | Complete — disposition recommendations recorded; D1–D3 remain explicit |
| 2. Design changes to v8 | For selected capabilities, identify contract differences, prerequisites, affected public behavior and the smallest coherent integration batches. Resolve how the workspace adapter uses the current engine without copying engine logic. | Each batch has a source/destination, integration method, compatibility impact, required checks and delivery endpoint. Independent review challenges material architecture decisions under repo policy. | First bounded structural design complete and critic-reviewed; later capability batches remain |
| 3. Implement selected batches | Apply changes to branches based on v8 in dependency order. Retain current v8 fixes; transfer workspace behavior through reviewed contracts. | Each selected batch meets its required checks and agreed delivery endpoint. Integration evidence covers interactions, not only isolated copied tests. | Not started |
| 4. Close consolidation | Verify the selected capability set together in the resulting v8 candidate; finish the disposition register and remaining-work notes. | Selected capabilities coexist on one v8-based candidate, with evidence and clearly separated local/PR/merged status. Deferrals are recorded, not called delivered. | Not started |

Step 1 is complete as a static disposition review. The first bounded Step 2 design is recorded in the [structural workspace design](graphify-v8-structural-workspace-design.md): adapter/input observation, external lifecycle and certified structural round trip, with ordered implementation slices and proposed acceptance checks. One native critic review required a memory-only optional tokenizer seam; it was incorporated and the same critic returned OKAY on one focused correction review. Later capability batches remain undesigned; repository enrollment, installation changes and implementation remain outside this task.

## Decisions that matter now

- Which workspace capabilities should be integrated now, and which can be deferred without compromising the selected contracts?
- How should workspace lifecycle management and v8's project-output transaction protocol coexist? Shared purpose does not make their authority, persistence or recovery contracts interchangeable.
- How should the workspace adapter move from its historical engine baseline to v8 while retaining v8 extraction, refresh, query and clustering improvements? The audit identifies this coupling at lines 34–43 and 53–59.
- What package, CLI, schema and generated-skill compatibility changes are necessary? Record explicit migration requirements for any changed persistent format; do not infer that a textual merge preserves behavior.

These decisions are about Graphify's shared capabilities. They do not require a new needs assessment or migration plan for each consumer repository.

Preserve support boundaries per capability: a workspace feature with narrower operating-system, filesystem or runtime support must not silently narrow ordinary v8 support. Record those limits and the unavailable-feature behavior in the integration design; consolidation does not require expanding platform support or restoring Windows CI.

## Capability register

Keep T1–T11 stable for handoffs. The comparison audit (session-local input; not included in this publication) retains source commits and suggested checks. Earlier reverse-transfer suggestions are superseded by the one-line direction: preserve those improvements in v8 instead of maintaining duplicate implementations on workspace/v1.

The Step 1 disposition report (session-local input; not included in this publication) refines every T ID with implementation state, source/line evidence, v8 equivalents, prerequisites, risks and preservation checks. Recommendations are not operator-approved omissions or implementation completion. Branch revisions remain W `01ad5a3c` / V `9896fa94`; checkout locations have changed: this checkout is now v8 and the former v8 checkout is detached at V. Workspace evidence was read from pinned Git objects.

| ID | Capability | Current disposition |
|---|---|---|
| T1 | Workspace dependency/security updates | Integrate selected maintenance changes through v8 constraints/lock. Cryptography is a bounded cherry-pick candidate, unproven; other inspected patches need adaptation. No vulnerability or compatibility conclusion from version age alone. |
| T2 | Workspace dependency/artifact checks | Integrate adapted candidate/runtime-authority proof and useful audit coverage. Defer advisory-versus-blocking enforcement changes pending D1; preserve v8 CI coverage. |
| T3 | Workspace lifecycle and governed semantic processing | Integrate registry/identity/activation, generations/leases/journal/recovery, status/doctor, certified query, code-only sync, rollback/repair/offline GC, queue, worker and internal handoff/certification/promotion. Adapt around one v8 engine; preserve narrower workspace support and explicit authority. Full semantic orchestration stays deferred under T11. |
| T4 | Workspace classifier, semantic policy and decision store | Integrate implemented private trust-root/classifier, ACTIVE-policy selection and decision-store/capacity/GC primitives. Defer encompassing content-release/DLP composition and live policy pending prerequisites and D2; no DLP-complete claim. |
| T5 | Workspace parallel test tooling | Defer until measured destination gate runtime justifies adaptation; not a consolidation prerequisite. |
| T6 | v8 interpreter/pointer/hook hardening | Already covered: preserve v8 launch safety and integrate only workspace-specific dispatch/private authority requirements. |
| T7 | v8 CommonJS/SQL extraction fixes | Already covered: preserve v8's complete extraction improvements and verify adapter corpus/language parity. |
| T8 | v8 cache/exclusion correctness | Already covered: preserve v8 fixes; integrate workspace no-write observation constraints through the adapter. |
| T9 | v8 prepared refresh and semantic preservation | Already covered: preserve ordinary v8 transaction/refresh behavior; explicitly reconcile external generation publication and recovery ownership. |
| T10 | v8 query scoring/native Leiden | Already covered: retain scoring parity, native Leiden and optional-dependency boundaries in adapter integration. |
| T11 | Shared, intentionally different and unfinished behavior | Preserve ordinary v8 product/package/platform defaults. Defer full semantic sync/backends, headless OAuth, services/broader query, new migrations/support, broad release work and consumer adoption. D3 retains the old certified-state compatibility decision. Recommend omitting duplicate ports, obsolete package identity, pre-workspace authoritative import and a second engine—not capability or branch deletion. |

The report's T3/T4/T11 subrows account for distinct contracts without adding another tracker. Remaining product decisions are D1 (security enforcement), D2 (governed release/live policy) and D3 (old workspace certified-state support). None reopens v8 as destination or makes consumer adoption a prerequisite. The first design batch now maps the adapter and candidate tuple, lifecycle/queue authority, structural sync/query round trip and recovery/preservation proof. The design proposes new versioned consumed-input evidence and independent publication ownership; it does not establish implementation feasibility or approve D3. Implementation has not begun.

Each selected batch needs one compact record:

`ID(s) | owner/task | source → destination revisions | behavior/prerequisites | method and compatibility impact | validation/evidence | agreed endpoint | actual status`

### First Step 2 batch records

All source reads are W `01ad5a3ca345da879c2f0c35161c4c185eb30d66` → V `9896fa9453e89eb9add8af06a1fca58703782469`. The [bounded design](graphify-v8-structural-workspace-design.md) defines S1–S7 and acceptance A1–A11. The design task owned this snapshot; implementation ownership/status at later revisions is not reported here. Proposed endpoints below require separate execution authority.

| Batch / IDs | Purpose and prerequisite order | Method / compatibility | Evidence and proposed endpoint | Actual status |
|---|---|---|---|---|
| B1 / T3f, T2a; preserve T6–T10 | S1 engine seams first, then S2 new contracts/candidate composition | Adapt around one v8 engine; complete consumed-input evidence, exact new tuple, scoped I/O, memory-only tokenizer; ordinary defaults preserved | A2–A4/A6/A8–A10 and package fixtures proposed; local reviewable diff | Design reviewed; implementation not started |
| B2 / T3a–c, T3f, T3j | S3 registry/lifecycle/queue and storage guard, then S4 adapter/sync/certify/promote | Separate workspace journal/pointers/fences from ordinary v8 receipts; new-format roots only; old managed trees protected against ordinary writes | A1–A11 relevant core/recovery checks proposed; provider-neutral local library round trip | Design reviewed; implementation not started |
| B3 / T3d–f, T2a | S5 public one-shot scenario and diagnostics, then S7 exact candidate/aggregate proof | Lazy workspace dispatch; narrow host support remains local; skills/extras/ordinary behavior retained | Full proposed matrix plus candidate/aggregate gates; local public candidate and evidence | Design reviewed; no tests or implementation; D3 still blocks final release compatibility promise |
| B4 / T3g–i, T11f | S6 maintenance transports after S3 core safety, with S5 dispatch integration | Core recovery is not deferred; public rollback/repair/offline GC can be separate. Optional old read-only access only if D3 selects B | A11 proposed; separate local maintenance diff, no real cleanup/migration | Safety dependencies designed; optional legacy reader remains D3-dependent |

Later T3k–l/T4 and other selected capability batches remain outside this bounded design. D1/D2 stay deferred. First separately authorized implementation task: S1; no task is automatically activated.

Use direct cherry-picks only where the full patch and prerequisites fit the destination. Use adapted ports for coupled behavior. Give each selected row one implementation owner. Do not launch routine backports to workspace/v1 as part of this consolidation.

## Verification and boundaries

- Recheck source/destination revisions, status and ownership before implementation. Reassess only findings affected by drift; preserve concurrent work.
- Use isolated `codex/` branches based on the verified v8 target. Follow destination AGENTS.md, contracts and mandatory gates. Non-trivial protected integration requires the repository's design/runtime stages and independent review; this tracker does not replace them.
- Define focused regressions for both preserved v8 behavior and imported workspace behavior, plus their interaction. Run destination full/surface gates at the required stage. Existing tests inspected by the audit are not passing receipts.
- Check package, CLI and generated skills together in disposable environments. When selected behavior changes formats or installation contracts, prove the corresponding compatibility/recovery path before delivery. Real user installations remain unchanged during this phase.
- Follow applicable graph-refresh policy for code changes. Pre-existing graph failures do not justify an unrelated repair or provider activation.
- This plan does not itself authorize implementation, GitHub delivery, merge, installation changes or cleanup. Record the endpoint granted to each execution task. Planning stops at a reviewable integration plan.

## Deferred scope

Aletheia first-use acceptance, enrollment of other repositories, suite-wide migration, global CLI/skill cutover, and real-installation rollback are later adoption work. Keep the known installed CLI/skill mismatch recorded in the audit; deferral does not mean it is fixed. No branch deletion, archival campaign, broad branch merge, original-roadmap completion campaign or issue-metadata cleanup is included.

## Stop condition

Planning is complete when the capability dispositions and dependency-ordered integration batches are reviewable. Eventual consolidation is complete when all selected capabilities work together on one v8-based candidate with required evidence, and every remaining capability has a reasoned defer/omit/already-covered disposition. A local candidate is not a merged branch; report the actual authorized delivery endpoint. Adoption is a separate milestone and does not keep branch reconciliation open.

Workspace/v1 retirement remains a later decision after its selected work is accounted for; this plan neither deletes nor freezes the branch. Future handoffs cite this file and exact T IDs and update this tracker on completion.

Step 2 review note (2026-09-18): one bounded native critic review of the structural design found an optional Chinese tokenizer disk-cache side effect. The writer added a memory-only engine seam and cold-process parity/no-write acceptance; the same critic returned OKAY on one focused correction review. This review does not cover later semantic/private-release capability designs. No code, installations or tests changed or ran.

Step 2 host context (2026-09-19): the operator reports a macOS update; exact version/build remains unverified. The structural design's A9/S7 native acceptance must record the updated host and recheck Python/toolchain and APFS/durability capabilities before relying on host-specific evidence. Static design review status is unchanged; no runtime validation was performed for this note.
