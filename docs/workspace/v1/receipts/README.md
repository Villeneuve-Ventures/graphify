# Workspace governance receipts

After the publication gate in [`../governance.md`](../governance.md) activates,
accepted receipts here are append-only evidence for Graphify-local `COMPLETE`
transitions. Until then, the external execution checklist retains
receipt-acceptance authority and these files are proposed migration records. A
later correction adds a superseding receipt; it does not rewrite an accepted
one. An implementation change may propose evidence, but acceptance and the
corresponding status transition require a separate governance-only closeout
from the verified canonical branch.

## Accepted receipts

- [P5B2a ADOPT pre-write correction](p5b2a-adopt-prewrite-correction.md)
- [P5B2 identity maintenance](p5b2-identity-maintenance.md)
- [P5B2 active-source activation](p5b2-active-source-activation.md)
- [P5B2 exact-last-good rollback](p5b2-exact-last-good-rollback.md)
- [P5B2 retained-source identity continuity](p5b2-retained-source-identity-continuity.md)
- [P5B2 bounded offline-GC preview](p5b2-offline-gc-preview.md)
- [P5B2 public fenced offline-GC lifecycle](p5b2-offline-gc-lifecycle.md)
- [P5B2b provider-neutral structural sync](p5b2b.md)
- [P5B2c one-shot certified workspace query](p5b2c.md)
- [P5C1 candidate runtime authority and isolated installation proof](p5c1.md)

## Historical evidence before this index

Earlier receipts remain preserved in the former host-local
`graphify-workspace-execution-checklist.md` outside this repository.

Its pre-staging SHA-256 is
`ddf873a889ec5ad43b35762ea372605555a322f10e61467dba0a57271c9c2d51`.
That file remains active for Graphify-local status and receipt acceptance until
the publication gate activates. After activation it is historical for those
surfaces; its prior starting SHAs, worktree records, and evidence remain
unchanged below its conditional migration notice.
