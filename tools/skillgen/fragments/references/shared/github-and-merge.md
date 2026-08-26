# graphify reference: GitHub clone and cross-repo merge

Load this when the user passed one or more `https://github.com/...` URLs, or named several local subfolders to merge into one graph.

### Step 0 - Clone GitHub repo(s) (only if a GitHub URL was given)

**Single repo:**
```@@GRAPHIFY_SHELL@@
@@GRAPHIFY_GUARD@@
@@GRAPHIFY_CMD@@ clone <github-url> [--branch <branch>]
# Use the printed local path as the target for all subsequent steps.
```

**Multiple repos (cross-repo graph):**
```@@GRAPHIFY_SHELL@@
@@GRAPHIFY_GUARD@@
# Clone each repo, run the full pipeline on each, then merge
@@GRAPHIFY_CMD@@ clone <url1>   # → ~/.graphify/repos/<owner1>/<repo1>
@@GRAPHIFY_CMD@@ clone <url2>   # → ~/.graphify/repos/<owner2>/<repo2>
# Run /graphify on each local path to produce their graph.json files
# Then merge:
@@GRAPHIFY_CMD@@ merge-graphs ~/.graphify/repos/<owner1>/<repo1>/graphify-out/graph.json ~/.graphify/repos/<owner2>/<repo2>/graphify-out/graph.json --out graphify-out/cross-repo-graph.json
```

Graphify clones into `~/.graphify/repos/<owner>/<repo>` and reuses existing clones on repeat runs. Each node in the merged graph carries a `repo` attribute so you can filter by origin.

**Multiple local subfolders (monorepo or multi-service layout):**

The skill pipeline writes all intermediate and final outputs to `graphify-out/` in the current working directory. Running the skill on each subfolder separately will clobber the same output dir. Instead, use the CLI directly for each subfolder — it places `graphify-out/` *inside* the scanned path:

```@@GRAPHIFY_SHELL@@
@@GRAPHIFY_GUARD@@
@@GRAPHIFY_CMD@@ extract ./core/     # → ./core/graphify-out/graph.json
@@GRAPHIFY_CMD@@ extract ./service/  # → ./service/graphify-out/graph.json
@@GRAPHIFY_CMD@@ extract ./platform/ # → ./platform/graphify-out/graph.json
# Add --backend gemini|kimi|openai|deepseek|claude-cli depending on which API key you have set

# Then merge at the project root:
@@GRAPHIFY_CMD@@ merge-graphs ./core/graphify-out/graph.json ./service/graphify-out/graph.json ./platform/graphify-out/graph.json --out graphify-out/graph.json
```

Once `graphify-out/graph.json` exists, the fast path above takes over: any codebase question runs `graphify query` directly on the merged graph — no re-extraction, no size gate.
