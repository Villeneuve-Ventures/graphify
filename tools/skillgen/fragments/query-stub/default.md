**Portable query — check before ordinary preflight:** Check for a directory entry at `graphify-out/.graphify_portable.json`, including a dangling symlink, even when graph.json is absent or the envelope is malformed or orphaned. When present, only query is supported; report `path`, `explain`, and `affected` as unsupported and stop. Use a compatible trusted installed runtime with cache suppression set before import:

```@@GRAPHIFY_SHELL@@
@@GRAPHIFY_GUARD@@
@@GRAPHIFY_CMD@@ query "QUESTION" --portable --output graphify-out --revision HEAD
```

Replace `QUESTION` with the user's original question; preserve requested `--dfs` and `--budget` options. The public portable CLI owns envelope, committed-tree, source, and byte validation. If no compatible trusted installed runtime is available, report it as a prerequisite and stop without installation or output writes. Do not run Step 1, scan-root persistence, ordinary preflight, query expansion, inline fallback, rebuild, query logging, save-result, or memory writes. A refusal never permits these operations. Answer only from admitted CLI output and cite its source locations. Stop after reporting the answer or refusal; preserve the three-file portable closure.

**Ordinary graph — only when no portable envelope entry exists:** Continue with the existing flow below.

When `graphify-out/graph.json` already exists and the user asks a question about the corpus, answer from the graph rather than rebuilding it:

```@@GRAPHIFY_SHELL@@
@@GRAPHIFY_GUARD@@
@@GRAPHIFY_CMD@@ query "<question>"
```

Before traversal, expand the question against the graph's own vocabulary so a wording mismatch does not collapse the answer to noise. If the `graphify query` CLI is unavailable, fall back to an inline NetworkX traversal of `graphify-out/graph.json`. Answer using only what the graph output contains, and quote `source_location` when citing a specific fact. For that vocab-expansion step, the BFS/DFS traversal modes, the `--budget` cap, the NetworkX fallback, `save-result` feedback, and the `/graphify path` and `/graphify explain` flows, see `references/query.md`.
