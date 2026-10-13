# Issue #198 acceptance extension

Baseline: v8 `a60c3eb67283f66f0f25d114455a5dcd30e2b190`.
Scope: local implementation, one direct owner. No publication, live reconciliation,
account/root transfer, physical cleanup, Windows extension or successor authority.

## Review before implementation

The operator selected local implementation of #198, then #199, after extension
review. A direct owner review checked the extension against the live issue,
`hook_installation.py`, recovery documentation and installer tests before code
changes. This is not an independent review or a protected-change designation.
The review corrected a creation-provenance gap: successful mkdir followed by an
open in an untrusted parent cannot authenticate the opened directory. New stages
must be constructed beneath admitted private authority before exposure to hooks.

## Selected contract

- New authority slots use `graphify.hook-installation.v2`. Construct a complete
  unpublished slot beneath the admitted authority root, persist capability and
  authenticated journal, then publish with no replacement. Preserve incomplete
  candidates; never adopt them because of their names or apparently valid bytes.
- Existing v1 stores retain their supported exact-request behavior. No automatic
  conversion, re-signing or migration. Unknown formats and incomplete canonical
  stores refuse. Old readers refuse v2 rather than accepting a stale v1 view.
- V2 stage construction requires authority and hooks on the same filesystem.
  Refuse when this is unavailable; no cross-filesystem copy fallback. This is a
  new-writer support limit, not a claim that existing v1 needs that prerequisite.
- Bind authority parent, slot, capability and journal object identities, hooks,
  original request, operation, selected hooks, rendered plan and stage identity.
  Bind the issuing account UID; changing file ownership does not transfer it.
  A pathname, prefix, digest or copied journal never replaces those bindings.
- Persist PREPARING ownership before payload creation. Preserve incomplete
  attempts and all unrecorded children. A fresh attempt requires unchanged
  original hook signatures and the exact original request, and relinquishes
  only the old attempt's publication right; it removes no artifacts.
- Persist complete payload/exclusion evidence and EXPOSING intent before moving
  the authenticated directory into hooks. Exposure uses no-replace rename and
  accepts only the recorded object at one legal location. After flushing both
  directories, persist PREPARED. Only then may existing per-file publication run.
  Recheck complete prepared inventory before exposure and exclusion/allowed
  members before each publication, including mixed-state interrupted retries.
- Complete v2 history records include the exact final stage inventory for the
  separate #199 extension. They add no cleanup authority.
- Apply the 16 MiB format limit to every v2 envelope before writing it,
  including completion obligations. Refusal leaves authenticated pending
  evidence; it must not report successful completion with an unreadable guard.
- Public status resolves existing paths only and writes no durable state.
  Pending, unavailable, unverified, retained-incomplete and completed evidence
  remain distinct. Status never repairs or creates missing directories.
- Keep UID/GID/metadata checks, exact request binding, nonblocking hooks/common
  directory locks, existing completion obligations and separate registration.
  This does not repair missing/torn completion authority after publication.

## Acceptance matrix

| ID | Required evidence |
| --- | --- |
| 198-A1 | Kill/I/O failure at slot creation, capability, journal and canonical publication; no incomplete canonical slot accepted, no residue adopted or deleted. |
| 198-A2 | Trusted stage birth and recorded identity; substituted authority, source, destination and copied journal refuse with exact preservation. |
| 198-A3 | Kill/failure at exclusion, every payload/metadata write, flush, exposure and PREPARED persistence; exact retry or explicit preserved uncertainty. |
| 198-A4 | Incomplete attempt retained before fresh preparation; changed request or original hook blocks retry, all partial/foreign children remain unchanged. |
| 198-A5 | API and CLI status on missing hooks/authority and damaged evidence; intercept durable mutations as well as compare bytes, identities and file sets. |
| 198-A6 | Three/five hooks, install/uninstall, no-op, default/custom/Husky/shared/linked paths and v1 compatibility retain existing guarantees. |
| 198-A7 | Native qualified macOS/APFS and Linux/ext4 evidence for host-specific claims; injected process/I/O faults are not power-loss proof. |

Required local checks follow the current repository gates. Native support gaps
must be reported explicitly. #196 qualification, #199 maintenance, merge
finalization and S6 GC remain separate. The endpoint is a validated local diff.

## Implementation handoff

The #198 boundary owns `_Store` issuance/serialization, `_open_store`, private
`_prepare_authenticated`, `_expose_stage`, pending-inventory checks and closed
completion inventory in `graphify/hook_installation.py`. It also owns the
read-only hooks-path/status changes in `graphify/hooks.py` and its separate
`tests/test_hook_authority_lifecycle.py` matrix. Existing installer and merge-
guard tests remain integration proof. #199 receives a v2 completed inventory and
immutable completion references; #198 grants no archival or removal authority.

Cross-filesystem issuance, v1 migration, old incomplete canonical slots and
account/root transfer require separate operator choices and reviewed contracts.
This handoff does not approve those choices or claim physical power-loss proof.
