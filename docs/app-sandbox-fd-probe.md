# App Sandbox descriptor-export probe (#147)

This is a standalone, opt-in native diagnostic of one capability-export route.
It does not implement or enable the [replacement maintenance backend](replacement-maintenance.md).
The production maintenance entry point remains disabled. The runner never uses
an existing graph, recovery authority store, retained VM or installed Graphify
runtime as a fixture.

The [2026-11-02 native result](qualification/issue147-app-sandbox-fd-export-20261102.md)
records a counterexample for the selected policy and inherited UNIX channel.
It supplies no production containment authority.

## Question and scope

Can a sender with a real App Sandbox and an explicitly permitted synthetic file
export its writable descriptor over an inherited UNIX socket, then leave an
outside receiver able to change that file after the sender and its supervising
broker are killed and reaped?

The first route is `SCM_RIGHTS` over a pre-established `AF_UNIX` socketpair. The
same route is exercised without the sender's sandbox as a positive control. A
valid containment failure stops this experiment; it does not start Mach/XPC,
descendant-escape or other mechanism probes.

The receiver is outside the sender's sandbox and supervised process family. The
controller remains alive as the independent byte verifier. These are synthetic
processes under the current unprivileged user, not an enrolled dedicated-UID
deployment. Their deliberately bounded process structure is not a general
process-containment mechanism.

## Native admission controls

Build a fresh app bundle with a unique identifier, using the local SDK and
compiler. Sign it ad hoc with the system `codesign`; no private signing identity,
account provisioning or certificate inventory is required by this diagnostic.
Verify the signature and inspect the actual entitlements before execution.

The initial sender has `com.apple.security.app-sandbox = true`. It must not use
an inherit-only helper as a substitute for initial sandbox establishment from
the unsandboxed broker. Its only explicit writable path grant is the fresh
authorized canary directory, using the documented absolute-path read/write
temporary exception with a trailing `/`. That grant is part of the measured
configuration; it is not a production policy recommendation.

The sandboxed sender must demonstrate both:

- Successful writable access to the authorized canary.
- Denial of writable opening of a separate sibling canary that the unsandboxed
  control can open. The denied canary is never changed after initialization.

Entitlement presence alone is insufficient. A signing, startup, container,
authorized-access or denial-control failure leaves the native question
inconclusive. Do not fall back to private Seatbelt profiles, `sandbox-exec`, a
broader path entitlement or administrator privileges to obtain a result.

Apple documents App Sandbox entitlements as unrestricted on macOS; this removes
the provisioning-profile requirement for these entitlements, not the need to
establish the exact runtime behavior. First launch may create a container under
`~/Library/Containers`. Record its unique bundle identifier and observed path;
do not remove an unknown or still-used container.

## Controlled execution and evidence

Use a new external output directory. The runner creates its synthetic files,
app bundle and command logs there, and refuses occupied output paths. It must
never select a `graphify-out` directory or a source checkout as its fixture.

1. The controller creates the two canaries and known local IPC channels.
2. A broker starts the sender; a separate outside receiver waits for the
   descriptor. Functional controls must pass before transfer is authorized.
3. Once the sender holds the writable canary descriptor, the controller removes
   pathname permissions from that synthetic file. The receiver must demonstrate
   that it cannot reopen the canary for writing by name. The controller retains
   only a pre-opened read-only verifier descriptor. The sender exports its
   existing descriptor, and the receiver records its device/inode identity.
4. The broker kills and reaps its exact sender child. The controller then kills
   and reaps its exact broker child. PID age or a process-list snapshot cannot
   replace these wait results.
5. Only after both death acknowledgments does the controller release the
   receiver's write gate. The receiver attempts its distinct marker write
   through the received descriptor. The controller then reads the canary and
   verifies the actual protected-byte change or denial.

The positive control must show that this exact transport, death ordering and
write route work without sandbox enforcement. Record source and binary hashes,
OS/SDK versions, signing output, entitlement bytes, canary identities, actor
identities, protocol order, exact exit statuses, receiver write results and final
byte hashes. Do not infer success from a received integer, successful `sendmsg`,
an exit event or an unchanged file alone.

Timeout and error cleanup is limited to this runner's explicitly owned child
processes and newly created process groups. The known helpers do not daemonize
or change sessions. Those cleanup assumptions must not be generalized to
arbitrary legacy clients. Preserve the original cause and any incomplete cleanup
in the receipt; uncertain process or descriptor lifetime prevents a conclusion.

## Interpretation and stops

| Observation | Meaning |
| --- | --- |
| Positive route or protocol/identity/order check fails | Invalid harness evidence; no native containment conclusion. |
| Sandbox signing, entry or functional access controls fail | Prerequisite blocked; no native containment conclusion. |
| Outside receiver changes the selected bytes after both deaths | A native containment counterexample for this exact sandbox/grant/channel configuration. Stop and retain evidence. |
| Export or post-death write is blocked with valid controls | A bounded observation for this route only; not complete containment qualification. |

A counterexample does not prove that every App Sandbox construction is
incapable of containment. A blocked observation does not establish other UNIX
channels, Mach fileports, XPC, mappings, descendant lifetime, broker-failure
exclusion, physical power-loss behavior or Graphify runtime compatibility.
The maintenance backend remains disabled under every outcome.

Normal imports and ordinary pytest must not compile, sign or launch the native
probe. Native execution requires an explicit opt-in. Pure tests check evidence
classification and admission/order rules; they are not native proof. Local
focused checks do not establish full PR readiness or authorize delivery.

From the source checkout, explicitly authorized native execution uses:

```sh
.venv/bin/python -I -B tools/app_sandbox_fd_probe.py --run-native --output-root /absolute/new/external/directory
```

The CLI exits `3` for a measured containment counterexample, `2` for blocked
prerequisites or invalid evidence, and `0` for a bounded blocked observation.
No exit code or receipt qualifies the maintenance backend. Keep the complete
receipt and command logs, including failures; do not change policy or rerun a
valid counterexample merely to obtain a zero exit status.

## Primary references

- [App Sandbox configuration](https://developer.apple.com/documentation/xcode/configuring-the-macos-app-sandbox)
- [Temporary file-access exceptions](https://developer.apple.com/library/archive/documentation/Miscellaneous/Reference/EntitlementKeyReference/Chapters/AppSandboxTemporaryExceptionEntitlements.html)
- [Unrestricted macOS entitlements](https://developer.apple.com/documentation/technotes/tn3125-inside-code-signing-provisioning-profiles)
- [Current container and file-access behavior](https://developer.apple.com/documentation/security/accessing-files-from-the-macos-app-sandbox)
- [UNIX descriptor transfer](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/recvmsg.2.html)
