# Checkpoint restoration candidate (#147)

This candidate implements prospective checkpoint capture and restoration into
a **new, absent output**. It preserves an approved graph without provider calls
and creates new generation-1 authority. It does not rebind the old output or
establish that its device ID changed because of reboot/remount.

**Native A14 passed** for the recorded macOS 15.7.7/APFS, CPython 3.14.9
installation: 17 required checks, real administrator/reader separation and
unchanged protected bytes. The [native receipt and complete report](qualification/issue147-a14-20261024.md)
bind that result to the tested source, wheel, harness and runtime. PR #222 stays
draft; this bounded result does not establish general platform readiness. The
[native opt-in qualification guide](checkpoint-qualification.md) describes the
disposable-guest harness and its evidence requirements. Retained outputs are
not qualification inputs.

The [accepted design](plans/issue147-recovery-design.md) defines the authority
model and A1–A15 matrix. The [existing identity refusal and trusted-corpus
fallback](transaction-recovery.md#output-identity-mismatch) remain applicable.
The historical #145/#219 outputs have no established pre-failure approval and
cannot be enrolled by copying their old records.

## Administrator provisioning boundary

The backend supports native 64-bit macOS/APFS with local storage and ownership
enforcement. Other hosts refuse. The normal Graphify UID cannot approve or
change a checkpoint head. Provisioning is a separate administrator action; no
command elevates, installs a daemon, selects a user-supplied trust root or
changes permissions automatically.

Before capture, the administrator must provide:

- A reviewed, installed Python/package/runtime under protected UID-0 ownership,
  started with isolated imports and bytecode writes disabled. An editable
  checkout or a user-writable package/interpreter is not an approved launcher.
- A protected ancestor chain and fixed configuration file at
  `/private/etc/graphify/recovery-authority.json`, UID 0, reader group, mode 0640.
- A canonical protected store, outside source/destination output trees, with
  directories mode 0750 and files mode 0640, UID 0 and the configured reader
  group. Its stable `.authority.lock` file must already exist; never replace it.
- No write-granting ACLs, unsafe links, network mounts or disabled ownership.
  Only deny ACL entries are permitted. Record actual custody and failure domain;
  another directory, volume or external drive does not itself grant authority.

The exact configuration schema is:

```json
{
  "schema": "graphify.recovery-authority.v1",
  "backend": "macos-apfs-v1",
  "administrator_uid": 0,
  "reader_uids": [501],
  "reader_gid": 20,
  "store_path": "/private/var/db/graphify/recovery",
  "custody": "Administrator-controlled checkpoint approvals",
  "failure_domain": "Same host; no protection against whole-host loss"
}
```

UID, group and store values are examples, not detected account settings. Reader
UIDs must be positive and belong to the configured reader group. The store is
not selected from an environment variable, project file or CLI override.
Backups on the same storage do not survive loss of that storage. Root/kernel
compromise and whole-host rollback are outside this backend's guarantees.

## Capture before failure

Assign one unique 64-character lowercase hexadecimal source ID to a healthy
output lineage. Select the exact source root and output. A capture validates
ordinary identity guards, fully closed authority, all receipted artifacts and
source entry identities under lock. It rejects pending work, hard links and
unsafe artifact names. Normal closure's exact preserved-complete predecessor
is retained as inert evidence; other predecessor states refuse.

From an administrator's qualified installed runtime, the interface is:

```text
<trusted-python> -I -B -m graphify.checkpoint_restore capture --source-id <SOURCE_ID> --root <CANONICAL_ROOT> --output <HEALTHY_OUTPUT> --expected-head absent
```

After the first capture, use the returned `head_sha256` as `--expected-head`.
A later approval must have the same output identity and a greater generation.
Concurrent approval uses an exact-head comparison. Incomplete slots never
authorize restoration. Old approval heads are not selected automatically.

Revocation uses the same qualified administrator runtime:

```text
<trusted-python> -I -B -m graphify.checkpoint_restore revoke --source-id <SOURCE_ID> --expected-head <HEAD_SHA256>
```

Revocation disables new imports and incomplete resume. It does not erase a
checkpoint or revoke a previously completed independent output. No history is
deleted by these commands.

## Preview and restore as the normal user

Create a request file with these exact fields; substitute real canonical paths
and the approved source ID:

```json
{
  "schema": 1,
  "source_id": "<64 lowercase hexadecimal characters>",
  "root": "<original canonical source root>",
  "output": "<private parent>/<new output basename>"
}
```

The parent must already be private (0700), owned by the caller, on qualified
local APFS. The destination must be absent, including absence of a dangling
symlink. Source relocation and adoption of an existing empty directory refuse.
The failed output is not an input to this command and is never opened.

```text
graphify checkpoint preview --request <REQUEST.json>
graphify checkpoint restore --request <REQUEST.json> --preview-sha256 <PREVIEW_SHA256>
```

Preview returns the protected head/checkpoint, capture time, old generation,
exact transformed inventory, provenance recipe and plan digest. It creates no
destination, token, queue, cache or provider call. It charges the final claim,
seal, provenance and publication limits before creation. Execute repeats the
checks and refuses a stale preview. The checkpoint may be older than source
changes that were never approved; this operation cannot recover those changes.

Graph content is preserved except for the versioned generation-1 watermark
replacement and JSON encoding. Manifest and other approved artifacts remain
byte-identical. The new receipt binds `.graphify_restore_provenance.json` and
new ownership; old receipt/token data is never live authority at this identity.
Later normal generations retain the immutable origin provenance. First-version
capture of restored lineages is intentionally unsupported.

## Interruption and exact resume

Normal graph reads and generic mutation/recovery refuse an incomplete restore,
including publication that has not finished closure/retirement. Transaction
status remains a diagnostic observation; it does not admit the graph. Preserve
partial destinations and staging evidence. The original claim records the new
device/inode and owner ID used by the exact resume interface:

```text
graphify checkpoint resume --request <REQUEST.json> --preview-sha256 <PREVIEW_SHA256> --device <NEW_DEVICE> --inode <NEW_INODE> --owner-id <NEW_TRANSACTION_ID>
```

An incomplete resume needs the unchanged protected head, request, parent,
destination identity, owner and seal. A completed exact retry only reports its
validated prior receipt. A directory created before a durable claim is not
adopted; preserve it and select a different absent destination. Do not delete
coordination files, restage modified bytes or use ordinary takeover to resume.

File/directory sync errors refuse and retain state. Process-exit tests do not
prove power-loss behavior. Advisory locks coordinate cooperating processes;
they do not fence malicious same-UID writers or root. No command cleans up the
source, checkpoint, partial destination or retired staging automatically.

## Acceptance evidence and remaining gate

Tests use newly created fixtures. `tests/test_recovery_authority.py` models root
custody for store tests and separately exercises native APFS/ACL helpers.
`tests/test_checkpoint_restore.py` exercises the end-to-end transaction using
that modeled authority and real filesystem operations. Process-exit cases run
in a separate interpreter and resume with a different PID. No model is counted
as proof of administrator isolation.

| Criteria | Candidate checks and limit |
| --- | --- |
| A1–A3 | Capture/restore, closed-record agreement, semantic graph preservation and generation-2 provenance retention. Administrator custody is modeled. |
| A4–A7 | Store owner/mode/ACL/path checks, no configured authority refusal, stale/revoked/tampered checkpoint, exact-head/generation checks and approval durability fixtures. |
| A8 | Two cooperating restore writers have one winner; parent retarget refuses before writing to a foreign destination. This is not arbitrary-writer fencing. |
| A9–A10 | Safe names/types/links/limits, metadata preflight and read-only preview/preservation checks. |
| A11–A13 | Publication failpoints, actual process exits, fsync failure, exact resume, generic-path fencing and origin retention. Exhaustive storage/power-loss qualification remains outside this result. |
| A14 | **Passed in the recorded native environment:** real protected installed runtime/store, separate administrator and reader, actual permission/ACL and stale-head controls. The enabled harness fails on missing prerequisites; unit tests and the default opt-out skip do not qualify it. See the [receipt](qualification/issue147-a14-20261024.md) and [guide](checkpoint-qualification.md). |
| A15 | Native disposable filesystem/process checks and synthetic identity mismatches. Observed remount renumbering and reboot causation remain unproved. |

Keep #147 open. Native A14 evidence covers the recorded backend boundary;
broader continuity evidence remains unresolved. Draft delivery is not a merge
or general operational readiness claim.

## Initial implementation validation (before native A14)

The following checks describe the initial implementation at `73e7a610`, before
the later [native A14 receipt](qualification/issue147-a14-20261024.md).

On native macOS/APFS with Python 3.14.9, the scoped transaction/bootstrap,
malformed-identity, portable-output, README-policy and recovery batch passed
**2,019 tests, with one A14 qualification skip**, in 340.19 seconds. The final
authority/restore suite, including added closure/older-reader checks and capture
store-isolation validation, passed **130 tests with the same A14 skip**. These
results overlap and must not be added as independent test totals. This is not
the complete repository suite or hosted CI.

Ruff, Pyright, whitespace and documentation-link checks passed. All five skill
generation checks passed, as did 80 optimized verifier tests with their expected
optimized-assertion warning. The built wheel was installed in a new private
runtime; CLI/install smoke and a modeled-authority end-to-end restoration passed
with real native destination checks. This installation did not provision the
system authority backend. Independent review's reproduced bounds, namespace,
hard-link, storage-name and retarget findings were corrected and verified.

Bandit reported two B103 permission findings for administrator-owned directories
set to 0750. That mode is required for the configured reader group's read and
traverse access; it grants no group write. Exact owner/group/mode and ACL checks
enforce that contract. The findings remain visible; no scanner suppression or
permission weakening was added.
