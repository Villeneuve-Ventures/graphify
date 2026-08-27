# graphify reference: commit hook and native AGENTS.md integration

Load this when the user asked to install the post-commit hook or wire graphify into a project's AGENTS.md.

## For git commit hook

Install a post-commit hook that auto-rebuilds the graph after every commit. No background process needed - triggers once per commit, works with any editor.

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
"$(cat graphify-out/.graphify_python)" -E -P -B -m graphify hook install    # install
"$(cat graphify-out/.graphify_python)" -E -P -B -m graphify hook uninstall  # remove
"$(cat graphify-out/.graphify_python)" -E -P -B -m graphify hook status     # check
```

After every `git commit`, the hook detects which code files changed (via `git diff HEAD~1`), re-runs AST extraction on those files, and rebuilds `graph.json` and `GRAPH_REPORT.md`. Doc/image changes are ignored by the hook - run `/graphify --update` manually for those.

If a post-commit hook already exists, graphify appends to it rather than replacing it.

---

## For native AGENTS.md integration

Run once per project to make graphify always-on in your agent sessions:

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
"$(cat graphify-out/.graphify_python)" -E -P -B -m graphify agents install
```

This writes a `## graphify` section to the local `AGENTS.md` that instructs your agent to check the graph before answering codebase questions and rebuild it after code changes. No manual `/graphify` needed in future sessions.

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
"$(cat graphify-out/.graphify_python)" -E -P -B -m graphify agents uninstall  # remove the section
```
