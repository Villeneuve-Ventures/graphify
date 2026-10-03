# Graphify consolidation delivery status

Status reconciled on 2026-10-02 against v8
`920ffe9c0c186d80b2ac8f1096b1236123af1862`, with S1–S4 merged.
The [original consolidation plan](graphify-product-consolidation-2026-09-18.md)
and [structural design](graphify-v8-structural-workspace-design.md) retain their
historical planning claims. This status record does not authorize successor
implementation, release, migration, installation, or consumer adoption.

## Delivered slices

| Slice | Delivery | Merge commit |
| --- | --- | --- |
| S1 — engine seams | Delivered by the engine-seams task through [PR #145](https://github.com/Villeneuve-Ventures/graphify/pull/145) | `14934e8ca263ae876724abdd2a83342a87f05cda` |
| S2 — structural contracts and candidate composition | Delivered by the separate contracts task through [PR #149](https://github.com/Villeneuve-Ventures/graphify/pull/149) | `af72150524c791db2d13d55bd762604a739a8995` |
| S3 — lifecycle stores and storage separation | Delivered through the integration stack ending in [PR #164](https://github.com/Villeneuve-Ventures/graphify/pull/164) | `d7f6972be1ce62674bbb96c77e8c2d626952fe4b` |
| S4 — v8 adapter and structural library round trip | Delivered through [PR #167](https://github.com/Villeneuve-Ventures/graphify/pull/167) | `45c7529811006e6fe8dc6d3d3137a59ad8b589de` |

S3 provides lifecycle stores, pointer transitions and recovery, internal GC,
structural-policy enforcement, and ordinary-write refusal for managed state.
The operational adapter and structural library orchestration merged through
PR #167 on 2026-09-29 UTC. The [S4 implementation and validation record](graphify-v8-s4-result.md)
retains its revision-specific historical evidence; S4 delivery does not establish
S7 release qualification. The final S3 integration
branch and its completed repair checkout were retired after merge; S2's former
contracts branch is a historical delivery reference, not a current assignment.

## Batch status and next work

The batch and capability IDs retain their meanings from the original plan.

| Batch | Status | Remaining endpoint |
| --- | --- | --- |
| B1 / T3f, T2a; preserve T6–T10 | S1 and S2 merged | Later exact-candidate and aggregate qualification belongs to S7; these merges do not establish release certification. |
| B2 / T3a–c, T3f, T3j | S3 and S4 merged | B2 implementation delivered; later exact-candidate and aggregate release qualification belongs to S7. |
| B3 / T3d–f, T2a | Incomplete | S5 public one-shot commands and diagnostics, then S7 exact-candidate aggregate proof. |
| B4 / T3g–i, T11f | S3 core recovery and GC safety dependencies merged; S6 transports incomplete | Public maintenance transports; an optional legacy reader only if D3 selects that option. |

S4 has a separate [implementation and historical validation record](graphify-v8-s4-result.md).
S5 is the next slice after the merged S4 library delivery, and is not activated
by this record. S6 maintenance
work can start after S3, with public dispatch integration depending on S5, as
specified in the design. Later implementation owners remain unassigned; no
successor task is activated by this status update.

Overall consolidation remains in progress. Later capability batches remain
outside the bounded structural design, and final consolidation verification
has not been completed.

## Revision-specific evidence

- The [S3 acceptance receipt](graphify-v8-s3-result.md#september-26-acceptance-receipt)
  records **6,618 passed, 41 skipped, and 235 subtests passed** in 792.03 seconds
  at `d4329ea88d957f6f2a1c746219b27723a416787f`.
- The [subsequent generation-recovery correction](graphify-v8-s3-result.md#post-receipt-generation-recovery-correction)
  records **15 capacity acceptance tests plus 13 focused tests** for
  `26905efef580010cf516c3bd3cf047c4672624e1`. The local full suite was not rerun
  for that correction; the earlier full-suite result does not cover it.
- The S3 merged tree exactly matches reviewed PR head
  `153b7c49476ade343ecb8682076c93260deb8110`. Hosted Python 3.14 tests, skillgen,
  security scan, macOS Leiden smoke, CodeQL, and CodeRabbit checks passed on that
  head before merge. Skipped draft jobs are not counted as passing tests.
- [PR #167](https://github.com/Villeneuve-Ventures/graphify/pull/167) merged S4 into
  v8 at `45c7529811006e6fe8dc6d3d3137a59ad8b589de` on 2026-09-29 UTC.
  The [S4 result](graphify-v8-s4-result.md) records the implementation and its
  revision-specific local checks, including installed-package and cold-query
  coverage. This merge does not complete S5–S7 or qualify a release candidate.
- These are historical delivery results. Publishing this status record does
  not constitute a new runtime test or acceptance run. Raw local logs remain
  session-local; the linked result document records their scope and revisions.

## Preserved decisions and limits

D1 security enforcement and D2 governed semantic release remain deferred.
D3 old certified-state access remains unresolved: it governs the optional S6
legacy reader and S7 release compatibility promise, and does not block S4
new-format work in disposable roots.

S5 public commands, S6 maintenance transports, S7 aggregate candidate proof,
and overall consolidation remain incomplete. Native power-loss and hostile
concurrent-rename proof remain unestablished. Aletheia first-use acceptance,
other repository enrollment, global installation changes, migration, and
workspace/v1 retirement remain separate later decisions.
