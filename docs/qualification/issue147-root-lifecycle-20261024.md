# Native requalification of the root lifecycle fixes (#147)

Result: **PASS**, 2026-10-24 UTC, for source commit
`f96bc70d50cc38c18c671a0b0cd5228ab470d081`. Both current P2 findings in PR #222
are repaired and qualified for this recorded environment: restore retains its
selected physical/canonical source-root capability through admission/publication,
and fully completed exact retries validate historical state without reopening
the original corpus root. Keep #147 open. Merge requires operator approval.

## Candidate and environment

| Input | Identity |
| --- | --- |
| Source commit | `f96bc70d50cc38c18c671a0b0cd5228ab470d081` |
| Candidate wheel SHA-256 | `86129a6460f304243822272f65a72386b824f32c0687bff4e410bc076568d0ec` |
| Unchanged A14 harness SHA-256 | `2425fd022035b7d2adc5093f43a443508972ac413a904fb6bf2803b4e692c2c0` |
| Supplemental root probe SHA-256 | `7aaecb7bca83aa230bcac9f9632e711b97835318485d57e5888ffab00080c504` |
| checkpoint_restore.py SHA-256 | `26ddb7a28c30fe9204b0f0e006469cab038f7da1e48b6d0f83cc9494e36f1e3e` |
| transaction.py SHA-256 | `a1cf5b00e40e541e8522a707c8e0562fba31fa8df21ffd079630dd5cdeb01192` |
| Guest | Fresh disposable macOS 15.7.7 / Darwin 24.6.0, ARM64; ownership-enforced local APFS `disk2s5` |
| Runtime | Root-owned CPython 3.14.9; isolated imports and bytecode disabled |
| Runtime SHA-256 | `35fc4816db374545e4b31a019712aa99abb39fbc230c79c54ecf9495f45e9d86` |
| Administrator / reader | Real/effective UID 0 / UID 61047; reader GID 20, outside admin group |

The guest was a new APFS copy of the secured prepared guest from the
[initial qualification](issue147-a14-20261024.md), with no fixed authority
configuration. It received the new source-matched wheel and exact reviewed
root-owned runners. All **266 Graphify payloads** match source; all **271 checked
installed payloads** match the wheel, excluding installer-rewritten RECORD.
Forty actual loaded module origins/hashes were bound for each role. The supplement
checked candidate/runtime identity before and after its matrix, including both
changed module digests. No editable project or pytest installation ran as root.

The source commit ID was calculated from a frozen tree and exact metadata before
qualification. After native passes, an ordinary commit matched that exact object
identity and bytes. Later documentation/evidence commits change no qualified
runtime bytes. No external bot review or CI monitoring was requested.

## Native results

The unchanged `GRAPHIFY_A14_SPEC` opt-in dispatch passed **17 required checks**
in **12.71 seconds**, with **30 commands and 14 equal preservation inventories**.
It exercised installed administrator capture, unprivileged preview/restore,
generation-1 authority, permission/ACL denials, unsafe authority refusal,
malformed/tampered data and revoked stale-preview refusal. No enabled row skipped.

The supplemental installed-runtime matrix passed **seven cases** in **25.19
seconds**, with **41 commands and 21 equal preservation inventories**:

| Case | Native result |
| --- | --- |
| Replacement before actual begin | Reader-local scheduling wrapper renamed the previewed corpus and created a different directory at its path, then called real transaction admission unchanged. Refused on physical identity; no graph or restore claim. |
| Replacement during real canonicalization | Same persistent replacement inside actual admission. Refused on physical identity; approved source and corpus files preserved. |
| Completed root independence | Restore completed, then a reader process renamed the root. A separate installed public CLI exact resume returned `already_complete`; graph/status reads worked. New mutations and ordinary-output reads with missing root still refused. Entire restored artifact/provenance inventory stayed exact. |
| Late incomplete, absent root | Real process exit 87 at `after_live_remove` left `COMPLETE` protocol, `CLOSE_PENDING`, no live owner/token. Separate-process exact resume refused missing root; partial output stayed exact and graph remained fenced. |
| Late incomplete, replaced root | Same real exit boundary, then a different directory at the original path. Refused changed preview; no partial output/coordination/provenance changes. |
| Same-inode alias after plan | After real plan validation, a source ancestor was renamed and replaced by a symlink to the same corpus. Inode stayed equal but canonical path changed. Refused the source capability path; partial output stayed fenced and exact. |
| Same-inode alias before publish | Same observed alias after source pinning and just before real publication entry. Refused current canonical resolution; partial output stayed fenced and exact. |

Wrappers only scheduled the explicit persistent filesystem changes and forwarded
real product calls. No authority, UID, runtime or admission validator was modeled.
This deterministic scheduling proof does not establish arbitrary-writer fencing
or detection of a change reverted between observations.

Each case used a new ordinary source, source ID, approved checkpoint and absent
destination. Source files and all approved ordinary outputs stayed exact. The
original A14 reader outputs, report and fixed configuration stayed exact through
the supplement. Pre-existing authority files stayed exact; only recorded new
head/checkpoint prefixes were added, with expected directory metadata changes.
Seven supplemental heads remain approved only inside the stopped disposable
guest. They are test state, not operational authority. Earlier retained outputs,
lifecycle fixtures and guest trials were not mutation targets or inputs.

All candidate/supplemental commands had network denial. The sole core A14 OS-only
sudo-policy probe retained its documented exception and denied reader privilege.
The whole guest transport discarded **105 Ethernet frames / 31,387 bytes**, with
no uplink, forwarding, shares, provider credentials, clipboard/audio/USB passthrough.
Shutdown ended the agent transport and the VM process exited zero. The prepared
source's identity/metadata and small-file hashes remained exact; prior guests
were preserved. No full-image content hash or power-loss result is claimed.

## Evidence and local checks

The [complete core report](issue147-root-lifecycle-20261024.json.gz) is losslessly compressed.
Its uncompressed SHA-256 is
`f869c326f8b5e6b03064969e780158c7a026d16dea5477ca61224819871e6107`.
The [root-case evidence envelope](issue147-root-lifecycle-20261024-native-roots.json.gz) contains
`native_report_json` (exact exported JSON text) and `probe_source` (complete reviewed
standalone source), with their hashes. The native root report SHA-256 is
`0c9dc3e46c0b34abaeabfb6266984602b06cd5eb9a1d49960c38120b262fca26`.
Inspect these as data. Repeat only under the
[qualification guide](../checkpoint-qualification.md) in another fresh explicitly
disposable guest; the supplemental source is bound to its recorded workspace.

The host driver refused a filename collision between export metadata and raw
report after core qualification/export had succeeded. Both original metadata and
stdout were preserved; the exact exported report was saved under a distinct name
and validated. No guest gate/export was repeated. Shutdown dropped agent transport
before stdout; final VM exit zero proved completion. These observation-driver
results are recorded locally and did not change product code or native results.

Source-matched local validation from the immediately preceding repair task was
reused after verifying all three file hashes and candidate tree unchanged:
**382 focused tests plus 27 disjoint transaction controls passed**, with **one
default A14 opt-out skip**. This includes all 17 new root cases, prior restoration,
takeover, authority, harness-unit and README-policy checks. Ruff, whitespace and
the final private AST refresh passed on the same source. The seven-case native
matrix above is additional enabled proof, separate from that default skip.
All five generated-artifact gates passed again before delivery.

Pyright 1.1.409 retains exactly **two unchanged baseline errors**, in
`TextIO.reconfigure` and `_OperationalCorpusScan._directory`'s generator annotation.
They remain separate. The focused check used the existing project interpreter
explicitly and reported no new errors or warnings. This is not a clean Pyright
pass, a full repository suite or green CI claim. CI not awaited.

The [previous binding-fix qualification](issue147-restore-binding-20261024.md)
and older receipts retain their original candidates and results. This new result
qualifies the root lifecycle repairs. It does not establish physical continuity,
historical-output enrollment, in-place identity rebinding, reboot/remount causation,
provider behavior or power-loss durability.
