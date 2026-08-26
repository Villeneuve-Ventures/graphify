# graphify reference: commit hook and native CLAUDE.md integration

Load this when the user asked to install the post-commit hook or wire graphify into a project's CLAUDE.md.

## For git commit hook

Install a post-commit hook that auto-rebuilds the graph after every commit. No background process needed - triggers once per commit, works with any editor.

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
& (Get-Content graphify-out\.graphify_python) -E -P -B -m graphify hook install    # install
& (Get-Content graphify-out\.graphify_python) -E -P -B -m graphify hook uninstall  # remove
& (Get-Content graphify-out\.graphify_python) -E -P -B -m graphify hook status     # check
```

After every `git commit`, the hook detects which code files changed (via `git diff HEAD~1`), re-runs AST extraction on those files, and rebuilds `graph.json` and `GRAPH_REPORT.md`. Doc/image changes are ignored by the hook - run `/graphify --update` manually for those.

If a post-commit hook already exists, graphify appends to it rather than replacing it.

---

## For native CLAUDE.md integration

Run once per project to make graphify always-on in Claude Code sessions:

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
& (Get-Content graphify-out\.graphify_python) -E -P -B -m graphify claude install
```

This writes a `## graphify` section to the local `CLAUDE.md` that instructs Claude to check the graph before answering codebase questions and rebuild it after code changes. No manual `/graphify` needed in future sessions.

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
& (Get-Content graphify-out\.graphify_python) -E -P -B -m graphify claude uninstall  # remove the section
```
