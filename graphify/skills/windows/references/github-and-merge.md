# graphify reference: GitHub clone and cross-repo merge

Load this when the user passed one or more `https://github.com/...` URLs, or named several local subfolders to merge into one graph.

### Step 0 - Clone GitHub repo(s) (only if a GitHub URL was given)

**Single repo:**
```powershell
if (-not (Test-Path -LiteralPath graphify-out\.graphify_python -PathType Leaf)) {
    throw "Missing graphify-out\.graphify_python; rerun Step 1 interpreter bootstrap."
}
$GraphifySaved = (Get-Content -LiteralPath graphify-out\.graphify_python -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($GraphifySaved) -or
    -not [IO.Path]::IsPathFullyQualified($GraphifySaved) -or
    $GraphifySaved -match "[\r\n]") {
    throw "Invalid graphify interpreter pointer."
}
& $GraphifySaved -E -P -B -c "import graphify, sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and sys.version_info.releaselevel == 'final' and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Saved graphify interpreter is unsupported or cannot import graphify."
}
& (Get-Content graphify-out\.graphify_python) -E -P -B -m graphify clone <github-url> [--branch <branch>]
# Use the printed local path as the target for all subsequent steps.
```

**Multiple repos (cross-repo graph):**
```powershell
if (-not (Test-Path -LiteralPath graphify-out\.graphify_python -PathType Leaf)) {
    throw "Missing graphify-out\.graphify_python; rerun Step 1 interpreter bootstrap."
}
$GraphifySaved = (Get-Content -LiteralPath graphify-out\.graphify_python -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($GraphifySaved) -or
    -not [IO.Path]::IsPathFullyQualified($GraphifySaved) -or
    $GraphifySaved -match "[\r\n]") {
    throw "Invalid graphify interpreter pointer."
}
& $GraphifySaved -E -P -B -c "import graphify, sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and sys.version_info.releaselevel == 'final' and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Saved graphify interpreter is unsupported or cannot import graphify."
}
# Clone each repo, run the full pipeline on each, then merge
& (Get-Content graphify-out\.graphify_python) -E -P -B -m graphify clone <url1>   # → ~/.graphify/repos/<owner1>/<repo1>
& (Get-Content graphify-out\.graphify_python) -E -P -B -m graphify clone <url2>   # → ~/.graphify/repos/<owner2>/<repo2>
# Run /graphify on each local path to produce their graph.json files
# Then merge:
& (Get-Content graphify-out\.graphify_python) -E -P -B -m graphify merge-graphs ~/.graphify/repos/<owner1>/<repo1>/graphify-out/graph.json ~/.graphify/repos/<owner2>/<repo2>/graphify-out/graph.json --out graphify-out/cross-repo-graph.json
```

Graphify clones into `~/.graphify/repos/<owner>/<repo>` and reuses existing clones on repeat runs. Each node in the merged graph carries a `repo` attribute so you can filter by origin.

**Multiple local subfolders (monorepo or multi-service layout):**

The skill pipeline writes all intermediate and final outputs to `graphify-out/` in the current working directory. Running the skill on each subfolder separately will clobber the same output dir. Instead, use the CLI directly for each subfolder — it places `graphify-out/` *inside* the scanned path:

```powershell
if (-not (Test-Path -LiteralPath graphify-out\.graphify_python -PathType Leaf)) {
    throw "Missing graphify-out\.graphify_python; rerun Step 1 interpreter bootstrap."
}
$GraphifySaved = (Get-Content -LiteralPath graphify-out\.graphify_python -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($GraphifySaved) -or
    -not [IO.Path]::IsPathFullyQualified($GraphifySaved) -or
    $GraphifySaved -match "[\r\n]") {
    throw "Invalid graphify interpreter pointer."
}
& $GraphifySaved -E -P -B -c "import graphify, sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and sys.version_info.releaselevel == 'final' and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Saved graphify interpreter is unsupported or cannot import graphify."
}
& (Get-Content graphify-out\.graphify_python) -E -P -B -m graphify extract ./core/     # → ./core/graphify-out/graph.json
& (Get-Content graphify-out\.graphify_python) -E -P -B -m graphify extract ./service/  # → ./service/graphify-out/graph.json
& (Get-Content graphify-out\.graphify_python) -E -P -B -m graphify extract ./platform/ # → ./platform/graphify-out/graph.json
# Add --backend gemini|kimi|openai|deepseek|claude-cli depending on which API key you have set

# Then merge at the project root:
& (Get-Content graphify-out\.graphify_python) -E -P -B -m graphify merge-graphs ./core/graphify-out/graph.json ./service/graphify-out/graph.json ./platform/graphify-out/graph.json --out graphify-out/graph.json
```

Once `graphify-out/graph.json` exists, the fast path above takes over: any codebase question runs `graphify query` directly on the merged graph — no re-extraction, no size gate.
