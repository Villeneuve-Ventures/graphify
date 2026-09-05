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
