**Step B2 - Dispatch subagents and paste their responses**

> **No automated subagent tool:** this host has no parallel Agent/Task API, so
> extraction is driven by hand. Dispatch a subagent per chunk however the host
> allows (a fresh conversation, a parallel pane), then paste each response back.

For each chunk of uncached files (20-25 per chunk), use the host file editor to save the returned JSON into its allocated intake CHUNK_PATH; do not paste it into a shell heredoc or a live graphify-out path.

Use the parent-only **Disk result staging** recipe in `references/extraction-spec.md`: allocate a separate intake file for each trusted chunk assignment, give only that untrusted intake path as CHUNK_PATH, then validate and import each successful result through the exact current prepared-token runner before registration. The intake path grants no prepared-output write authority. Keep failed or partial intake results and diagnostics; stop before B3 on a failed dispatch/import.

Subagent prompt template:

See `references/extraction-spec.md` for the exact subagent prompt (JSON schema, node-ID rules, confidence rubric, hyperedge, and vision rules). Load it only here, only when at least one chunk holds a doc, paper, or image; a pure-code corpus has skipped Part B and never reads it. Pass each subagent that prompt verbatim with FILE_LIST, CHUNK_NUM, TOTAL_CHUNKS, and DEEP_MODE substituted, and save its response at the intake CHUNK_PATH, then use the parent import recipe.
