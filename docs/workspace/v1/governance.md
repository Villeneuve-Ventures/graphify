# Workspace governance

Ledger refresh: `2026-08-22T21:31:53Z`

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
| Canonical base | At governance-acceptance preflight on `2026-08-22T21:31:53Z`, the original checkout, local `workspace/v1`, freshly fetched `origin/workspace/v1`, and repository-qualified remote branch all resolved to `c54c6116a45cf026546579b2fd6421fbad6dcf74`, tree `5d5de9f6b7bcbc3fae2a0d51399b262a896ffe90`, with local divergence `0/0`. Both the original checkout and the isolated acceptance worktree were clean before the nine-document edit. |
| Worktrees | Two proven worktrees existed at preflight: the clean original `workspace/v1` checkout and the clean task-owned `codex/p5b2-decision-store-capacity-gc-acceptance` worktree, both at the canonical base above. Repository-qualified inspection found no open pull request. This staged closeout authorizes only the task worktree/branch and no cleanup, rebase, force-push, merge, or other worktree/ref mutation. |
| GitHub | PR [#79](https://github.com/Villeneuve-Ventures/graphify/pull/79) is the latest merge to `workspace/v1` and produced the canonical base above. Its exact-head CI [32593448372](https://github.com/Villeneuve-Ventures/graphify/actions/runs/32593448372), exact-head PR Agent [32596616049](https://github.com/Villeneuve-Ventures/graphify/actions/runs/32596616049), and exact post-merge CI [32597059464](https://github.com/Villeneuve-Ventures/graphify/actions/runs/32597059464) passed; CI included `skillgen-check`, `test (3.14)`, and `security-scan`, and PR Agent passed `review`. PR #76 has one resolved/outdated plus five unresolved/outdated threads; PR #77 has nine resolved, six current unresolved, and one unresolved/outdated; PR #79 has zero threads. UI state was not treated as proof or mutated. GitHub Issues are disabled. |
| P5B2 host-agent semantic-worker contract provenance | PR [#43](https://github.com/Villeneuve-Ventures/graphify/pull/43) exact head `1f202c9134ee0993e4bba40482fa8113f598920a`; merge `5d730fe6e7d781c4d44f87989bf148ab2fdb63e3`; tree `27f7259fc3d716a78a3b28417204b1968c05d421`. Exact-head CI [30681324681](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30681324681) passed `skillgen-check`, `test (3.14)`, and `security-scan`. |
| P5B2 host-agent semantic-worker implementation delivery | PR #45 exact base `99af03803a44d575123a18f1c0eafa48149df492`; head `5f57e565bd188789c984bc1370943caa758148c3`; merge/current commit `36b2e3426ebe3095a0b81c36656789b6790f103f`; delivery/merge/current tree `06d20480337bc94edba4de37c06d2dbf1ab595f2`. Exact-head CI [30730561721](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30730561721) passed `skillgen-check`, `test (3.14)`, and `security-scan`; the separate `CodeRabbit` context succeeded. |
| P5B2 host-agent semantic-worker governance acceptance | PR [#46](https://github.com/Villeneuve-Ventures/graphify/pull/46) exact head `a0c3763acd20cb9886a4e26cc3c2e776597fe162`; merge `c2bb53d733d43784b76ab3cf559c48c16688f298`; tree `98b0ed85599794a152c1fd8ddde6ae3ebacb98aa`. Exact-head CI [30734181344](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30734181344) passed `skillgen-check`, `test (3.14)`, and `security-scan`; the separate `CodeRabbit` context succeeded. The acceptance is limited to the worker transport and promotes no successor. |
| JOS test-harness determinism delivery | PR #47 exact base `c2bb53d733d43784b76ab3cf559c48c16688f298`; head `e17482c61a5cfad2d227a4b0d8d27c2bcd723c32`; merge/current commit `d19ff5467a48778b14a4cdb62eada4ba3fa48293`; delivery/merge/current tree `8b2fc5a29c06eb7df2a41cd79c896e052636a19e`. Exact-head CI [30771565129](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30771565129) passed `skillgen-check`, `test (3.14)`, and `security-scan`; the separate `CodeRabbit` context succeeded. |
| JOS test-harness governance acceptance | PR [#48](https://github.com/Villeneuve-Ventures/graphify/pull/48) exact head `a099ce64ac533ae61b14275f67c07eabd126c9a3`; merge/current commit `e9967f18de55aba2a51803cb51d225a221d42fdc`; head/merge/current tree `13117628e5b22cce5d95d26dfd5456a2d9136d58`. Exact-head CI [30780293723](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30780293723) passed `skillgen-check`, `test (3.14)`, and `security-scan`; the separate `CodeRabbit` context succeeded. Both test-harness JOS rows are `CLOSED` historical evidence only. |
| Semantic-result handoff contract delivery | PR [#49](https://github.com/Villeneuve-Ventures/graphify/pull/49) exact base `e9967f18de55aba2a51803cb51d225a221d42fdc`; head `f46de7408df3b70e57a8bb17047449caff658326`; merge/current commit `92b81db6d39e42c4b4a52aa69f1113398f9115ad`; head/merge/current tree `7e77046c8dce66ad6a21e423cd3ca153385a8d74`. The merge commit's ordered parents are the exact base and head. Exact-head CI [30789398224](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30789398224) passed `skillgen-check`, `test (3.14)`, and `security-scan`; the separate `CodeRabbit` context succeeded. The hosted [Codex review](https://github.com/Villeneuve-Ventures/graphify/pull/49#issuecomment-5163081658) reported no major issue against reviewed commit `f46de7408d`, the exact head's unique prefix. |
| Semantic-result handoff implementation delivery | PR [#51](https://github.com/Villeneuve-Ventures/graphify/pull/51) exact base `1d092a86fce5ba2eec5723908ec442d8ecdd639e`; head `272e56248c56ea6bc699e035b69f732c20e94d1e`; merge/current commit `2d9efe7e79b16953e62523684fbf8c6bf8b7a20a`; delivery/merge/current tree `dee2624fb3729b3e9b30a855f2c3635e672dd797`. Exact-head CI [30882417900](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30882417900) passed `skillgen-check`, `test (3.14)`, and `security-scan`; the separate `CodeRabbit` context succeeded. The delivery changed exactly seven implementation/test files. |
| Semantic-result handoff governance acceptance | PR [#53](https://github.com/Villeneuve-Ventures/graphify/pull/53) exact base `2d9efe7e79b16953e62523684fbf8c6bf8b7a20a`; head `309c3d96eb633211103e2546a5b5f6fdb7dcafd7`; merge/current canonical commit `9c98d77830238a0de299977e5230690f7bb504b1`; head/merge/current tree `598081f934c838dec5c3abf41c23380dd5660e22`. Exact-head CI [30887151160](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30887151160) passed `skillgen-check`, `test (3.14)`, and `security-scan`; the separate PR-Agent review and `CodeRabbit` context succeeded. The accepted closeout remains limited to the semantic-result handoff and promoted no successor. |
| Semantic-generation certification-finalization contract delivery | PR [#54](https://github.com/Villeneuve-Ventures/graphify/pull/54) exact base `9c98d77830238a0de299977e5230690f7bb504b1`; reviewed head `07b521c4386c0c97134f89de5f989be4a70455d2`; merge/current canonical commit `fb7b5850ea13c248e50fa17a9b4780599063f5ac`; head/merge/current tree `1dc7e40e3cb6d0d143745bb52bdf50e26629f831`; merged at `2026-08-04T19:48:46Z`. The merge commit's ordered parents are the exact base and reviewed head. Exact-head CI [30922838023](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30922838023) passed `skillgen-check`, `test (3.14)`, and `security-scan`; PR-Agent [30922842255](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30922842255) passed `review`; the separate `CodeRabbit` context succeeded. Independent exact-head specification and architecture/state-consistency reviews both reported `CLEAN`. GitHub's review decision remained unset, with no submitted reviews, inline review comments, or review threads. The delivery froze the lifecycle contract only and granted no implementation, acceptance, completion, or successor-promotion authority. |
| Semantic-generation certification-finalization implementation delivery | PR [#56](https://github.com/Villeneuve-Ventures/graphify/pull/56) exact base `759dca764d5aab59adf760389ff0298f386f962c`; head `c614a58d71aa37784129554a6f67e5f167cc8fcc`; merge `8a6d5994e3ed44108768093062e66e6d602dfc44`; head/merge tree `9923f1004c948b34a6ff703e954d3bd9767e99eb`; merged at `2026-08-05T16:36:29Z`. Exact-head CI [31024895405](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31024895405) passed `skillgen-check`, `test (3.14)`, and `security-scan`. The exact final head included deadline and bound-`COMPLETE` recovery corrections, but the retained-`CERTIFIED` cleanup defect required PR #57; PR #56 alone is not accepted as the final boundary. |
| Semantic-generation certification-finalization corrective delivery | PR [#57](https://github.com/Villeneuve-Ventures/graphify/pull/57) exact base `8a6d5994e3ed44108768093062e66e6d602dfc44`; head `10f3d4758776bb78a4122f62e02ebfc281dbb589`; merge/current canonical commit `27d60deebe47ba11ef8858b55e0d0c04d4a24d4c`; head/merge/current tree `4129a7c4ed879a94ffca6c87c1c82ce52ccbb847`; merged at `2026-08-05T19:22:35Z`. Exact-head CI [31038073131](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31038073131) passed `skillgen-check`, `test (3.14)`, and `security-scan`. Qodo's final report recorded zero bugs and zero gaps; exact-head Codex reported no major issue. CodeRabbit's post-push review was rate-limited, so it is not recorded as a fresh clean review. PR #57 corrected retained-`CERTIFIED` cleanup and the later competing-cleanup race. |
| Semantic-generation certification-finalization governance acceptance | PR [#58](https://github.com/Villeneuve-Ventures/graphify/pull/58) exact base `27d60deebe47ba11ef8858b55e0d0c04d4a24d4c`; head `dea27638186346678439bf40d1db284d24b60b01`; merge/current canonical commit `968c5df938772a3ad0249e5418d138bced474349`; head/merge/current tree `767e999fb475663a9e16c50c2b4b413bcc843d36`; merged at `2026-08-05T23:33:45Z`. Exact-head CI [31048040499](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31048040499) passed `skillgen-check`, `test (3.14)`, and `security-scan`; the separate `CodeRabbit` context succeeded. PR #58 accepts the certification-finalization boundary only when PR #56 is combined with PR #57's corrections and promotes no successor. |
| Semantic-generation promotion and pointer-finalization contract delivery | PR [#59](https://github.com/Villeneuve-Ventures/graphify/pull/59) exact base `968c5df938772a3ad0249e5418d138bced474349`; final head `32d4a1fee7a396b8cb0ff906d68f2f1d4e9404c3`; merge/current canonical commit `c928fbc8326c09cb0c51ea44164b7325a4c07122`; head/merge/current tree `007bd1302ba747a754c3b1edce796c901d27ab12`; merged at `2026-08-06T01:43:38Z`. Exact-head CI [31063042837](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31063042837) passed `skillgen-check`, `test (3.14)`, and `security-scan`; the separate `CodeRabbit` context succeeded. The single review thread was addressed by the final head and is resolved. GitHub's `reviewDecision` remained unset, so no formal approval is claimed. The delivery froze the lifecycle contract only and granted no implementation, acceptance, completion, execution, or later-successor authority. |
| Semantic-generation promotion readiness preflight | At `2026-08-06T02:07:34Z`, the canonical owned repository, `workspace/v1` HEAD/tree, clean `0/0` divergence, one-worktree inventory, empty open-PR inventory, disabled Issues, PR #59 merge and tree identity, exact-head and post-merge CI, successful `CodeRabbit` context, resolved sole review thread, and unset `reviewDecision` were revalidated with repository-qualified live calls. The generated graph report remained stale orientation from `bba3a7cee5e910161d4b48d9d31ced19cf451dd2` and was not rebuilt or used as current authority. This local reconciliation records only the promotion and pointer-finalization child as `READY`; `READY` is implementation eligibility only. It changed no code, tests, schemas, receipts, or generated output; performed no GitHub thread mutation, branch/worktree operation, commit, push, PR, merge, fast-forward, or cleanup; and implemented, accepted, completed, or executed nothing and activated no later successor. |
| Semantic-generation promotion readiness reconciliation | PR [#60](https://github.com/Villeneuve-Ventures/graphify/pull/60) exact base `c928fbc8326c09cb0c51ea44164b7325a4c07122`; head `8492e3cf2d900a2586c5e66eec42d1981e750ee9`; merge `18a4bb91dea3813f1d02c45509cf53a6e8eccb43`; head/merge tree `cd94fb5195cb6947c4ad6bb0a7069d87da614a9d`; merged at `2026-08-06T03:37:28Z`. Exact-head CI [31068073085](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31068073085) passed `skillgen-check`, `test (3.14)`, and `security-scan`; the separate `CodeRabbit` context succeeded. Both inline review threads are resolved. The delivery reconciled governance readiness only and granted no implementation or acceptance. |
| Semantic-generation promotion regression correction | PR [#61](https://github.com/Villeneuve-Ventures/graphify/pull/61) exact base `18a4bb91dea3813f1d02c45509cf53a6e8eccb43`; head `a52cc37b38a9f8386d5d6e5dae8a7927d96be1bc`; merge `b7e646fb1e58c4b7b184c2cc604b6486d083deb9`; head/merge tree `2f4120e03cdb6cfbd8c68614f6c82c160aa3b8f6`; merged at `2026-08-06T13:31:02Z`. Exact-head CI [31104869472](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31104869472) passed `skillgen-check`, `test (3.14)`, and `security-scan`; the separate `CodeRabbit` context succeeded. All three inline review threads are resolved. The strict regression exposed the promoted-terminal cleanup acquisition prerequisite without granting implementation acceptance. |
| Promoted-terminal cleanup acquisition correction | PR [#62](https://github.com/Villeneuve-Ventures/graphify/pull/62) exact base `b7e646fb1e58c4b7b184c2cc604b6486d083deb9`; head `1a99b09fcf8bc563845d38c9a19f80e59be86bcb`; merge `9fc8e2568ae52a599a8fe26039fa33066642b3d5`; head/merge tree `e1f5b0911aa15e0e4dea4f81364c3a854d594d86`; merged at `2026-08-08T13:50:47Z`. Exact-head CI [31258660015](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31258660015) passed `skillgen-check`, `test (3.14)`, and `security-scan`; the separate `CodeRabbit` context succeeded. The correction supplied exact request/target/attempt-bound cleanup acquisition and its focused regression evidence; unresolved review UI state is dispositioned in the accepted receipt rather than mutated. |
| Semantic-generation promotion and pointer-finalization implementation delivery | PR [#63](https://github.com/Villeneuve-Ventures/graphify/pull/63) exact base `9fc8e2568ae52a599a8fe26039fa33066642b3d5`; head `eb57c8ab60f9fede26661c8c2c733dd2a8a641ac`; merge `2ab6a4060a2c132b89e79dcd21a12292b69f2b89`; head/merge tree `967e7a24cae68427353fbea805be8a51b1346981`; merged at `2026-08-09T16:58:17Z`. Exact-head CI [31324484102](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31324484102) passed `skillgen-check`, `test (3.14)`, and `security-scan`; the separate `CodeRabbit` context succeeded. The delivery implemented the frozen internal composition and corrections, but did not grant governance acceptance. |
| Semantic-generation promotion acceptance-coverage completion | PR [#64](https://github.com/Villeneuve-Ventures/graphify/pull/64) exact base `2ab6a4060a2c132b89e79dcd21a12292b69f2b89`; head `aa1ec40291b0e10dfc85e82a4a89483514c35379`; merge/current canonical commit `3a24d32196e80d521c3fa77fca10bca3c899c597`; head/merge/current tree `f52480178c2d57f20200a08247719f0e7fc5a535`; merged at `2026-08-09T23:02:01Z`. Exact-head CI [31333003405](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31333003405) passed `skillgen-check`, `test (3.14)`, and `security-scan`; PR-Agent [31333132796](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31333132796) passed `review`; and the separate `CodeRabbit` context succeeded. The delivery completed the frozen rejection and terminal-proof coverage needed for separate acceptance; its one low-value schema-constant nit remains deferred and does not change the frozen boundary. |
| Semantic-generation promotion governance acceptance | PR [#65](https://github.com/Villeneuve-Ventures/graphify/pull/65) exact base `3a24d32196e80d521c3fa77fca10bca3c899c597`; head `2f46c2ff6f9e2c31407234b7da9ad0d98abb539d`; merge/current canonical commit `c8fbf10bd6d7e25790c81f18db0ce906a16bb562`; head/merge/current tree `7d4a597ed509513471dfcd7be4b2a6349a3bc1f4`; merged at `2026-08-11T02:48:52Z`. Exact-head CI [31452781923](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31452781923) passed `skillgen-check`, `test (3.14)`, and `security-scan`; PR-Agent [31452835350](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31452835350) passed `review`; and the separate `CodeRabbit` context succeeded. PR #65 accepts only the promotion/pointer-finalization child, makes its receipt canonical, and activates no later successor. |
| Semantic-content release/DLP contract preflight | At `2026-08-11T08:29:14Z`, the canonical owned repository, `workspace/v1` HEAD/tree, clean `0/0` divergence, one-worktree inventory, empty open-PR inventory, disabled Issues, PR #65 acceptance, exact-head and post-merge CI, and stale generated graph report were revalidated with repository-qualified live calls. `graphify-out/GRAPH_REPORT.md` remains orientation from `2ab6a4060a2c132b89e79dcd21a12292b69f2b89` and was not rebuilt or used as current authority. This local change freezes only the next semantic-content release/DLP decision child in `WAITING`; no code, test, schema, receipt, generated output, GitHub thread, branch/worktree operation, commit, push, PR, merge, fast-forward, cleanup, implementation, readiness, acceptance, execution, or later-successor mutation is claimed. |
| Semantic-content release/DLP contract delivery | PR [#66](https://github.com/Villeneuve-Ventures/graphify/pull/66) exact base `c8fbf10bd6d7e25790c81f18db0ce906a16bb562`; head `772fd8809f6784c9cc859dcc654bfcdf873df40f`; merge/current canonical commit `d2839bb3c2c155cd707694819ae06538d4ec9dd3`; head/merge/current tree `904a91047bcdbaae724d9688c586ec88fd3198f7`; merged at `2026-08-11T18:06:56Z`. Exact-head CI [31519403573](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31519403573) and post-merge CI [31521042681](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31521042681) each passed `skillgen-check`, `test (3.14)`, and `security-scan`; all nine review threads are resolved. The merge froze documentation only and granted no implementation, readiness, acceptance, execution, release, or successor authority. |
| Semantic-release trust-root readiness preflight | At `2026-08-11T18:36:45Z`, repository root, `workspace/v1` HEAD/tree, clean `0/0` divergence, one-worktree inventory, empty open-PR inventory, disabled Issues, PR #66 head/merge tree identity, all nine resolved threads, exact-head CI, and exact-SHA post-merge CI were revalidated with repository-qualified live calls. `graphify-out/GRAPH_REPORT.md` remains stale orientation from `2ab6a4060a2c132b89e79dcd21a12292b69f2b89` and was not rebuilt or used as current authority. This local reconciliation records only the semantic-release bundle and deterministic-classifier trust-root prerequisite as `READY`; it changes no code, test, schema, package data, receipt, generated output, GitHub state, branch/worktree, commit, push, PR, merge, cleanup, implementation, acceptance, execution, release, or later-successor status. |
| Semantic-release trust-root readiness reconciliation | PR [#67](https://github.com/Villeneuve-Ventures/graphify/pull/67) exact base `d2839bb3c2c155cd707694819ae06538d4ec9dd3`; head `5542c97ed0c69a53ea540968fae1725e34e9663a`; merge `daa3b695db24022f2fbefd1dbee2cdbc46777286`; head/merge tree `1acb80abbdae531304362e2c918ade657c9a3e45`; merged at `2026-08-11T20:11:28Z`. Exact-head CI [31530368023](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31530368023) and post-merge CI [31531660783](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31531660783) each passed `skillgen-check`, `test (3.14)`, and `security-scan`; its sole review thread is resolved. The delivery reconciled only implementation eligibility and granted no implementation or acceptance. |
| Semantic-release trust-root implementation delivery | PR [#68](https://github.com/Villeneuve-Ventures/graphify/pull/68) exact base `daa3b695db24022f2fbefd1dbee2cdbc46777286`; head `4579093222c1b25863c43c15db529e2122beaf27`; merge `3f96e361a09b098e15ded0f6c71ad11f28970549`; head/merge tree `7b79bb9dfbc8d88464589bc24ed0a61df732e765`; merged at `2026-08-14T12:27:50Z`. Exact-head CI [31799543372](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31799543372) and post-merge CI [31800433300](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31800433300) each passed `skillgen-check`, `test (3.14)`, and `security-scan`. Its 75 threads comprise 41 resolved and 34 unresolved; unresolved UI state is dispositioned in the staged acceptance receipt rather than mutated. The delivery implemented the frozen prerequisite but did not accept it. |
| Semantic-release trust-root C1 repair | PR [#69](https://github.com/Villeneuve-Ventures/graphify/pull/69) exact base `3f96e361a09b098e15ded0f6c71ad11f28970549`; head `4e2510309f5563e512dbc562328fe98909185c17`; merge/current canonical commit `01bc19cbb5e275fe0a63e5af278cbee663f218f5`; head/merge/current tree `9e3cae64d53165145bbeab0cb6a1402509f041e3`; merged at `2026-08-14T20:55:19Z`. Exact-head CI [31814026195](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31814026195) and post-merge CI [31840122100](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31840122100) each passed `skillgen-check`, `test (3.14)`, and `security-scan`; PR #69 has no review thread. It repairs both duplicated PR #68 C1 findings, rebinds the classifier manifest digest, and adds exact C0/C1 boundary vectors. |
| Semantic-release trust-root governance acceptance preflight | At `2026-08-14T22:39:49Z`, the canonical repository, exact HEAD/tree, clean `0/0` divergence, one-worktree inventory, zero open-PR inventory, PR #66-#69 identities/checks, authority-file fingerprints, and all thread states were revalidated. All 34 unresolved PR #68 threads received exact-current-tree dispositions; the required focused, combined, full-suite, lock, lint, type, security, generator, pre-commit, and disposable two-candidate build/audit gates passed. `graphify-out/GRAPH_REPORT.md` remained stale orientation from `91a34b4b2b83f54fa5f94b8f3c09f62c3f631603` and was neither authority nor modified. This governance-only acceptance receipt remains `STAGED` until separately published and merged; no GitHub, branch, worktree, commit, publication, or external workflow state was mutated. |
| Semantic-release trust-root governance acceptance | PR [#70](https://github.com/Villeneuve-Ventures/graphify/pull/70) exact base `01bc19cbb5e275fe0a63e5af278cbee663f218f5`; head `3b171d264b7048c0b597c569985bc407701d751f`; merge/current canonical commit `17505a5c03e8945c2d3be932ce85cc09b93883fe`; head/merge/current tree `ce7bcbb95d0fe0535445d01a5a368c8e36b2e914`; merged at `2026-08-15T02:38:22Z`. Exact-merge CI [31859714756](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31859714756) passed. PR #70 accepts only the semantic-release bundle and deterministic-classifier trust-root prerequisite as `COMPLETE`; policy-authority provisioning and the encompassing release/DLP decision remain `WAITING`, and no successor becomes `READY`. |
| Semantic-release policy-authority contract delivery | PR [#71](https://github.com/Villeneuve-Ventures/graphify/pull/71) exact base `17505a5c03e8945c2d3be932ce85cc09b93883fe`; head `0e8ee7457089c7f58c1bf98c8fe89eb263c7b73b`; merge/current canonical commit `5d534d0b769f1217ed0a1574fb54915504892b4c`; head/merge/current tree `bad5348abaa1a59f01f3eaa48a3126b58d1bbeb0`; merged at `2026-08-15T13:43:21Z`. Exact-head CI [31885426041](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31885426041) and exact-merge CI [31887969988](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31887969988) passed. PR #71 froze documentation only and left the prerequisite `WAITING`; it granted no implementation, readiness, acceptance, live provisioning, or successor authority. |
| Semantic-release policy-authority implementation delivery | PR [#72](https://github.com/Villeneuve-Ventures/graphify/pull/72) exact base `5d534d0b769f1217ed0a1574fb54915504892b4c`; head `5e6e91fdc7ab6c6cd764e4ee0a04f76e77f643ea`; merge `b88e81bae1bdfec9ab960199b42cd81e582e41b5`; head/merge tree `b046110da1ba7246d579a6d5bc39c9550d3b3b75`; merged at `2026-08-15T21:55:17Z`. Exact-head CI [31906370747](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31906370747) and exact-merge CI [31910754058](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31910754058) passed. The delivery added the private store and focused tests but granted no governance acceptance. |
| Semantic-release policy-authority lock-discipline correction | PR [#74](https://github.com/Villeneuve-Ventures/graphify/pull/74) exact base `b88e81bae1bdfec9ab960199b42cd81e582e41b5`; head `9ed8e8a45582587c3226fd434e15b3a21bf5bc0c`; merge/current canonical commit `e28afc95f1f5b262b7673ef7b8c0ce9f7b1a4fa8`; head/merge/current tree `4b8d2faf95aef1caec02bcb95165cbb67b2983e1`; merged at `2026-08-17T03:57:47Z`. Exact-head CI [31992069990](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31992069990) and exact-merge CI [31992829074](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31992829074) passed. The correction serializes initializer election with the retained initialization lock and removes the broad exception catch raised in review. |
| Semantic-release policy-authority governance acceptance preflight | At `2026-08-17T05:38:16Z`, repository root, exact canonical HEAD/tree, clean `0/0` divergence, one-worktree inventory, zero open pull requests, disabled Issues, PR #71/#72/#74 identities, exact-head and exact-merge CI, and all seven review-thread dispositions were revalidated. PR #71 had three unresolved threads, PR #72 had two, and PR #74 had one current unresolved plus one resolved/outdated thread; all seven concerns are fixed in the exact current tree. Required focused, predecessor, trust-root, full-suite, lock, skillgen, and type gates passed, including a clean full-suite rerun of 5,620 passed and 3 skipped after one non-reproducing deadline-sensitive runtime-test failure. This staged governance-only closeout accepts only the policy-authority provisioning prerequisite as `COMPLETE`; it provisions no live record and activates no successor. |
| Semantic-release policy-authority governance acceptance | PR [#75](https://github.com/Villeneuve-Ventures/graphify/pull/75) exact base `e28afc95f1f5b262b7673ef7b8c0ce9f7b1a4fa8`; head `9c46c4ced0e0c87d7f18a64f0690074769f08e13`; merge/current canonical commit `33c7d8255b18128f8371219c823f78f6cbb010f6`; head/merge/current tree `b495407ecb8e63507cd9186c2a3922b2aed1d5e1`; merged at `2026-08-17T19:23:49Z`. Exact-head CI [32058926752](https://github.com/Villeneuve-Ventures/graphify/actions/runs/32058926752) and exact-merge CI [32060109454](https://github.com/Villeneuve-Ventures/graphify/actions/runs/32060109454) each passed `skillgen-check`, `test (3.14)`, and `security-scan`. PR #75 made only the private policy-authority provisioning prerequisite's acceptance canonical as `COMPLETE`; it provisions no live record and activates no successor. |
| Semantic-release decision-store and capacity/GC contract preflight | At pre-edit `2026-08-17T19:33:20Z`, repository root, exact canonical HEAD/tree, clean `0/0` divergence, one-worktree inventory, zero open pull requests, PR #75 merge identity and exact-head CI, and the stale generated graph report were revalidated with repository-qualified live calls. Exact-merge CI was still running at that snapshot and is separate later corroboration in the GitHub row above. `graphify-out/GRAPH_REPORT.md` remained orientation from `107fa12830177f22c22d4cc80a0ddf6b55a0428d` and was neither authority nor modified. The resulting seven-document diff freezes only the separate decision-store and capacity/GC prerequisite at `WAITING` and changes no code, test, schema, fixture, receipt, generated output, runtime state, or JOS disposition. The contract grants no GitHub mutation, implementation, readiness, acceptance, execution, parent completion, or successor authority. |
| Semantic-release decision-store and capacity/GC contract delivery | PR [#76](https://github.com/Villeneuve-Ventures/graphify/pull/76) exact base `33c7d8255b18128f8371219c823f78f6cbb010f6`; head `94ef3ba716f18504705569a584b1fb03b29d4c42`; merge/current canonical commit `13a5abe45a14bc7051bc2f82c1bf183ade59ed67`; head/merge/current tree `8fa8b78ba33b598a5ae7e32262114e7508cc038d`; merged at `2026-08-18T01:07:56Z`. Exact-head CI [32086058260](https://github.com/Villeneuve-Ventures/graphify/actions/runs/32086058260) and exact post-merge CI [32087107935](https://github.com/Villeneuve-Ventures/graphify/actions/runs/32087107935) each passed. PR #76 froze documentation only, left the prerequisite `WAITING`, and granted no implementation, readiness, acceptance, live decision, execution, parent completion, or successor authority. |
| Semantic-release decision-store and capacity/GC implementation delivery | PR [#77](https://github.com/Villeneuve-Ventures/graphify/pull/77) exact base `13a5abe45a14bc7051bc2f82c1bf183ade59ed67`; head `28d204cf66fc4026a8cd631d4a6462d64575063e`; merge `e4b930ca0073d2404216f7392f78a192a49ab9b5`; head/merge tree `15603dd51f187e86da19c5ed0d66ada44def5dd7`; merged at `2026-08-19T15:58:01Z`. Exact-head CI [32258592787](https://github.com/Villeneuve-Ventures/graphify/actions/runs/32258592787), exact-head PR Agent [32271545087](https://github.com/Villeneuve-Ventures/graphify/actions/runs/32271545087), and exact-merge CI [32273175871](https://github.com/Villeneuve-Ventures/graphify/actions/runs/32273175871) passed. PR #77 delivered the private store, authoritative capacity/GC integration, runtime composition, and focused hostile/failure tests but granted no governance acceptance. |
| Semantic-release decision-store canonical-directory correction | PR [#79](https://github.com/Villeneuve-Ventures/graphify/pull/79) exact base `a3e5021fbdfcc3fe8e70fc75b34a0214fc3b03d2`; head `26f4025274d9cd2184397fbbfde22aa8baf2f98d`; merge/current canonical commit `c54c6116a45cf026546579b2fd6421fbad6dcf74`; head/merge/current tree `5d5de9f6b7bcbc3fae2a0d51399b262a896ffe90`; merged at `2026-08-22T20:34:08Z`. Exact-head CI [32593448372](https://github.com/Villeneuve-Ventures/graphify/actions/runs/32593448372), exact-head PR Agent [32596616049](https://github.com/Villeneuve-Ventures/graphify/actions/runs/32596616049), and exact-merge CI [32597059464](https://github.com/Villeneuve-Ventures/graphify/actions/runs/32597059464) passed. The correction rebinds both capacity and GC scans to the canonical held decision directory after final inventory stabilization; it changes only `generations.py` and its exact regressions. |
| Semantic-release decision-store and capacity/GC governance acceptance preflight | At `2026-08-22T21:31:53Z`, repository and remote identity, exact canonical HEAD/tree, clean `0/0` divergence, two-worktree inventory, zero open pull requests, PR #76/#77/#79 identities/checks/manifests, 49 top-level timeline comments, ten submitted reviews, and all 22 review-thread dispositions were revalidated. PR #76 remained one resolved/outdated plus five unresolved/outdated threads; PR #77 remained nine resolved, six current unresolved, and one unresolved/outdated; PR #79 remained thread-free. All substantive concerns are fixed, rejected, or explicitly deferred with exact-current-tree evidence; the sole deferral is bounded scan performance/resource qualification retained as later P5C work. Exact canonical-directory regressions, the 652-test six-file implementation suite, targeted Ruff, and targeted Pyright passed. This staged governance-only closeout accepts only the decision-store/capacity/GC prerequisite as `COMPLETE`; it provisions no live record and activates no successor. |
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
| P5 | P4, H1, H2 | IN_PROGRESS | P5A and delivered P5B children, including the accepted host-agent semantic-worker transport, semantic-result handoff, corrected semantic-generation certification finalization, semantic-generation promotion and pointer-finalization, semantic-release trust-root, policy-authority provisioning, and decision-store/capacity/GC prerequisites, are complete. The encompassing semantic-content release/DLP decision and remaining P5B2/P5C work remain `WAITING`. |
| P5A | P4, H1, H2 | COMPLETE | Durable semantic queue and stable certification watermark closed. |
| P5B1 | P5A | COMPLETE | Production composition, versioned read-only status, and read-only doctor closed. |
| P5B2 | P5B1 | IN_PROGRESS | Delivered children, including the accepted host-agent semantic-worker transport, semantic-result handoff, corrected semantic-generation certification finalization, semantic-generation promotion and pointer-finalization, semantic-release trust-root, policy-authority provisioning, and decision-store/capacity/GC prerequisites, are complete. The encompassing semantic-content release/DLP decision, full semantic sync, explicit backend integration, migrate, broader repair, broader mutation/query authority, and every other undelivered command remain `WAITING`. |
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
| P5B2 semantic-generation certification finalization | P5B2 semantic-result handoff and sealed-input finalization | COMPLETE | Accepted only at the frozen boundary in [`semantic-sync.md`](semantic-sync.md#p5b2-semantic-generation-certification-finalization). Entry requires the accepted handoff's exact reopened staged `COMPLETE` manifest and equal queue sealed-input digest; the only mutating lane is same-request `BUILD` recovery through the existing semantic certification view, immutable binding, generation receipt/journal, reservation, and staged-state authorities until exact `CERTIFIED` proof and lease release. Completion evidence: [`P5B2 semantic-generation certification finalization`](receipts/p5b2-semantic-generation-certification-finalization.md), accepting PR #56 only together with PR #57's corrective delivery. PR #58 made that acceptance canonical and promoted no parent phase or successor. |
| P5B2 semantic-generation promotion and pointer-finalization | P5B2 semantic-generation certification finalization | COMPLETE | Accepted only at the frozen boundary in [`semantic-sync.md`](semantic-sync.md#p5b2-semantic-generation-promotion-and-pointer-finalization). Entry remains the accepted exact staged `CERTIFIED` terminal with verified installed target/receipt/binding and journal, absent reservation and certification `BUILD` grant, and unchanged request pointer CAS. Forward authority remains limited to same-request staged `PROMOTE`, including exact already-visible replay, or exact pending-intent `POINTER_RECOVERY`; terminal proof requires staged `PROMOTED`, exact visible-current and journal evidence, no pending intent, unchanged semantic evidence, and exact grant release. Completion evidence: [`P5B2 semantic-generation promotion and pointer-finalization`](receipts/p5b2-semantic-generation-promotion-finalization.md), binding the exact PR #59 through PR #64 chain and made canonical by PR #65. It grants no execution or later-successor authority. |
| P5B2 semantic-release bundle and deterministic-classifier trust-root | P5B2 | COMPLETE | Accepted only at the [internal trust-root boundary](semantic-sync.md#p5b2-semantic-release-bundle-and-deterministic-classifier-trust-root): repo-owned installed manifest, deterministic classifier implementation/ABI, closed taxonomy, normalization, ruleset, required `core_secrets.v1`, selectable profile bundle, and existing installed executable bootstrap that excludes package-local bytecode caches and Python startup hooks from trusted execution. Completion evidence: [`P5B2 semantic-release trust root`](receipts/p5b2-semantic-release-trust-root.md), binding the exact PR #66 through PR #69 chain. It grants no policy selection, durable decision store, semantic-field composition, omission, projection, new public command/schema/runtime receipt, provider/backend, publication, release, execution, parent completion, or successor authority. |
| P5B2 semantic-release policy-authority provisioning | P5B2 semantic-release bundle and deterministic-classifier trust-root | COMPLETE | The accepted frozen [`semantic-sync.md`](semantic-sync.md#p5b2-semantic-release-policy-authority-provisioning-prerequisite) boundary owns exact `ACTIVE` selection input and digest preimages, revision-plus-one/predecessor CAS, registry-then-workspace locking, fixed 256 KiB peak, durable commit/recovery, idempotency, and commit uncertainty. Completion evidence: [`P5B2 semantic-release policy authority`](receipts/p5b2-semantic-release-policy-authority.md), binding PR #71, PR #72, and PR #74. Acceptance provisions no live record; `SELECT_SEMANTIC_RELEASE_POLICY` cannot revoke or reactivate, and `REVOKED` is consumer-side fail-closed vocabulary. Decision binding, public surfaces, publication, and successor authority remain absent. |
| P5B2 semantic-release decision-store and capacity/GC | P5B2 semantic-generation promotion and pointer-finalization, P5B2 semantic-release policy-authority provisioning | COMPLETE | Accepted only at the frozen [`semantic-sync.md`](semantic-sync.md#p5b2-semantic-release-decision-store-and-capacitygc-prerequisite) boundary. It owns the private request-addressed binding namespace, one bounded non-authoritative publication-staging slot with exclusive first-boundary visibility and residue-only recovery, exact member sets and digest preimages, bounded capacity accounting, install-once/replay, commit-uncertainty, existing lock order, authoritative capacity scanning, shared-path GC blocking, and internal runtime composition with direct hostile and failure-injection tests. Completion evidence: [`P5B2 semantic-release decision store and capacity/GC`](receipts/p5b2-semantic-release-decision-store-capacity-gc.md), binding PR #76, PR #77, and PR #79. Acceptance provisions no live decision and grants no classification composition, canonical-state cleanup, repair, deletion, quarantine, rollback, public surface, publication, or successor authority. |
| P5B2 semantic-content release/DLP decision | P5B2 semantic-generation promotion and pointer-finalization, P5B2 semantic-release bundle and deterministic-classifier trust-root, P5B2 semantic-release policy-authority provisioning, P5B2 semantic-release decision-store and capacity/GC | WAITING | Contract freeze only in [`semantic-sync.md`](semantic-sync.md#p5b2-semantic-content-releasedlp-decision). Entry requires the exact accepted staged `PROMOTED` and visible-current terminal, implemented and accepted trusted bundle and policy-authority provisioning mechanism, a provisioned stable current `ACTIVE` operator policy-authority revision, and separately implemented and accepted decision-store/capacity/GC integration. No live record is provisioned; classification composition, omission execution, graph/query projection, public CLI/schema/runtime receipt, provider/backend, publication, implementation, acceptance, and successor authority remain absent. |
| Remaining P5B2 commands | P5B2 | WAITING | Full semantic sync, named/headless backend integration, migrate, every repair mode beyond the accepted public fenced pointer-repair lifecycle, every mutation beyond the accepted explicit GC and pointer-repair lifecycles, every query authority beyond P5B2c's one-shot transport, and every other command remain waiting. The accepted internal handoff, certification-finalization, promotion/pointer-finalization, and trust-root children and the release/DLP contract freeze grant no broader or public command authority. |
| P5C | P5B2 | WAITING | The broad service, installation, performance/resource, and publication parent is unchanged and is not promoted by the child split below. |
| P5C1 | P5B2b | COMPLETE | Accepted receipt: [`P5C1`](receipts/p5c1.md). Candidate-bound canonical runtime authority generation and isolated atomic installation/compensation proof only. |
| Remaining P5C concerns | P5C | WAITING | Watch/service, performance, shared-lock/root-traversal optimization, publication, retained query/service authority, and all other P5C work remain unchanged. |

P6-P12 are intentionally absent from this Graphify-local ledger. Their
cross-repository ordering remains in the external portfolio plan, and all
remain waiting at this handoff.

Statements in accepted boundary freezes below that a receipt promoted no later
child describe that receipt's authority at its acceptance point. They do not
override the current ledger. PR #54 froze the certification contract; PR #56
implemented it; PR #57 supplied required retained-`CERTIFIED` cleanup and race
corrections; PR #58 made only that corrected child's `COMPLETE` acceptance
canonical; PR #59 froze the promotion and pointer-finalization contract; PR #60
reconciled readiness; PR #61 exposed the cleanup-acquisition regression; PR #62
corrected that prerequisite; PR #63 delivered the internal finalizer; and
PR #64 completed the frozen rejection and terminal-proof coverage. PR #65 made
only that promotion/pointer-finalization acceptance canonical, and PR #66 froze
the encompassing semantic-content release/DLP decision without making it
ready. PR #67 reconciled only the separate semantic-release bundle and
deterministic-classifier trust-root prerequisite as `READY`; PR #68 implemented
that frozen boundary; and PR #69 supplied the required C1-control repair. This
PR #70 governance acceptance transitioned only that trust-root prerequisite to
`COMPLETE`. PR #71 then froze the separate policy-authority provisioning
prerequisite, PR #72 implemented it, and PR #74 corrected its lock discipline.
PR #75 made only that policy-authority acceptance canonical as `COMPLETE` and
activated no successor. PR #76 then froze the separate decision-store and
capacity/GC contract at `WAITING`; PR #77 delivered its implementation; and pull
request #79 corrected the final canonical-directory rebinding defect. This staged
governance closeout proposes only that prerequisite as `COMPLETE`. The encompassing
release/DLP decision and every downstream dependency remain `WAITING`. Parent P5
and P5B2 remain `IN_PROGRESS`, H3 remains `DEFERRED`, and no later successor is
activated.

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

## P5B2 semantic-generation certification finalization accepted boundary

This separate unnumbered P5B2 child is `COMPLETE` only at the frozen internal
boundary below. Merged PR #58 records the
[accepted completion receipt](receipts/p5b2-semantic-generation-certification-finalization.md)
as canonical. Acceptance binds PR #56's exact implementation together with
PR `#57`'s corrective delivery; PR #56 alone does not satisfy the final boundary.
The transition changes no parent phase, JOS row, or later successor status.

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

The sole cleanup exception is an exact staged `CERTIFIED` proof that still has
its paired request-bound `BUILD` lease and persisted staged-attempt digest. A
later invocation may adopt that digest only to reopen the same current-owner
grant, or replace it after normal expiry/reboot proof, verify the unchanged
terminal evidence, release the cleanup grant, and prove absence. It may not
recertify or rewrite certified evidence; foreign, changed, or ambiguous lease
authority fails closed.

The exact stop boundary is the same staged record durably reopened as
`CERTIFIED`, bound to its unchanged request and manifest plus the exact verified
generation receipt, immutable semantic certification binding, matching journal
event, cleared target reservation, unchanged pointer boundary, and proven
release of the recovery owner/fence. An exact terminal replay may only return
that same proof read-only after grant absence is proven. Reacquiring `BUILD` is
limited to the paired terminal-cleanup exception above. A `PROMOTED` target is
outside this child.

This acceptance closeout adds no product code, tests, schema, fixture,
dependency, configuration, workflow, generated Graphify output, public command,
runtime receipt, status field, content-release or DLP decision, graph/query
projection, promotion, pointer movement, provider/backend, credential/network
path, migrate, repair, GC, service/watch, publication, P5C, H3, P6+, parent
completion, successor readiness, or execution authority. P5 and P5B2 remain
`IN_PROGRESS`; H3 remains `DEFERRED`; remaining P5B2 and P5C remain `WAITING`;
no JOS row is activated or closed.

## P5B2 semantic-generation promotion and pointer-finalization accepted boundary

This separate unnumbered P5B2 child transitions from `READY` to `COMPLETE` only
at the frozen internal boundary below. The transition and
[accepted completion receipt](receipts/p5b2-semantic-generation-promotion-finalization.md)
were made canonical by merged PR #65. Acceptance binds the exact PR #59
contract freeze, PR #60 readiness reconciliation, PR #61 regression, PR #62
cleanup-acquisition correction, PR #63 implementation, and PR #64
acceptance-coverage completion. The transition changes no parent phase, JOS
row, or later successor status.

Its exact start boundary is the already accepted certification terminal for one
canonical request and target:

- the staged record is exactly `CERTIFIED` for the same repository, target,
  structural request, payload manifest, and verified receipt, with no pointer
  revision or abandonment evidence;
- the one installed target generation, generation receipt, payload inventory,
  coordination lock, and immutable semantic certification binding reopen and
  agree exactly;
- the lifecycle journal has the singular matching `CERTIFIED` event with
  pointer revision zero and no target promotion event;
- the target capacity reservation is absent, the visible pointer still equals
  the request's expected revision/current-receipt CAS, no pending pointer intent
  exists, and the certification `BUILD` recovery grant plus staged-attempt
  digest are absent; and
- current durable registry, active-source, certified-source, migration,
  operation, compatibility, policy, and request authority has not drifted.

Those absence and no-promotion clauses define fresh entry. Commit-unknown
recovery may substitute only the exact persisted promotion attempt plus its
live or expiry/reboot-replaceable grant and the matching target-bound
pending/visible pointer and `PROMOTED`/`REPAIRED` journal residue. The target
reservation and certification `BUILD` grant remain absent, and every request,
target, receipt, binding, and authority field remains unchanged.

Forward authority may be acquired only through
`GenerationStore.acquire_staged_recovery()` for that same request and target.
When no pointer intent exists, the accepted lane is `PROMOTE`: a new move may
use only the original complete pointer CAS with the exact certified target and
receipt, while an exact already-visible target with complete journal proof
performs no new move. When durable pending intent exists from that same move,
the accepted lane is `POINTER_RECOVERY`, but recovery is admissible only after
its locked plan is shown to derive from that exact pending or visible
target-bound residue.

Selecting an unrelated current, prior, last-good, arbitrary certified, newer,
or substituted generation is forbidden. A repaired pointer may use the exact
monotonic revision and `REPAIRED` event required by the existing store, but it
does not change the target or receipt and is not generic repair authority.

Exact current-target replay after the pointer is already durable performs no
new pointer move. Direct and recovery paths both require the existing registry,
source, migration, operation, fence, schema, pointer-revision, and
current-receipt checks and preserve registry-before-workspace plus
workspace-before-sorted-generation-lock order. Pointer intent, visible pointer,
journal, staged-state, and grant-release commit uncertainty is resolved only by
exact locked reread or same-attempt recovery. Presence or absence of one record
alone, a newer state, or an unrelated successful pointer is never inferred
success.

The terminal proof requires the same staged record durably `PROMOTED` with the
same request, target, manifest, and receipt; the visible pointer current bound
to that exact target and receipt; the staged pointer revision equal to the
visible revision and matching authoritative `PROMOTED` or `REPAIRED` journal
event; no pending pointer intent; unchanged installed payload, receipt, and
semantic certification binding; and a locked reread proving the exact promotion
owner/fence and staged-attempt digest absent after release. Only that exact
promoted current generation may later be offered as carried semantic-result
evidence to a separately authorized handoff. This contract makes no content
release, DLP, graph/query projection, or later-handoff acceptance claim.

Content release or DLP decisions, graph/query projection, changes to
`query_structural()`, public semantic-sync commands, schemas, runtime formats,
runtime receipts, providers/backends, credentials, networking, migrate, repair,
GC, service/watch, publication, P5C, H3, P6+, parent completion, acceptance
beyond this child, execution, and later-successor readiness are excluded. This
acceptance closeout adds no product code, test, schema, fixture, dependency,
configuration, workflow, or generated Graphify output.
P5 and P5B2 remain `IN_PROGRESS`; H3 remains `DEFERRED`; only this child
transitions to `COMPLETE`. Remaining P5B2 work and P5C work remain `WAITING`;
every later successor remains not `READY`.

## P5B2 semantic-release bundle and deterministic-classifier trust-root accepted boundary

This separate unnumbered P5B2 prerequisite is implemented and accepted only at
the frozen boundary below. Its
[accepted completion receipt](receipts/p5b2-semantic-release-trust-root.md)
binds the exact PR #66 through PR #69 chain. Acceptance is not content release,
publication, execution, parent completion, or successor authority.

Its exact bounded scope is the repo-owned installed
`graphify/workspace/semantic_release_manifest.json` plus only the
manifest-inventoried deterministic classifier implementation and byte-defined ABI,
closed taxonomy, normalization contract, ordered ruleset, required
explicit-evidence-only `core_secrets.v1`, and every selectable coverage-profile
artifact, plus the installed private `_graphify-semantic-authority` and
`_graphify-mcp-semantic-authority` source-executed bootstrap scripts whose POSIX
shell prelude starts installed
Python under `-S` isolation before Python startup hooks can run, then establish
a fresh package-external pycache prefix and suppress `.pth`, `sitecustomize`,
and automatic user-site startup imports before importing Graphify. The public
cross-platform console entry points are not semantic-release authority. Plain user
script installs may add the installed script-prefix package root, or a PEP 610
editable source root recorded by a `graphifyy` direct URL in that same script
prefix, explicitly after that startup boundary. The canonical semantic contract
already freezes the installed package root,
package-relative path grammar, descriptor-relative no-follow traversal,
single-link regular-file modes, byte counts and digests,
deterministic-pattern-only ABI, `utf8_lex_v1`, taxonomy/profile separation,
exact core-profile categories, hard caps, and fail-closed behavior. No product
choice remains that would require this subchild to invent operator policy
provisioning or decision-store behavior.

The subchild stops after internal installed-bundle validation, deterministic
factual classification over explicit already-canonical bounded UTF-8 bytes, and
the existing executable bootstrap needed to exclude package-local bytecode
caches and Python startup hooks. It does not choose active profiles, map
categories to policy dispositions, compose semantic-generation fields, read or
mutate workspace authority, install a decision binding, account capacity or GC,
execute omissions, project content, expose a new public CLI/schema/runtime
receipt, invoke a provider/backend, or publish. The acceptance receipt proves
only this frozen behavior.

P5 and P5B2 remain `IN_PROGRESS`; the policy-authority provisioning and
decision-store/capacity/GC prerequisites are separately accepted `COMPLETE`. The
encompassing release/DLP decision, live operator policy selection/provisioning,
classification composition, omission execution, projection, public surfaces,
provider/backend, publication, remaining P5B2 work, and P5C remain `WAITING`;
the trust-root and policy-authority provisioning prerequisites are `COMPLETE`;
H3 remains `DEFERRED`; no later successor is `READY`.

## P5B2 semantic-release policy-authority provisioning accepted prerequisite

This separate internal unnumbered P5B2 prerequisite is implemented and accepted
as `COMPLETE` at its frozen private boundary. Completion evidence is the
[`P5B2 semantic-release policy-authority` receipt](receipts/p5b2-semantic-release-policy-authority.md),
binding PR #71, PR #72, and PR #74.
It owns only the `SemanticReleasePolicyAuthorityStore` mechanism and the three private
current/previous/pending policy-authority paths frozen in the canonical
[semantic contract](semantic-sync.md#p5b2-semantic-release-policy-authority-provisioning-prerequisite).
It provisions no live authority record and selects no release context, profile
set, coverage-sufficiency value, policy mapping, or operator record.

The sole mutation authority is an exact
`SELECT_SEMANTIC_RELEASE_POLICY` envelope that results in an `ACTIVE` record.
Genesis requires absent state and revision `0`/digest-null CAS; advancement
requires the exact stable current `ACTIVE` revision and complete-record digest,
increments by exactly one, and names the reopened current digest as predecessor.
At higher revisions, retained previous must be the exact revision-minus-one
record named by current. `REVOKED` remains closed consumer vocabulary that
causes release rejection; this prerequisite has no revocation or reactivation
action and may not use selection authority to produce either transition.

Stable read and read-only recovery projection hold shared registry then shared
workspace locks and revalidate the applicable current/previous/pending snapshot
before returning; stable read also requires pending absent and an exact chain.
Mutation and exact recovery hold the exclusive forms of the same lock pair,
revalidate the registered repository, installed bundle, canonical candidate,
CAS, and predecessor chain, and delegate exact candidate bytes to the existing
`DurableStateRoot` protocol: durable pending, exact-current retention as
previous, current installation, then pending clear. Each stable record is at
most 64 KiB, the selection envelope is at most 16 KiB, and the fixed three-file
namespace plus one atomic temporary has a hard 256 KiB transaction peak.
Failure after pending may be visible is `CommitUnknown` and permits only exact
recovery under the same locks. Byte-identical completed replay is a no-write
success; divergent, stale, corrupt, skipped, foreign, or `REVOKED` pending
state blocks rather than granting cleanup or repair.

Authorized lifecycle operations are limited to stable read, genesis `ACTIVE`
selection, monotonic `ACTIVE` advancement, read-only recovery projection,
exact transaction recovery, byte-identical replay, exact recovered-pending
clear, and bounded orphan-temporary cleanup. It grants no revocation,
reactivation, rollback, downgrade, arbitrary repair, current/previous deletion,
policy-authority GC, public CLI/schema/runtime receipt, decision-store mutation,
classification composition, omission, projection, provider/backend,
publication, parent completion, or
successor authority.

P5 and P5B2 remain `IN_PROGRESS`; the trust-root, policy-authority provisioning,
and decision-store/capacity/GC prerequisites are accepted `COMPLETE`. The
encompassing release/DLP decision, classification composition,
omission, projection, public surfaces, provider/backend, publication, remaining
P5B2 work, and P5C remain `WAITING`; H3 remains `DEFERRED`; no later successor
is `READY`.

## P5B2 semantic-release decision-store and capacity/GC accepted prerequisite

This separate internal unnumbered P5B2 prerequisite is implemented and accepted
as `COMPLETE` only at its frozen private boundary. Completion evidence is the
[`P5B2 semantic-release decision-store and capacity/GC` receipt](receipts/p5b2-semantic-release-decision-store-capacity-gc.md),
binding PR #76's contract freeze, PR #77's implementation delivery, and PR #79's
canonical-directory correction. The canonical
[semantic contract](semantic-sync.md#p5b2-semantic-release-decision-store-and-capacitygc-prerequisite)
gives `SemanticReleaseDecisionStore` sole ownership of the private
request-addressed namespace, exact binding member sets and digest preimages,
bounded capacity integration, install-once/replay and commit-uncertainty rules,
existing lock-order integration, and nonempty-state generation protection. This
accepted prerequisite provisions no live policy or decision record and grants no
classification or terminal release-decision authority.

The frozen namespace is exactly
`workspaces/<repository_uuid>/semantic-release-decisions/<generation_id>/<decision_request_sha256>.json`
beneath the external workspace root. Mode-`0700` directories and one single-link
mode-`0600` canonical binding use descriptor-relative no-follow traversal.
Generation and complete canonical decision-request digest are validated identity,
not caller paths. Unexpected, alternate, unsafe, foreign, duplicated, or
ambiguous entries fail closed. The binding is outside the sealed generation and
never becomes a public schema, runtime receipt, journal event, staged-build
state, generation receipt, or publication artifact.

The exact format-version-1 top-level and nested member sets, canonical ordering,
field-value digest preimage, `full_result_sha256` preimage, and completed-binding
`binding_sha256` preimage remain frozen in `semantic-sync.md` and
`state-contract.md`. The binding never contains its own digest and contains no
raw semantic prose, matched substring, generated explanation, confidence score,
public source location, provider response, or credential. Request addressing
prevents different authority revisions or complete requests from overwriting or
substituting for one another.

Hard bounds are 25 MiB per binding, 64 bindings per generation, and 4,096 per
workspace. The store performs bounded no-follow enumeration before
classification and again immediately before install. Binding counts use those
fixed independent caps, not new or repurposed `CapacityPolicy` fields.
Decision-store bytes are included in existing global/workspace byte ceilings and
filesystem-reserve calculations while existing unconsumed durable byte
reservations remain charged in that arithmetic. Capture uses shared registry,
exclusive workspace, then shared target-generation locks;
final install uses exclusive registry, exclusive workspace, then the same shared
generation lock. Under the final retained composition the store revalidates the
request-derived path, exact canonical bytes/digest, namespace shape,
global/workspace counts and bytes, capacity ceilings, durable reservations,
filesystem reserve, and GC eligibility state before install-once and exact reopen.

Identical concurrent requests converge on the same canonical bytes. A
byte-identical completed replay is no-write success; same-path different bytes
conflict; distinct requests use distinct paths. Failure is definite no-commit
only before possible binding visibility. After possible visibility, exact
reopened bytes adopt the commit; proven absence may retry only while request,
candidate bytes, authority, and capacity proof remain exact. Partial, unsafe,
unreadable, different, or ambiguous state is commit-unknown and fails closed.

Any nonempty decision state aborts the shared workspace reachability proof
before a successful GC preview or plan and therefore blocks downstream execute,
reconcile, and purge. A safely observed absent top-level
namespace is the zero-binding initial state; once present, unreadable, unsafe,
ambiguous, or drifted state fails closed. The prerequisite adds no public
protection-reason token and grants no GC mutation, deletion, cleanup,
quarantine, repair, rollback, compaction, decision-request or
full-result composition, classifier/policy reduction, omission, redaction,
projection, query, public CLI/schema/runtime receipt, provider/backend, network,
publication, release, parent completion, or
successor authority.

P5 and P5B2 remain `IN_PROGRESS`; the trust-root, policy-authority provisioning,
and decision-store/capacity/GC prerequisites remain `COMPLETE`. Live operator
policy selection/provisioning, the encompassing release/DLP decision,
classification composition, omission, projection, public surfaces,
provider/backend, publication, remaining P5B2 work, and P5C remain `WAITING`;
H3 remains `DEFERRED`; no later successor is `READY`.

## P5B2 semantic-content release/DLP decision contract freeze

This encompassing proposed separate unnumbered P5B2 child remains `WAITING`,
not `READY` or `COMPLETE`, and has no implementation or acceptance receipt. It
consumes the accepted trust-root, policy-authority provisioning, and
decision-store/capacity/GC prerequisites above but still depends on a provisioned
stable current `ACTIVE` operator policy-authority record, classification composition,
and the remaining frozen prerequisites.
Freezing the contract changes no accepted receipt, parent phase, JOS row,
execution authority, or later-successor status.

For the encompassing decision contract freeze, the operator selected the
following normative policy semantics: a policy-restricted node or hyperedge
label rejects the release; a
policy-restricted optional node rationale is omitted; ambiguity, drift,
classifier failure, policy-evaluation failure, missing coverage, and unknown or
unmapped categories reject; otherwise a field may be allowed only under the
exact selected coverage-sufficiency declaration. These decisions define the
future policy contract but are not themselves a provisioned runtime authority
record or acceptance receipt. Classifier facts and policy dispositions remain
separate.

Its sole entry is the accepted promotion terminal as one unchanged composite:
exact staged `PROMOTED` request/target/manifest/receipt and pointer authority;
the same visible current and authoritative promotion/recovery journal; no
pending pointer or journal recovery; unchanged installed payload, retained
handoff, target-owned semantic-input bytes, coordination lock, and immutable
semantic certification binding; exact promotion-grant absence; and current
registry, active-source, migration, operation, compatibility, state-schema,
queue-policy, and certified-source authority. A historical generation, one
constituent artifact, or any drift is not entry authority.

One canonical `decision_request_sha256` binds that entry plus the exact private
semantic-input byte count and digest, the eligible-field inventory, the trusted
repo-owned installed bundle manifest owned by the trust-root prerequisite, and
the stable current `ACTIVE` operator policy-authority revision produced only by
the separately accepted provisioning prerequisite. No such live record is
provisioned by acceptance. The decision child
consumes but cannot supply, select around, override, or weaken that installed
bundle. The separately owned policy-authority record binds the named release context, exact selected
profiles and policy, the closed version-1 coverage-sufficiency declaration and
its digest, monotonic revision, predecessor digest, and operator selection
envelope. The declaration's release context and selected-profile set must
equal the surrounding authority record exactly; `INSUFFICIENT`, mismatch, or
invalidity rejects. That envelope binds the authority-body digest to the
existing five operator fields while explicitly adding only the internal
`SELECT_SEMANTIC_RELEASE_POLICY` action, which authorizes only an `ACTIVE`
selection or monotonic `ACTIVE` advancement. `REVOKED` is consumer-side
fail-closed vocabulary; neither this child nor the provisioning prerequisite
authorizes revocation or reactivation. Older revisions and their bindings are
historical candidates only. `core_secrets.v1` is mandatory for every
allow-capable policy. No provider, backend, model, credential, network, live
catalogue, environment variable, ambient default, or implicit fallback may
select or modify decision authority. Taxonomy, classifier implementation,
ruleset, normalization, profiles, and policy retain distinct identities,
versions, canonical bytes where applicable, and digests.

Installed manifest paths are unique sorted POSIX relative-normal-form beneath
the canonical `graphify` package root. Descriptor-relative no-follow traversal
rejects absolute, dot/dotdot, empty, repeated-separator, backslash, alias,
symlink, hard-link, special-file, unsafe-mode, containment, size, or digest
disagreement; artifacts are single-link regular mode `0444` or `0644` files.

The version-1 deterministic-pattern-only, byte-defined classifier reports only
`NO_MATCH`, `MATCH` with `utf8_lex_v1`-sorted unique factual category IDs, or
`INDETERMINATE`. `utf8_lex_v1` compares unsigned canonical UTF-8 bytes,
shorter-prefix-first, without locale, Unicode collation, or runtime text
ordering; selected profiles and private rule IDs use the same comparator, and
complete field results use the canonical contract's fixed field ordering. It
scans
required node labels, present optional node rationales, and required hyperedge
labels at the existing 16 KiB UTF-8 field bound and exact canonical
normalization. There is no hyperedge rationale in the accepted schema.
It does not consult runtime Unicode categories, locale, or host-dependent text
behavior; only syntax-defined ASCII names use the ABI-defined ASCII fold.
Entropy-only guesses, confidence scores, free-form classifications, generated
explanations, and contextual confidentiality inference are excluded. `NO_MATCH`
is not a safety claim; an
allow outcome additionally requires an exact policy declaration that the
selected profile set is sufficient for the named release context.

`INDETERMINATE` rejects. `NO_MATCH` produces `ALLOW_FIELD` only under that exact
coverage declaration. For `MATCH`, policy maps every
`(field_type, category_id)` pair to `ALLOW_FIELD`, `OMIT_RATIONALE`, or
`REJECT_RELEASE`; an unknown or unmapped pair rejects, and rejection takes
precedence over omission and omission over allow. Label omission is invalid,
so a restricted node or hyperedge label
rejects the release. A restricted optional node rationale may be omitted. The
aggregate outcome is exactly `ALLOW_UNCHANGED`, `ALLOW_WITH_OMISSIONS`, or
`REJECTED`. Deterministic redaction, label removal, entity pruning, ID remapping,
and topology rewriting remain outside the child.

A future encompassing implementation may consume only the separate prerequisite's
private canonical
format-version-1 release-decision binding at
`workspaces/<repository_uuid>/semantic-release-decisions/<generation_id>/<decision_request_sha256>.json`.
The external mode-`0700` namespace and single-link mode-`0600` file use
descriptor-relative no-follow access and are not added to the sealed generation.
The binding commits to authority/input/profile/policy digests, complete bounded
field results, dispositions, and terminal outcome. `full_result_sha256` excludes
both digest members; `binding_sha256` is computed over completed binding bytes
and is never stored recursively. It duplicates no semantic prose,
matched substring, confidence score, or public source location. The binding is
not a public or runtime receipt, lifecycle journal event, staged-build state,
generation receipt, or publication artifact.

The encompassing decision retains hard version-1 limits of 64 KiB per decision
request and policy-authority record, 1 MiB for the installed manifest, 25 MiB
total referenced bundle bytes,
64 selected profiles, 4,096 categories, 4,096 rules, 256 UTF-8 bytes per
classifier-related ID, 30,000 eligible fields, 256 category and rule IDs per
field. The separate prerequisite owns the 25 MiB-per-binding,
64-bindings-per-generation, and 4,096-bindings-per-workspace limits plus
decision-store capacity and reserve accounting. Exceeding or failing to prove
any applicable limit rejects release.

Composition with the separate store prerequisite follows
capture-classify-revalidate-install: capture the exact
entry, current policy authority, installed bundle, store capacity, and bounded
private bytes under shared registry, exclusive workspace, then shared
target-generation locks; store only their exact byte count and digest in the
decision request and classify those same captured bytes outside coordination
locks; then acquire
exclusive registry, exclusive workspace, and shared target-generation locks in
that order for global/workspace capacity and reserve revalidation, exact
semantic-input reread, install, and binding reopen. Identical concurrent requests
converge; a same-path different-byte result conflicts. Proven absence may retry
only while the full request remains exact. Unsafe, unreadable, partial,
ambiguous, different, or drifted state is commit-unknown and fails closed. No
new lease, journal transition, lifecycle state, inferred cleanup, destructive
rollback, or rewrite authority exists.

Neither prerequisite nor decision child deletes bindings. The accepted
decision-store/capacity/GC prerequisite makes any nonempty generation decision
directory protect that generation from purge. This is a retention constraint,
not cleanup or GC authority.

Terminal proof takes shared registry, exclusive workspace, then shared
target-generation locks; reopens the still-current exact promoted terminal, decision
request, current `ACTIVE` policy authority, and private binding; and revalidates
all entry, request, authority, input, result, and binding coordinates before
releasing any lock. It proves equal classifier/taxonomy/profile/policy/input/
result digests, bounded counts, and terminal outcome. An
`ALLOW_WITH_OMISSIONS` proof contains no entity/field locator or value digest;
those remain exclusively in the private binding. A later pointer or authority change
leaves the binding as historical private evidence but removes release
authority. Any future projection or publication consumer must name the exact
generation/request/binding-digest tuple, independently reopen the entire proof,
and prove the bound policy revision is still current `ACTIVE`; enumeration or
newest/historical selection is not authority.

This freeze adds no product code, tests, schema file, fixture, dependency,
configuration, workflow, generated Graphify output, receipt, public command,
status field, omission execution, graph construction/merge/query projection,
`query_structural()` change, provider/backend/model or credential path,
networking, migrate, repair, GC or binding cleanup, service/watch, publication,
production/runtime installation, performance/resource qualification, P5C, H3,
P6+, parent completion, implementation, readiness, acceptance, execution, or
later-successor authority. `JOS-SEMANTIC-RATIONALE-PROJECTION` remains
`OPPORTUNISTIC` with its trigger unchanged. P5 and P5B2 remain `IN_PROGRESS`;
the separate trust-root, policy-authority provisioning, and
decision-store/capacity/GC prerequisites are accepted `COMPLETE`. This
encompassing child, live operator policy selection/provisioning, classification composition, omission execution,
projection, public surfaces, provider/backend, publication, remaining P5B2 work,
and P5C remain `WAITING`; H3 remains `DEFERRED`; no later successor is `READY`.

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
