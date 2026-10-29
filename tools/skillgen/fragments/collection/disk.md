Wait for all dispatches. Follow **Disk result staging** in `references/extraction-spec.md` before B3: stop on any failed, missing, invalid or truncated result, retain diagnostics, and do not import siblings, cache or publish. The public `graphify-out` directory is not the chunk success signal.

For each successful dispatch, bind GRAPHIFY_CHUNK_USAGE_JSON to its actual parent-observed tool usage (or explicit JSON null when unavailable), then use the exact current prepared-token import. Success requires verified prepared chunk readback and registration in this batch's `.graphify_completed_chunks.json`. Confirm every planned chunk was imported before proceeding. An empty extraction with explicit validated completion is valid; partial or unowned claims do not certify complete cache entries.

Merge only the registered prepared paths into `.graphify_semantic_new.json`. Token counts were applied during import; do not rewrite registered chunk JSON or substitute public paths. Then run:
