**Step B2 - Dispatch ALL subagents in a single message (OpenCode)**

> **OpenCode platform:** Uses `@mention` dispatch instead of the Agent tool. All mentions in a single message run in parallel.

Dispatch one `@mention` per chunk — ALL in the same response:

```
@agent Chunk CHUNK_NUM of TOTAL_CHUNKS: [extraction prompt with FILE_LIST, CHUNK_NUM, TOTAL_CHUNKS, DEEP_MODE substituted]

@agent Chunk 2 of TOTAL_CHUNKS: [next chunk]
```

Wait for all agents to return. Parse each response as JSON. Accumulate nodes/edges/hyperedges and validated completed_sources across current successful results. Stage the direct aggregate as `.graphify_semantic_new.json` inside the prepared output directory through the exact prepared-token runner used by B3. B3 preserves this current aggregate. If direct collection fails, use the serial path to write each chunk file and register its absolute path in the prepared success manifest before the B3 fallback merge.

If any dispatch fails, has no result, or returns invalid/truncated output, retain
the responses and diagnostics and stop before B3. The serial fallback applies to
valid responses that could not be combined directly, not failed extraction.
Keep `incomplete_sources` and any `_partial` flags in each response and the
aggregate so B3 can withhold completion. Do not drop a failure while combining
siblings or turn an absent response into empty success.

Subagent prompt template:

See `references/extraction-spec.md` for the exact subagent prompt (JSON schema, node-ID rules, confidence rubric, hyperedge, and vision rules). Load it only here, only when at least one chunk holds a doc, paper, or image; a pure-code corpus has skipped Part B and never reads it. Pass each agent that prompt verbatim with FILE_LIST, CHUNK_NUM, TOTAL_CHUNKS, and DEEP_MODE substituted.
