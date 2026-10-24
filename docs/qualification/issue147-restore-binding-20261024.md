# Native requalification of the close-recovery binding fix (#147)

Result: **PASS**, 2026-10-24 UTC, for source commit
`b36168d148f8af2de3d74db631bef7c54373a6f7`. The no-live `CLOSE_PENDING`
recovery path validates the optional `restore_binding` with the existing strict
validator before changing close state, then passes the narrowed value into
`Transaction`. No cast, type suppression or new authority rule was added.
Keep #147 open. Merge requires operator approval.

## Candidate and environment

| Input | Identity |
| --- | --- |
| Source commit | `b36168d148f8af2de3d74db631bef7c54373a6f7` |
| Candidate wheel SHA-256 | `51c6a822246daf84a41bafe951dd67a34a5d74100698a79fff333f5d2f0e8933` |
| Unchanged A14 harness SHA-256 | `2425fd022035b7d2adc5093f43a443508972ac413a904fb6bf2803b4e692c2c0` |
| Close probe SHA-256 | `08818a77cf27b24c0ff4f84976c3fd64979f84b8dda9a6894f833b30e1c6edf5` |
| Installed transaction.py SHA-256 | `5ab4710ad7b0bed42de210f0e78d64438a4e98614198460a5448ebec11c766c1` |
| Guest | Fresh disposable macOS 15.7.7 / Darwin 24.6.0, ARM64; ownership-enforced local APFS `disk2s5` |
| Runtime | Root-owned CPython 3.14.9, isolated imports, bytecode disabled |
| Runtime SHA-256 | `35fc4816db374545e4b31a019712aa99abb39fbc230c79c54ecf9495f45e9d86` |
| Administrator / reader | Real/effective UID 0 / UID 61047; reader GID 20, outside admin group |

The new guest was a separate APFS copy of the secured prepared guest from the
[initial qualification](issue147-a14-20261024.md), with no authority configuration.
All 266 Graphify wheel payloads match the frozen source. The real opt-in harness
verified all 271 installed payloads, excluding installer-rewritten RECORD, and
40 actual loaded module origins for each role. The close probe bound the passed
A14 report, wheel, source revision, interpreter and installed transaction bytes,
and rechecked both roles' installed candidate before and after its case.
No editable project or pytest installation ran as root.

The source commit ID was calculated from a frozen tree and exact commit metadata
before qualification. The ordinary commit was created only after native passes
and matched that identity and exact commit bytes. Later evidence/documentation
commits do not change the qualified runtime bytes.

## Native results

The unchanged `GRAPHIFY_A14_SPEC` dispatch passed **17 required checks** in
**11.95 seconds**, with **30 commands and 14 equal preservation inventories**.
It exercised administrator capture, unprivileged preview/restore, generation-1
authority, actual permission/ACL denials, unsafe authority refusal and revoked
stale-preview refusal. No enabled native row was skipped.

The supplemental close case passed in **7.16 seconds**, with **13 commands and
five equal preservation inventories**:

- A new ordinary source was approved and restored into a new absent output.
- A real reader process published generation 2, then exited via `os._exit(87)`
  at the supported `after_live_remove` boundary. The protocol was `COMPLETE`,
  the drainer was `CLOSE_PENDING`, and the live owner and token were absent.
- A second real reader process called `recover_selected_transaction` with the
  exact transaction ID, generation and output identity. This reached the changed
  helper and returned `CompletedRecovery` with the same non-null restore binding.
- The drainer closed, generation-2 status and graph reads worked, and graph,
  provenance and other approved artifacts stayed exact through recovery.

The original A14 reader outputs, report and authority configuration stayed exact.
Pre-existing authority files stayed exact; the report separately identifies the
new supplemental head/checkpoint entries and expected directory metadata changes.
The supplemental head remains approved only within the stopped disposable guest;
it is not operational authority. Earlier retained outputs and lifecycle fixtures
were not trial inputs or mutation targets.

All candidate and supplemental fixture commands had network denial. The single
A14 OS-only sudo-policy probe retained its documented exception and denied reader
privilege. The whole guest transport discarded **86 Ethernet frames / 25,135
bytes**, with no uplink, forwarding, host shares, credentials or clipboard/audio/USB
passthrough. Guest shutdown ended the agent transport; the VM process exited zero.
The prepared source and prior stopped trials were preserved.

## Evidence and local checks

The [complete A14 report](issue147-restore-binding-20261024.json.gz) is losslessly compressed;
its uncompressed SHA-256 is
`e062477a00dbdbf14aa6bd634db270e8ca9ecb5d12b016f70deaed5ddef7a2a9`.
The [close-case envelope](issue147-restore-binding-20261024-native-close.json.gz) contains
`native_report_json`, the exact original JSON text, and `probe_source`, the complete
reviewed standalone source, with hashes for both. The native close report SHA-256
is `f4f97ebd5d5c83a13e358d1329818fd49b54e10665ef7ea9ee1721b5c401a01a`.
These are inspectable evidence, not execution instructions. Repeat only in a new
explicitly disposable guest under the [qualification guide](../checkpoint-qualification.md).
The supplemental source is bound to its recorded workspace; adaptation needs review.

Focused restore, takeover/close, authority, harness-unit and README-policy checks
passed **365 tests, with one default A14 opt-out skip**. That skip is separate from
the enabled native passes above. Ruff, whitespace and all five generated-artifact
checks passed. The required AST refresh used a new private output with network
and checkout-write denial. Existing output preservation matched all **47,476**
entries for content, identities, modes and timestamps, excluding access time.

Pyright 1.1.409 reports **two errors** in the three checked files. The targeted
`restore_binding` argument diagnostic is gone. The unrelated `TextIO.reconfigure`
diagnostic at `graphify/__main__.py:504` and scoped-corpus generator return type
at `graphify/transaction.py:3822` are unchanged and remain separate. Both affected
code sections predate this PR in v8. This is not a clean Pyright pass or a full
repository-suite result. CI not awaited.

The [previous repair requalification](issue147-a14-requalification-20261024.md)
and its six supplemental checks remain historical evidence for their recorded
candidate. This fresh trial covers the binding fix. It does not establish physical
continuity, historical-output admission, remount/reboot causation, arbitrary-writer
fencing or power-loss durability.
