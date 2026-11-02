# Agent contract map

Read the section triggered by root [AGENTS.md](../AGENTS.md#contract-navigation).
This map directs maintenance work to current contracts. It does not authorize
product changes, graph rebuilds, installation, publication or resource removal.
Existing graph reports and branch work are navigation evidence; verify claims
against the current owning source before adopting them as rules.

## Graph representation

Before changing graph behavior, trace the actual entry point through
[build.py](../graphify/build.py), [cluster.py](../graphify/cluster.py),
[report.py](../graphify/report.py) and [export.py](../graphify/export.py), as
applicable. Identify the graph class, edge identity and any projection at each
boundary. Do not infer direction, parallel-edge or hyperedge preservation from
another entry point's support.

- `build_from_json` and `build_merge` produce simple `Graph` or `DiGraph`
  representations. A directed graph preserves endpoint order but does not by
  itself retain parallel relationships. Undirected storage uses `_src`/`_tgt`
  for surviving edge orientation; it cannot keep every reciprocal relationship.
- `edge_data` selects one parallel edge; `edge_datas` returns all edge attribute
  dictionaries. Choose according to the caller's contract. `dedupe_edges` uses
  `(source, target, relation)` and keeps the first occurrence; do not use it for
  intentionally parallel output. Node identity and entity deduplication are
  separate from source ownership.
- Clustering can project direction away and constructs a simple partition
  graph. Check the scoring formula and the graph passed to it before describing
  cohesion as density or assuming a bounded score for directed or multigraph
  inputs. Reports consume supplied scores and filter displayed communities;
  displayed counts are not a complete graph inventory.
- `write_json` emits graph-class flags, keyed multigraph edges and hyperedge
  metadata, and restores saved endpoint orientation. Serialization cannot
  recover facts already collapsed upstream. Hyperedges are graph metadata,
  not interchangeable with ordinary edges or their pairwise projections.

For a report or traversal repair, use a case that separates the affected
semantics: reciprocal arcs, two relations on one pair, keyed parallel edges,
or a hyperedge with a removed member. Check identities, attributes and relevant
output, not only total counts. Select cases required by the supported input
classes; this is not a requirement to add support for every graph class.
Existing coverage includes [semantic merge preservation](../tests/test_build_merge_semantic_preservation.py),
[hyperedges and pruning](../tests/test_build_merge_hyperedges_and_prune.py),
[clustering](../tests/test_cluster.py), [reports](../tests/test_report.py) and
[export](../tests/test_export.py). Follow the existing validation route; these
links are proof locations, not an additional universal test gate.

## Source contributions

Before changing completion or replacement, read
[semantic replacement](semantic-replacement.md) and, for scoped engine input,
[engine input interfaces](engine-input-seams.md). Trace
[extract](../graphify/extract.py) through its caller to `build_merge` and the
publication owner. Keep these distinctions explicit:

- A complete successful empty extraction can remove its source's old
  contribution. A missing, failed, truncated or incomplete extraction cannot
  establish that completion. Populate `replacement_sources` only from current
  successful completion evidence; relationship ownership or a progress callback
  alone is insufficient.
- An explicit `replacement_sources=[]` adds compatible claims without replacing
  sources. Omitting it retains legacy node-owner replacement behavior; use the
  explicit declaration for new callers. Keep AST refresh's separate contract.
- Preserve foreign-owned retained identities and conflict refusals. Normalized
  source keys decide contribution ownership; they grant no permission to read,
  delete or publish filesystem paths. File-symlink owners remain distinct.
- In-memory composition, caller-owned stream serialization, filesystem output
  admission and publication are separate boundaries. `write_json` leaves stream
  lifetime and publication to its caller; `to_json` has filesystem behavior and
  shrink protection. Scoped input evidence records observations, not atomic
  snapshot isolation or publication authority.

For replacement changes, prove successful-empty versus failed extraction,
foreign-owner preservation/conflict refusal, and the relevant prune precedence.
Start with the linked merge tests and the extraction tests bound to the changed
caller. Carry the scoped public-interface docstring requirements from
[AGENTS.md](../AGENTS.md#python-docstrings) only where applicable; do not turn
them into unrelated interface rewrites.

## Instruction source ownership

Before changing shipped instructions, trace the authored inputs in
[platforms.toml](../tools/skillgen/platforms.toml),
[gen.py](../tools/skillgen/gen.py) and [fragments](../tools/skillgen/fragments/).
The authored always-on block for AGENTS integrations is
[agents-md.md](../tools/skillgen/fragments/always-on/agents-md.md).
`graphify/skill*.md`, `graphify/skills/*/references/`, `graphify/always_on/` and
`tools/skillgen/expected/` are generated or guarded outputs, not independent
authoring surfaces. Repair drift at its source. Do not bless unexplained changes
as proof of correctness. Full rendering includes always-on blocks; a platform
render does not. Preserve existing coverage, schema and round-trip constraints.

Before reorganizing integration guidance, inspect
[install.py](../graphify/install.py), its re-exports in
[__main__.py](../graphify/__main__.py), and
[section replacement tests](../tests/test_replace_or_append_section.py).
Installation replaces the last exact `## graphify` heading through the next
column-zero level-two heading or EOF. Removal also uses the heading and next
level-two heading as boundaries. Keep unrelated durable rules outside that
section; preserve the actual integration heading and content. An installation
or regeneration is a separate action requiring task authority.

For authorized generator or installer changes, use the current
[validation route](validation-routing.md) and its existing checks. Relevant
contracts live in [skill generation tests](../tests/test_skillgen.py),
[installation tests](../tests/test_install.py) and
[reference bundle tests](../tests/test_install_references.py).
Generated-skill drift requires tracing authored inputs and the affected host
bundle; changing only a generated copy or expected snapshot does not repair it.
