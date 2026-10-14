# Optional learning sidecar

`graphify reflect` still writes `reflections/LESSONS.md` when optional publication
of `.graphify_learning.json` fails. Sidecar readers return an empty overlay on
malformed JSON, admission failure or an oversized payload. Failures produce one
stderr diagnostic of at most 240 characters, without document content or paths.
An absent unowned sidecar is quiet. A missing receipt-owned sidecar produces a
diagnostic, including when an HTML or report consumer first admits its snapshot.

Admission and publication limit the complete serialized sidecar to **1 MiB**.
Readers apply this limit to descriptor-based reads before JSON parsing, and check
file identity, content and the admitted graph/output/receipt snapshot before
returning bytes. Serve uses the snapshot already admitted for its graph context.
Replacing or retargeting that output cannot attach another output's overlay.
Canonical graph admission validates every receipt artifact. The subsequent
optional refresh rechecks exact graph bytes, receipt identity/digest and live
protocol/drainer authority without rescanning unrelated artifact bodies. A
receipt-owned sidecar also matches its admitted digest.
HTML rendering admits the overlay from its graph JSON path; the default is the
`graph.json` sibling of the HTML destination. Custom graph paths are passed by
the CLI. Explicit overlays continue to bypass sidecar admission.
The version 1 node map, deterministic sorted UTF-8 serialization, provenance cap,
code fingerprints and conservative staleness calculation keep their existing
behavior.

A sidecar name that collides with the graph filename, including case variants,
is refused before publication starts. The custom graph remains usable, and
reflection still writes its lessons document.

Managed destinations use exact transaction ownership and a complete publication
plan. A successful publication carries all retained graph facts and receipt
artifacts forward, adds the sidecar, and advances the generation watermark.
Update/watch and cluster-only successors retain receipt-owned sidecar bytes and
fingerprints, so consumers mark changed source code stale. Borrowed report-only
learning remains outside receipt ownership.
After successful publication, reflection advances the already written lessons
mtime through the descriptor of the written file so `reflect --if-stale` can
skip unchanged inputs. Replacing the output directory or lessons path during
publication cannot create or mark another file fresh. Publication failure does
not advance that marker. Unrelated non-receipt artifacts remain in place. A sparse
reader snapshot cannot be used for publication. An already active managed
generation is refused without mutation; the optional sidecar does not take over
that owner. Plan preparation is validated before entering publication. A
preparation failure uses the existing exact-owner cancellation boundary, which
restores the predecessor only after proving that nothing was published.
External destinations
use the existing atomic unmanaged writer, with the source snapshot checked again
at its final publication boundary. Preliminary output locks are released before
entering that writer's ancestor-first lock order.
Symlink sidecar destinations are refused rather than followed. Unsupported native
Windows mutation remains refused.

A sidecar diagnostic does not confirm graph publication. If managed publication
is interrupted after it starts, existing transaction rules refuse inconsistent
reader admission until that transaction is resolved through the existing recovery
interface. The prior receipt is not rewritten as success. Reflection's returned
path confirms the lessons document only; direct `write_learning_sidecar` calls
continue to raise publication errors.

The transaction-interface prerequisite was reviewed separately before consumer
integration. Its bounded scope is optional canonical sibling admission, complete
plan publication and source-bound unmanaged writes. Transaction decomposition,
replacement policy and recovery sequencing are unchanged.
A separate follow-up interface review covered the exact-receipt auxiliary
recheck, unpublished-plan failure handling and external lock ordering.

A further bounded interface review covered refusal of graph/sidecar filename
collisions before either publication boundary is entered.
