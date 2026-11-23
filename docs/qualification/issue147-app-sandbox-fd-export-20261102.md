# #147 App Sandbox FD export after process death: native counterexample

Observed on 2026-11-02. This is one local diagnostic on macOS 27.2 (26B123),
SDK 27.2, based on the maintenance-contract candidate at
`8c224defeb18abe51648a15780427a8392affc95` (#265). The probe source is a local
increment, identified by raw-file hashes below; it is not part of that base commit.

## Result

**The outside receiver retained writable descriptor authority after both the
sandboxed sender and its broker died.** The runner returned diagnostic exit 3,
`containment-failed`, with `qualified: false`. This is a measured counterexample
for the selected policy and pre-established `SCM_RIGHTS` channel. Live replacement
remains disabled.

| Required evidence | Unsandboxed control | App Sandbox case |
| --- | --- | --- |
| Sender opens/writes the explicitly authorized canary | Passed | Passed |
| Sender can open the separate sibling file for writing | Allowed | Denied, EPERM (1) |
| Receiver can open that sibling file outside the sender's sandbox | Allowed | Allowed |
| Receiver can reopen the selected canary by pathname after mode 000 | Denied, EACCES (13) | Denied, EACCES (13) |
| Received descriptor identifies the selected canary and is O_RDWR | Passed | Passed |
| Sender SIGKILL and exact broker wait acknowledgment | -9 | -9 |
| Broker SIGKILL and exact controller wait acknowledgment | -9 | -9 |
| Receiver write after both acknowledgments | 52 bytes | 52 bytes |
| Independent read-only verifier observes the distinct marker/hash change | Passed | Passed |
| Three actor cleanup records agree; receiver exits normally | Passed | Passed |

The controller revoked pathname permissions only on its new synthetic canary
once the sender held its writable descriptor. Verification used a pre-opened
read-only descriptor. Permissions were restored to the original 0600 only after
recording the observation. The receiver wrote solely through the transferred
handle, after its private write gate was released following both reaps.

## Measured construction

The native sender was an ad hoc-signed initial app executable, not an
inherit-only helper. Its inspected signed entitlements were exactly:

- `com.apple.security.app-sandbox = true`
- One `com.apple.security.temporary-exception.files.absolute-path.read-write`
  entry for the task-owned authorized directory, ending in `/`.

The positive control used the same native source, transfer channel, revocation,
death sequence and receiver write route without App Sandbox entitlements. The
receiver and broker were separate unsandboxed controller children, each started
in a new session. The sender was the broker's child. The known actors did not
daemonize. This bounded cleanup arrangement is not a general containment system.

Both app signatures verified and the functional sandbox controls passed before
transfer. The sandbox app created one unique OS container; its metadata and the
small native fixtures remain retained with the local packet. No pre-existing
container was selected or removed.

## Source and evidence identity

| Input | SHA-256 of raw bytes |
| --- | --- |
| `tools/app_sandbox_fd_probe.py` | `82e9ca9c0707d78102ded5803e9e2a505374cb2bf4c06c4e9cbdb3b839782201` |
| `tools/fixtures/app_sandbox_fd_sender.c` | `53461eba5e6c4033c5cda743483be0a412b6b074d5a4ae8a0752751723069e66` |
| Signed sandbox sender | `8e933562d8441d647eda68bbc39a151b01dc0c47a88fb18348f6c1d4ea1b1e21` |
| Complete native receipt | `e87553525dea8fd5460cc8e1dc65cfbb59ca71c2ce538178b062db69e5197f67` |

These hashes identify the original measured inputs. Subsequent runner changes
retain diagnostics on command timeout or launch failure and were checked with
mocked commands only. Later fixture-admission and evidence-validation repairs
are covered by pure regression tests. The native experiment was not rerun;
the retained failed counterexample remains bound to the original source hashes
above. These repairs supply no new native qualification.

The machine-local retention packet is
`graphify-app-sandbox-fd-probe-20261102-01a18acd`, with `native-01/receipt.json`,
two per-case receipts, complete command/signing output and the signed bundles.
The external retention index holds actual local paths. Raw machine-local data
is not copied into the repository.

## Limits and disposition

The result demonstrates that this App Sandbox/grant/channel construction does
not provide descriptor revocation or complete holder exclusion merely by killing
the sender and broker. It does not rule out a different composition of documented
restrictions and construction invariants. Any future proposal must independently
close or account for export routes and every surviving writable-capability holder.

The experiment stopped after this counterexample. No Mach/XPC, mapping,
descendant-escape, dedicated-UID, privileged-broker, physical power-loss or
Graphify runtime-containment qualification was attempted. No VM, hosted backend,
existing graph fixture, live replacement, runtime installation or PR delivery
was used. Original graphs remain outside the experiment.

Practical checks on the probe increment passed: 38 focused Python checks,
scoped locked Ruff/Pyright, strict C syntax with the explicit SDK, and independent
static execution review. An initial C syntax command omitted `-isysroot` and
failed to find system headers; the corrected SDK-selected invocation passed.
That failed invocation is retained as a command failure, not a source defect or
native containment result. Full-suite and PR readiness are not claimed.
