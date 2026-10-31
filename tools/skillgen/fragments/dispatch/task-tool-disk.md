**Step B2 - Dispatch ALL subagents in a single message**

> Uses the `Task` tool for parallel subagent dispatch.
> Call `Task` once per chunk — ALL in the same response so they run in parallel.

Pass the extraction prompt as the task description:

```
Task(description="Your task is to perform the following. Follow the instructions below exactly.\n\n<agent-instructions>\n[extraction prompt, with FILE_LIST, CHUNK_NUM, TOTAL_CHUNKS, DEEP_MODE, CHUNK_PATH substituted]\n</agent-instructions>\n\nExecute this now. Write the structured JSON to CHUNK_PATH and return only a completion notice.")
```

Each subagent writes its result to its own intake CHUNK_PATH. Collect completion status as each `Task` finishes; the parent imports disk results without sending their large JSON through a tool response.

Use the parent-only **Disk result staging** recipe in `references/extraction-spec.md`: allocate a separate intake file for each trusted chunk assignment, give only that untrusted intake path as CHUNK_PATH, then validate and import each successful result through the exact current prepared-token runner before registration. The intake path grants no prepared-output write authority. Keep failed or partial intake results and diagnostics; stop before B3 on a failed dispatch/import.

Subagent prompt template:

See `references/extraction-spec.md` for the exact subagent prompt (JSON schema, node-ID rules, confidence rubric, hyperedge, and vision rules). Load it only here, only when at least one chunk holds a doc, paper, or image; a pure-code corpus has skipped Part B and never reads it. Pass each subagent that prompt verbatim with FILE_LIST, CHUNK_NUM, TOTAL_CHUNKS, DEEP_MODE, and CHUNK_PATH substituted, and have it write the result to the intake CHUNK_PATH. A compact completion notice is enough; do not return large JSON inline.
