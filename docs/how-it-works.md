# How graphify works

## The three passes

Graphify processes your files in three passes.

**Pass 1 — Code structure (free, no API calls)**
Tree-sitter parses your code files and extracts classes, functions, imports, call graphs, and inline comments. This runs locally with no LLM involved; see the [supported formats](../README.md#what-files-it-handles) for language coverage and optional extras. SQL files get special treatment: tables, views, foreign keys, and JOIN relationships are extracted deterministically.

Code files are not sent to the LLM semantic extractor in the normal pipeline. If a corpus contains only code files, Pass 3 is skipped entirely; semantic extraction is reserved for docs, papers, images, and transcripts.

**Pass 2 — Video and audio (local, no API calls)**
Video and audio files are transcribed with faster-whisper. To focus the transcript on your domain, the transcription prompt is seeded with your top god nodes (the most-connected concepts in your code graph so far). Transcripts are cached — re-runs skip already-processed files.

**Pass 3 — Docs, papers, images (assistant or configured backend, costs tokens)**
The host assistant extracts relationships from Markdown, PDFs, images, and
transcripts, or the headless CLI uses a configured LLM backend. Parallel dispatch
depends on the assistant and backend. Each batch produces a JSON fragment with
nodes, edges, and group relationships, which are merged into a single graph.
See the [command reference](../README.md#full-command-reference) for headless
backend examples.

Before Pass 3, optional converters turn supported pointer/binary formats into
Markdown sidecars under `graphify-out/converted/`. Office files (`.docx`,
`.xlsx`) use the `[office]` extra. Google Workspace shortcuts (`.gdoc`,
`.gsheet`, `.gslides`) are opt-in with `--google-workspace` or
`GRAPHIFY_GOOGLE_WORKSPACE=1` and require an authenticated `gws` CLI.

---

## How community detection works

Community detection uses native Leiden through `graspologic_native`, installed
with the optional `leiden` extra (also included in `all`). NetworkX Louvain is
used only when the top-level native module is absent. A broken native import or
Leiden execution error propagates instead of silently changing algorithms.
Both algorithms group nodes by graph connectivity;
[`graphify/cluster.py`](../graphify/cluster.py) owns the selection and splitting
logic.

**No embeddings needed.** Semantic similarity edges (`semantically_similar_to`)
from the semantic pass influence community shape alongside structural edges.
The graph structure is the similarity signal, with no separate embedding step
or vector database.

---

## Confidence tagging

Every relationship is tagged with one of three labels:

| Tag | Meaning |
|-----|---------|
| `EXTRACTED` | Found directly in the source (e.g. a function call, an import) |
| `INFERRED` | A relationship derived by structural resolution or semantic inference |
| `AMBIGUOUS` | Uncertain — flagged in the report for manual review |

The installed skill's semantic extraction instructions assign EXTRACTED edges a
`confidence_score` of 1.0, AMBIGUOUS edges a score of 0.1–0.3, and use this rubric
for INFERRED edges:
- **0.95** — near-certain (explicit cross-file reference, one plausible target)
- **0.85** — strong evidence (naming + context align)
- **0.75** — reasonable (contextual but not explicit)
- **0.65** — weak (naming similarity only)
- **0.55** — speculative

This rubric describes semantic-pass output. Structural/code-only edges may
supply their own scores or omit them. JSON export preserves supplied scores and
fills missing scores with 1.0 for EXTRACTED, 0.5 for INFERRED, and 0.2 for
AMBIGUOUS; these defaults are not semantic inference scores.

---

## Token benchmark

An initial mixed-corpus build spends tokens on semantic extraction; a code-only
AST build needs no LLM. Subsequent graph queries retrieve compact graph context
instead of re-reading the full corpus. That's where the savings compound.

On a mixed corpus (Karpathy repos + 5 papers + 4 images, 52 files): **71.5x fewer tokens per query** vs reading the raw files directly.

| Corpus | Files | Reduction |
|--------|-------|-----------|
| Karpathy repos + papers + images | 52 | **71.5x** |
| graphify source + Transformer paper | 4 | **5.4x** |
| httpx (synthetic Python library) | 6 | ~1x |

Token reduction scales with corpus size. Six files already fits in a context window — the graph value there is structural clarity, not compression. At 52 files the savings compound quickly.

Each `worked/` folder in the repo has the raw input files and actual output (`GRAPH_REPORT.md`, `graph.json`) so you can run it yourself and verify.

---

## Parallel extraction

Code files can be extracted in parallel using `ProcessPoolExecutor` for
multiprocessing. Doc/paper/image concurrency depends on the host assistant or
configured backend. In the recorded 84-code-file example, parallel AST extraction
ran in about 1.66x less time than sequential.

---

## SHA256 cache

Content hashes support skipping unchanged extraction in cache-enabled paths.
The cache lives in `graphify-out/cache/`. Prepared `graphify extract PATH
--code-only` against an existing graph (clustering enabled, without `--force`)
rebuilds the full admitted code corpus and cross-file resolution while bypassing
AST caches. It has no changed-file-only extraction fast path. This differs from
the separate `graphify update` command.

Prepared refresh preserves retained semantic evidence only with valid semantic
hash proof for its accepted code-source bytes; current AST hashes alone do not
prove semantic freshness. Missing or conflicting proof refuses publication.
Selected-source extraction errors also abort before publication, including a
missing required language extra such as SQL. Genuine optional metadata absence,
tolerant syntax and documented working absent-backend fallbacks remain valid.

---

## The graph format

Semantic incremental merges replace contributions from re-extracted sources and
deduplicate only their fresh input. Accepted entities, references and hyperedges
from unrelated sources keep their identities and evidence. New references can
target retained entities; a conflicting definition or occupied relationship
refuses the merge before publication. Explicit source replacement or pruning
can still retire contributions and their dependent references. A full rebuild
continues to apply its ordinary deduplication rules to the complete input.

The output `graph.json` uses NetworkX's node-link format. Each node has:
- `id` — stable identifier
- `label` — human-readable name
- `file_type` — `code`, `document`, `paper`, `image`, `rationale`
- `source_file` — where it came from

See [RFC: file-level node summaries](node-summaries-rfc.md) for two proposed
ways to add compact optional summaries for AI navigation.

Each edge has:
- `source`, `target` — node IDs
- `relation` — verb phrase (e.g. `calls`, `imports`, `implements`, `semantically_similar_to`)
- `confidence` — `EXTRACTED`, `INFERRED`, or `AMBIGUOUS`
- `confidence_score` — numeric score supplied by the extractor or filled by JSON export when absent
- `source_file` — where the relationship was found

Hyperedges (group relationships connecting 3+ nodes) live in `G.graph["hyperedges"]`.
