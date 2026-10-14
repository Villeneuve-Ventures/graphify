# Optional learning sidecar

`graphify reflect` still writes `reflections/LESSONS.md` when optional publication
of `.graphify_learning.json` fails. Sidecar readers return an empty overlay on
malformed JSON, admission failure or an oversized payload. Failures produce one
stderr diagnostic of at most 240 characters, without document content or paths.
An absent sidecar is quiet.

Admission and publication limit the complete serialized sidecar to **1 MiB**.
Readers apply this limit to descriptor-based reads before JSON parsing, and check
file identity, content and the admitted graph/output/receipt snapshot before
returning bytes. Serve uses the snapshot already admitted for its graph context.
Replacing or retargeting that output cannot attach another output's overlay.
The version 1 node map, deterministic sorted UTF-8 serialization, provenance cap,
code fingerprints and conservative staleness calculation keep their existing
behavior.

Managed destinations use exact transaction ownership and a complete publication
plan. A successful publication carries all retained graph facts and receipt
artifacts forward, adds the sidecar, and advances the generation watermark.
Unrelated non-receipt artifacts remain in place. A sparse reader snapshot cannot
be used for publication. An already active managed generation is refused without
mutation; the optional sidecar does not take over that owner. External destinations
use the existing atomic unmanaged writer, with the source snapshot checked again at its final publication boundary.
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
