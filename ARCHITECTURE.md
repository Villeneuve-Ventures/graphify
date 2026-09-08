# Architecture

Graphify is a Python library with CLI, MCP, and host-agent skill surfaces.
The skill orchestrates the library; the library can also be used standalone.

## Pipeline

```
detect() → extract(paths) → build() / build_from_json() → cluster()
         → god_nodes() / surprising_connections() / suggest_questions()
         → generate() → to_json() / to_html() / other exports
```

The stages exchange extraction dictionaries, NetworkX graphs, and community
mappings. Detection and extraction use caches; update/watch paths maintain
graph and manifest state. Output and cache roots are caller/configuration
dependent. Ingestion writes to a selected corpus directory, host installers
manage host configuration. Their respective modules own these boundaries.

## Module responsibilities

| Module | Function | Input → Output |
|--------|----------|----------------|
| `detect.py` | `detect(root)` | directory → categorized inventory and detection metadata |
| `extract.py` | `extract(paths)` | list of code paths → extraction dict |
| `build.py` | `build(extractions)` / `build_from_json(extraction)` | extraction dicts → `nx.Graph` or directed graph |
| `cluster.py` | `cluster(G)` | graph → `{community_id: [node_ids]}` mapping |
| `analyze.py` | `god_nodes` / `surprising_connections` / `suggest_questions` | graph and relevant community data → analysis lists |
| `report.py` | `generate(...)` | graph, communities, analysis and metadata → Markdown report string |
| `export.py` | `to_json` / `to_html` / `to_obsidian` / `to_svg` / other `to_*` functions | graph and export options → selected output files |
| `callflow_html.py` | `write_callflow_html(...)` | graphify-out files → Mermaid architecture/call-flow HTML |
| `ingest.py` | `ingest(url, ...)` | URL → file saved to corpus dir |
| `cache.py` | `check_semantic_cache` / `save_semantic_cache` | files → cached nodes/edges/hyperedges and uncached paths; extraction data → per-file cache entries |
| `security.py` | validation and sanitization helpers | URL / path → validated or raises; label → sanitized text |
| `validate.py` | `validate_extraction(data)` | extraction dict → list of schema errors |
| `serve.py` | `serve(graph_path)` / `serve_http(...)` | graph file path → MCP stdio or HTTP server |
| `watch.py` | `watch(watch_path, debounce=3.0)` | directory → code rebuilds or semantic-update flag |
| `benchmark.py` | `run_benchmark(graph_path)` | graph file → corpus vs subgraph token comparison |

## Extraction output schema

Extraction dictionaries use the following node and edge fields. For example:

```json
{
  "nodes": [
    {"id": "main", "label": "main", "file_type": "code", "source_file": "app.py", "source_location": "L42"},
    {"id": "helper", "label": "helper", "file_type": "code", "source_file": "app.py", "source_location": "L10"}
  ],
  "edges": [
    {"source": "main", "target": "helper", "relation": "calls", "confidence": "EXTRACTED", "source_file": "app.py"}
  ]
}
```

`validate_extraction(data)` returns error strings; an empty list means valid.
`assert_valid(data)` raises `ValueError` when validation fails.
`build_from_json()` normalizes legacy fields before validation and warns about
non-endpoint schema errors; it does not use `assert_valid()` as a strict gate.
The validator also reports unmatched edge endpoints, which graph construction
treats separately from schema warnings because external imports are expected.

## Confidence labels

| Label | Meaning |
|-------|---------|
| `EXTRACTED` | Relationship is explicitly stated in the source (e.g., an import statement, a direct call) |
| `INFERRED` | Relationship is a reasonable deduction (e.g., call-graph second pass, co-occurrence in context) |
| `AMBIGUOUS` | Relationship is uncertain; flagged for human review in GRAPH_REPORT.md |

## Adding a new language extractor

Language implementations live partly in `extract.py` and partly in
`graphify/extractors/`. The shared generic engine, models, and resolution
helpers are already in `extractors/`; `extract.py` remains the dispatch facade
and re-exports migrated extractors. `LANGUAGE_EXTRACTORS` is a registry seed,
not the dispatch implementation.

For new language support, follow the existing language module and facade
patterns, update the relevant suffix dispatch in `extract.py`, detection in
`detect.py`, and watched extensions in `watch.py`, and add focused language
fixtures/tests. Match parser dependency placement to `pyproject.toml`, including
its optional-extra boundaries. For moving an existing extractor without
behavior changes, follow the separate [migration playbook](graphify/extractors/MIGRATION.md).

## Security

`graphify/security.py` provides shared helpers used at specific call sites:

- URLs → `validate_url()` (http/https only) + `_NoFileRedirectHandler` (blocks file:// redirects)
- Fetched content → `safe_fetch()` / `safe_fetch_text()` (size cap, timeout)
- Graph file paths → `validate_graph_path()` (must resolve inside the selected allowed base, normally the graph output root)
- Node labels → `sanitize_label()` (strips control chars and caps 256 chars; direct HTML insertion still requires HTML escaping)

Path, cache, manifest, installer, and transaction checks also live in their
own modules. `transaction.py` coordinates project-local graph state through
capability-anchored filesystem commits; serialization and rendering finish
before `commit_bytes()`. See [SECURITY.md](SECURITY.md) for the threat model.

## Testing

The suite under `tests/` includes unit, filesystem, CLI/subprocess, installer,
and transaction/recovery coverage. With Python 3.14.2 or later in the 3.14
series and the committed lockfile,
run the canonical CI test gate:

```bash
uv run --frozen pytest tests/ -q --tb=short
```

Use focused test files for narrow changes; installer and transaction tests use
disposable environments and explicit test seams. See
[CI](.github/workflows/ci.yml) for generated-skill checks, the optimized-Python
protected-verifier check, and the separate PR-Agent compatibility job. That job
installs its own pinned PR-Agent runtime and runs
`python -m pytest --noconftest tests/test_pr_agent_runtime.py -q` with
`LITELLM_LOCAL_MODEL_COST_MAP=true`; it is distinct from the frozen package test
environment.
