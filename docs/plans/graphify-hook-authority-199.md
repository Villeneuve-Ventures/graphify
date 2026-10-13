# Issue #199 acceptance extension

Baseline: v8 `a60c3eb67283f66f0f25d114455a5dcd30e2b190`, plus the separately
validated #198 v2 authority implementation. Scope: local archive and reference
retirement, one direct owner. No physical stage removal or live reconciliation.

## Review before implementation

The operator selected this implementation after #198 and extension review.
Direct owner review checked current history references, completion obligations,
retained preimages, stage exclusion and shared authority limits before code
changes. No independent review or protected-change designation is claimed.
The review distinguished reference retirement from physical deletion: the current
rename primitives do not perform conditional removal of an authenticated source.

### Deferred-feedback extension review

The operator subsequently requested implementing the items deferred from #208
on #209. Before implementation, direct owner review compared #209 `c45f3466`,
#208 `821d1235`, the scope comment, live #199, and the completed-history checks.
The review selected continuous admission checks for completed v2 manifests:
status is read-only, while ordinary install/uninstall and retirement refuse
changed or incomplete historical payloads before mutation. Missing v2 inventory
is unverified legacy evidence, never reconstructed. V1 keeps its original
admission contract and remains ineligible for retirement. This is the reviewed
acceptance extension for that stronger policy, not an independent-review receipt.

The review also requires carrying #208's producer fences and diagnostics into
the dependent source. Candidate names remain hints, and unpublished/unclaimed
artifacts must remain visible beside active, retained and completed observations.
Native account/root checks prove refusal and preservation only; they approve
no transfer, migration, cleanup or broader #196 qualification. Local base
integration is authorized by the new repair request; publication remains separate.

### Completion preservation repair review

Before repairing the metadata gap at #209 `25cf4715`, direct owner review checked
the existing A1/A3/A4 obligations. Capture admitted completion metadata separately
from legacy authority signatures, bind it to preview and durable maintenance,
and verify it against archive evidence. New maintenance uses reader contract 3
so earlier readers refuse these obligations. Older archives or pending records
without preservation evidence remain unverified and intact; no current metadata
is backfilled into them. This repairs the selected contract and approves no
transfer, migration, restoration or removal choice.

## Selected contract

- Maintenance is explicit: read-only preview, then execution naming the exact
  completed batch and the preview binding. Revalidate authority, references and
  source inventory under the existing nonblocking lifecycle locks.
- A valid v2 completed manifest is required. Active work, pending completion,
  unsupported legacy evidence, missing/replaced stages, altered exclusion,
  unknown members and uncertain reachability refuse without cleanup.
- Retain complete preimages, actual displaced originals, completion envelopes,
  original preservation metadata and their reference closure. Older completion
  envelopes contain history references; retaining only the latest is insufficient.
- Archive file bytes under private current-user storage. Original UID/GID,
  mode, ACL/xattrs/flags and identity are retained as evidence, not fabricated as
  the archive object's native identity. Archival grants no restoration authority.
- Persist archive ownership before copy, preserve partial archives on failure,
  and validate the complete archive before its durability fence. Only then may
  a sealed retirement transition replace the original-stage presence dependency.
- Preserve original stage paths and all their objects. A valid archived record
  permits original-stage absence; a foreign replacement remains unowned and
  blocks mutation. Missing archive evidence also blocks continuation.
- Retry an interrupted operation only with its authenticated maintenance request
  and exact objects. Neither status nor an error assumes that persistence failed
  or succeeded. Copied payloads alone cannot establish archive ownership.
- Maintenance validates retained historical evidence; it does not require live
  hooks to equal an older batch's successors or borrow its publication request.
- Admission checks every present completed v2 stage against its recorded closed
  inventory, including status and ordinary mutations. Damaged historical evidence
  is unverified, not a recoverable active request. A valid archived record may
  still admit an absent original, and any present original must match. Missing
  manifests or preservation fields refuse without adoption or reconstruction.
- Keep v1 history usable under its existing contract but ineligible for automatic
  retirement. No account transfer, root migration, automatic expiration or purge.
- Report retained stage count, archive bytes and incomplete artifacts. No automatic
  eviction or numeric capacity promise. Allocation/I/O failure preserves evidence.
  Reference retirement leaves physical stages and does not bound total disk use.

## Acceptance matrix

| ID | Required evidence |
| --- | --- |
| 199-A1 | Read-only API/CLI preview, bound exact selection, stale preview refusal, multiple completed batches and complete reachable history. |
| 199-A2 | Active/completion obligations and all missing/substituted/foreign/legacy inventory cases refuse; hooks, registration and unowned objects remain exact. |
| 199-A3 | Kill/I/O failure after archive creation, every copy, manifest/flush and reference transition; partial copies retained and retry authenticated. |
| 199-A4 | Successful archive has complete bytes and provenance, original files unchanged; absent original accepted only after durable retirement, replacement refused. |
| 199-A5 | Default/custom/Husky/shared hooks, authority-root/account changes, Git exclusion and native multi-account claims use qualified evidence. |
| 199-A6 | Status remains read-only; storage failure causes no eviction, stage movement, removal or preimage purge. |
| 199-A7 | Changed/missing preimages, exclusion damage, extra or unsafe members in any v2 completion are unverified in API/CLI status and block ordinary mutations/retirement with exact preservation; v1 stays compatible. |
| 199-A8 | Integrated #208 completion fence, ACL/label guards and active-location checks remain effective; candidate/unclaimed warnings coexist with pending and retained summaries without weakening ownership. |
| 199-A9 | Shared hooks, distinct authority roots and a native different-account attempt refuse adoption while preserving hooks, registration, history, archives and uncertain artifacts; platform/account limits are explicit. |

Run focused maintenance checks separately from #198, then relevant integration
and repository gates. Native evidence gaps remain explicit. The endpoint is a
validated local diff; no commit, push, GitHub merge, cleanup or issue closure is
authorized. The reviewed local base integration remains uncommitted.

The added native account check is explicitly enabled on the owned Linux guest.
It uses an issuer at UID 501 and a real root process at UID 0, with both retained
and archived history. It checks authority refusal and exact preservation through
status, preview, execution and ordinary operations. This is not nonprivileged
account-to-account or macOS account qualification. Those broader claims, account
transfer and enforcing SELinux policy remain outside this extension.

## Implementation handoff

#199 depends on the #198 v2 issuance and closed-inventory boundary. It owns
`_stage_inventory`, `_completion_closure`, `_validate_archive`, `_copy_archive`
and explicit `retire` in `graphify/hook_installation.py`, the public wrapper in
`graphify/hooks.py`, CLI dispatch/help and `tests/test_hook_stage_retirement.py`.
It extends the compatible status summary and advances the reader contract to 2
before maintenance intent in the original implementation, and to 3 for completion
preservation obligations. The separate #198 acceptance remains in force.

Physical deletion, total-storage quotas, expiration, restoration, account/root
transfer and legacy-history migration remain unresolved operator choices. None
is approved by this local archive implementation. Broader native multi-account
qualification also remains separate; injected account fields are not that proof.
