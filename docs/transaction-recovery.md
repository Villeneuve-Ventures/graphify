# Watch intent and prepared-transfer recovery

`transaction.inspect_recovery(output)` returns a read-only projection of the
successor-ready recovery state. It does not replay journals, extend leases,
rotate tokens, close a generation, or retire a prepared marker. Its result is an
observation, not a capability: mutation revalidates the selected output and its
current authority under the output lock.

| State | Meaning and next action |
| --- | --- |
| `absent` | No applicable successor-ready recovery. Continue normal snapshot admission and enqueue. |
| `live-deferred` | The validated owner still holds its lease. Admit new intent durably and defer recovery. |
| `transfer-replay` | An exact prepared-owner transfer is already journaled. Admit new intent before resuming that transfer. |
| `completed-retirement` | Publication has completed and validated closure or marker retirement remains. Finish it through transaction recovery. |
| `malformed` | Recovery state cannot be validated. Reject the operation without treating it as absent or granting recovery authority. |
| `ready-to-recover` | The validated successor-ready owner has expired. Admit intent, then use normal takeover and finalization. |

An in-progress transfer takes precedence over lease classification. A live owner
with an unexpired lease remains deferred even if its receipt has been published.
Ordinary transaction and prepared-workspace states keep their existing guards;
`absent` does not declare an output safe for an unrelated operation.

`transaction.recover_rebuild_intent` owns admission-before-replay for watch and
update invocations. CLI update and the installed rebuild-hook bodies share this
operation through `_rebuild_code`. For a prepared transfer it validates the exact
root, output identity, journal target, token/drainer binding, preserved merge
admission, predecessor receipt, prepared inventory, and public artifact overlap.
It then uses the existing canonical queue identity/coalescing and durable queue
transition, without rewriting transfer authority. Every path-based reopen keeps
the pinned output identity. Normal snapshot admission and enqueue still handle
outputs without applicable successor-ready recovery.

The event becomes durable when its queue file or replayable queue journal has
been persisted by the existing file-and-directory fsync primitives. A second
crash can leave queue publication or owner transfer incomplete, but a subsequent
valid recovery can discover the event without another filesystem event. Direct
and selected transaction recovery resume pending queue transitions before the
prepared-transfer replay; they do not manufacture an invocation intent. Existing
full-intent coalescing and duplicate-delivery rules remain in force.

The transaction layer owns prepared-marker retirement. It validates successor
publication, artifact/deletion inventory, receipt, and closure, including recovery
of a completed owner with residual state. Callers do not unlink markers or use a
state label as permission to start a generation. A subsequent generation begins
only through normal transaction admission after the previous generation's exact
closure/retirement requirements are satisfied; newly queued work remains durable
until published and acknowledged.

Cancellation remains limited to exact unpublished work and rejects an active
token transfer. Public graph readers remain fail closed during partial or
ambiguous recovery. This ordering improvement adds no background scheduler,
new durable format, recovery bypass, or Windows mutation support.

## Output identity mismatch

Protocol, close-drainer, generation-receipt and preserved-predecessor records
bind the output directory's filesystem device and inode. A well-formed identity
that differs from the pinned directory raises `OutputIdentityMismatchError`, a
subclass of `PendingTransactionError`. Its code is `output_identity_mismatch`;
its `record`, `recorded_identity` and `observed_identity` attributes describe the
failed comparison. The diagnostic includes both device/inode pairs, refuses
admission, and gives the separate-output rebuild fallback. Malformed identity
fields retain their malformed-state errors. A directory replaced during the
comparison fails the directory-identity guard instead of reporting a stale pin
as the observed identity.

A mismatch encountered by `transaction.inspect_recovery(output)` is returned as
`RecoveryInspection(state="malformed", reason=...)`, rather than raised. Its
bounded reason contains the diagnostic text; the result does not expose the
exception's structured attributes. This inspection covers applicable successor
recovery only; `absent` does not establish output admission.

The diagnostic reports only the first reached failure. It does not establish
that the rest of the record, other authority records, receipts or managed
artifacts passed validation. Update and watch report the refusal through their
existing rebuild error path; transaction status and selected recovery also
refuse it. No identity fields, tokens, queues or receipts are changed to produce
the diagnostic. Existing recovery-inspection state names and successful status
output remain unchanged.

An unchanged path or inode, matching artifact hashes, and agreement among local
records do not establish filesystem continuity or authorize rebinding. The
diagnostic does not attribute the mismatch to reboot or remount. A copied output
or replacement volume can also contain internally consistent old state.

Preserve the affected output and its authority records. Select the trusted
current source corpus explicitly and build into a separately named, absent
output; keep the original outside that build's inputs. An offline AST rebuild
provides structural coverage, not semantic parity with the old graph. A fresh
receipt establishes the new output's generation, not recovery of the old one.
Canonical-output adoption or replacement requires its own design and authority.
Do not rewrite recorded identities, regenerate receipts for old bytes, remove
coordination files, or use `--force` to bypass admission.

Ordinary-output rebinding is not supported. The receiptless predicate in
[legacy bootstrap](legacy-bootstrap.md) does not admit current coordinated
outputs with identity failures. Legacy five-field merge-pending recovery (#100)
and closed outputs with missing or mismatched managed artifacts (#114) remain
separate recovery contracts. An identity diagnostic does not establish artifact
corruption or make either recovery path eligible.
