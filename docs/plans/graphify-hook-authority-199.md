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

Run focused maintenance checks separately from #198, then relevant integration
and repository gates. Native evidence gaps remain explicit. The endpoint is a
validated local diff; no push, merge, cleanup or issue closure is authorized.

## Implementation handoff

#199 depends on the #198 v2 issuance and closed-inventory boundary. It owns
`_stage_inventory`, `_completion_closure`, `_validate_archive`, `_copy_archive`
and explicit `retire` in `graphify/hook_installation.py`, the public wrapper in
`graphify/hooks.py`, CLI dispatch/help and `tests/test_hook_stage_retirement.py`.
It extends the compatible status summary and advances the reader contract to 2
before maintenance intent. The separate #198 acceptance remains in force.

Physical deletion, total-storage quotas, expiration, restoration, account/root
transfer and legacy-history migration remain unresolved operator choices. None
is approved by this local archive implementation. Broader native multi-account
qualification also remains separate; injected account fields are not that proof.
