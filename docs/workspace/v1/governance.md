# Workspace governance

Ledger refresh: `2026-08-04T21:22:06Z`

This document became the canonical live ledger for Graphify-local phases
P1-P5C, H1-H3, their readiness state, and accepted completion receipts only
after the one-time migration publication gate was satisfied. That gate required
one commit reachable from the published
`Villeneuve-Ventures/graphify@workspace/v1` branch to add this file,
[`receipts/README.md`](receipts/README.md), and
[`receipts/p5b2b.md`](receipts/p5b2b.md), and to update
[`README.md`](README.md) to identify this repository authority set. Later
receipts do not expand or reopen that initial gate: each becomes repo-local
accepted evidence only when its separate governance-only commit is published
and merged, and [`receipts/README.md`](receipts/README.md) is the current
accepted-receipt inventory. If the initial gate cannot be verified, authority
fails closed to the external execution checklist and global plan. After
activation, those external plans retain authority only for cross-repository
dependencies and P6-P12 portfolio sequencing.

## Authority precedence

The precedence below applies after the publication gate activates. Before
activation, the external plans retain Graphify-local status, readiness, and
receipt authority under the same execution-authorization limit.

1. A direct operator instruction owns execution authorization.
2. Repository schemas, reference models, and implementation documents own
   product contracts.
3. This document owns current Graphify-local phase and readiness state.
4. Accepted files under [`receipts/`](receipts/) own Graphify completion
   evidence.
5. External portfolio plans own only cross-repository dependencies and P6-P12
   sequencing.
6. Historical snapshots never override a higher-precedence current source.

Uncommitted host-local `.omx/` artifacts, including artifacts hidden by a local
Git exclude such as `.git/info/exclude`, are historical execution evidence, not
current Graphify-local status or execution authority. A newly activated
workflow may use its own bounded plan state, but that state cannot override a
direct operator instruction, this ledger, an accepted receipt, or a frozen
product contract.

`READY` is implementation eligibility only: dependencies and the recorded live
preflight permit a bounded prompt to be reviewed. It does not authorize
implementation. A document edit cannot grant execution authority, and an
implementation change cannot accept its own completion receipt. Conflicts fail
closed and require a fresh governance-only reconciliation from the canonical
branch.

## Current live snapshot

| Surface | State at refresh |
|---|---|
| Canonical base | At pre-edit, the checkout, local `workspace/v1`, local tracking ref, and refreshed live `origin/workspace/v1` all resolved to `fb7b5850ea13c248e50fa17a9b4780599063f5ac`, tree `1dc7e40e3cb6d0d143745bb52bdf50e26629f831`, with local divergence `0/0` and a clean working tree. |
| Worktrees | One worktree existed at preflight, clean on `workspace/v1` at the canonical base above. No delivery worktree, competing governance worktree, or other PR owner was present. The authorized `codex/p5b2-semantic-certification-governance` branch did not yet exist locally or remotely. |
| GitHub | PR [#54](https://github.com/Villeneuve-Ventures/graphify/pull/54) is the latest merged delivery and produced the canonical base above. Repository-qualified live inspection found zero open pull requests; GitHub Issues are disabled. Exact-HEAD CI [30944985816](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30944985816) passed `skillgen-check`, `test (3.14)`, and `security-scan`. GitHub's default branch remains `v8`, while `workspace/v1` remains this ledger's canonical base. |
| P5B2 host-agent semantic-worker contract provenance | PR [#43](https://github.com/Villeneuve-Ventures/graphify/pull/43) exact head `1f202c9134ee0993e4bba40482fa8113f598920a`; merge `5d730fe6e7d781c4d44f87989bf148ab2fdb63e3`; tree `27f7259fc3d716a78a3b28417204b1968c05d421`. Exact-head CI [30681324681](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30681324681) passed `skillgen-check`, `test (3.14)`, and `security-scan`. |
| P5B2 host-agent semantic-worker implementation delivery | PR #45 exact base `99af03803a44d575123a18f1c0eafa48149df492`; head `5f57e565bd188789c984bc1370943caa758148c3`; merge/current commit `36b2e3426ebe3095a0b81c36656789b6790f103f`; delivery/merge/current tree `06d20480337bc94edba4de37c06d2dbf1ab595f2`. Exact-head CI [30730561721](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30730561721) passed `skillgen-check`, `test (3.14)`, and `security-scan`; the separate `CodeRabbit` context succeeded. |
| P5B2 host-agent semantic-worker governance acceptance | PR [#46](https://github.com/Villeneuve-Ventures/graphify/pull/46) exact head `a0c3763acd20cb9886a4e26cc3c2e776597fe162`; merge `c2bb53d733d43784b76ab3cf559c48c16688f298`; tree `98b0ed85599794a152c1fd8ddde6ae3ebacb98aa`. Exact-head CI [30734181344](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30734181344) passed `skillgen-check`, `test (3.14)`, and `security-scan`; the separate `CodeRabbit` context succeeded. The acceptance is limited to the worker transport and promotes no successor. |
| JOS test-harness determinism delivery | PR #47 exact base `c2bb53d733d43784b76ab3cf559c48c16688f298`; head `e17482c61a5cfad2d227a4b0d8d27c2bcd723c32`; merge/current commit `d19ff5467a48778b14a4cdb62eada4ba3fa48293`; delivery/merge/current tree `8b2fc5a29c06eb7df2a41cd79c896e052636a19e`. Exact-head CI [30771565129](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30771565129) passed `skillgen-check`, `test (3.14)`, and `security-scan`; the separate `CodeRabbit` context succeeded. |
| JOS test-harness governance acceptance | PR [#48](https://github.com/Villeneuve-Ventures/graphify/pull/48) exact head `a099ce64ac533ae61b14275f67c07eabd126c9a3`; merge/current commit `e9967f18de55aba2a51803cb51d225a221d42fdc`; head/merge/current tree `13117628e5b22cce5d95d26dfd5456a2d9136d58`. Exact-head CI [30780293723](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30780293723) passed `skillgen-check`, `test (3.14)`, and `security-scan`; the separate `CodeRabbit` context succeeded. Both test-harness JOS rows are `CLOSED` historical evidence only. |
| Semantic-result handoff contract delivery | PR [#49](https://github.com/Villeneuve-Ventures/graphify/pull/49) exact base `e9967f18de55aba2a51803cb51d225a221d42fdc`; head `f46de7408df3b70e57a8bb17047449caff658326`; merge/current commit `92b81db6d39e42c4b4a52aa69f1113398f9115ad`; head/merge/current tree `7e77046c8dce66ad6a21e423cd3ca153385a8d74`. The merge commit's ordered parents are the exact base and head. Exact-head CI [30789398224](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30789398224) passed `skillgen-check`, `test (3.14)`, and `security-scan`; the separate `CodeRabbit` context succeeded. The hosted [Codex review](https://github.com/Villeneuve-Ventures/graphify/pull/49#issuecomment-5163081658) reported no major issue against reviewed commit `f46de7408d`, the exact head's unique prefix. |
| Semantic-result handoff implementation delivery | PR [#51](https://github.com/Villeneuve-Ventures/graphify/pull/51) exact base `1d092a86fce5ba2eec5723908ec442d8ecdd639e`; head `272e56248c56ea6bc699e035b69f732c20e94d1e`; merge/current commit `2d9efe7e79b16953e62523684fbf8c6bf8b7a20a`; delivery/merge/current tree `dee2624fb3729b3e9b30a855f2c3635e672dd797`. Exact-head CI [30882417900](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30882417900) passed `skillgen-check`, `test (3.14)`, and `security-scan`; the separate `CodeRabbit` context succeeded. The delivery changed exactly seven implementation/test files. |
| Semantic-result handoff governance acceptance | PR [#53](https://github.com/Villeneuve-Ventures/graphify/pull/53) exact base `2d9efe7e79b16953e62523684fbf8c6bf8b7a20a`; head `309c3d96eb633211103e2546a5b5f6fdb7dcafd7`; merge/current canonical commit `9c98d77830238a0de299977e5230690f7bb504b1`; head/merge/current tree `598081f934c838dec5c3abf41c23380dd5660e22`. Exact-head CI [30887151160](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30887151160) passed `skillgen-check`, `test (3.14)`, and `security-scan`; the separate PR-Agent review and `CodeRabbit` context succeeded. The accepted closeout remains limited to the semantic-result handoff and promoted no successor. |
| Semantic-generation certification-finalization contract delivery | PR [#54](https://github.com/Villeneuve-Ventures/graphify/pull/54) exact base `9c98d77830238a0de299977e5230690f7bb504b1`; reviewed head `07b521c4386c0c97134f89de5f989be4a70455d2`; merge/current canonical commit `fb7b5850ea13c248e50fa17a9b4780599063f5ac`; head/merge/current tree `1dc7e40e3cb6d0d143745bb52bdf50e26629f831`; merged at `2026-08-04T19:48:46Z`. The merge commit's ordered parents are the exact base and reviewed head. Exact-head CI [30922838023](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30922838023) passed `skillgen-check`, `test (3.14)`, and `security-scan`; PR-Agent [30922842255](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30922842255) passed `review`; the separate `CodeRabbit` context succeeded. Independent exact-head specification and architecture/state-consistency reviews both reported `CLEAN`. GitHub's review decision remained unset, with no submitted reviews, inline review comments, or review threads. The delivery froze the lifecycle contract only and granted no implementation, acceptance, completion, or successor-promotion authority. |
| Semantic-generation certification-finalization governance preflight | At `2026-08-04T21:22:06Z`, the canonical repository, `workspace/v1` branch, HEAD/tree, clean `0/0` divergence, one-worktree inventory, empty open-PR inventory, disabled Issues, PR #54 ownership/merge state, hosted checks, review disposition, and absence of competing governance work were revalidated with repository-qualified live GitHub calls. The generated graph report remained stale orientation from `2d9efe7e79b16953e62523684fbf8c6bf8b7a20a` and was not rebuilt. This local reconciliation proposes only the `READY` transition recorded below; no code, receipt, JOS, review-thread, merge, fast-forward, or cleanup mutation occurred. |
| Support baseline | Observed host CPython `3.14.6`; project CPython `3.14.3`; uv `0.11.30` |

Every later status transition must refresh this snapshot. A stale snapshot is
orientation only and cannot justify execution.

## Current Graphify ledger

| ID or surface | Depends on | Status | Current boundary/evidence |
|---|---|---|---|
| P1 | R0 | COMPLETE | Fork bootstrap and contract freeze closed. |
| P2 | P1 | COMPLETE | Registry, UUID identity, active source, and fenced leases closed. |
| P3 | P2 | COMPLETE | Immutable generations, journal, pointers, and recovery closed. |
| P4 | P3 | COMPLETE | Engine adapter, update compatibility, and read-only freshness closed. |
| P4F | P4 | COMPLETE | Post-merge comparison-authority repair closed. |
| H1 | P4F | COMPLETE | Inherited labeling-order test stabilization closed. |
| H2 | H1 | COMPLETE | Candidate packaging, dependency, and blocking security hygiene closed. |
| H3 | H2 | DEFERRED | Full-repository Pyright and medium-severity Bandit debt remains non-blocking. |
| P5 | P4, H1, H2 | IN_PROGRESS | P5A and delivered P5B children, including the accepted host-agent semantic-worker transport and semantic-result handoff, are complete. The semantic-generation certification finalization child is the sole `READY` child recorded by this local reconciliation; `READY` is implementation eligibility only. Its implementation, remaining P5B2, and the broad P5C gate are not complete. |
| P5A | P4, H1, H2 | COMPLETE | Durable semantic queue and stable certification watermark closed. |
| P5B1 | P5A | COMPLETE | Production composition, versioned read-only status, and read-only doctor closed. |
| P5B2 | P5B1 | IN_PROGRESS | Delivered children, including the accepted host-agent semantic-worker transport and semantic-result handoff, are complete. The certification-finalization contract is the sole `READY` child recorded by this local reconciliation and remains unimplemented. Full semantic sync, explicit backend integration, migrate, broader repair, broader mutation/query authority, and all other undelivered commands remain waiting. |
| P5B2a | P5B1 | COMPLETE | Initial operator-authorized enrollment and explicit verified adoption remain closed. Accepted corrective receipt: [`P5B2a ADOPT pre-write correction`](receipts/p5b2a-adopt-prewrite-correction.md). |
| P5B2 identity maintenance | P5B2a | COMPLETE | Accepted receipt: [`P5B2 identity maintenance`](receipts/p5b2-identity-maintenance.md). Rebind and rotation only. |
| P5B2 active-source activation | P5B2a | COMPLETE | Accepted receipt: [`P5B2 active-source activation`](receipts/p5b2-active-source-activation.md). Standalone fenced `workspace activate` only. |
| P5B2 exact-last-good rollback | P5B2 | COMPLETE | Accepted receipt: [`P5B2 exact-last-good rollback`](receipts/p5b2-exact-last-good-rollback.md). One-step fenced `workspace rollback --request-stdin` to the visible pointer's exact `last_good` only. |
| P5B2 retained-source identity continuity | P5B2 identity maintenance, P5B2 active-source activation | COMPLETE | Accepted receipt: [`P5B2 retained-source identity continuity`](receipts/p5b2-retained-source-identity-continuity.md). `rotate_enrollment_evidence()` and `resolve_active_source()` independently require a shared immutable enrollment history root or the enrolled Git common-directory identity. Rejected rotation occurs before the requested source evidence, identity-action evidence, or registry revision is persisted. |
| P5B2 bounded offline-GC preview | P5B2 | COMPLETE | Accepted receipt: [`P5B2 bounded offline-GC preview`](receipts/p5b2-offline-gc-preview.md). Exact public `workspace gc --dry-run --request-stdin` read-only, unfenced preview only; mutation remains outside this preview receipt. |
| P5B2 public fenced offline-GC lifecycle | P5B2 bounded offline-GC preview | COMPLETE | Accepted receipt: [`P5B2 public fenced offline-GC lifecycle`](receipts/p5b2-offline-gc-lifecycle.md). Exact explicit `workspace gc --execute`, `--reconcile`, and `--purge` forms with `--request-stdin` only; automatic, online, service, repair, migrate, and semantic-sync authority remain excluded. |
| P5B2 public fenced pointer-repair lifecycle | P5B2 | COMPLETE | Accepted receipt: [`P5B2 public fenced pointer-repair lifecycle`](receipts/p5b2-pointer-repair.md). Exact `workspace repair --dry-run --request-stdin` existing-state-only preview and `workspace repair --execute --request-stdin` fenced execution only; broader repair, semantic sync, migrate, GC reconciliation, and every other mutation/query authority remain excluded. |
| P5B2b0 | P5B2a | COMPLETE | Request-bound staged-build recovery prerequisite closed. |
| P5B2b | P5B2b0 | COMPLETE | Accepted receipt: [`P5B2b`](receipts/p5b2b.md). |
| P5B2c | P5C1 | COMPLETE | Accepted receipt: [`P5B2c`](receipts/p5b2c.md). One-shot certified `workspace query --request-stdin` transport only. |
| P5B2 host-agent semantic-worker transport | P5A, P5C1 | COMPLETE | Accepted exact `workspace semantic-worker --stdio` host-agent lifecycle in [`semantic-sync.md`](semantic-sync.md). P5A directly supplies queue semantics; P5C1 supplies installed runtime authority and transitively includes P5B1. Completion evidence: [`P5B2 host-agent semantic worker`](receipts/p5b2-semantic-worker.md). |
| P5B2 semantic-result handoff and sealed-input finalization | P5A, P5B2b0, P5B2 host-agent semantic-worker transport | COMPLETE | Accepted internal handoff in [`semantic-sync.md`](semantic-sync.md#p5b2-semantic-result-handoff-and-sealed-input-finalization). It preserves exact accepted worker evidence, distinguishes the optional carried-source generation from the new target generation, materializes one target-generation-owned semantic-input record, completes the staged payload manifest, and binds it through `bind_sealed_inputs()`. Completion evidence: [`P5B2 semantic-result handoff`](receipts/p5b2-semantic-result-handoff.md), made canonical by PR #53. It grants no public command, parent-phase completion, or successor activation. |
| P5B2 semantic-generation certification finalization | P5B2 semantic-result handoff and sealed-input finalization | READY | Contract freeze only in [`semantic-sync.md`](semantic-sync.md#p5b2-semantic-generation-certification-finalization). Entry requires the accepted handoff's exact reopened staged `COMPLETE` manifest and equal queue sealed-input digest; the only mutating lane is same-request `BUILD` recovery through the existing semantic certification view, immutable binding, generation receipt/journal, reservation, and staged-state authorities until exact `CERTIFIED` proof and lease release. PR #54's exact merge/current and exact-head check/review evidence satisfy the dependency and preflight gate. `READY` is implementation eligibility only; this local transition remains proposed until publication and merge and does not perform or authorize implementation, receipt acceptance, completion, phase promotion, or later successor activation. |
| Remaining P5B2 commands | P5B2 | WAITING | Full semantic sync, named/headless backend integration, migrate, every repair mode beyond the accepted public fenced pointer-repair lifecycle, every mutation beyond the accepted explicit GC and pointer-repair lifecycles, every query authority beyond P5B2c's one-shot transport, and every other command remain waiting. The accepted internal handoff grants no broader or public command authority. |
| P5C | P5B2 | WAITING | The broad service, installation, performance/resource, and publication parent is unchanged and is not promoted by the child split below. |
| P5C1 | P5B2b | COMPLETE | Accepted receipt: [`P5C1`](receipts/p5c1.md). Candidate-bound canonical runtime authority generation and isolated atomic installation/compensation proof only. |
| Remaining P5C concerns | P5C | WAITING | Watch/service, performance, shared-lock/root-traversal optimization, publication, retained query/service authority, and all other P5C work remain unchanged. |

P6-P12 are intentionally absent from this Graphify-local ledger. Their
cross-repository ordering remains in the external portfolio plan, and all
remain waiting at this handoff.

Statements in accepted boundary freezes below that a receipt promoted no later
child describe that receipt's authority at its acceptance point. They do not
override the current ledger. PR #54's exact merge and the refreshed live
preflight support only the certification-finalization row's proposed `READY`
transition. `READY` is implementation eligibility only, not execution,
implementation, receipt acceptance, completion, phase promotion, or later
successor activation. Until this commit is published and merged, the published
canonical branch remains authoritative.

## Justified out-of-scope follow-up register

This register indexes only findings that were independently supported and
justified, but outside the delivery that surfaced them. It does not own phase
state, change an accepted receipt, authorize implementation, or turn a review
thread's unresolved UI state into technical debt.

The tracking states below are local to this register and are not Graphify phase
statuses. `JOS` means `justified out-of-scope`:

- `DOCUMENTED_NONCLAIM` points to an already-frozen boundary and creates no new
  work;
- `TRIGGER_GATED` remains inactive until its exact compatibility trigger occurs;
- `OPPORTUNISTIC` permits a behavior-preserving cleanup only when adjacent
  authorized work already touches the named surface;
- `GUARDED` records that an executable guard exists and must be revalidated when
  its dependency changes;
- `SEPARATE_AUTHORIZATION_REQUIRED` identifies a bounded successor candidate but
  grants no implementation or acceptance authority; and
- `CLOSED` records a historical follow-up whose stated closure evidence was
  delivered and independently accepted. It grants no broader phase,
  implementation, execution, or successor authority.

| ID | Source | Tracking state | Justification and scope boundary | Activation trigger and closure evidence |
|---|---|---|---|---|
| `JOS-GC-CAPACITY-V1` | PR [#35](https://github.com/Villeneuve-Ventures/graphify/pull/35) and governance PR [#36](https://github.com/Villeneuve-Ventures/graphify/pull/36); [exact-head evidence](#jos-gc-capacity-v1-evidence) | `DOCUMENTED_NONCLAIM` | The published capacity-policy representation is compatibility-sensitive, while the accepted preview makes no performance/resource or bounded pre-enumeration claim. Those are legitimate future boundaries, but the delivery and receipt were limited to the read-only preview. | The boundary remains owned by the [workspace ownership map](README.md#governance-and-deferred-work-ownership). Closure is not applicable while this entry remains `DOCUMENTED_NONCLAIM`; the [accepted preview receipt](receipts/p5b2-offline-gc-preview.md) is its permanent boundary evidence. Activate only through a separately authorized versioned compatibility change or qualification batch, which must define its own closure receipt or qualification test before changing the boundary. |
| `JOS-GC-PREVIEW-VERSION` | PR [#37](https://github.com/Villeneuve-Ventures/graphify/pull/37); [exact-head evidence](#jos-gc-preview-version-evidence) | `TRIGGER_GATED` | [`gc_preview_result_bytes()`](../../../graphify/workspace/gc_command.py) selects `GC_LIFECYCLE_SCHEMA_VERSION`, so a future lifecycle-v2 change could drift frozen preview-v1 bytes. Current v1 behavior is not defective, and the lifecycle delivery did not authorize a speculative version refactor. | Activate before changing either lifecycle or preview schema versioning. Closure must independently freeze preview-version selection and prove preview-v1 canonical bytes remain unchanged across a lifecycle-version change. |
| `JOS-BOUNDED-INPUT-READERS` | PR [#39](https://github.com/Villeneuve-Ventures/graphify/pull/39); [exact-head evidence](#jos-bounded-input-readers-evidence) | `OPPORTUNISTIC` | Repeated bounded-input readers across established commands are justified maintainability debt, but consolidating them was a cross-command refactor outside the pointer-repair PR's surgical behavior fixes. | Activate only when an authorized batch adds another `--request-stdin` command or changes more than one established reader. Closure requires regression-locked parser behavior and must preserve each command's bounds, canonicality, error mapping, authority-loading order, and deadline semantics. |
| `JOS-PR-AGENT-DRAIN` | PR [#40](https://github.com/Villeneuve-Ventures/graphify/pull/40); [exact-head evidence](#jos-pr-agent-drain-evidence) | `GUARDED` | The pinned PR-Agent revision relies on a private drain-aware runner, which creates a real pin-upgrade compatibility risk. The current exact pin imports and executes it successfully, so this is not a defect in the delivered workflow. | The [policy regression](../../../tests/test_pr_agent_policy.py) requires the exact import and call. Reactivate on any PR-Agent pin or runner-import change. Closure requires an exact-import smoke test and proof that review/description completion remains drain-aware with no silent legacy fallback. |
| `JOS-SEMANTIC-WORKER-CONFORMANCE` | Contract PR [#43](https://github.com/Villeneuve-Ventures/graphify/pull/43), delivery PR [#45](https://github.com/Villeneuve-Ventures/graphify/pull/45), and [`P5B2 semantic-worker receipt`](receipts/p5b2-semantic-worker.md); [exact-head evidence](#jos-semantic-worker-conformance-evidence) | `CLOSED` | PR #43 froze the prose contract and explicitly deferred machine-readable schemas, closed runtime validation, conformance tests, and implementation. PR #45 delivered those exact conformance surfaces, and the separate receipt independently accepts only the frozen transport. | Historical closure only: PR #45 plus the accepted receipt satisfy the stated schema/validator, conformance-suite, exact-decimal, checkpoint-capacity, uncertainty-recovery, and deadline-aware delivery evidence. Full semantic sync and every explicit backend remain excluded. `CLOSED` grants no broader phase, implementation, execution, or successor authority. |
| `JOS-BACKEND-DETECTION-TEST-ISOLATION` | Original source PR [#45](https://github.com/Villeneuve-Ventures/graphify/pull/45), closure delivery PR [#47](https://github.com/Villeneuve-Ventures/graphify/pull/47), and [`JOS test-harness determinism receipt`](receipts/jos-test-harness-determinism.md); [exact-head evidence](#jos-backend-detection-test-isolation-evidence) | `CLOSED` | PR #45 preserved the reproduced ambient-provider defect as separately authorized test-fixture isolation debt. PR #47 delivered provider-neutral fixtures that clear the complete dynamic API-key selector set plus the direct Azure, Bedrock/AWS, and Ollama selectors without changing production backend behavior. | Historical closure only: PR #47 plus the accepted receipt bind the hostile ambient-selector regression, full affected backend file, exact delivery evidence, hosted checks, and review disposition. `CLOSED` grants no product, provider, semantic-sync, phase, execution, or successor authority. |
| `JOS-GIT-SEED-HISTORY-STABILITY` | Original source PR [#45](https://github.com/Villeneuve-Ventures/graphify/pull/45), closure delivery PR [#47](https://github.com/Villeneuve-Ventures/graphify/pull/47), and [`JOS test-harness determinism receipt`](receipts/jos-test-harness-determinism.md); [exact-head evidence](#jos-git-seed-history-stability-evidence) | `CLOSED` | PR #45 preserved the reproduced one-second seed-commit identity drift as separately authorized test-fixture maintenance. PR #47 fixed only the synthetic seed commit's author, committer, timestamps, signing, and hook inputs, then proved stable commit identity across hostile inherited Git environments. | Historical closure only: PR #47 plus the accepted receipt bind deterministic seed identity and the exact persistent-source-replacement security-meaning regression. `CLOSED` grants no product, source-identity-policy, phase, execution, or successor authority. |
| `JOS-SEMANTIC-RATIONALE-PROJECTION` | Delivery PR [#45](https://github.com/Villeneuve-Ventures/graphify/pull/45); [exact-head evidence](#jos-semantic-rationale-projection-evidence) | `OPPORTUNISTIC` | Bounded rationale projection/classification is duplicated across semantic cleanup and worker validation. Consolidation is maintainability work, not a current conformance mismatch, and would broaden this acceptance. | Owner: semantic sanitizer/projection maintenance. The entry remains inactive until a separately authorized sanitizer, projection, or full semantic-sync batch already touches that behavior. Closure requires behavior-locked projection bounds, classification, and sanitizer output parity. This row grants no standalone implementation authority. |
| `JOS-TOP-LEVEL-COMMAND-INVENTORY` | Delivery PR [#45](https://github.com/Villeneuve-Ventures/graphify/pull/45); [exact-head evidence](#jos-top-level-command-inventory-evidence) | `TRIGGER_GATED` | A derived consistency harness for the repository-wide top-level command inventory is justified maintainability work, but it spans unrelated commands and no current classifier mismatch was reproduced. | Owner: top-level CLI dispatcher/classifier maintenance. Activate only when the top-level dispatcher or semantic-worker classifier changes; adding or changing a nested workspace subcommand alone is not a trigger. Closure requires a derived canonical inventory plus targeted parity coverage for top-level dispatch and semantic-worker classification. |

### Exact-head source evidence

These original source records were refreshed at `2026-08-02T04:55:01Z` for
canonical repository `Villeneuve-Ventures/graphify`. The two test-harness
closure additions were independently refreshed at `2026-08-03T02:37:33Z`;
unrelated source records retain their prior refresh. Each exact-SHA compare
link is the changed-file manifest for its recorded base and head.
`not-applicable` is explicit when a finding came from a PR description or a
top-level comment instead of a review thread; thread identity, location, or
workflow state is never inferred.

#### JOS-GC-CAPACITY-V1 evidence

- Source record: `PR_DESCRIPTION_NONCLAIM`; PR #35's “Non-blocking architecture
  watch” and PR #36's “Residual nonclaims” preserve the exact claim that the
  capacity-policy representation is compatibility-sensitive and the accepted
  preview makes no performance/resource or bounded pre-enumeration claim.
- Immutable source node IDs: PR #35 `PR_kwDOTZvP8s73RkdK`; PR #36
  `PR_kwDOTZvP8s73isnH`.
- PR #35 revision: base `129e4d561a10061f2629780b5f5c221c0f19449b`, head
  `b32503e0aabf802970d9d7032a07e0a322f41c28`, tree
  `1104ac8a74b4abd1bf2e46cb1439cc3d29d6639a`, [changed-file manifest](https://github.com/Villeneuve-Ventures/graphify/compare/129e4d561a10061f2629780b5f5c221c0f19449b...b32503e0aabf802970d9d7032a07e0a322f41c28).
- PR #36 confirmation: base `864a3e77a66f83a45e3ee9395180dc511b4bf059`,
  head `e95960b9f1d852c45405a96ffee39eb4e8811d94`, tree
  `cda837c38f16fa6a17599cdc41efbfe99f9ba5ab`, [changed-file manifest](https://github.com/Villeneuve-Ventures/graphify/compare/864a3e77a66f83a45e3ee9395180dc511b4bf059...e95960b9f1d852c45405a96ffee39eb4e8811d94).
- Thread identity, path/line, `isResolved`, and `isOutdated`:
  `not-applicable`.

#### JOS-GC-PREVIEW-VERSION evidence

- Source record: `TOP_LEVEL_COMMENT`; Qodo's [PR #37 assessment](https://github.com/Villeneuve-Ventures/graphify/pull/37#issuecomment-5108919612),
  node `IC_kwDOTZvP8s8AAAABMIPtPA`, states that a future lifecycle
  version should select the frozen preview version independently.
- Exact revision: base `1af466d58e91541fc95b3af66c3c18a2ce0b70a6`, head
  `b2454ea78ce80b0e3aa25c7c73d2a073da4ca38a`, tree
  `02bb3582b055bec478d3f1caea31baf797417889`, [changed-file manifest](https://github.com/Villeneuve-Ventures/graphify/compare/1af466d58e91541fc95b3af66c3c18a2ce0b70a6...b2454ea78ce80b0e3aa25c7c73d2a073da4ca38a).
- Thread identity, path/line, `isResolved`, and `isOutdated`:
  `not-applicable`; the source is a top-level comment created and last updated
  `2026-07-28T19:46:32Z`.

#### JOS-BOUNDED-INPUT-READERS evidence

- Source record: `PR_DESCRIPTION_DEFERRED`; PR #39's “Whole-PR AI slop cleanup”
  preserves the exact claim that repeated bounded-input readers were deferred
  as a cross-command refactor outside the pointer-repair delivery.
- Immutable source node ID: `PR_kwDOTZvP8s730dHx`.
- Exact revision: base `73dea771e50a1b066cbd971f85b0a5a196d34804`, head
  `8dc93e4b5f554e05cb0d7dd4f533e8618cdcad0b`, tree
  `5ceef4cf831093b0562413971ec2208c036c0920`, [changed-file manifest](https://github.com/Villeneuve-Ventures/graphify/compare/73dea771e50a1b066cbd971f85b0a5a196d34804...8dc93e4b5f554e05cb0d7dd4f533e8618cdcad0b).
- Thread identity, path/line, `isResolved`, and `isOutdated`:
  `not-applicable`.

#### JOS-PR-AGENT-DRAIN evidence

- Source record: `REVIEW_THREAD`; thread `PRRT_kwDOTZvP8s6VMBab`, Qodo
  [comment `PRRC_kwDOTZvP8s7bopkX`](https://github.com/Villeneuve-Ventures/graphify/pull/40#discussion_r3684866327).
- Exact revision: base `e7953df65a2bb0996f5422f9c9ca343cf1ee3828`, reviewed
  head `1e47b513ae23c3e197d10cb33955201385a3a8b1`, tree
  `1bc9a98b64911de421357c499efe82d8ca6e1550`, [changed-file manifest](https://github.com/Villeneuve-Ventures/graphify/compare/e7953df65a2bb0996f5422f9c9ca343cf1ee3828...1e47b513ae23c3e197d10cb33955201385a3a8b1).
- Anchor/state at the evidence refresh: current and original path
  `.github/workflows/pr-agent.yml`, current and original line `357`, original
  comment commit `1bc1d8a324c105458738247390d4fb3b094364e5`,
  `isResolved=false`, `isOutdated=false`.
- Disposition: `deferred: JOS-PR-AGENT-DRAIN`.

#### JOS-SEMANTIC-WORKER-CONFORMANCE evidence

- Original source record: `PR_DESCRIPTION_DEFERRED` plus `TOP_LEVEL_COMMENT`;
  PR #43's “Deferred justified follow-up” and Qodo's [assessment](https://github.com/Villeneuve-Ventures/graphify/pull/43#issuecomment-5139204878),
  node `IC_kwDOTZvP8s8AAAABMlILDg`, preserve the machine-readable
  schema/closed-validator and conformance-test successor boundary.
- Immutable PR #43 description source node ID: `PR_kwDOTZvP8s742UQL`.
- Contract revision: base `d70219f07b37f96b2406c9f97c7a40e5c2592486`, head
  `1f202c9134ee0993e4bba40482fa8113f598920a`, tree
  `27f7259fc3d716a78a3b28417204b1968c05d421`, [changed-file manifest](https://github.com/Villeneuve-Ventures/graphify/compare/d70219f07b37f96b2406c9f97c7a40e5c2592486...1f202c9134ee0993e4bba40482fa8113f598920a).
- Closure delivery: PR [#45](https://github.com/Villeneuve-Ventures/graphify/pull/45),
  immutable node `PR_kwDOTZvP8s75bnmy`; base
  `99af03803a44d575123a18f1c0eafa48149df492`, head
  `5f57e565bd188789c984bc1370943caa758148c3`, delivery tree
  `06d20480337bc94edba4de37c06d2dbf1ab595f2`, [changed-file manifest](https://github.com/Villeneuve-Ventures/graphify/compare/99af03803a44d575123a18f1c0eafa48149df492...5f57e565bd188789c984bc1370943caa758148c3).
- Closure acceptance: the
  [`P5B2 semantic-worker receipt`](receipts/p5b2-semantic-worker.md) binds the
  merged delivery, exact-head hosted validation, focused conformance evidence,
  review disposition, and exclusions. Together with PR #45, it satisfies the
  original PR #43 closure contract and transitions this entry to `CLOSED`.
- Thread identity, path/line, `isResolved`, and `isOutdated`:
  `not-applicable`; the top-level source comment was created and last updated
  `2026-07-31T04:16:54Z`.

#### JOS-BACKEND-DETECTION-TEST-ISOLATION evidence

- Source record: `PR_DESCRIPTION_DEFERRED`; PR #45's “Justified deferrals”
  preserves the exact claim that three backend-detection tests inherit ambient
  provider selectors instead of clearing the complete priority set.
- Immutable source node ID: `PR_kwDOTZvP8s75bnmy`; exact body SHA-256 at
  refresh: `8b3ab5a6a3a28c05fa1c142da5c2c5c5767ec453efb73fc2e7fdc9c69d8fb50f`.
- Exact revision: base `99af03803a44d575123a18f1c0eafa48149df492`, head
  `5f57e565bd188789c984bc1370943caa758148c3`, tree
  `06d20480337bc94edba4de37c06d2dbf1ab595f2`, [changed-file manifest](https://github.com/Villeneuve-Ventures/graphify/compare/99af03803a44d575123a18f1c0eafa48149df492...5f57e565bd188789c984bc1370943caa758148c3).
- Reproduction: with `GEMINI_API_KEY=governance-evidence`, the focused
  backend-detection selection reported `3 failed, 1 passed, 9 deselected, 1
  warning`; the Ollama, Kimi-over-Ollama, and no-provider cases each observed
  `gemini` from the ambient selector.
- Thread identity, path/line, `isResolved`, and `isOutdated`:
  `not-applicable`.
- Closure delivery: PR [#47](https://github.com/Villeneuve-Ventures/graphify/pull/47),
  immutable node `PR_kwDOTZvP8s75vPSD`; base
  `c2bb53d733d43784b76ab3cf559c48c16688f298`, head
  `e17482c61a5cfad2d227a4b0d8d27c2bcd723c32`, merge
  `d19ff5467a48778b14a4cdb62eada4ba3fa48293`, and delivery/merge tree
  `8b2fc5a29c06eb7df2a41cd79c896e052636a19e`, with the exact
  [three-file manifest](https://github.com/Villeneuve-Ventures/graphify/compare/c2bb53d733d43784b76ab3cf559c48c16688f298...e17482c61a5cfad2d227a4b0d8d27c2bcd723c32).
- Closure acceptance: the
  [`JOS test-harness determinism receipt`](receipts/jos-test-harness-determinism.md)
  binds the complete selector-clearing fixture, hostile ambient-provider
  regression, full affected backend file, exact-head hosted validation, and
  review/thread disposition. Together with PR #47, it satisfies the original
  closure contract and changes this row to `CLOSED` without broader authority.

#### JOS-GIT-SEED-HISTORY-STABILITY evidence

- Source record: `PR_DESCRIPTION_DEFERRED`; PR #45's “Justified deferrals”
  preserves the exact claim that shared seed commits can diverge across a
  one-second timestamp boundary.
- Immutable source node ID: `PR_kwDOTZvP8s75bnmy`; exact revision and body
  digest are the PR #45 values recorded immediately above.
- Reproduction: a disposable bare-repository probe held tree, author,
  committer, and message constant while shifting author/committer time by one
  second. The commit IDs differed:
  `6e51d74b1e04ae12a0e8f0d24cd3f96edaa5dac7` versus
  `9730502641837cb4f8ac399b4d772156dc4b61d2`.
- Thread identity, path/line, `isResolved`, and `isOutdated`:
  `not-applicable`.
- Closure delivery: PR [#47](https://github.com/Villeneuve-Ventures/graphify/pull/47),
  immutable node `PR_kwDOTZvP8s75vPSD`, at the same exact base, head, merge,
  tree, and three-file manifest recorded immediately above.
- Closure acceptance: the
  [`JOS test-harness determinism receipt`](receipts/jos-test-harness-determinism.md)
  binds the seed-only fixed identity/timestamp/signing/hook inputs, deterministic
  commit-identity regression across hostile inherited Git environments, exact
  persistent-source-replacement regression, exact-head hosted validation, and
  review/thread disposition. Together with PR #47, it satisfies the original
  closure contract and changes this row to `CLOSED` without broader authority.

#### JOS-SEMANTIC-RATIONALE-PROJECTION evidence

- Source record: `PR_DESCRIPTION_DEFERRED`; PR #45's “Justified deferrals”
  preserves the exact claim that duplicated bounded rationale
  projection/classification is a nonblocking cross-module cleanup packet whose
  refactor would broaden the conformance batch.
- Immutable source node ID: `PR_kwDOTZvP8s75bnmy`; exact revision and body
  digest are the PR #45 values recorded above.
- Activation is inactive until separately authorized sanitizer, projection, or
  full semantic-sync work already changes this behavior. The accepted worker
  alone supplies no standalone refactor authority.
- Thread identity, path/line, `isResolved`, and `isOutdated`:
  `not-applicable`.

#### JOS-TOP-LEVEL-COMMAND-INVENTORY evidence

- Source record: `PR_DESCRIPTION_DEFERRED`; PR #45's “Justified deferrals”
  preserves the exact claim that a derived repository-wide top-level command
  inventory is reasonable maintainability work but no current conformance
  mismatch exists.
- Immutable source node ID: `PR_kwDOTZvP8s75bnmy`; exact revision and body
  digest are the PR #45 values recorded above.
- Activation requires a top-level dispatcher or semantic-worker classifier
  change. A nested workspace subcommand addition or change alone is expressly
  not a trigger.
- Thread identity, path/line, `isResolved`, and `isOutdated`:
  `not-applicable`.

PR #37's separate note about a `.codex` parent path and 100 ms timing cases is
not registered as justified debt because it does not identify a reproducible,
independently actionable packet. It may enter only after exact tests, commands,
environment, and failure evidence are reproduced; path portability and timing
stability must receive separate IDs if both survive that gate. Generic
docstring coverage, disproven workflow-syntax warnings, fixed findings, and
addressed workflow residue are likewise absent by design.

## P5C1 boundary freeze

P5C1 is limited to all of the following as one reviewable proof boundary:

- generate canonical `runtime-manifest.json` from the existing compatibility
  manifest plus an explicit `SemanticQueuePolicy`;
- bind the manifest's exact bytes and SHA-256 to the immutable candidate;
- install it atomically only in isolated external-state fixtures;
- prove compensation after deterministic installation failures; and
- preserve P5B1's read-only loader unchanged.

P5C1 excludes real `HOME`, XDG, and `CODEX_HOME` state; watch/service work;
performance or resource qualification; shared-lock or root-traversal
optimization; publication; retained production query/service authority; H3;
P6+; and every remaining P5B2 command. The bounded candidate/proof
implementation is complete under the accepted
[`P5C1` receipt](receipts/p5c1.md); none of those exclusions changed, and no
later child was promoted by that receipt.

## P5B2c boundary freeze

P5B2c is limited to the exact
`graphify workspace query --request-stdin` transport:

- accept one bounded canonical CLI-v1 request and reuse the existing
  `QueryRequest` validation;
- load and compose installed runtime authority before consuming standard
  input;
- call the existing freshness query authority exactly once, without an
  advisory status probe;
- emit native UTF-8 output only for `release` / `observed_current`, with one
  canonical redacted result certificate binding its byte count and SHA-256;
  and
- create no query log and write nothing to source, Git, workspace state,
  `HOME`, or `CODEX_HOME`.

Provider selection, networking, semantic execution, mutation, retained
service/watch, publication, performance/resource qualification, H3, P6+, and
every broader query or workspace-command authority remain excluded. The
bounded delivery is complete under the accepted
[`P5B2c` receipt](receipts/p5b2c.md); that receipt promoted no later child.

## P5B2 host-agent semantic-worker accepted boundary freeze

The completed child is limited to the exact transport:

```text
graphify workspace semantic-worker --stdio
```

The exact protocol and lifecycle are frozen in
[`semantic-sync.md`](semantic-sync.md), with completion evidence in the
[`P5B2 semantic-worker receipt`](receipts/p5b2-semantic-worker.md). One
long-lived process owns one
`SEMANTIC_CLAIM` lease from claim through optional checkpoints and terminal
completion or classified failure. The caller must state `host_agent_active`
as the Boolean `true`; the transport passes no explicit backend and performs no
ambient provider or credential discovery.

Before queue completion, a successful host-agent fragment must pass the
worker-specific closed validation and bounded indexed sanitization around the
existing semantic helpers, be installed as one canonical private immutable
result envelope under external workspace semantic staging, be reopened and
cryptographically verified, and be bound to the live claim's existing
checkpoint. The current queue state machine is not redesigned.
Commit uncertainty after queue completion begins is not replay or success
authority because the completed queue item does not retain that result digest.

This acceptance authorizes no implementation beyond the merged exact transport.
It adds no named/headless backend, network, `graphify.llm` provider
discovery, API-key handling, automatic fallback, full semantic sync,
`bind_sealed_inputs()` finalization, generation certification, promotion,
pointer mutation, migrate, repair, GC, service/watch, publication, or cleanup
authority. P5 and P5B2 remain `IN_PROGRESS`; all other remaining P5B2 and P5C
surfaces remain `WAITING` or deferred as recorded above.

## P5B2 semantic-result handoff and sealed-input finalization boundary freeze

This is a separate unnumbered P5B2 contract child. It preserves the accepted
worker boundary and receipt unchanged. PR #51 implements the exact frozen
boundary, and merged PR #53 records only this child as `COMPLETE`. The
completion evidence is the
[P5B2 semantic-result handoff receipt](receipts/p5b2-semantic-result-handoff.md).
It does not complete P5 or P5B2 or promote or activate a later successor.

The frozen implementation boundary is limited to all of the following as one
coherent internal operation:

- accept one result for every desired work identity in an exact completed
  semantic-required reconciliation, and only from an exact exit-0 worker session
  with one final completed terminal plus a reopened immutable result envelope,
  or identical retained version-1 evidence for carried completion;
- revalidate the repository, distinct target and optional carried-source
  generation identities, complete structural request,
  registry/active-source/operation/migration/pointer authority, source/policy
  observations, queue revision/hash/policy/watermarks, reconciliation, and exact
  one-to-one result set under canonical lock ordering;
- install one canonical immutable
  `graphify.workspace.semantic_result_handoff.internal` format-version-1 record
  at the derived private target-generation/request path, with exact same-byte
  replay and fail-closed uncertain-commit recovery;
- deterministically apply per-path ascending-revision `UPSERT` replacement and
  `DELETE` removal, rejecting missing, duplicate, stale, foreign, conflicting,
  or extra results, and copy the exact handoff bytes into request-bound target
  generation staging as `graphify-out/semantic-inputs.json`;
- use the existing staged-build recovery, inventory, source re-observation, and
  `payload_manifest_sha256("graphify-out", entries)` rules to reach exact staged
  `COMPLETE`; and
- under the same current `BUILD` grant, revalidate every authority and byte
  binding, call `bind_sealed_inputs()` with that exact manifest, and reopen the
  queue to prove the same digest before stopping.

The complete record grammar, installation order, replay rules, capacity and path
bounds, cleanup eligibility, redaction, content boundary, and fault-injection
gates are frozen in
[`semantic-sync.md`](semantic-sync.md#p5b2-semantic-result-handoff-and-sealed-input-finalization),
with corresponding ownership in [`architecture.md`](architecture.md), durable
state invariants in [`state-contract.md`](state-contract.md), threats in
[`threat-model.md`](threat-model.md), and gates in
[`verification.md`](verification.md#p5b2-semantic-result-handoff-and-sealed-input-finalization-acceptance-gates).

The handoff may retain bounded worker-accepted labels and rationales in private
state. Sanitization is not content-level DLP, and neither staged completion nor
sealed-input binding releases content. Cleanup may delete an original consumed
worker envelope only after the handoff, generation copy, staged manifest, and
queue binding all agree, and never the only recovery evidence. Conflicting,
stale, orphaned, legacy-unindexed, or commit-unknown staging is retained for
separately authorized inspection, repair, or GC.

This acceptance adds no product code, test, helper, JSON Schema, public argv,
status/result field, runtime receipt, provider/backend,
credential/network/model/fallback path, content-release policy, graph/query
projection, certification, promotion, pointer mutation, migrate, repair, GC
execution, service/watch, publication, production/runtime installation
authority, performance/resource proof, parent-phase completion, or successor
authority. P5 and P5B2 remain `IN_PROGRESS`; H3
remains `DEFERRED`; P5C, remaining P5B2 commands, and remaining P5C concerns
remain `WAITING`. `JOS-SEMANTIC-RATIONALE-PROJECTION` remains `OPPORTUNISTIC`,
`JOS-TOP-LEVEL-COMMAND-INVENTORY` remains `TRIGGER_GATED`, and both test-harness
JOS rows remain `CLOSED` historical evidence.

## P5B2 semantic-generation certification finalization contract freeze

This is the next separate unnumbered P5B2 contract child. This post-merge
governance reconciliation records only this child as `READY`; `READY` is
implementation eligibility only. The transition remains a local proposal until
publication and merge. The child is not `COMPLETE`, has no implementation or
acceptance receipt, and does not change any accepted receipt, parent phase, JOS
row, or later successor status.

Its exact start boundary is the accepted semantic-result handoff terminal:

- one canonical accepted `SyncRequest`, exact target generation, and complete
  `StructuralBuildRequest` remain bound by the request-bound staged
  `COMPLETE` record;
- the reopened target inventory reproduces that record's exact payload manifest,
  the retained handoff is byte-identical to the generation-owned
  `graphify-out/semantic-inputs.json`, and both are inventory-bound;
- the current semantic-required reconciliation is complete and its reopened
  `sealed_input_manifest_sha256` equals that same manifest; and
- current registry, active-source, operation, migration, pointer, policy, compatibility,
  source, and two-equal-observation evidence still matches the request and
  handoff. Any unexplained drift, foreign target, mismatched request, ambiguous
  durable state, or staged lifecycle other than `COMPLETE` blocks mutation.

The only mutating entry is the same request's existing staged `BUILD` recovery
lane. It must reconstruct the exact allocation and completion authority without
resetting or rewriting staging. The existing semantic certification view must
bind the same manifest and report `semantic_completeness="complete"`. The exact
certification request uses that view plus the selected compatibility and the
existing `payload_manifest`, `coordination_lock_precreated`, and
`stable_semantic_queue` validations. `GenerationStore.certify()` alone owns the
immutable target/request/view/manifest binding, existing generation receipt,
installed-generation verification, `CERTIFIED` journal event, reservation
clear, and staged `COMPLETE` to `CERTIFIED` transition under the existing lock
order.

Before an immutable binding exists, queue, source, policy, pointer, epoch,
request, manifest, inventory, target, or compatibility drift blocks new
certification. An already durable exact binding or receipt is not rewritten
from newer state; the existing recovery APIs may only finish those same bytes.
Exact same-byte/state replay is idempotent. Binding, receipt, staged-state,
reservation-clear, or lease-release uncertainty requires exact durable reread;
it never authorizes inferred success, target abandonment, staging reset, or
cleanup.

The exact stop boundary is the same staged record durably reopened as
`CERTIFIED`, bound to its unchanged request and manifest plus the exact verified
generation receipt, immutable semantic certification binding, matching journal
event, cleared target reservation, unchanged pointer boundary, and proven
release of the recovery owner/fence. An exact terminal replay may only return
that same proof read-only; it may not reacquire `BUILD`. A `PROMOTED` target is
outside this child.

This freeze adds no product code, tests, schema, fixture, dependency,
configuration, workflow, generated Graphify output, public command, runtime or
governance receipt, status field, content-release or DLP decision, graph/query
projection, promotion, pointer movement, provider/backend, credential/network
path, migrate, repair, GC, service/watch, publication, P5C, H3, P6+, parent
completion, readiness, acceptance, or execution authority. P5 and P5B2 remain
`IN_PROGRESS`; H3 remains `DEFERRED`; remaining P5B2 and P5C remain `WAITING`;
no JOS row is activated or closed.

## P5B2 identity-maintenance boundary freeze

The unnumbered P5B2 identity-maintenance surface is limited to the exact
`graphify workspace register rebind` and `graphify workspace register rotate`
forms accepted by the
[`P5B2 identity-maintenance receipt`](receipts/p5b2-identity-maintenance.md).
Both reuse installed authority, explicit UUID and registry-revision CAS,
bounded action-matching authorization, Git-top-level source proof, and the
existing registry policy. Rebind rejects a source identity persisted under a
different UUID before new source or identity-action evidence is persisted or
the requested registry mutation is committed. Rotate requires an explicitly
bound source and, under the separately accepted
[`P5B2 retained-source identity-continuity receipt`](receipts/p5b2-retained-source-identity-continuity.md),
independently requires either a shared immutable enrollment history root or the
enrolled Git common-directory identity before the requested evidence or
registry write.
Later active-source resolution independently repeats that continuity check.
Neither operation changes `active_source` or `active_source_revision`.
Registration v1 remains limited to `enroll` and `adopt`, and durable schema v1
remains unchanged. This ordering governs the requested mutation only; registry
lock acquisition and recovery may reconcile pre-existing state first.

Activation, additional sync, migrate, rollback, GC, repair, broader mutation
or query authority, production installation, watch/service, performance or
resource qualification, candidate publication, H3, P6+, and cleanup remain
excluded. This acceptance itself promoted no later child.

## P5B2 active-source activation boundary freeze

The unnumbered P5B2 active-source activation surface is limited to standalone
`graphify workspace activate` under the accepted
[`P5B2 active-source activation receipt`](receipts/p5b2-active-source-activation.md).
It loads installed authority before one bounded canonical `ACTIVATE`
authorization, requires the explicit repo UUID and four caller-supplied CAS
values, derives lease identity and timing internally, revalidates the exact Git
top-level source twice, and delegates once to the existing fenced registry
policy. The target must be explicitly bound, must still share an immutable
enrollment history root or retain the enrolled Git common-directory identity,
and must differ from the currently selected source. Success emits one redacted
CLI-v1 receipt. Denied, stale, and invalid paths preserve the documented exit
and redaction behavior; injected faults remain internal and are re-raised.

The separately accepted
[`P5B2 retained-source identity-continuity receipt`](receipts/p5b2-retained-source-identity-continuity.md)
closes the prior rotation and later-resolution nonclaims without reopening or
broadening this activation receipt. Additional sync modes, migrate, rollback,
GC, repair, broader mutation or query authority, production installation,
watch/service, performance or resource qualification, candidate publication,
H3, P6+, and cleanup remain excluded. That acceptance promoted no later child.

## P5B2 exact-last-good rollback boundary freeze

The unnumbered P5B2 exact-last-good rollback surface is limited to
`graphify workspace rollback --request-stdin` under the accepted
[`P5B2 exact-last-good rollback receipt`](receipts/p5b2-exact-last-good-rollback.md).
It loads and composes installed authority before consuming one bounded
canonical request; requires the explicit repo UUID, every caller-supplied
pre-acquisition registry, active-source, operation, migration, and pointer CAS
value, the current receipt, the visible pointer's exact non-null `last_good`
generation and receipt, its source epoch, and canonical `ROLLBACK`
authorization; and rejects current-generation reselection or arbitrary
historical selection.

One trusted 30-second `ROLLBACK` lease supplies the accepted operation and
fence authority. The same liveness deadline bounds the post-acquisition
pointer/receipt checks, generation locks, journal recovery, and durable
pointer/journal boundary. The orchestration delegates exactly once to
`PointerStore.rollback()`, while the existing pointer, generation, journal,
lease, recovery, and commit-unknown policies retain durable-state ownership.
Success emits one canonical receipt; every failure receipt remains redacted,
release cannot mask the primary error, and injected faults remain internal.

This acceptance adds no arbitrary historical selector, semantic sync,
migrate, GC, repair, broader mutation or query authority, production
installation, watch/service, performance or resource qualification, candidate
publication, H3, P6+, or cleanup authority. The separately accepted
retained-source identity-continuity receipt does not broaden rollback
authority, and that acceptance promoted no later child.

## P5B2 bounded offline-GC preview boundary freeze

The unnumbered bounded offline-GC preview surface is limited to
`graphify workspace gc --dry-run --request-stdin` under the accepted
[`P5B2 bounded offline-GC preview receipt`](receipts/p5b2-offline-gc-preview.md).
It loads and composes installed authority before consuming one bounded
canonical CLI-v1 request. The caller supplies the repo UUID, every expected
registry, active-source, operation, migration, and pointer revision,
`timeout_ms`, the complete `CapacityPolicy`, and all six `GcProtection`
classes. The request parser infers none of those values.

The existing read-only GC preview seam uses registry/workspace coordination
and generation-lock probes, requires two matching reachability snapshots, and
emits one deterministic canonical unfenced result. It creates no `LeaseGrant`,
fence, or executable `GcPlan` and makes zero durable writes on success or
failure. Existing fenced `GcStore.plan()`, `execute()`, `reconcile()`, and
`purge()` behavior remains unchanged and outside the public preview command.

GC mutation, quarantine, repair, migrate, semantic sync, broader query or
mutation authority, production installation, service/watch, publication, H3,
and P6+ remain excluded from this preview receipt. This acceptance makes no
performance or resource qualification and no bounded pre-enumeration traversal
claim. The published CLI-v1 capacity-policy fields remain frozen; any
compatibility change requires separate versioned review. No later child is
promoted to `READY`.

## P5B2 public fenced offline-GC lifecycle boundary freeze

The unnumbered public fenced offline-GC lifecycle is limited to the exact
commands accepted by the
[`P5B2 public fenced offline-GC lifecycle receipt`](receipts/p5b2-offline-gc-lifecycle.md):

```text
graphify workspace gc --execute --request-stdin
graphify workspace gc --reconcile --request-stdin
graphify workspace gc --purge --request-stdin
```

The frozen product contract remains in
[`README.md`](README.md#explicit-fenced-lifecycle). Execute binds the SHA-256 of
the exact canonical preview-result bytes to a fresh fenced `GC` plan before
quarantine. Reconcile remains explicit and is limited to an existing durable
intent or matching current-epoch completion recovery; a matching completion
replay and the no-recovery-state result are no-write. Purge remains explicit,
exact-plan-bound, and idempotent; first-time deletion rechecks fresh lease,
protection, pointer, and generation-lock authority, while exact terminal replay
is no-write. The request deadline remains in force through fenced mutation and
recursive deletion. The 4096 public generation bound is enforced by reading at
most one additional no-follow directory entry; this is a traversal-safety bound,
not performance or resource qualification.

This acceptance adds no automatic, online, or service GC; semantic sync;
migrate; repair; mutation beyond this exact lifecycle; broader query authority;
production installation; watch/service; publication; performance or resource
proof; H3; P6+; or cleanup authority. P5 and P5B2 remain `IN_PROGRESS`. At this
earlier receipt's acceptance point, the separately reviewed host-agent
semantic-worker contract was the sole `READY` child; the later semantic-worker
acceptance supersedes that live status without expanding this GC receipt.

## P5B2 public fenced pointer-repair lifecycle boundary freeze

The unnumbered public fenced pointer-repair lifecycle is limited to the exact
commands accepted by the
[`P5B2 public fenced pointer-repair lifecycle receipt`](receipts/p5b2-pointer-repair.md):

```text
graphify workspace repair --dry-run --request-stdin
graphify workspace repair --execute --request-stdin
```

The frozen product contract remains in
[`README.md`](README.md#public-fenced-pointer-repair-cli). Dry-run is
existing-state-only, read-only inspection: it allocates no lease or fence,
performs no recovery or cleanup, and writes no state. Execute binds the exact
canonical preview-result bytes, including their terminating newline, to
`approved_preview_sha256`; requires canonical `REPAIR_EXECUTE` authorization;
acquires one fresh `REPAIR` lease; and requires the private in-lock plan to
match the approved preview decision before `PointerStore` may mutate pointer,
journal, or eligible corrupt-generation state. The one absolute request
deadline spans preview, source verification, lease acquisition, locked
revalidation, mutation, and release.

Unsafe state paths remain outside repair authority. In particular, an unsafe
semantic certification-binding path propagates as `unsafe_state_path` /
`configure_safe_state_root` without writes; it is not downgraded to ordinary
generation corruption or quarantine authority. GC intent, nonterminal or
corrupt staged builds, semantic-queue, registry, lease, source-authority, and
broader journal failures retain their separately owned operator guidance.

This acceptance adds no semantic sync; migrate; broader repair; arbitrary
generation selection; GC reconciliation; mutation or query authority beyond
the two exact forms; production installation; watch/service; publication;
performance or resource proof; H3; P6+; or cleanup authority. P5 and P5B2
remain `IN_PROGRESS`, P5C and its remaining concerns remain `WAITING`, and no
later child was promoted by that acceptance.

## P5B2a ADOPT correction boundary freeze

The append-only
[`P5B2a corrective receipt`](receipts/p5b2a-adopt-prewrite-correction.md)
freezes only the PR #27 cross-UUID persisted Git common-directory device/inode
check before new source or identity-action evidence is persisted or the
requested ADOPT registry mutation is committed. Registry lock acquisition and
recovery may reconcile pre-existing state first; no broader no-write guarantee
is made. P5B2a remains `COMPLETE` without reopening or new authority. Explicit
ADOPT authorization, expected-revision CAS, existing binding and shared-history
policy, and same-UUID retained-inode adoption are unchanged.

## Migration provenance

The external document-level authority remains active until the publication
gate is satisfied. The hashes below identify its bytes before the
migration-staging notices were added. At activation, the external files retain
their historical evidence and P6-P12 portfolio authority but cease to own
Graphify-local status, readiness, or receipts.

| Historical source | Pre-staging SHA-256 |
|---|---|
| `graphify-workspace-execution-checklist.md` | `ddf873a889ec5ad43b35762ea372605555a322f10e61467dba0a57271c9c2d51` |
| `graphify-workspace-global-plan.md` | `28ec1c3ea527857fb6f03687e540d11c8755b5b139b60263bfff8eeea09dbe6b` |

The checklist name and its pre-staging hash are repeated in the receipt index.
Both external files retain their prior historical starting SHAs, worktree
records, and execution evidence below their conditional migration notices.
