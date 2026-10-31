**Step B2 - Dispatch ALL subagents in a single message (Codex)**

> **Codex platform:** Uses `spawn_agent` + `wait_agent` + `close_agent` instead of the Agent tool.
> Requires `multi_agent = true` under `[features]` in `~/.codex/config.toml`.
> If `spawn_agent` is unavailable, tell the user to add that config and restart Codex.

Call `spawn_agent` once per chunk — ALL in the same response so they run in parallel. Build the message by wrapping the extraction prompt in task-delegation framing:

```
spawn_agent(agent_type="worker", message="Your task is to perform the following. Follow the instructions below exactly.\n\n<agent-instructions>\n[extraction prompt, with FILE_LIST, CHUNK_NUM, TOTAL_CHUNKS, DEEP_MODE substituted]\n</agent-instructions>\n\nExecute this now. Output ONLY the structured JSON response.")
```

After all agents are dispatched, collect results sequentially in memory:
```
result = wait_agent(handle); close_agent(handle)   # repeat per handle
```

Parse each result as JSON. Accumulate nodes/edges/hyperedges and validated completed_sources across current successful results and stage `.graphify_semantic_new.json` inside the prepared output directory through the exact prepared-token runner used by B3. Codex collects in memory; run B3 validation, cache saving and merging on this staged aggregate. No disk chunk files are required. A chunk that returns invalid JSON is a failure.

If any dispatch fails, has no result, or returns invalid/truncated output, retain
the responses and diagnostics and stop before B3. Keep `incomplete_sources` and
any `_partial` flags in the aggregate so B3 can withhold completion. Do not drop
a failure while combining siblings or turn an absent response into empty success.
Run B3 validation for the staged aggregate; only its serial fallback is skipped.

Subagent prompt template:

See `references/extraction-spec.md` for the compact subagent prompt (rules, node-ID format, confidence rubric, hyperedge and vision rules, JSON schema). Load it only here, only when at least one chunk holds a doc, paper, or image; a pure-code corpus has skipped Part B and never reads it. Pass each agent that prompt verbatim with FILE_LIST, CHUNK_NUM, TOTAL_CHUNKS, and DEEP_MODE substituted, and have it return the JSON inline.
