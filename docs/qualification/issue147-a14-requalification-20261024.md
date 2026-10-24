# Native A14 requalification after PR #222 repairs

Result: **PASS**, 2026-10-24 UTC, for source commit
`1c2d361c3a4684a63a0684ec81c36fd0701d3c7c`. The administrator-approved
checkpoint boundary passed again after the takeover-binding, filesystem-identity
overlap and CLI-help repairs. Six supplemental native checks passed too.
Keep #147 open; this does not establish physical continuity, historical-output
recovery, remount/reboot causation or power-loss durability.

## Candidate and environment

| Input | Identity |
| --- | --- |
| Source commit | `1c2d361c3a4684a63a0684ec81c36fd0701d3c7c` |
| Candidate wheel | `graphifyy-0.10.0-py3-none-any.whl` |
| Wheel SHA-256 | `d3918c7eee4df667b17046a38c3cde69310d2d99d9ac1caba4ca508407cb97b5` |
| Unchanged A14 harness SHA-256 | `2425fd022035b7d2adc5093f43a443508972ac413a904fb6bf2803b4e692c2c0` |
| Supplemental probe SHA-256 | `dcd018ef1ea68a7f085a955c24b0a025b0bc66043ad779eef053494f1dc84377` |
| Guest | macOS 15.7.7 / Darwin 24.6.0, ARM64; local APFS `disk2s5`, ownership enforced |
| Installed interpreter | Root-owned CPython 3.14.9, isolated imports and bytecode disabled |
| Interpreter SHA-256 | `35fc4816db374545e4b31a019712aa99abb39fbc230c79c54ecf9495f45e9d86` |
| Administrator / reader | Real/effective UID 0 / UID 61047; reader GID 20, outside admin group |
| VM tool / source | Tart 2.41.1; separate APFS copy of the secured prepared guest from the [initial qualification](issue147-a14-20261024.md) |

The fresh guest copy had no authority configuration. It received the new wheel
and the exact reviewed root-owned runner. All 266 Graphify wheel payloads match
the source; all 271 checked installed payloads match the wheel, excluding the
installer-rewritten RECORD. Forty actual loaded module origins/hashes were
bound for both roles. The supplemental probe rechecked these before and after
its matrix. No editable project or pytest installation ran as root.

The source commit identity was calculated from a frozen tree and exact commit
metadata before qualification. After both native passes, the ordinary commit
was created and its exact object bytes and ID matched that planned identity.
Later receipt/documentation commits do not change the tested runtime bytes.

## Native results

The real `GRAPHIFY_A14_SPEC` opt-in dispatch passed **17 required checks** in
**12.02 seconds**, with **30 commands and 14 equal preservation inventories**.
It exercised installed administrator capture, unprivileged preview/restore,
new generation-1 authority, artifact preservation, actual OS write denials,
reader approval/revocation refusal, unsafe mode/ACL controls, malformed/tampered
authority and revoked stale-preview refusal. No native matrix row was skipped.

The separate native repair probe passed **six checks** in **39.76 seconds**,
with **17 commands and eight equal preservation inventories**:

- Confirmed the installed repaired candidate against the source manifest.
- Captured a new ordinary source and restored it into another absent output.
- Waited **30.046 seconds** for the actual generation-2 lease to expire, then
  took over with the default clock. The successor token published and closed
  generation 2; status and graph reads worked and origin provenance stayed exact.
- Proved `authority` and `AUTHORITY` had the same device/inode. Administrator
  capture of a new ordinary source beneath that aliased store refused at the
  overlap guard, preserving the store.
- Reader preview and restore beneath the aliased store refused before creating
  a destination. The temporary configuration spelling was restored to exact
  original bytes/identity/mode; its controlled timestamp changes are recorded.
- Verified installed top-level CLI help lists all five checkpoint actions and
  their selector options.

Existing A14 source/restored outputs and report stayed exact during the
supplement. Original authority files stayed exact; separately recorded new
supplemental entries account for expected directory metadata changes. The new
supplemental test head remains approved only inside the stopped disposable guest;
it is not operational authority. No retained output from an earlier investigation
or historical lifecycle fixture was an input or mutation target.

Every candidate/fixture command had network denial. The sole A14 OS-only reader
sudo-policy probe retained its documented exception and produced a policy denial.
The whole guest's Ethernet transport discarded **101 frames / 31,100 bytes**,
with no uplink, forwarding, host shares, clipboard/audio/USB passthrough or
provider credentials. The new guest shut down with VM exit status zero. The
prepared source image and prior stopped trials were left unchanged.

## Evidence and repeatability

The [complete A14 report](issue147-a14-requalification-20261024.json.gz) is
losslessly compressed. Its uncompressed SHA-256 is
`fc3e03b29b7e0f5341421fb2bc7ab0e8094645da960ad685fb7ca766b434ed94`.

The [supplemental evidence envelope](issue147-a14-native-fixes-20261024.json.gz)
contains `native_report_json` (the exact original JSON text) and `probe_source`
(the complete reviewed standalone source), with hashes for both. The native
report SHA-256 is
`af6a8b5ceacc534c0f4dd0bdbeae7b534280ac122fbdd0a88ecaa8d8d47e2d82`.
The envelope keeps the probe and native output inspectable without treating
either as an instruction to execute. Inspect it with `gzip -dc` and a JSON
reader. Repeat only under the [qualification guide](../checkpoint-qualification.md)
in another fresh, explicitly disposable native guest. The supplemental source
is intentionally bound to its recorded workspace; any adaptation needs review.

Source-matched focused local checks passed **364 tests, with one default A14
opt-out skip**. That skip is separate from the enabled native pass above.
Ruff, whitespace and all five generated-artifact checks passed. Pyright reports
three unchanged diagnostics reproduced at the prior head `43c3c10`; no new
diagnostics were found. The AST refresh used only a new private output. A bounded
independent review confirmed the A14 input/module/native-evidence bindings.
These are scoped checks, not the complete repository suite. CI not awaited.

The [initial qualification receipt](issue147-a14-20261024.md) retains its original
candidate, failures and disposition. This requalification is a separate result.
