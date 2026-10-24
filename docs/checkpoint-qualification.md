# Native checkpoint authority qualification (A14)

A14 qualifies the installed macOS/APFS authority boundary. Unit tests that model
UIDs or store ownership do not satisfy it. Run the qualification in a fresh,
disposable native macOS guest with APFS and enforced ownership. Keep existing
outputs and historical recovery fixtures outside the guest and its inputs.

The runner is `tools/checkpoint_qualification.py`. It is a test operator tool,
not a product provisioning command. It requires explicit disposable-environment
opt-in and refuses existing configuration or workspace paths. It does not
elevate, install Python, create accounts, change existing outputs, or clean up
the trial. Preserve a failed trial before diagnosing it; a missing prerequisite
or unexpected negative control is a failure when the gate is enabled.

## Prepare the disposable guest

Use a new macOS guest without host-directory shares, forwarded agents, provider
credentials or private project data. Record the VM tool, image digest, OS build,
filesystem and guest identity. Install a reviewed Python 3.14 runtime and the
candidate wheel under UID-0 ownership, with a protected ancestor chain. Install
the dependencies needed by the checkpoint entry point in the same protected
runtime. A user-owned Homebrew Python, editable checkout or shared development
environment cannot qualify administrator capture.

An installer signature does not establish safe filesystem permissions. For
example, the Python 3.14.9 macOS installer creates group-writable runtime files
and directories. The guest administrator must remove group/other write access
from that new runtime and its protected ancestors before qualification. Record
the exact provisioning changes; do not bypass the runtime refusal checks or
apply broad permission changes to the host.

Create or select one ordinary guest account and its group. The reader must be
outside the administrator group and must not have passwordless elevation.
Record its numeric UID and GID. The runner drops real child-process credentials
to these values. It does not model `getuid`, ACL checks, the fixed trust root or administrator
runtime checks. The administrator and reader use the same installed candidate.

Review the standalone runner and install an exact root-owned copy outside the
project tree. Record its SHA-256, the candidate source revision, wheel SHA-256
and installed package bytes. Hashing a wheel alongside an unrelated installed
package does not qualify that package. Do not execute editable project imports
as root. Runtime qualification is a refusal check, not a safe launcher.

The fixed `/private/etc/graphify/recovery-authority.json` must be absent. Select
a new absent workspace below `/private/var/db/graphify-a14-…`. The runner owns
only that workspace and the new fixed qualification configuration. Do not use
the host's production configuration or an existing authority store. Disable
network access for the trial commands; successful setup downloads do not prove
that the qualification itself is offline.

macOS refuses to execute the set-ID `sudo` binary inside the command sandbox.
The runner therefore makes one explicit pre-provisioning environment probe:
the reader's fixed `sudo -n /usr/bin/true` invocation must return an actual
authentication or policy denial. That OS-only probe runs outside the command
sandbox and imports no candidate code. Isolate the guest's network transport
for the trial as well. Every candidate and fixture command remains inside the
network-denying sandbox; a sandbox exec failure is not sudo-policy evidence.

## Run and retain the result

Invoke the reviewed installed interpreter directly with isolated imports and
bytecode writes disabled:

```text
<trusted-python> -I -B <root-owned-runner>/checkpoint_qualification.py --disposable --workspace /private/var/db/graphify-a14-<UNIQUE_ID> --reader-uid <UID> --reader-gid <GID> --wheel <ROOT_OWNED_WHEEL> --expected-wheel-sha256 <SHA256> --expected-source-revision <COMMIT>
```

An exit status of zero is necessary. Read the structured result and require all
native checks, the expected candidate identities and an explicit passing status.
Do not count a skipped pytest row, a prerequisite refusal, or unit tests of the
runner as an A14 pass. Retain commands, results, identities and preservation
inventories with the trial. Review the result at the tested source revision;
material code, runtime or provisioning changes need new relevant proof.

The ordinary pytest suite leaves this privileged gate disabled. To enable its
entry point, set `GRAPHIFY_A14_SPEC` to a strict JSON specification for the
reviewed installed runner. Enabled mode must fail for a missing or invalid
specification or unmet native prerequisites. It must not turn those failures
into skips. Run privileged qualification only from the reviewed installed
environment, not from a user-writable pytest installation.

The strict specification has these fields: `schema` (1), `disposable` (true),
absolute `python`, `harness`, `wheel` and absent `workspace` paths, `reader_uid`,
`reader_gid`, `harness_sha256`, `wheel_sha256` and `source_revision`. The hashes
are lowercase SHA-256; the revision is a full lowercase 40-character Git ID.
Keep the spec in a new root-owned regular file. To exercise the same opt-in
dispatch without installing pytest as root, use the reviewed standalone runner:

```text
GRAPHIFY_A14_SPEC=<ROOT_OWNED_SPEC.json> <trusted-python> -I -B -c 'import runpy; h=runpy.run_path("<root-owned-runner>/checkpoint_qualification.py",run_name="a14_dispatch"); h["execute_spec"](h["opt_in_spec"]())'
```

This path runs the real native matrix and validates its report. See the
[2026-10-24 receipt and complete report](qualification/issue147-a14-20261024.md)
for the tested input identities and observed result. A repeat needs a fresh
guest with no authority configuration; do not clear a failed trial to retry.

## Scope of the proof

The gate must exercise actual administrator capture, reader preview and restore,
new generation-1 identity, semantic graph preservation, and exact preservation
of other approved artifacts. Reader operations must preserve the source and
protected authority bytes. Real OS write attempts must demonstrate denial for
configuration, selected head, checkpoint records and stable lock, and for new
store entries. Reader capture and revocation must refuse.

Unsafe permissions and a write-granting ACL must cause authority refusal. A
malformed head, tampered checkpoint and revoked stale preview must refuse before
destination creation. Controlled negative injections apply only to new trial
resources. Record their restoration separately from reader preservation checks.
Unexpected behavior stops the trial and leaves its evidence available.

Passing A14 does not establish physical device continuity, recovery of unenrolled
historical outputs, remount renumbering, reboot causation, arbitrary same-UID
writer fencing or power-loss durability. Keep issue #147 open. See the
[authority model and acceptance matrix](plans/issue147-recovery-design.md) and
[checkpoint operation guide](checkpoint-restoration.md).
