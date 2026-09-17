# How graphify works

## The three passes

The existing graph-building pipeline processes your files in three passes.
This fork also has a [workspace control plane](workspace/v1/README.md) that
coordinates identity, durable generations, and certified queries around the
extraction engine. Its full semantic-sync and release routes remain unfinished;
the pipeline described here does not imply that those workspace routes are ready.

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

Community detection uses Leiden when a compatible `graspologic` installation is
available, otherwise NetworkX Louvain. The default dependency set does not
include `graspologic`. Both algorithms group nodes by graph connectivity;
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

Scores may be assigned by structural or semantic extraction; a numeric score
does not by itself identify model-derived evidence. The semantic extraction
prompt assigns EXTRACTED edges a `confidence_score` of 1.0 and uses this rubric
for INFERRED edges:

- **0.95** — near-certain (explicit cross-file reference, one plausible target)
- **0.85** — strong evidence (naming + context align)
- **0.75** — reasonable (contextual but not explicit)
- **0.65** — weak (naming similarity only)
- **0.55** — speculative

When a score is absent, [`to_json()`](../graphify/export.py) fills it in for
`graph.json` using the confidence tag: EXTRACTED **1.0**, INFERRED **0.5**, or
AMBIGUOUS **0.2**. Existing scores are preserved. Separately,
[`normalize_edge()`](../graphify/callflow_html.py) provides a read-time fallback
for call-flow rendering; it is not the source of these export defaults.

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

Every extracted file is fingerprinted by content hash. Re-runs skip unchanged files entirely — only new or modified files go through extraction again. The cache lives in `graphify-out/cache/`.

---

## The graph format

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
- `confidence_score` — numeric score from structural or semantic extraction, or a confidence-tag default applied during export when absent (see [Confidence tagging](#confidence-tagging))
- `source_file` — where the relationship was found

Hyperedges (group relationships connecting 3+ nodes) live in `G.graph["hyperedges"]`.
