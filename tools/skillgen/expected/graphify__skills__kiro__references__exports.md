# graphify reference: extra exports and benchmark

Load this when the user passed one of the export flags (`--wiki`, `--neo4j`, `--neo4j-push`, `--falkordb`, `--falkordb-push`, `--svg`, `--graphml`, `--mcp`), or when the corpus is large enough for the token-reduction benchmark. Each step runs only for its own flag.

### Step 6b - Wiki (only if --wiki flag)

**Only run this step if `--wiki` was explicitly given in the original command.**

Run this before Step 9 (cleanup) so `.graphify_labels.json` is still available.

```bash
if [ ! -f graphify-out/.graphify_python ]; then
    echo "Missing graphify-out/.graphify_python; rerun Step 1 interpreter bootstrap." >&2
    exit 1
fi
_GRAPHIFY_SAVED=$(cat graphify-out/.graphify_python)
case "$_GRAPHIFY_SAVED" in
    /*) ;;
    *) echo "Invalid graphify interpreter pointer." >&2; exit 1 ;;
esac
if [ ! -x "$_GRAPHIFY_SAVED" ]; then
    echo "Saved graphify interpreter is not executable." >&2
    exit 1
fi
"$_GRAPHIFY_SAVED" -E -P -B -c 'import graphify, sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)' >/dev/null 2>&1 || {
    echo "Saved graphify interpreter is unsupported or cannot import graphify." >&2
    exit 1
}
"$(cat graphify-out/.graphify_python)" -E -P -B -m graphify export wiki
```

### Step 7 - Neo4j export (only if --neo4j or --neo4j-push flag)

**If `--neo4j`** - generate a Cypher file for manual import:

```bash
if [ ! -f graphify-out/.graphify_python ]; then
    echo "Missing graphify-out/.graphify_python; rerun Step 1 interpreter bootstrap." >&2
    exit 1
fi
_GRAPHIFY_SAVED=$(cat graphify-out/.graphify_python)
case "$_GRAPHIFY_SAVED" in
    /*) ;;
    *) echo "Invalid graphify interpreter pointer." >&2; exit 1 ;;
esac
if [ ! -x "$_GRAPHIFY_SAVED" ]; then
    echo "Saved graphify interpreter is not executable." >&2
    exit 1
fi
"$_GRAPHIFY_SAVED" -E -P -B -c 'import graphify, sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)' >/dev/null 2>&1 || {
    echo "Saved graphify interpreter is unsupported or cannot import graphify." >&2
    exit 1
}
"$(cat graphify-out/.graphify_python)" -E -P -B -m graphify export neo4j
```

**If `--neo4j-push <uri>`** - push directly to a running Neo4j instance. Ask the user for credentials if not provided:

```bash
if [ ! -f graphify-out/.graphify_python ]; then
    echo "Missing graphify-out/.graphify_python; rerun Step 1 interpreter bootstrap." >&2
    exit 1
fi
_GRAPHIFY_SAVED=$(cat graphify-out/.graphify_python)
case "$_GRAPHIFY_SAVED" in
    /*) ;;
    *) echo "Invalid graphify interpreter pointer." >&2; exit 1 ;;
esac
if [ ! -x "$_GRAPHIFY_SAVED" ]; then
    echo "Saved graphify interpreter is not executable." >&2
    exit 1
fi
"$_GRAPHIFY_SAVED" -E -P -B -c 'import graphify, sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)' >/dev/null 2>&1 || {
    echo "Saved graphify interpreter is unsupported or cannot import graphify." >&2
    exit 1
}
"$(cat graphify-out/.graphify_python)" -E -P -B -m graphify export neo4j --push bolt://localhost:7687 --user neo4j --password PASSWORD
```

Default URI is `bolt://localhost:7687`, default user is `neo4j`. Uses MERGE - safe to re-run without creating duplicates.

### Step 7a - FalkorDB export (only if --falkordb or --falkordb-push flag)

**If `--falkordb`** - generate a Cypher file. The statements are OpenCypher, but FalkorDB's `GRAPH.QUERY` runs one statement at a time (no bulk script import like Neo4j's `cypher-shell`), so prefer `--falkordb-push` to load a graph. Use this only when you want the portable `cypher.txt` artifact:

```bash
if [ ! -f graphify-out/.graphify_python ]; then
    echo "Missing graphify-out/.graphify_python; rerun Step 1 interpreter bootstrap." >&2
    exit 1
fi
_GRAPHIFY_SAVED=$(cat graphify-out/.graphify_python)
case "$_GRAPHIFY_SAVED" in
    /*) ;;
    *) echo "Invalid graphify interpreter pointer." >&2; exit 1 ;;
esac
if [ ! -x "$_GRAPHIFY_SAVED" ]; then
    echo "Saved graphify interpreter is not executable." >&2
    exit 1
fi
"$_GRAPHIFY_SAVED" -E -P -B -c 'import graphify, sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)' >/dev/null 2>&1 || {
    echo "Saved graphify interpreter is unsupported or cannot import graphify." >&2
    exit 1
}
"$(cat graphify-out/.graphify_python)" -E -P -B -m graphify export falkordb
```

**If `--falkordb-push <uri>`** - push directly to a running FalkorDB instance. Credentials are optional; ask the user only if the instance requires auth:

```bash
if [ ! -f graphify-out/.graphify_python ]; then
    echo "Missing graphify-out/.graphify_python; rerun Step 1 interpreter bootstrap." >&2
    exit 1
fi
_GRAPHIFY_SAVED=$(cat graphify-out/.graphify_python)
case "$_GRAPHIFY_SAVED" in
    /*) ;;
    *) echo "Invalid graphify interpreter pointer." >&2; exit 1 ;;
esac
if [ ! -x "$_GRAPHIFY_SAVED" ]; then
    echo "Saved graphify interpreter is not executable." >&2
    exit 1
fi
"$_GRAPHIFY_SAVED" -E -P -B -c 'import graphify, sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)' >/dev/null 2>&1 || {
    echo "Saved graphify interpreter is unsupported or cannot import graphify." >&2
    exit 1
}
"$(cat graphify-out/.graphify_python)" -E -P -B -m graphify export falkordb --push falkordb://localhost:6379
```

Default URI is `falkordb://localhost:6379` (the scheme is informational - `redis://` or a bare `host:port` work too), auth is optional, and the target graph defaults to `graphify`. Uses MERGE - safe to re-run without creating duplicates.

### Step 7b - SVG export (only if --svg flag)

```bash
if [ ! -f graphify-out/.graphify_python ]; then
    echo "Missing graphify-out/.graphify_python; rerun Step 1 interpreter bootstrap." >&2
    exit 1
fi
_GRAPHIFY_SAVED=$(cat graphify-out/.graphify_python)
case "$_GRAPHIFY_SAVED" in
    /*) ;;
    *) echo "Invalid graphify interpreter pointer." >&2; exit 1 ;;
esac
if [ ! -x "$_GRAPHIFY_SAVED" ]; then
    echo "Saved graphify interpreter is not executable." >&2
    exit 1
fi
"$_GRAPHIFY_SAVED" -E -P -B -c 'import graphify, sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)' >/dev/null 2>&1 || {
    echo "Saved graphify interpreter is unsupported or cannot import graphify." >&2
    exit 1
}
"$(cat graphify-out/.graphify_python)" -E -P -B -m graphify export svg
```

### Step 7c - GraphML export (only if --graphml flag)

```bash
if [ ! -f graphify-out/.graphify_python ]; then
    echo "Missing graphify-out/.graphify_python; rerun Step 1 interpreter bootstrap." >&2
    exit 1
fi
_GRAPHIFY_SAVED=$(cat graphify-out/.graphify_python)
case "$_GRAPHIFY_SAVED" in
    /*) ;;
    *) echo "Invalid graphify interpreter pointer." >&2; exit 1 ;;
esac
if [ ! -x "$_GRAPHIFY_SAVED" ]; then
    echo "Saved graphify interpreter is not executable." >&2
    exit 1
fi
"$_GRAPHIFY_SAVED" -E -P -B -c 'import graphify, sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)' >/dev/null 2>&1 || {
    echo "Saved graphify interpreter is unsupported or cannot import graphify." >&2
    exit 1
}
"$(cat graphify-out/.graphify_python)" -E -P -B -m graphify export graphml
```

### Step 7d - MCP server (only if --mcp flag)

```bash
if [ ! -f graphify-out/.graphify_python ]; then
    echo "Missing graphify-out/.graphify_python; rerun Step 1 interpreter bootstrap." >&2
    exit 1
fi
_GRAPHIFY_SAVED=$(cat graphify-out/.graphify_python)
case "$_GRAPHIFY_SAVED" in
    /*) ;;
    *) echo "Invalid graphify interpreter pointer." >&2; exit 1 ;;
esac
if [ ! -x "$_GRAPHIFY_SAVED" ]; then
    echo "Saved graphify interpreter is not executable." >&2
    exit 1
fi
"$_GRAPHIFY_SAVED" -E -P -B -c 'import graphify, sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)' >/dev/null 2>&1 || {
    echo "Saved graphify interpreter is unsupported or cannot import graphify." >&2
    exit 1
}
"$(cat graphify-out/.graphify_python)" -E -P -B -m graphify.serve graphify-out/graph.json
```

This starts a stdio MCP server that exposes tools: `query_graph`, `get_node`, `get_neighbors`, `get_community`, `god_nodes`, `graph_stats`, `shortest_path`. Add to Claude Desktop or any MCP-compatible agent orchestrator so other agents can query the graph live.

To configure in Claude Desktop, add to `claude_desktop_config.json`. Claude Desktop can't run `$(...)`, and under `uv tool install` the system `python3` can't import graphify — so set `command` to the **absolute interpreter path** printed by `cat graphify-out/.graphify_python`:
```json
{
  "mcpServers": {
    "graphify": {
      "command": "<absolute path from: cat graphify-out/.graphify_python>",
      "args": ["-E", "-P", "-B", "-m", "graphify.serve", "/absolute/path/to/graphify-out/graph.json"]
    }
  }
}
```

### Step 8 - Token reduction benchmark (only if total_words > 5000)

If `total_words` from `graphify-out/.graphify_detect.json` is greater than 5,000, run:

```bash
if [ ! -f graphify-out/.graphify_python ]; then
    echo "Missing graphify-out/.graphify_python; rerun Step 1 interpreter bootstrap." >&2
    exit 1
fi
_GRAPHIFY_SAVED=$(cat graphify-out/.graphify_python)
case "$_GRAPHIFY_SAVED" in
    /*) ;;
    *) echo "Invalid graphify interpreter pointer." >&2; exit 1 ;;
esac
if [ ! -x "$_GRAPHIFY_SAVED" ]; then
    echo "Saved graphify interpreter is not executable." >&2
    exit 1
fi
"$_GRAPHIFY_SAVED" -E -P -B -c 'import graphify, sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)' >/dev/null 2>&1 || {
    echo "Saved graphify interpreter is unsupported or cannot import graphify." >&2
    exit 1
}
"$(cat graphify-out/.graphify_python)" -E -P -B -m graphify benchmark
```

Print the output directly in chat. If `total_words <= 5000`, skip silently - the graph value is structural clarity, not token compression, for small corpora.
