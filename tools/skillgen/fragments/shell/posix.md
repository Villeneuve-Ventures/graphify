```bash
# Detect the correct Python interpreter (handles uv tool, pipx, venv, system installs)
PYTHON=""
PYTHON_VERSION_CHECK='import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)'
is_supported_python() {
    [ -n "$1" ] && "$1" -E -P -B -c "$PYTHON_VERSION_CHECK" >/dev/null 2>&1
}
is_supported_graphify_python() {
    is_supported_python "$1" && "$1" -E -P -B -c "import graphify" >/dev/null 2>&1
}
GRAPHIFY_BIN=$(command -v graphify 2>/dev/null)
# 1. uv tool installs — most reliable on modern Mac/Linux
if [ -z "$PYTHON" ] && command -v uv >/dev/null 2>&1; then
    _UV_TOOL_DIR=$(uv tool dir 2>/dev/null)
    _UV_PY="${_UV_TOOL_DIR:+$_UV_TOOL_DIR/graphifyy/bin/python}"
    if is_supported_graphify_python "$_UV_PY"; then PYTHON="$_UV_PY"; fi
fi
# 2. Read shebang from graphify binary (pipx and direct pip installs)
if [ -z "$PYTHON" ] && [ -n "$GRAPHIFY_BIN" ]; then
    _SHEBANG=$(head -1 "$GRAPHIFY_BIN" | tr -d '#!')
    case "$_SHEBANG" in
        "/usr/bin/env "*)
            _ENV_COMMAND=${_SHEBANG#"/usr/bin/env "}
            case "$_ENV_COMMAND" in
                ""|*[!a-zA-Z0-9_.@+-]*) _SHEBANG="" ;;
                *) # Resolve exactly one allowlisted command name to an absolute path.
                    _ENV_PATH=$(command -v "$_ENV_COMMAND" 2>/dev/null)
                    case "$_ENV_PATH" in
                        /*) _SHEBANG="$_ENV_PATH" ;;
                        *) _SHEBANG="" ;;
                    esac # resolved /usr/bin/env interpreter path
                    ;; # accepted /usr/bin/env command
            esac # /usr/bin/env command allowlist
            is_supported_graphify_python "$_SHEBANG" && PYTHON="$_SHEBANG" ;;
        *[!a-zA-Z0-9/_.@-]*) ;;
        *) is_supported_graphify_python "$_SHEBANG" && PYTHON="$_SHEBANG" ;;
    esac
fi
# 3. Select a supported interpreter for a direct pip install
if [ -z "$PYTHON" ]; then
    for _CANDIDATE in python3.14 python3; do
        _CANDIDATE_PATH=$(command -v "$_CANDIDATE" 2>/dev/null)
        if is_supported_python "$_CANDIDATE_PATH"; then
            PYTHON="$_CANDIDATE_PATH"
            break
        fi
    done
fi
if ! is_supported_graphify_python "$PYTHON"; then
    if command -v uv >/dev/null 2>&1; then
        uv tool install --python '>=3.14.2,<3.15' --upgrade graphifyy -q 2>&1 | tail -3
        _UV_TOOL_DIR=$(uv tool dir 2>/dev/null)
        _UV_PY="${_UV_TOOL_DIR:+$_UV_TOOL_DIR/graphifyy/bin/python}"
        if is_supported_graphify_python "$_UV_PY"; then PYTHON="$_UV_PY"; fi
    else
        [ -n "$PYTHON" ] && { "$PYTHON" -E -P -B -m pip install graphifyy -q 2>/dev/null \
          || "$PYTHON" -E -P -B -m pip install graphifyy -q --break-system-packages 2>&1 | tail -3; }
    fi
fi
if ! is_supported_graphify_python "$PYTHON"; then
    echo "Graphify requires Python 3.14.2 through the final 3.14.x release." >&2
    exit 1
fi
# Write interpreter path for all subsequent steps (persists across invocations)
"$PYTHON" -E -P -B -c 'from pathlib import Path; Path("graphify-out").mkdir(parents=True, exist_ok=True)' || exit 1
"$PYTHON" -E -P -B -m graphify.interpreter_pointer write graphify-out/.graphify_python || exit 1

# Full-build transaction handoff. The token binds the canonical input root and
# the actual output directory; ids/roots in the environment are never authority.
GRAPHIFY_TRANSACTION_TOKEN=$(
    "$PYTHON" -E -P -B -c 'import sys; from pathlib import Path; from graphify.transaction import begin_transaction, stage_transaction_handoff; root=Path(sys.argv[1]).resolve(strict=True); output=(root / "graphify-out").resolve(); print(stage_transaction_handoff(begin_transaction("full", root, output=output)).path, end="")' INPUT_PATH
) || exit $?
export GRAPHIFY_TRANSACTION_TOKEN
graphify_transaction_python() {
    [ -n "${GRAPHIFY_TRANSACTION_TOKEN-}" ] || { echo "missing immutable Graphify transaction token" >&2; return 1; }
    "$PYTHON" -E -P -B -m graphify.transaction run-token "$GRAPHIFY_TRANSACTION_TOKEN" -- "$@"
}
```

If the import succeeds, print nothing and move straight to Step 2.

For a full build with an explicit `INPUT_PATH`, persist the scan root in a separate block:

```bash
GRAPHIFY_TRANSACTION_TOKEN=$("$GRAPHIFY_PYTHON" -E -P -B -c 'from graphify.transaction import active_transaction_token_path; print(active_transaction_token_path())') || exit $?; "$GRAPHIFY_PYTHON" -E -P -B -m graphify.transaction run-prepared-token "$GRAPHIFY_TRANSACTION_TOKEN" -- -c 'import sys; from pathlib import Path; Path(".graphify_root").write_text(str(Path(sys.argv[1]).resolve(strict=True)), encoding="utf-8")' INPUT_PATH
```

Do not run that scan-root block for no-path subcommands such as `query`, `path`,
`explain`, hooks, installs, or exports. The interpreter bootstrap and
`.graphify_python` persistence are independent of `.graphify_root`.

**In every subsequent bash block, replace `python3` with `"$(cat graphify-out/.graphify_python)" -E -P -B` to use the correct interpreter without importing project-local or `PYTHONPATH` shadows or writing bytecode.**

The saved interpreter and its user-site packages are trusted inputs outside the
inspected-corpus boundary. Pointer symlink and time-of-check/time-of-use hardening
remain separate work; these startup flags do not provide that identity guarantee.
