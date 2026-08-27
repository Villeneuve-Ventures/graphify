# graphify reference: query, path, explain

Load this when the user asks a question against an existing graph, or runs `/graphify path` or `/graphify explain`. The core's query stub points here for the full traversal flow. These flows use the `graphify query` CLI when it is available and fall back to an inline NetworkX traversal otherwise.

Two traversal modes - choose based on the question:

| Mode | Flag | Best for |
|------|------|----------|
| BFS (default) | _(none)_ | "What is X connected to?" - broad context, nearest neighbors first |
| DFS | `--dfs` | "How does X reach Y?" - trace a specific chain or dependency path |

First check the graph exists:
```bash
eval "$(printf '%b' 'GRAPHIFY_PYTHON=""\n_GRAPHIFY_VERSION_CHECK='"'"'import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)'"'"'\n_GRAPHIFY_WORKSPACE=$(/bin/pwd -P) || exit 1\n_graphify_canonical_root() {\n    _gfy_root=$1; [ -n "$_gfy_root" ] || return 1\n    case "$_gfy_root" in /*) ;; *) _gfy_root=$_GRAPHIFY_WORKSPACE/$_gfy_root ;; esac\n    [ -d "$_gfy_root" ] && CDPATH= cd -P -- "$_gfy_root" 2>/dev/null && /bin/pwd -P\n}\n_GRAPHIFY_INPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_INPUT_PATH-}") || _GRAPHIFY_INPUT_ROOT=""; _GRAPHIFY_OUTPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_OUTPUT_ROOT-${GRAPHIFY_OUT-graphify-out}}") || _GRAPHIFY_OUTPUT_ROOT=""\n_graphify_path_denied() { _gfy_policy_path=$1; [ "$_GRAPHIFY_WORKSPACE" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_WORKSPACE"|"$_GRAPHIFY_WORKSPACE"/*) return 0 ;; esac; [ -z "$_GRAPHIFY_INPUT_ROOT" ] || { [ "$_GRAPHIFY_INPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_INPUT_ROOT"|"$_GRAPHIFY_INPUT_ROOT"/*) return 0 ;; esac; }; [ -z "$_GRAPHIFY_OUTPUT_ROOT" ] || { [ "$_GRAPHIFY_OUTPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_OUTPUT_ROOT"|"$_GRAPHIFY_OUTPUT_ROOT"/*) return 0 ;; esac; }; return 1; }\n_graphify_resolve_ambient() {\n    _gfy_lexical=$1; case "$_gfy_lexical" in /*) ;; *) return 1 ;; esac\n    _graphify_path_denied "$_gfy_lexical" && return 1; _gfy_path=$_gfy_lexical\n    _gfy_links=0\n    while [ -L "$_gfy_path" ]; do\n        _gfy_links=$((_gfy_links + 1))\n        [ "$_gfy_links" -le 40 ] || return 1\n        [ -x /usr/bin/readlink ] || return 1\n        _gfy_link=$(/usr/bin/readlink "$_gfy_path") || return 1\n        case "$_gfy_link" in\n            /*) _gfy_path=$_gfy_link ;;\n            *) _gfy_dir=${_gfy_path%/*}; _gfy_path=$_gfy_dir/$_gfy_link ;;\n        esac\n    done\n    _gfy_dir=${_gfy_path%/*}; _gfy_base=${_gfy_path##*/}\n    _gfy_dir=$(CDPATH= cd -P -- "$_gfy_dir" 2>/dev/null && /bin/pwd -P) || return 1\n    _gfy_path=$_gfy_dir/$_gfy_base\n    _graphify_path_denied "$_gfy_path" && return 1\n    [ -x "$_gfy_lexical" ] || return 1\n    GRAPHIFY_RESOLVED=$_gfy_lexical\n}\n_graphify_command() {\n    _gfy_found=$(command -v "$1" 2>/dev/null) || return 1\n    _graphify_resolve_ambient "$_gfy_found"\n}\n_graphify_supported() {\n    [ -n "$1" ] && "$1" -E -P -B -c "$_GRAPHIFY_VERSION_CHECK" >/dev/null 2>&1\n}\n_graphify_usable() {\n    _graphify_supported "$1" && "$1" -E -P -B -c '"'"'import graphify'"'"' >/dev/null 2>&1\n}\n# An explicit absolute active environment is caller-selected, including a\n# project-local venv. Keep its lexical path for invocation.\ncase "${VIRTUAL_ENV-}" in\n    /*) _gfy_venv_python=$VIRTUAL_ENV/bin/python\n        _graphify_usable "$_gfy_venv_python" && GRAPHIFY_PYTHON=$_gfy_venv_python ;;\nesac\n# Trusted uv and pipx metadata, then trusted candidates derived from it.\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command uv; then\n    _gfy_uv=$GRAPHIFY_RESOLVED\n    _gfy_uv_dir=$("$_gfy_uv" tool dir 2>/dev/null)\n    _gfy_candidate=${_gfy_uv_dir:+$_gfy_uv_dir/graphifyy/bin/python}\n    if _graphify_resolve_ambient "$_gfy_candidate"; then\n        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED\n    fi\nfi\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command pipx; then\n    _gfy_pipx=$GRAPHIFY_RESOLVED\n    _gfy_pipx_home=$("$_gfy_pipx" environment --value PIPX_LOCAL_VENVS 2>/dev/null)\n    _gfy_candidate=${_gfy_pipx_home:+$_gfy_pipx_home/graphifyy/bin/python}\n    if _graphify_resolve_ambient "$_gfy_candidate"; then\n        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED\n    fi\nfi\n# Console-script shebang covers direct and pipx installs without executing the launcher.\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command graphify; then\n    _gfy_graphify=$GRAPHIFY_RESOLVED\n    IFS= read -r _gfy_shebang < "$_gfy_graphify" || _gfy_shebang=""\n    _gfy_shebang=${_gfy_shebang#\\#!}\n    case "$_gfy_shebang" in\n        "/usr/bin/env "*)\n            _gfy_env_command=${_gfy_shebang#"/usr/bin/env "}\n            case "$_gfy_env_command" in\n                ""|*[!a-zA-Z0-9_.@+-]*) _gfy_shebang="" ;;\n                *) if _graphify_command "$_gfy_env_command"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;\n            esac ;;\n        *[!a-zA-Z0-9/_.@+-]*) _gfy_shebang="" ;;\n        *) if _graphify_resolve_ambient "$_gfy_shebang"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;\n    esac\n    _graphify_usable "$_gfy_shebang" && GRAPHIFY_PYTHON=$_gfy_shebang\nfi\nif [ -z "$GRAPHIFY_PYTHON" ]; then\n    for _gfy_name in python3.14 python3 python; do\n        if _graphify_command "$_gfy_name"; then\n            _graphify_usable "$GRAPHIFY_RESOLVED" && { GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; break; }\n        fi\n    done\nfi\nif [ -z "$GRAPHIFY_PYTHON" ] && [ "${GRAPHIFY_DISCOVERY_OPTIONAL-0}" != 1 ]; then\n    echo "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." >&2\n    exit 1\nfi')"
"$GRAPHIFY_PYTHON" -E -P -B -c "
from pathlib import Path
if not Path('graphify-out/graph.json').exists():
    print('ERROR: No graph found. Run /graphify <path> first to build the graph.')
    raise SystemExit(1)
"
```
If it fails, stop and tell the user to run `/graphify <path>` first.

### Step 0 — Constrained query expansion (REQUIRED before traversal)

graphify's `query` CLI matches nodes via case-folded substring + IDF — there is **no stemming, no synonyms, no cross-language match** inside the binary, and the inline fallback below matches the same way. If the user's question uses different language or different domain vocabulary than the graph's labels (user says "обработчик" / graph says "handler"; user says "authentication" / graph says "Guardian"), the literal matcher returns 0 hits and the answer collapses to noise.

Fix this **without inventing tokens** by expanding the query against the actual graph vocabulary first:

1. Extract the token vocabulary from node labels:
```bash
eval "$(printf '%b' 'GRAPHIFY_PYTHON=""\n_GRAPHIFY_VERSION_CHECK='"'"'import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)'"'"'\n_GRAPHIFY_WORKSPACE=$(/bin/pwd -P) || exit 1\n_graphify_canonical_root() {\n    _gfy_root=$1; [ -n "$_gfy_root" ] || return 1\n    case "$_gfy_root" in /*) ;; *) _gfy_root=$_GRAPHIFY_WORKSPACE/$_gfy_root ;; esac\n    [ -d "$_gfy_root" ] && CDPATH= cd -P -- "$_gfy_root" 2>/dev/null && /bin/pwd -P\n}\n_GRAPHIFY_INPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_INPUT_PATH-}") || _GRAPHIFY_INPUT_ROOT=""; _GRAPHIFY_OUTPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_OUTPUT_ROOT-${GRAPHIFY_OUT-graphify-out}}") || _GRAPHIFY_OUTPUT_ROOT=""\n_graphify_path_denied() { _gfy_policy_path=$1; [ "$_GRAPHIFY_WORKSPACE" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_WORKSPACE"|"$_GRAPHIFY_WORKSPACE"/*) return 0 ;; esac; [ -z "$_GRAPHIFY_INPUT_ROOT" ] || { [ "$_GRAPHIFY_INPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_INPUT_ROOT"|"$_GRAPHIFY_INPUT_ROOT"/*) return 0 ;; esac; }; [ -z "$_GRAPHIFY_OUTPUT_ROOT" ] || { [ "$_GRAPHIFY_OUTPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_OUTPUT_ROOT"|"$_GRAPHIFY_OUTPUT_ROOT"/*) return 0 ;; esac; }; return 1; }\n_graphify_resolve_ambient() {\n    _gfy_lexical=$1; case "$_gfy_lexical" in /*) ;; *) return 1 ;; esac\n    _graphify_path_denied "$_gfy_lexical" && return 1; _gfy_path=$_gfy_lexical\n    _gfy_links=0\n    while [ -L "$_gfy_path" ]; do\n        _gfy_links=$((_gfy_links + 1))\n        [ "$_gfy_links" -le 40 ] || return 1\n        [ -x /usr/bin/readlink ] || return 1\n        _gfy_link=$(/usr/bin/readlink "$_gfy_path") || return 1\n        case "$_gfy_link" in\n            /*) _gfy_path=$_gfy_link ;;\n            *) _gfy_dir=${_gfy_path%/*}; _gfy_path=$_gfy_dir/$_gfy_link ;;\n        esac\n    done\n    _gfy_dir=${_gfy_path%/*}; _gfy_base=${_gfy_path##*/}\n    _gfy_dir=$(CDPATH= cd -P -- "$_gfy_dir" 2>/dev/null && /bin/pwd -P) || return 1\n    _gfy_path=$_gfy_dir/$_gfy_base\n    _graphify_path_denied "$_gfy_path" && return 1\n    [ -x "$_gfy_lexical" ] || return 1\n    GRAPHIFY_RESOLVED=$_gfy_lexical\n}\n_graphify_command() {\n    _gfy_found=$(command -v "$1" 2>/dev/null) || return 1\n    _graphify_resolve_ambient "$_gfy_found"\n}\n_graphify_supported() {\n    [ -n "$1" ] && "$1" -E -P -B -c "$_GRAPHIFY_VERSION_CHECK" >/dev/null 2>&1\n}\n_graphify_usable() {\n    _graphify_supported "$1" && "$1" -E -P -B -c '"'"'import graphify'"'"' >/dev/null 2>&1\n}\n# An explicit absolute active environment is caller-selected, including a\n# project-local venv. Keep its lexical path for invocation.\ncase "${VIRTUAL_ENV-}" in\n    /*) _gfy_venv_python=$VIRTUAL_ENV/bin/python\n        _graphify_usable "$_gfy_venv_python" && GRAPHIFY_PYTHON=$_gfy_venv_python ;;\nesac\n# Trusted uv and pipx metadata, then trusted candidates derived from it.\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command uv; then\n    _gfy_uv=$GRAPHIFY_RESOLVED\n    _gfy_uv_dir=$("$_gfy_uv" tool dir 2>/dev/null)\n    _gfy_candidate=${_gfy_uv_dir:+$_gfy_uv_dir/graphifyy/bin/python}\n    if _graphify_resolve_ambient "$_gfy_candidate"; then\n        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED\n    fi\nfi\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command pipx; then\n    _gfy_pipx=$GRAPHIFY_RESOLVED\n    _gfy_pipx_home=$("$_gfy_pipx" environment --value PIPX_LOCAL_VENVS 2>/dev/null)\n    _gfy_candidate=${_gfy_pipx_home:+$_gfy_pipx_home/graphifyy/bin/python}\n    if _graphify_resolve_ambient "$_gfy_candidate"; then\n        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED\n    fi\nfi\n# Console-script shebang covers direct and pipx installs without executing the launcher.\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command graphify; then\n    _gfy_graphify=$GRAPHIFY_RESOLVED\n    IFS= read -r _gfy_shebang < "$_gfy_graphify" || _gfy_shebang=""\n    _gfy_shebang=${_gfy_shebang#\\#!}\n    case "$_gfy_shebang" in\n        "/usr/bin/env "*)\n            _gfy_env_command=${_gfy_shebang#"/usr/bin/env "}\n            case "$_gfy_env_command" in\n                ""|*[!a-zA-Z0-9_.@+-]*) _gfy_shebang="" ;;\n                *) if _graphify_command "$_gfy_env_command"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;\n            esac ;;\n        *[!a-zA-Z0-9/_.@+-]*) _gfy_shebang="" ;;\n        *) if _graphify_resolve_ambient "$_gfy_shebang"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;\n    esac\n    _graphify_usable "$_gfy_shebang" && GRAPHIFY_PYTHON=$_gfy_shebang\nfi\nif [ -z "$GRAPHIFY_PYTHON" ]; then\n    for _gfy_name in python3.14 python3 python; do\n        if _graphify_command "$_gfy_name"; then\n            _graphify_usable "$GRAPHIFY_RESOLVED" && { GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; break; }\n        fi\n    done\nfi\nif [ -z "$GRAPHIFY_PYTHON" ] && [ "${GRAPHIFY_DISCOVERY_OPTIONAL-0}" != 1 ]; then\n    echo "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." >&2\n    exit 1\nfi')"
"$GRAPHIFY_PYTHON" -E -P -B -c "
import json, re
from pathlib import Path
data = json.loads(Path('graphify-out/graph.json').read_text(encoding='utf-8'))
vocab = set()
for n in data['nodes']:
    for c in re.findall(r'[^\W\d_]+', n.get('label','') or '', re.UNICODE):
        parts = re.findall(r'[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+', c) or [c]
        for p in parts:
            t = p.lower()
            if 3 <= len(t) <= 30:
                vocab.add(t)
Path('graphify-out/.vocab.txt').write_text('\n'.join(sorted(vocab)), encoding='utf-8')
print(f'vocab: {len(vocab)} tokens')
"
```

2. Read `graphify-out/.vocab.txt`. Then for the user's question, select **up to 12 tokens from this exact list** that semantically match the query intent. Hard constraints:
   - You MUST pick only tokens present in the vocabulary file. Do NOT invent tokens.
   - If a query concept has no plausible token in the vocab, skip it — do not substitute a near-synonym from training memory.
   - If **no** vocab tokens match the query at all, output an empty list and tell the user the corpus has no relevant vocabulary for this question. Do not fabricate a search.
   - Translate cross-language: Russian "аутентификация" → look for `auth`, `credential`, `token`, `security` IFF present in vocab.
   - Morphology: "handlers" maps to `handler` IFF present; "todos" maps to `todo` IFF present.

3. Print the selection explicitly to the user before running the query, so the expansion is auditable:
```
Query expanded to (from graph vocab, N tokens): [token1, token2, ...]
```
If the list is empty, say so plainly and stop — do not proceed to traversal.

### Step 1 — Traversal

Build the **expanded query string** by joining the selected tokens with spaces. Use this string as `QUESTION` below — NOT the original user question. (The original question is preserved only for `save-result` at the end.)

Prefer the CLI when it is installed:
```bash
GRAPHIFY_INPUT_PATH='INPUT_PATH'
# Installation is the only discovery path allowed to mutate the environment.
GRAPHIFY_DISCOVERY_OPTIONAL=1
GRAPHIFY_PYTHON=""
_GRAPHIFY_VERSION_CHECK='import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)'
_GRAPHIFY_WORKSPACE=$(/bin/pwd -P) || exit 1
_graphify_canonical_root() {
    _gfy_root=$1; [ -n "$_gfy_root" ] || return 1
    case "$_gfy_root" in /*) ;; *) _gfy_root=$_GRAPHIFY_WORKSPACE/$_gfy_root ;; esac
    [ -d "$_gfy_root" ] && CDPATH= cd -P -- "$_gfy_root" 2>/dev/null && /bin/pwd -P
}
_GRAPHIFY_INPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_INPUT_PATH-}") || _GRAPHIFY_INPUT_ROOT=""; _GRAPHIFY_OUTPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_OUTPUT_ROOT-${GRAPHIFY_OUT-graphify-out}}") || _GRAPHIFY_OUTPUT_ROOT=""
_graphify_path_denied() { _gfy_policy_path=$1; [ "$_GRAPHIFY_WORKSPACE" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_WORKSPACE"|"$_GRAPHIFY_WORKSPACE"/*) return 0 ;; esac; [ -z "$_GRAPHIFY_INPUT_ROOT" ] || { [ "$_GRAPHIFY_INPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_INPUT_ROOT"|"$_GRAPHIFY_INPUT_ROOT"/*) return 0 ;; esac; }; [ -z "$_GRAPHIFY_OUTPUT_ROOT" ] || { [ "$_GRAPHIFY_OUTPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_OUTPUT_ROOT"|"$_GRAPHIFY_OUTPUT_ROOT"/*) return 0 ;; esac; }; return 1; }
_graphify_resolve_ambient() {
    _gfy_lexical=$1; case "$_gfy_lexical" in /*) ;; *) return 1 ;; esac
    _graphify_path_denied "$_gfy_lexical" && return 1; _gfy_path=$_gfy_lexical
    _gfy_links=0
    while [ -L "$_gfy_path" ]; do
        _gfy_links=$((_gfy_links + 1))
        [ "$_gfy_links" -le 40 ] || return 1
        [ -x /usr/bin/readlink ] || return 1
        _gfy_link=$(/usr/bin/readlink "$_gfy_path") || return 1
        case "$_gfy_link" in
            /*) _gfy_path=$_gfy_link ;;
            *) _gfy_dir=${_gfy_path%/*}; _gfy_path=$_gfy_dir/$_gfy_link ;;
        esac
    done
    _gfy_dir=${_gfy_path%/*}; _gfy_base=${_gfy_path##*/}
    _gfy_dir=$(CDPATH= cd -P -- "$_gfy_dir" 2>/dev/null && /bin/pwd -P) || return 1
    _gfy_path=$_gfy_dir/$_gfy_base
    _graphify_path_denied "$_gfy_path" && return 1
    [ -x "$_gfy_lexical" ] || return 1
    GRAPHIFY_RESOLVED=$_gfy_lexical
}
_graphify_command() {
    _gfy_found=$(command -v "$1" 2>/dev/null) || return 1
    _graphify_resolve_ambient "$_gfy_found"
}
_graphify_supported() {
    [ -n "$1" ] && "$1" -E -P -B -c "$_GRAPHIFY_VERSION_CHECK" >/dev/null 2>&1
}
_graphify_usable() {
    _graphify_supported "$1" && "$1" -E -P -B -c 'import graphify' >/dev/null 2>&1
}
# An explicit absolute active environment is caller-selected, including a
# project-local venv. Keep its lexical path for invocation.
case "${VIRTUAL_ENV-}" in
    /*) _gfy_venv_python=$VIRTUAL_ENV/bin/python
        _graphify_usable "$_gfy_venv_python" && GRAPHIFY_PYTHON=$_gfy_venv_python ;;
esac
# Trusted uv and pipx metadata, then trusted candidates derived from it.
if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command uv; then
    _gfy_uv=$GRAPHIFY_RESOLVED
    _gfy_uv_dir=$("$_gfy_uv" tool dir 2>/dev/null)
    _gfy_candidate=${_gfy_uv_dir:+$_gfy_uv_dir/graphifyy/bin/python}
    if _graphify_resolve_ambient "$_gfy_candidate"; then
        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED
    fi
fi
if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command pipx; then
    _gfy_pipx=$GRAPHIFY_RESOLVED
    _gfy_pipx_home=$("$_gfy_pipx" environment --value PIPX_LOCAL_VENVS 2>/dev/null)
    _gfy_candidate=${_gfy_pipx_home:+$_gfy_pipx_home/graphifyy/bin/python}
    if _graphify_resolve_ambient "$_gfy_candidate"; then
        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED
    fi
fi
# Console-script shebang covers direct and pipx installs without executing the launcher.
if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command graphify; then
    _gfy_graphify=$GRAPHIFY_RESOLVED
    IFS= read -r _gfy_shebang < "$_gfy_graphify" || _gfy_shebang=""
    _gfy_shebang=${_gfy_shebang#\#!}
    case "$_gfy_shebang" in
        "/usr/bin/env "*)
            _gfy_env_command=${_gfy_shebang#"/usr/bin/env "}
            case "$_gfy_env_command" in
                ""|*[!a-zA-Z0-9_.@+-]*) _gfy_shebang="" ;;
                *) if _graphify_command "$_gfy_env_command"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;
            esac ;;
        *[!a-zA-Z0-9/_.@+-]*) _gfy_shebang="" ;;
        *) if _graphify_resolve_ambient "$_gfy_shebang"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;
    esac
    _graphify_usable "$_gfy_shebang" && GRAPHIFY_PYTHON=$_gfy_shebang
fi
if [ -z "$GRAPHIFY_PYTHON" ]; then
    for _gfy_name in python3.14 python3 python; do
        if _graphify_command "$_gfy_name"; then
            _graphify_usable "$GRAPHIFY_RESOLVED" && { GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; break; }
        fi
    done
fi
if [ -z "$GRAPHIFY_PYTHON" ] && [ "${GRAPHIFY_DISCOVERY_OPTIONAL-0}" != 1 ]; then
    echo "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." >&2
    exit 1
fi
unset GRAPHIFY_DISCOVERY_OPTIONAL
PYTHON=$GRAPHIFY_PYTHON
if [ -z "$PYTHON" ]; then
    case "${VIRTUAL_ENV-}" in
        /*) _gfy_candidate=$VIRTUAL_ENV/bin/python
            _graphify_supported "$_gfy_candidate" && PYTHON=$_gfy_candidate ;;
    esac
fi
if [ -z "$PYTHON" ]; then
    for _gfy_name in python3.14 python3 python; do
        if _graphify_command "$_gfy_name"; then
            _graphify_supported "$GRAPHIFY_RESOLVED" && { PYTHON=$GRAPHIFY_RESOLVED; break; }
        fi
    done
fi
if ! _graphify_usable "$PYTHON"; then
    if _graphify_command uv; then
        _gfy_uv=$GRAPHIFY_RESOLVED
        "$_gfy_uv" tool install --python '>=3.14.2,<3.15' --upgrade graphifyy -q
        _gfy_uv_dir=$("$_gfy_uv" tool dir 2>/dev/null)
        _gfy_candidate=${_gfy_uv_dir:+$_gfy_uv_dir/graphifyy/bin/python}
        if _graphify_resolve_ambient "$_gfy_candidate" && _graphify_usable "$GRAPHIFY_RESOLVED"; then
            PYTHON=$GRAPHIFY_RESOLVED
        fi
    elif [ -n "$PYTHON" ]; then
        "$PYTHON" -E -P -B -m pip install graphifyy -q 2>/dev/null           || "$PYTHON" -E -P -B -m pip install graphifyy -q --break-system-packages
    fi
fi
_graphify_usable "$PYTHON" || { echo "Graphify requires CPython 3.14.2 through the final 3.14.x release." >&2; exit 1; }
"$PYTHON" -E -P -B -c 'from pathlib import Path; Path("graphify-out").mkdir(parents=True, exist_ok=True)' || exit 1
"$PYTHON" -E -P -B -m graphify.interpreter_pointer write graphify-out/.graphify_python || exit 1
export PYTHONUTF8=1
```

If the CLI is unavailable, load `graphify-out/graph.json` and run the traversal inline:

1. Find the 1-3 nodes whose label best matches the expanded tokens.
2. Run the appropriate traversal from each starting node.
3. Read the subgraph - node labels, edge relations, confidence tags, source locations.
4. Answer using **only** what the graph contains. Quote `source_location` when citing a specific fact.
5. If the graph lacks enough information, say so - do not hallucinate edges.

```bash
eval "$(printf '%b' 'GRAPHIFY_PYTHON=""\n_GRAPHIFY_VERSION_CHECK='"'"'import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)'"'"'\n_GRAPHIFY_WORKSPACE=$(/bin/pwd -P) || exit 1\n_graphify_canonical_root() {\n    _gfy_root=$1; [ -n "$_gfy_root" ] || return 1\n    case "$_gfy_root" in /*) ;; *) _gfy_root=$_GRAPHIFY_WORKSPACE/$_gfy_root ;; esac\n    [ -d "$_gfy_root" ] && CDPATH= cd -P -- "$_gfy_root" 2>/dev/null && /bin/pwd -P\n}\n_GRAPHIFY_INPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_INPUT_PATH-}") || _GRAPHIFY_INPUT_ROOT=""; _GRAPHIFY_OUTPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_OUTPUT_ROOT-${GRAPHIFY_OUT-graphify-out}}") || _GRAPHIFY_OUTPUT_ROOT=""\n_graphify_path_denied() { _gfy_policy_path=$1; [ "$_GRAPHIFY_WORKSPACE" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_WORKSPACE"|"$_GRAPHIFY_WORKSPACE"/*) return 0 ;; esac; [ -z "$_GRAPHIFY_INPUT_ROOT" ] || { [ "$_GRAPHIFY_INPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_INPUT_ROOT"|"$_GRAPHIFY_INPUT_ROOT"/*) return 0 ;; esac; }; [ -z "$_GRAPHIFY_OUTPUT_ROOT" ] || { [ "$_GRAPHIFY_OUTPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_OUTPUT_ROOT"|"$_GRAPHIFY_OUTPUT_ROOT"/*) return 0 ;; esac; }; return 1; }\n_graphify_resolve_ambient() {\n    _gfy_lexical=$1; case "$_gfy_lexical" in /*) ;; *) return 1 ;; esac\n    _graphify_path_denied "$_gfy_lexical" && return 1; _gfy_path=$_gfy_lexical\n    _gfy_links=0\n    while [ -L "$_gfy_path" ]; do\n        _gfy_links=$((_gfy_links + 1))\n        [ "$_gfy_links" -le 40 ] || return 1\n        [ -x /usr/bin/readlink ] || return 1\n        _gfy_link=$(/usr/bin/readlink "$_gfy_path") || return 1\n        case "$_gfy_link" in\n            /*) _gfy_path=$_gfy_link ;;\n            *) _gfy_dir=${_gfy_path%/*}; _gfy_path=$_gfy_dir/$_gfy_link ;;\n        esac\n    done\n    _gfy_dir=${_gfy_path%/*}; _gfy_base=${_gfy_path##*/}\n    _gfy_dir=$(CDPATH= cd -P -- "$_gfy_dir" 2>/dev/null && /bin/pwd -P) || return 1\n    _gfy_path=$_gfy_dir/$_gfy_base\n    _graphify_path_denied "$_gfy_path" && return 1\n    [ -x "$_gfy_lexical" ] || return 1\n    GRAPHIFY_RESOLVED=$_gfy_lexical\n}\n_graphify_command() {\n    _gfy_found=$(command -v "$1" 2>/dev/null) || return 1\n    _graphify_resolve_ambient "$_gfy_found"\n}\n_graphify_supported() {\n    [ -n "$1" ] && "$1" -E -P -B -c "$_GRAPHIFY_VERSION_CHECK" >/dev/null 2>&1\n}\n_graphify_usable() {\n    _graphify_supported "$1" && "$1" -E -P -B -c '"'"'import graphify'"'"' >/dev/null 2>&1\n}\n# An explicit absolute active environment is caller-selected, including a\n# project-local venv. Keep its lexical path for invocation.\ncase "${VIRTUAL_ENV-}" in\n    /*) _gfy_venv_python=$VIRTUAL_ENV/bin/python\n        _graphify_usable "$_gfy_venv_python" && GRAPHIFY_PYTHON=$_gfy_venv_python ;;\nesac\n# Trusted uv and pipx metadata, then trusted candidates derived from it.\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command uv; then\n    _gfy_uv=$GRAPHIFY_RESOLVED\n    _gfy_uv_dir=$("$_gfy_uv" tool dir 2>/dev/null)\n    _gfy_candidate=${_gfy_uv_dir:+$_gfy_uv_dir/graphifyy/bin/python}\n    if _graphify_resolve_ambient "$_gfy_candidate"; then\n        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED\n    fi\nfi\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command pipx; then\n    _gfy_pipx=$GRAPHIFY_RESOLVED\n    _gfy_pipx_home=$("$_gfy_pipx" environment --value PIPX_LOCAL_VENVS 2>/dev/null)\n    _gfy_candidate=${_gfy_pipx_home:+$_gfy_pipx_home/graphifyy/bin/python}\n    if _graphify_resolve_ambient "$_gfy_candidate"; then\n        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED\n    fi\nfi\n# Console-script shebang covers direct and pipx installs without executing the launcher.\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command graphify; then\n    _gfy_graphify=$GRAPHIFY_RESOLVED\n    IFS= read -r _gfy_shebang < "$_gfy_graphify" || _gfy_shebang=""\n    _gfy_shebang=${_gfy_shebang#\\#!}\n    case "$_gfy_shebang" in\n        "/usr/bin/env "*)\n            _gfy_env_command=${_gfy_shebang#"/usr/bin/env "}\n            case "$_gfy_env_command" in\n                ""|*[!a-zA-Z0-9_.@+-]*) _gfy_shebang="" ;;\n                *) if _graphify_command "$_gfy_env_command"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;\n            esac ;;\n        *[!a-zA-Z0-9/_.@+-]*) _gfy_shebang="" ;;\n        *) if _graphify_resolve_ambient "$_gfy_shebang"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;\n    esac\n    _graphify_usable "$_gfy_shebang" && GRAPHIFY_PYTHON=$_gfy_shebang\nfi\nif [ -z "$GRAPHIFY_PYTHON" ]; then\n    for _gfy_name in python3.14 python3 python; do\n        if _graphify_command "$_gfy_name"; then\n            _graphify_usable "$GRAPHIFY_RESOLVED" && { GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; break; }\n        fi\n    done\nfi\nif [ -z "$GRAPHIFY_PYTHON" ] && [ "${GRAPHIFY_DISCOVERY_OPTIONAL-0}" != 1 ]; then\n    echo "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." >&2\n    exit 1\nfi')"
"$GRAPHIFY_PYTHON" -E -P -B -c "
import sys, json
from networkx.readwrite import json_graph
import networkx as nx
from pathlib import Path

data = json.loads(Path('graphify-out/graph.json').read_text(encoding='utf-8'))
G = json_graph.node_link_graph(data, edges='links')

question = 'QUESTION'
mode = 'MODE'  # 'bfs' or 'dfs'
terms = [t.lower() for t in question.split() if len(t) >= 3]  # match the vocab threshold; keeps api/jwt/ios (#1392)

# Find best-matching start nodes
scored = []
for nid, ndata in G.nodes(data=True):
    label = ndata.get('label', '').lower()
    score = sum(1 for t in terms if t in label)
    if score > 0:
        scored.append((score, nid))
scored.sort(reverse=True)
start_nodes = [nid for _, nid in scored[:3]]

if not start_nodes:
    print('No matching nodes found for query terms:', terms)
    sys.exit(0)

subgraph_nodes = set()
subgraph_edges = []

if mode == 'dfs':
    # DFS: follow one path as deep as possible before backtracking.
    # Depth-limited to 6 to avoid traversing the whole graph.
    visited = set()
    stack = [(n, 0) for n in reversed(start_nodes)]
    while stack:
        node, depth = stack.pop()
        if node in visited or depth > 6:
            continue
        visited.add(node)
        subgraph_nodes.add(node)
        for neighbor in G.neighbors(node):
            if neighbor not in visited:
                stack.append((neighbor, depth + 1))
                subgraph_edges.append((node, neighbor))
else:
    # BFS: explore all neighbors layer by layer up to depth 3.
    frontier = set(start_nodes)
    subgraph_nodes = set(start_nodes)
    for _ in range(3):
        next_frontier = set()
        for n in frontier:
            for neighbor in G.neighbors(n):
                if neighbor not in subgraph_nodes:
                    next_frontier.add(neighbor)
                    subgraph_edges.append((n, neighbor))
        subgraph_nodes.update(next_frontier)
        frontier = next_frontier

# Token-budget aware output: rank by relevance, cut at budget (~4 chars/token)
token_budget = BUDGET  # default 2000
char_budget = token_budget * 4

# Score each node by term overlap for ranked output
def relevance(nid):
    label = G.nodes[nid].get('label', '').lower()
    return sum(1 for t in terms if t in label)

ranked_nodes = sorted(subgraph_nodes, key=relevance, reverse=True)

lines = [f'Traversal: {mode.upper()} | Start: {[G.nodes[n].get(\"label\",n) for n in start_nodes]} | {len(subgraph_nodes)} nodes']
for nid in ranked_nodes:
    d = G.nodes[nid]
    lines.append(f'  NODE {d.get(\"label\", nid)} [src={d.get(\"source_file\",\"\")} loc={d.get(\"source_location\",\"\")}]')
for u, v in subgraph_edges:
    if u in subgraph_nodes and v in subgraph_nodes:
        _raw = G[u][v]; d = next(iter(_raw.values()), {}) if isinstance(G, nx.MultiGraph) else _raw
        lines.append(f'  EDGE {G.nodes[u].get(\"label\",u)} --{d.get(\"relation\",\"\")} [{d.get(\"confidence\",\"\")}]--> {G.nodes[v].get(\"label\",v)}')

output = '\n'.join(lines)
if len(output) > char_budget:
    output = output[:char_budget] + f'\n... (truncated at ~{token_budget} token budget - use --budget N for more)'
print(output)
"
```

Replace `QUESTION` with the **expanded** query string, `MODE` with `bfs` or `dfs`, and `BUDGET` with the token budget (default `2000`, or whatever `--budget N` specifies). Then answer based on the subgraph output above, using only what the graph contains.

After writing the answer, save it back into the graph so it improves future queries. Include the expanded tokens inside the `--answer` text (e.g. `"Expanded from original query via vocab: [tokens]. Then traversed..."`) so the next `--update` extracts the expansion history as a graph node:

```bash
eval "$(printf '%b' 'GRAPHIFY_PYTHON=""\n_GRAPHIFY_VERSION_CHECK='"'"'import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)'"'"'\n_GRAPHIFY_WORKSPACE=$(/bin/pwd -P) || exit 1\n_graphify_canonical_root() {\n    _gfy_root=$1; [ -n "$_gfy_root" ] || return 1\n    case "$_gfy_root" in /*) ;; *) _gfy_root=$_GRAPHIFY_WORKSPACE/$_gfy_root ;; esac\n    [ -d "$_gfy_root" ] && CDPATH= cd -P -- "$_gfy_root" 2>/dev/null && /bin/pwd -P\n}\n_GRAPHIFY_INPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_INPUT_PATH-}") || _GRAPHIFY_INPUT_ROOT=""; _GRAPHIFY_OUTPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_OUTPUT_ROOT-${GRAPHIFY_OUT-graphify-out}}") || _GRAPHIFY_OUTPUT_ROOT=""\n_graphify_path_denied() { _gfy_policy_path=$1; [ "$_GRAPHIFY_WORKSPACE" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_WORKSPACE"|"$_GRAPHIFY_WORKSPACE"/*) return 0 ;; esac; [ -z "$_GRAPHIFY_INPUT_ROOT" ] || { [ "$_GRAPHIFY_INPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_INPUT_ROOT"|"$_GRAPHIFY_INPUT_ROOT"/*) return 0 ;; esac; }; [ -z "$_GRAPHIFY_OUTPUT_ROOT" ] || { [ "$_GRAPHIFY_OUTPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_OUTPUT_ROOT"|"$_GRAPHIFY_OUTPUT_ROOT"/*) return 0 ;; esac; }; return 1; }\n_graphify_resolve_ambient() {\n    _gfy_lexical=$1; case "$_gfy_lexical" in /*) ;; *) return 1 ;; esac\n    _graphify_path_denied "$_gfy_lexical" && return 1; _gfy_path=$_gfy_lexical\n    _gfy_links=0\n    while [ -L "$_gfy_path" ]; do\n        _gfy_links=$((_gfy_links + 1))\n        [ "$_gfy_links" -le 40 ] || return 1\n        [ -x /usr/bin/readlink ] || return 1\n        _gfy_link=$(/usr/bin/readlink "$_gfy_path") || return 1\n        case "$_gfy_link" in\n            /*) _gfy_path=$_gfy_link ;;\n            *) _gfy_dir=${_gfy_path%/*}; _gfy_path=$_gfy_dir/$_gfy_link ;;\n        esac\n    done\n    _gfy_dir=${_gfy_path%/*}; _gfy_base=${_gfy_path##*/}\n    _gfy_dir=$(CDPATH= cd -P -- "$_gfy_dir" 2>/dev/null && /bin/pwd -P) || return 1\n    _gfy_path=$_gfy_dir/$_gfy_base\n    _graphify_path_denied "$_gfy_path" && return 1\n    [ -x "$_gfy_lexical" ] || return 1\n    GRAPHIFY_RESOLVED=$_gfy_lexical\n}\n_graphify_command() {\n    _gfy_found=$(command -v "$1" 2>/dev/null) || return 1\n    _graphify_resolve_ambient "$_gfy_found"\n}\n_graphify_supported() {\n    [ -n "$1" ] && "$1" -E -P -B -c "$_GRAPHIFY_VERSION_CHECK" >/dev/null 2>&1\n}\n_graphify_usable() {\n    _graphify_supported "$1" && "$1" -E -P -B -c '"'"'import graphify'"'"' >/dev/null 2>&1\n}\n# An explicit absolute active environment is caller-selected, including a\n# project-local venv. Keep its lexical path for invocation.\ncase "${VIRTUAL_ENV-}" in\n    /*) _gfy_venv_python=$VIRTUAL_ENV/bin/python\n        _graphify_usable "$_gfy_venv_python" && GRAPHIFY_PYTHON=$_gfy_venv_python ;;\nesac\n# Trusted uv and pipx metadata, then trusted candidates derived from it.\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command uv; then\n    _gfy_uv=$GRAPHIFY_RESOLVED\n    _gfy_uv_dir=$("$_gfy_uv" tool dir 2>/dev/null)\n    _gfy_candidate=${_gfy_uv_dir:+$_gfy_uv_dir/graphifyy/bin/python}\n    if _graphify_resolve_ambient "$_gfy_candidate"; then\n        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED\n    fi\nfi\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command pipx; then\n    _gfy_pipx=$GRAPHIFY_RESOLVED\n    _gfy_pipx_home=$("$_gfy_pipx" environment --value PIPX_LOCAL_VENVS 2>/dev/null)\n    _gfy_candidate=${_gfy_pipx_home:+$_gfy_pipx_home/graphifyy/bin/python}\n    if _graphify_resolve_ambient "$_gfy_candidate"; then\n        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED\n    fi\nfi\n# Console-script shebang covers direct and pipx installs without executing the launcher.\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command graphify; then\n    _gfy_graphify=$GRAPHIFY_RESOLVED\n    IFS= read -r _gfy_shebang < "$_gfy_graphify" || _gfy_shebang=""\n    _gfy_shebang=${_gfy_shebang#\\#!}\n    case "$_gfy_shebang" in\n        "/usr/bin/env "*)\n            _gfy_env_command=${_gfy_shebang#"/usr/bin/env "}\n            case "$_gfy_env_command" in\n                ""|*[!a-zA-Z0-9_.@+-]*) _gfy_shebang="" ;;\n                *) if _graphify_command "$_gfy_env_command"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;\n            esac ;;\n        *[!a-zA-Z0-9/_.@+-]*) _gfy_shebang="" ;;\n        *) if _graphify_resolve_ambient "$_gfy_shebang"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;\n    esac\n    _graphify_usable "$_gfy_shebang" && GRAPHIFY_PYTHON=$_gfy_shebang\nfi\nif [ -z "$GRAPHIFY_PYTHON" ]; then\n    for _gfy_name in python3.14 python3 python; do\n        if _graphify_command "$_gfy_name"; then\n            _graphify_usable "$GRAPHIFY_RESOLVED" && { GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; break; }\n        fi\n    done\nfi\nif [ -z "$GRAPHIFY_PYTHON" ] && [ "${GRAPHIFY_DISCOVERY_OPTIONAL-0}" != 1 ]; then\n    echo "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." >&2\n    exit 1\nfi')"
"$GRAPHIFY_PYTHON" -E -P -B -m graphify save-result --question "ORIGINAL_QUESTION" --answer "ANSWER" --type query --nodes NODE1 NODE2
```

Replace `ORIGINAL_QUESTION` with the user's verbatim question, `ANSWER` with your full answer text (containing the expanded-token trace), `NODE1 NODE2` with the list of node labels you cited. This closes the feedback loop: the next `--update` will extract this Q&A as a node in the graph.

**Work memory (self-improving loop).** Add an `--outcome` so future sessions learn from this one — append `--outcome useful|dead_end|corrected` to the `save-result` command (and `--correction "the right answer"` when correcting):

- `useful` — the cited nodes answered the question well (they become *preferred sources*).
- `dead_end` — the question/path led nowhere; don't re-derive it next time.
- `corrected` — the saved answer was wrong; `--correction` records what was right.

At the **start** of graph work, refresh and read the lessons with `"$GRAPHIFY_PYTHON" -E -P -B -m graphify reflect --if-stale` (cheap, deterministic, no LLM; `--if-stale` makes it a no-op when `LESSONS.md` is already newer than every input, e.g. when the git hook just refreshed it), then read `graphify-out/reflections/LESSONS.md`. It lists **preferred sources** (start there), **known dead ends** (skip them), and prior **corrections**. Running `reflect` yourself keeps the lessons current even without the git hook installed; if the post-commit hook *is* installed, `--if-stale` means your session-start run costs almost nothing.

---

## For /graphify path

Find the shortest path between two named concepts in the graph. Prefer the CLI when installed:

```bash
eval "$(printf '%b' 'GRAPHIFY_PYTHON=""\n_GRAPHIFY_VERSION_CHECK='"'"'import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)'"'"'\n_GRAPHIFY_WORKSPACE=$(/bin/pwd -P) || exit 1\n_graphify_canonical_root() {\n    _gfy_root=$1; [ -n "$_gfy_root" ] || return 1\n    case "$_gfy_root" in /*) ;; *) _gfy_root=$_GRAPHIFY_WORKSPACE/$_gfy_root ;; esac\n    [ -d "$_gfy_root" ] && CDPATH= cd -P -- "$_gfy_root" 2>/dev/null && /bin/pwd -P\n}\n_GRAPHIFY_INPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_INPUT_PATH-}") || _GRAPHIFY_INPUT_ROOT=""; _GRAPHIFY_OUTPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_OUTPUT_ROOT-${GRAPHIFY_OUT-graphify-out}}") || _GRAPHIFY_OUTPUT_ROOT=""\n_graphify_path_denied() { _gfy_policy_path=$1; [ "$_GRAPHIFY_WORKSPACE" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_WORKSPACE"|"$_GRAPHIFY_WORKSPACE"/*) return 0 ;; esac; [ -z "$_GRAPHIFY_INPUT_ROOT" ] || { [ "$_GRAPHIFY_INPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_INPUT_ROOT"|"$_GRAPHIFY_INPUT_ROOT"/*) return 0 ;; esac; }; [ -z "$_GRAPHIFY_OUTPUT_ROOT" ] || { [ "$_GRAPHIFY_OUTPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_OUTPUT_ROOT"|"$_GRAPHIFY_OUTPUT_ROOT"/*) return 0 ;; esac; }; return 1; }\n_graphify_resolve_ambient() {\n    _gfy_lexical=$1; case "$_gfy_lexical" in /*) ;; *) return 1 ;; esac\n    _graphify_path_denied "$_gfy_lexical" && return 1; _gfy_path=$_gfy_lexical\n    _gfy_links=0\n    while [ -L "$_gfy_path" ]; do\n        _gfy_links=$((_gfy_links + 1))\n        [ "$_gfy_links" -le 40 ] || return 1\n        [ -x /usr/bin/readlink ] || return 1\n        _gfy_link=$(/usr/bin/readlink "$_gfy_path") || return 1\n        case "$_gfy_link" in\n            /*) _gfy_path=$_gfy_link ;;\n            *) _gfy_dir=${_gfy_path%/*}; _gfy_path=$_gfy_dir/$_gfy_link ;;\n        esac\n    done\n    _gfy_dir=${_gfy_path%/*}; _gfy_base=${_gfy_path##*/}\n    _gfy_dir=$(CDPATH= cd -P -- "$_gfy_dir" 2>/dev/null && /bin/pwd -P) || return 1\n    _gfy_path=$_gfy_dir/$_gfy_base\n    _graphify_path_denied "$_gfy_path" && return 1\n    [ -x "$_gfy_lexical" ] || return 1\n    GRAPHIFY_RESOLVED=$_gfy_lexical\n}\n_graphify_command() {\n    _gfy_found=$(command -v "$1" 2>/dev/null) || return 1\n    _graphify_resolve_ambient "$_gfy_found"\n}\n_graphify_supported() {\n    [ -n "$1" ] && "$1" -E -P -B -c "$_GRAPHIFY_VERSION_CHECK" >/dev/null 2>&1\n}\n_graphify_usable() {\n    _graphify_supported "$1" && "$1" -E -P -B -c '"'"'import graphify'"'"' >/dev/null 2>&1\n}\n# An explicit absolute active environment is caller-selected, including a\n# project-local venv. Keep its lexical path for invocation.\ncase "${VIRTUAL_ENV-}" in\n    /*) _gfy_venv_python=$VIRTUAL_ENV/bin/python\n        _graphify_usable "$_gfy_venv_python" && GRAPHIFY_PYTHON=$_gfy_venv_python ;;\nesac\n# Trusted uv and pipx metadata, then trusted candidates derived from it.\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command uv; then\n    _gfy_uv=$GRAPHIFY_RESOLVED\n    _gfy_uv_dir=$("$_gfy_uv" tool dir 2>/dev/null)\n    _gfy_candidate=${_gfy_uv_dir:+$_gfy_uv_dir/graphifyy/bin/python}\n    if _graphify_resolve_ambient "$_gfy_candidate"; then\n        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED\n    fi\nfi\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command pipx; then\n    _gfy_pipx=$GRAPHIFY_RESOLVED\n    _gfy_pipx_home=$("$_gfy_pipx" environment --value PIPX_LOCAL_VENVS 2>/dev/null)\n    _gfy_candidate=${_gfy_pipx_home:+$_gfy_pipx_home/graphifyy/bin/python}\n    if _graphify_resolve_ambient "$_gfy_candidate"; then\n        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED\n    fi\nfi\n# Console-script shebang covers direct and pipx installs without executing the launcher.\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command graphify; then\n    _gfy_graphify=$GRAPHIFY_RESOLVED\n    IFS= read -r _gfy_shebang < "$_gfy_graphify" || _gfy_shebang=""\n    _gfy_shebang=${_gfy_shebang#\\#!}\n    case "$_gfy_shebang" in\n        "/usr/bin/env "*)\n            _gfy_env_command=${_gfy_shebang#"/usr/bin/env "}\n            case "$_gfy_env_command" in\n                ""|*[!a-zA-Z0-9_.@+-]*) _gfy_shebang="" ;;\n                *) if _graphify_command "$_gfy_env_command"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;\n            esac ;;\n        *[!a-zA-Z0-9/_.@+-]*) _gfy_shebang="" ;;\n        *) if _graphify_resolve_ambient "$_gfy_shebang"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;\n    esac\n    _graphify_usable "$_gfy_shebang" && GRAPHIFY_PYTHON=$_gfy_shebang\nfi\nif [ -z "$GRAPHIFY_PYTHON" ]; then\n    for _gfy_name in python3.14 python3 python; do\n        if _graphify_command "$_gfy_name"; then\n            _graphify_usable "$GRAPHIFY_RESOLVED" && { GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; break; }\n        fi\n    done\nfi\nif [ -z "$GRAPHIFY_PYTHON" ] && [ "${GRAPHIFY_DISCOVERY_OPTIONAL-0}" != 1 ]; then\n    echo "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." >&2\n    exit 1\nfi')"
"$GRAPHIFY_PYTHON" -E -P -B -m graphify path "NODE_A" "NODE_B"
```

If the CLI is unavailable, run it inline:

```bash
eval "$(printf '%b' 'GRAPHIFY_PYTHON=""\n_GRAPHIFY_VERSION_CHECK='"'"'import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)'"'"'\n_GRAPHIFY_WORKSPACE=$(/bin/pwd -P) || exit 1\n_graphify_canonical_root() {\n    _gfy_root=$1; [ -n "$_gfy_root" ] || return 1\n    case "$_gfy_root" in /*) ;; *) _gfy_root=$_GRAPHIFY_WORKSPACE/$_gfy_root ;; esac\n    [ -d "$_gfy_root" ] && CDPATH= cd -P -- "$_gfy_root" 2>/dev/null && /bin/pwd -P\n}\n_GRAPHIFY_INPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_INPUT_PATH-}") || _GRAPHIFY_INPUT_ROOT=""; _GRAPHIFY_OUTPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_OUTPUT_ROOT-${GRAPHIFY_OUT-graphify-out}}") || _GRAPHIFY_OUTPUT_ROOT=""\n_graphify_path_denied() { _gfy_policy_path=$1; [ "$_GRAPHIFY_WORKSPACE" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_WORKSPACE"|"$_GRAPHIFY_WORKSPACE"/*) return 0 ;; esac; [ -z "$_GRAPHIFY_INPUT_ROOT" ] || { [ "$_GRAPHIFY_INPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_INPUT_ROOT"|"$_GRAPHIFY_INPUT_ROOT"/*) return 0 ;; esac; }; [ -z "$_GRAPHIFY_OUTPUT_ROOT" ] || { [ "$_GRAPHIFY_OUTPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_OUTPUT_ROOT"|"$_GRAPHIFY_OUTPUT_ROOT"/*) return 0 ;; esac; }; return 1; }\n_graphify_resolve_ambient() {\n    _gfy_lexical=$1; case "$_gfy_lexical" in /*) ;; *) return 1 ;; esac\n    _graphify_path_denied "$_gfy_lexical" && return 1; _gfy_path=$_gfy_lexical\n    _gfy_links=0\n    while [ -L "$_gfy_path" ]; do\n        _gfy_links=$((_gfy_links + 1))\n        [ "$_gfy_links" -le 40 ] || return 1\n        [ -x /usr/bin/readlink ] || return 1\n        _gfy_link=$(/usr/bin/readlink "$_gfy_path") || return 1\n        case "$_gfy_link" in\n            /*) _gfy_path=$_gfy_link ;;\n            *) _gfy_dir=${_gfy_path%/*}; _gfy_path=$_gfy_dir/$_gfy_link ;;\n        esac\n    done\n    _gfy_dir=${_gfy_path%/*}; _gfy_base=${_gfy_path##*/}\n    _gfy_dir=$(CDPATH= cd -P -- "$_gfy_dir" 2>/dev/null && /bin/pwd -P) || return 1\n    _gfy_path=$_gfy_dir/$_gfy_base\n    _graphify_path_denied "$_gfy_path" && return 1\n    [ -x "$_gfy_lexical" ] || return 1\n    GRAPHIFY_RESOLVED=$_gfy_lexical\n}\n_graphify_command() {\n    _gfy_found=$(command -v "$1" 2>/dev/null) || return 1\n    _graphify_resolve_ambient "$_gfy_found"\n}\n_graphify_supported() {\n    [ -n "$1" ] && "$1" -E -P -B -c "$_GRAPHIFY_VERSION_CHECK" >/dev/null 2>&1\n}\n_graphify_usable() {\n    _graphify_supported "$1" && "$1" -E -P -B -c '"'"'import graphify'"'"' >/dev/null 2>&1\n}\n# An explicit absolute active environment is caller-selected, including a\n# project-local venv. Keep its lexical path for invocation.\ncase "${VIRTUAL_ENV-}" in\n    /*) _gfy_venv_python=$VIRTUAL_ENV/bin/python\n        _graphify_usable "$_gfy_venv_python" && GRAPHIFY_PYTHON=$_gfy_venv_python ;;\nesac\n# Trusted uv and pipx metadata, then trusted candidates derived from it.\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command uv; then\n    _gfy_uv=$GRAPHIFY_RESOLVED\n    _gfy_uv_dir=$("$_gfy_uv" tool dir 2>/dev/null)\n    _gfy_candidate=${_gfy_uv_dir:+$_gfy_uv_dir/graphifyy/bin/python}\n    if _graphify_resolve_ambient "$_gfy_candidate"; then\n        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED\n    fi\nfi\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command pipx; then\n    _gfy_pipx=$GRAPHIFY_RESOLVED\n    _gfy_pipx_home=$("$_gfy_pipx" environment --value PIPX_LOCAL_VENVS 2>/dev/null)\n    _gfy_candidate=${_gfy_pipx_home:+$_gfy_pipx_home/graphifyy/bin/python}\n    if _graphify_resolve_ambient "$_gfy_candidate"; then\n        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED\n    fi\nfi\n# Console-script shebang covers direct and pipx installs without executing the launcher.\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command graphify; then\n    _gfy_graphify=$GRAPHIFY_RESOLVED\n    IFS= read -r _gfy_shebang < "$_gfy_graphify" || _gfy_shebang=""\n    _gfy_shebang=${_gfy_shebang#\\#!}\n    case "$_gfy_shebang" in\n        "/usr/bin/env "*)\n            _gfy_env_command=${_gfy_shebang#"/usr/bin/env "}\n            case "$_gfy_env_command" in\n                ""|*[!a-zA-Z0-9_.@+-]*) _gfy_shebang="" ;;\n                *) if _graphify_command "$_gfy_env_command"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;\n            esac ;;\n        *[!a-zA-Z0-9/_.@+-]*) _gfy_shebang="" ;;\n        *) if _graphify_resolve_ambient "$_gfy_shebang"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;\n    esac\n    _graphify_usable "$_gfy_shebang" && GRAPHIFY_PYTHON=$_gfy_shebang\nfi\nif [ -z "$GRAPHIFY_PYTHON" ]; then\n    for _gfy_name in python3.14 python3 python; do\n        if _graphify_command "$_gfy_name"; then\n            _graphify_usable "$GRAPHIFY_RESOLVED" && { GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; break; }\n        fi\n    done\nfi\nif [ -z "$GRAPHIFY_PYTHON" ] && [ "${GRAPHIFY_DISCOVERY_OPTIONAL-0}" != 1 ]; then\n    echo "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." >&2\n    exit 1\nfi')"
"$GRAPHIFY_PYTHON" -E -P -B -c "
import json, sys
import networkx as nx
from networkx.readwrite import json_graph
from pathlib import Path

data = json.loads(Path('graphify-out/graph.json').read_text(encoding='utf-8'))
G = json_graph.node_link_graph(data, edges='links')

a_term = 'NODE_A'
b_term = 'NODE_B'

def find_node(term):
    term = term.lower()
    scored = sorted(
        [(sum(1 for w in term.split() if w in G.nodes[n].get('label','').lower()), n)
         for n in G.nodes()],
        reverse=True
    )
    return scored[0][1] if scored and scored[0][0] > 0 else None

src = find_node(a_term)
tgt = find_node(b_term)

if not src or not tgt:
    print(f'Could not find nodes matching: {a_term!r} or {b_term!r}')
    sys.exit(0)

try:
    path = nx.shortest_path(G, src, tgt)
    print(f'Shortest path ({len(path)-1} hops):')
    for i, nid in enumerate(path):
        label = G.nodes[nid].get('label', nid)
        if i < len(path) - 1:
            _raw = G[nid][path[i+1]]; edge = next(iter(_raw.values()), {}) if isinstance(G, nx.MultiGraph) else _raw
            rel = edge.get('relation', '')
            conf = edge.get('confidence', '')
            print(f'  {label} --{rel}--> [{conf}]')
        else:
            print(f'  {label}')
except nx.NetworkXNoPath:
    print(f'No path found between {a_term!r} and {b_term!r}')
except nx.NodeNotFound as e:
    print(f'Node not found: {e}')
"
```

Replace `NODE_A` and `NODE_B` with the actual concept names from the user. Then explain the path in plain language - what each hop means, why it's significant.

After writing the explanation, save it back:

```bash
eval "$(printf '%b' 'GRAPHIFY_PYTHON=""\n_GRAPHIFY_VERSION_CHECK='"'"'import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)'"'"'\n_GRAPHIFY_WORKSPACE=$(/bin/pwd -P) || exit 1\n_graphify_canonical_root() {\n    _gfy_root=$1; [ -n "$_gfy_root" ] || return 1\n    case "$_gfy_root" in /*) ;; *) _gfy_root=$_GRAPHIFY_WORKSPACE/$_gfy_root ;; esac\n    [ -d "$_gfy_root" ] && CDPATH= cd -P -- "$_gfy_root" 2>/dev/null && /bin/pwd -P\n}\n_GRAPHIFY_INPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_INPUT_PATH-}") || _GRAPHIFY_INPUT_ROOT=""; _GRAPHIFY_OUTPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_OUTPUT_ROOT-${GRAPHIFY_OUT-graphify-out}}") || _GRAPHIFY_OUTPUT_ROOT=""\n_graphify_path_denied() { _gfy_policy_path=$1; [ "$_GRAPHIFY_WORKSPACE" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_WORKSPACE"|"$_GRAPHIFY_WORKSPACE"/*) return 0 ;; esac; [ -z "$_GRAPHIFY_INPUT_ROOT" ] || { [ "$_GRAPHIFY_INPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_INPUT_ROOT"|"$_GRAPHIFY_INPUT_ROOT"/*) return 0 ;; esac; }; [ -z "$_GRAPHIFY_OUTPUT_ROOT" ] || { [ "$_GRAPHIFY_OUTPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_OUTPUT_ROOT"|"$_GRAPHIFY_OUTPUT_ROOT"/*) return 0 ;; esac; }; return 1; }\n_graphify_resolve_ambient() {\n    _gfy_lexical=$1; case "$_gfy_lexical" in /*) ;; *) return 1 ;; esac\n    _graphify_path_denied "$_gfy_lexical" && return 1; _gfy_path=$_gfy_lexical\n    _gfy_links=0\n    while [ -L "$_gfy_path" ]; do\n        _gfy_links=$((_gfy_links + 1))\n        [ "$_gfy_links" -le 40 ] || return 1\n        [ -x /usr/bin/readlink ] || return 1\n        _gfy_link=$(/usr/bin/readlink "$_gfy_path") || return 1\n        case "$_gfy_link" in\n            /*) _gfy_path=$_gfy_link ;;\n            *) _gfy_dir=${_gfy_path%/*}; _gfy_path=$_gfy_dir/$_gfy_link ;;\n        esac\n    done\n    _gfy_dir=${_gfy_path%/*}; _gfy_base=${_gfy_path##*/}\n    _gfy_dir=$(CDPATH= cd -P -- "$_gfy_dir" 2>/dev/null && /bin/pwd -P) || return 1\n    _gfy_path=$_gfy_dir/$_gfy_base\n    _graphify_path_denied "$_gfy_path" && return 1\n    [ -x "$_gfy_lexical" ] || return 1\n    GRAPHIFY_RESOLVED=$_gfy_lexical\n}\n_graphify_command() {\n    _gfy_found=$(command -v "$1" 2>/dev/null) || return 1\n    _graphify_resolve_ambient "$_gfy_found"\n}\n_graphify_supported() {\n    [ -n "$1" ] && "$1" -E -P -B -c "$_GRAPHIFY_VERSION_CHECK" >/dev/null 2>&1\n}\n_graphify_usable() {\n    _graphify_supported "$1" && "$1" -E -P -B -c '"'"'import graphify'"'"' >/dev/null 2>&1\n}\n# An explicit absolute active environment is caller-selected, including a\n# project-local venv. Keep its lexical path for invocation.\ncase "${VIRTUAL_ENV-}" in\n    /*) _gfy_venv_python=$VIRTUAL_ENV/bin/python\n        _graphify_usable "$_gfy_venv_python" && GRAPHIFY_PYTHON=$_gfy_venv_python ;;\nesac\n# Trusted uv and pipx metadata, then trusted candidates derived from it.\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command uv; then\n    _gfy_uv=$GRAPHIFY_RESOLVED\n    _gfy_uv_dir=$("$_gfy_uv" tool dir 2>/dev/null)\n    _gfy_candidate=${_gfy_uv_dir:+$_gfy_uv_dir/graphifyy/bin/python}\n    if _graphify_resolve_ambient "$_gfy_candidate"; then\n        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED\n    fi\nfi\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command pipx; then\n    _gfy_pipx=$GRAPHIFY_RESOLVED\n    _gfy_pipx_home=$("$_gfy_pipx" environment --value PIPX_LOCAL_VENVS 2>/dev/null)\n    _gfy_candidate=${_gfy_pipx_home:+$_gfy_pipx_home/graphifyy/bin/python}\n    if _graphify_resolve_ambient "$_gfy_candidate"; then\n        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED\n    fi\nfi\n# Console-script shebang covers direct and pipx installs without executing the launcher.\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command graphify; then\n    _gfy_graphify=$GRAPHIFY_RESOLVED\n    IFS= read -r _gfy_shebang < "$_gfy_graphify" || _gfy_shebang=""\n    _gfy_shebang=${_gfy_shebang#\\#!}\n    case "$_gfy_shebang" in\n        "/usr/bin/env "*)\n            _gfy_env_command=${_gfy_shebang#"/usr/bin/env "}\n            case "$_gfy_env_command" in\n                ""|*[!a-zA-Z0-9_.@+-]*) _gfy_shebang="" ;;\n                *) if _graphify_command "$_gfy_env_command"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;\n            esac ;;\n        *[!a-zA-Z0-9/_.@+-]*) _gfy_shebang="" ;;\n        *) if _graphify_resolve_ambient "$_gfy_shebang"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;\n    esac\n    _graphify_usable "$_gfy_shebang" && GRAPHIFY_PYTHON=$_gfy_shebang\nfi\nif [ -z "$GRAPHIFY_PYTHON" ]; then\n    for _gfy_name in python3.14 python3 python; do\n        if _graphify_command "$_gfy_name"; then\n            _graphify_usable "$GRAPHIFY_RESOLVED" && { GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; break; }\n        fi\n    done\nfi\nif [ -z "$GRAPHIFY_PYTHON" ] && [ "${GRAPHIFY_DISCOVERY_OPTIONAL-0}" != 1 ]; then\n    echo "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." >&2\n    exit 1\nfi')"
"$GRAPHIFY_PYTHON" -E -P -B -m graphify save-result --question "Path from NODE_A to NODE_B" --answer "ANSWER" --type path_query --nodes NODE_A NODE_B
```

---

## For /graphify explain

Give a plain-language explanation of a single node - everything connected to it. Prefer the CLI when installed:

```bash
eval "$(printf '%b' 'GRAPHIFY_PYTHON=""\n_GRAPHIFY_VERSION_CHECK='"'"'import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)'"'"'\n_GRAPHIFY_WORKSPACE=$(/bin/pwd -P) || exit 1\n_graphify_canonical_root() {\n    _gfy_root=$1; [ -n "$_gfy_root" ] || return 1\n    case "$_gfy_root" in /*) ;; *) _gfy_root=$_GRAPHIFY_WORKSPACE/$_gfy_root ;; esac\n    [ -d "$_gfy_root" ] && CDPATH= cd -P -- "$_gfy_root" 2>/dev/null && /bin/pwd -P\n}\n_GRAPHIFY_INPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_INPUT_PATH-}") || _GRAPHIFY_INPUT_ROOT=""; _GRAPHIFY_OUTPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_OUTPUT_ROOT-${GRAPHIFY_OUT-graphify-out}}") || _GRAPHIFY_OUTPUT_ROOT=""\n_graphify_path_denied() { _gfy_policy_path=$1; [ "$_GRAPHIFY_WORKSPACE" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_WORKSPACE"|"$_GRAPHIFY_WORKSPACE"/*) return 0 ;; esac; [ -z "$_GRAPHIFY_INPUT_ROOT" ] || { [ "$_GRAPHIFY_INPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_INPUT_ROOT"|"$_GRAPHIFY_INPUT_ROOT"/*) return 0 ;; esac; }; [ -z "$_GRAPHIFY_OUTPUT_ROOT" ] || { [ "$_GRAPHIFY_OUTPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_OUTPUT_ROOT"|"$_GRAPHIFY_OUTPUT_ROOT"/*) return 0 ;; esac; }; return 1; }\n_graphify_resolve_ambient() {\n    _gfy_lexical=$1; case "$_gfy_lexical" in /*) ;; *) return 1 ;; esac\n    _graphify_path_denied "$_gfy_lexical" && return 1; _gfy_path=$_gfy_lexical\n    _gfy_links=0\n    while [ -L "$_gfy_path" ]; do\n        _gfy_links=$((_gfy_links + 1))\n        [ "$_gfy_links" -le 40 ] || return 1\n        [ -x /usr/bin/readlink ] || return 1\n        _gfy_link=$(/usr/bin/readlink "$_gfy_path") || return 1\n        case "$_gfy_link" in\n            /*) _gfy_path=$_gfy_link ;;\n            *) _gfy_dir=${_gfy_path%/*}; _gfy_path=$_gfy_dir/$_gfy_link ;;\n        esac\n    done\n    _gfy_dir=${_gfy_path%/*}; _gfy_base=${_gfy_path##*/}\n    _gfy_dir=$(CDPATH= cd -P -- "$_gfy_dir" 2>/dev/null && /bin/pwd -P) || return 1\n    _gfy_path=$_gfy_dir/$_gfy_base\n    _graphify_path_denied "$_gfy_path" && return 1\n    [ -x "$_gfy_lexical" ] || return 1\n    GRAPHIFY_RESOLVED=$_gfy_lexical\n}\n_graphify_command() {\n    _gfy_found=$(command -v "$1" 2>/dev/null) || return 1\n    _graphify_resolve_ambient "$_gfy_found"\n}\n_graphify_supported() {\n    [ -n "$1" ] && "$1" -E -P -B -c "$_GRAPHIFY_VERSION_CHECK" >/dev/null 2>&1\n}\n_graphify_usable() {\n    _graphify_supported "$1" && "$1" -E -P -B -c '"'"'import graphify'"'"' >/dev/null 2>&1\n}\n# An explicit absolute active environment is caller-selected, including a\n# project-local venv. Keep its lexical path for invocation.\ncase "${VIRTUAL_ENV-}" in\n    /*) _gfy_venv_python=$VIRTUAL_ENV/bin/python\n        _graphify_usable "$_gfy_venv_python" && GRAPHIFY_PYTHON=$_gfy_venv_python ;;\nesac\n# Trusted uv and pipx metadata, then trusted candidates derived from it.\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command uv; then\n    _gfy_uv=$GRAPHIFY_RESOLVED\n    _gfy_uv_dir=$("$_gfy_uv" tool dir 2>/dev/null)\n    _gfy_candidate=${_gfy_uv_dir:+$_gfy_uv_dir/graphifyy/bin/python}\n    if _graphify_resolve_ambient "$_gfy_candidate"; then\n        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED\n    fi\nfi\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command pipx; then\n    _gfy_pipx=$GRAPHIFY_RESOLVED\n    _gfy_pipx_home=$("$_gfy_pipx" environment --value PIPX_LOCAL_VENVS 2>/dev/null)\n    _gfy_candidate=${_gfy_pipx_home:+$_gfy_pipx_home/graphifyy/bin/python}\n    if _graphify_resolve_ambient "$_gfy_candidate"; then\n        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED\n    fi\nfi\n# Console-script shebang covers direct and pipx installs without executing the launcher.\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command graphify; then\n    _gfy_graphify=$GRAPHIFY_RESOLVED\n    IFS= read -r _gfy_shebang < "$_gfy_graphify" || _gfy_shebang=""\n    _gfy_shebang=${_gfy_shebang#\\#!}\n    case "$_gfy_shebang" in\n        "/usr/bin/env "*)\n            _gfy_env_command=${_gfy_shebang#"/usr/bin/env "}\n            case "$_gfy_env_command" in\n                ""|*[!a-zA-Z0-9_.@+-]*) _gfy_shebang="" ;;\n                *) if _graphify_command "$_gfy_env_command"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;\n            esac ;;\n        *[!a-zA-Z0-9/_.@+-]*) _gfy_shebang="" ;;\n        *) if _graphify_resolve_ambient "$_gfy_shebang"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;\n    esac\n    _graphify_usable "$_gfy_shebang" && GRAPHIFY_PYTHON=$_gfy_shebang\nfi\nif [ -z "$GRAPHIFY_PYTHON" ]; then\n    for _gfy_name in python3.14 python3 python; do\n        if _graphify_command "$_gfy_name"; then\n            _graphify_usable "$GRAPHIFY_RESOLVED" && { GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; break; }\n        fi\n    done\nfi\nif [ -z "$GRAPHIFY_PYTHON" ] && [ "${GRAPHIFY_DISCOVERY_OPTIONAL-0}" != 1 ]; then\n    echo "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." >&2\n    exit 1\nfi')"
"$GRAPHIFY_PYTHON" -E -P -B -m graphify explain "NODE_NAME"
```

If the CLI is unavailable, run it inline:

```bash
eval "$(printf '%b' 'GRAPHIFY_PYTHON=""\n_GRAPHIFY_VERSION_CHECK='"'"'import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)'"'"'\n_GRAPHIFY_WORKSPACE=$(/bin/pwd -P) || exit 1\n_graphify_canonical_root() {\n    _gfy_root=$1; [ -n "$_gfy_root" ] || return 1\n    case "$_gfy_root" in /*) ;; *) _gfy_root=$_GRAPHIFY_WORKSPACE/$_gfy_root ;; esac\n    [ -d "$_gfy_root" ] && CDPATH= cd -P -- "$_gfy_root" 2>/dev/null && /bin/pwd -P\n}\n_GRAPHIFY_INPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_INPUT_PATH-}") || _GRAPHIFY_INPUT_ROOT=""; _GRAPHIFY_OUTPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_OUTPUT_ROOT-${GRAPHIFY_OUT-graphify-out}}") || _GRAPHIFY_OUTPUT_ROOT=""\n_graphify_path_denied() { _gfy_policy_path=$1; [ "$_GRAPHIFY_WORKSPACE" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_WORKSPACE"|"$_GRAPHIFY_WORKSPACE"/*) return 0 ;; esac; [ -z "$_GRAPHIFY_INPUT_ROOT" ] || { [ "$_GRAPHIFY_INPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_INPUT_ROOT"|"$_GRAPHIFY_INPUT_ROOT"/*) return 0 ;; esac; }; [ -z "$_GRAPHIFY_OUTPUT_ROOT" ] || { [ "$_GRAPHIFY_OUTPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_OUTPUT_ROOT"|"$_GRAPHIFY_OUTPUT_ROOT"/*) return 0 ;; esac; }; return 1; }\n_graphify_resolve_ambient() {\n    _gfy_lexical=$1; case "$_gfy_lexical" in /*) ;; *) return 1 ;; esac\n    _graphify_path_denied "$_gfy_lexical" && return 1; _gfy_path=$_gfy_lexical\n    _gfy_links=0\n    while [ -L "$_gfy_path" ]; do\n        _gfy_links=$((_gfy_links + 1))\n        [ "$_gfy_links" -le 40 ] || return 1\n        [ -x /usr/bin/readlink ] || return 1\n        _gfy_link=$(/usr/bin/readlink "$_gfy_path") || return 1\n        case "$_gfy_link" in\n            /*) _gfy_path=$_gfy_link ;;\n            *) _gfy_dir=${_gfy_path%/*}; _gfy_path=$_gfy_dir/$_gfy_link ;;\n        esac\n    done\n    _gfy_dir=${_gfy_path%/*}; _gfy_base=${_gfy_path##*/}\n    _gfy_dir=$(CDPATH= cd -P -- "$_gfy_dir" 2>/dev/null && /bin/pwd -P) || return 1\n    _gfy_path=$_gfy_dir/$_gfy_base\n    _graphify_path_denied "$_gfy_path" && return 1\n    [ -x "$_gfy_lexical" ] || return 1\n    GRAPHIFY_RESOLVED=$_gfy_lexical\n}\n_graphify_command() {\n    _gfy_found=$(command -v "$1" 2>/dev/null) || return 1\n    _graphify_resolve_ambient "$_gfy_found"\n}\n_graphify_supported() {\n    [ -n "$1" ] && "$1" -E -P -B -c "$_GRAPHIFY_VERSION_CHECK" >/dev/null 2>&1\n}\n_graphify_usable() {\n    _graphify_supported "$1" && "$1" -E -P -B -c '"'"'import graphify'"'"' >/dev/null 2>&1\n}\n# An explicit absolute active environment is caller-selected, including a\n# project-local venv. Keep its lexical path for invocation.\ncase "${VIRTUAL_ENV-}" in\n    /*) _gfy_venv_python=$VIRTUAL_ENV/bin/python\n        _graphify_usable "$_gfy_venv_python" && GRAPHIFY_PYTHON=$_gfy_venv_python ;;\nesac\n# Trusted uv and pipx metadata, then trusted candidates derived from it.\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command uv; then\n    _gfy_uv=$GRAPHIFY_RESOLVED\n    _gfy_uv_dir=$("$_gfy_uv" tool dir 2>/dev/null)\n    _gfy_candidate=${_gfy_uv_dir:+$_gfy_uv_dir/graphifyy/bin/python}\n    if _graphify_resolve_ambient "$_gfy_candidate"; then\n        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED\n    fi\nfi\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command pipx; then\n    _gfy_pipx=$GRAPHIFY_RESOLVED\n    _gfy_pipx_home=$("$_gfy_pipx" environment --value PIPX_LOCAL_VENVS 2>/dev/null)\n    _gfy_candidate=${_gfy_pipx_home:+$_gfy_pipx_home/graphifyy/bin/python}\n    if _graphify_resolve_ambient "$_gfy_candidate"; then\n        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED\n    fi\nfi\n# Console-script shebang covers direct and pipx installs without executing the launcher.\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command graphify; then\n    _gfy_graphify=$GRAPHIFY_RESOLVED\n    IFS= read -r _gfy_shebang < "$_gfy_graphify" || _gfy_shebang=""\n    _gfy_shebang=${_gfy_shebang#\\#!}\n    case "$_gfy_shebang" in\n        "/usr/bin/env "*)\n            _gfy_env_command=${_gfy_shebang#"/usr/bin/env "}\n            case "$_gfy_env_command" in\n                ""|*[!a-zA-Z0-9_.@+-]*) _gfy_shebang="" ;;\n                *) if _graphify_command "$_gfy_env_command"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;\n            esac ;;\n        *[!a-zA-Z0-9/_.@+-]*) _gfy_shebang="" ;;\n        *) if _graphify_resolve_ambient "$_gfy_shebang"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;\n    esac\n    _graphify_usable "$_gfy_shebang" && GRAPHIFY_PYTHON=$_gfy_shebang\nfi\nif [ -z "$GRAPHIFY_PYTHON" ]; then\n    for _gfy_name in python3.14 python3 python; do\n        if _graphify_command "$_gfy_name"; then\n            _graphify_usable "$GRAPHIFY_RESOLVED" && { GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; break; }\n        fi\n    done\nfi\nif [ -z "$GRAPHIFY_PYTHON" ] && [ "${GRAPHIFY_DISCOVERY_OPTIONAL-0}" != 1 ]; then\n    echo "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." >&2\n    exit 1\nfi')"
"$GRAPHIFY_PYTHON" -E -P -B -c "
import json, sys
import networkx as nx
from networkx.readwrite import json_graph
from pathlib import Path

data = json.loads(Path('graphify-out/graph.json').read_text(encoding='utf-8'))
G = json_graph.node_link_graph(data, edges='links')

term = 'NODE_NAME'
term_lower = term.lower()

# Find best matching node
scored = sorted(
    [(sum(1 for w in term_lower.split() if w in G.nodes[n].get('label','').lower()), n)
     for n in G.nodes()],
    reverse=True
)
if not scored or scored[0][0] == 0:
    print(f'No node matching {term!r}')
    sys.exit(0)

nid = scored[0][1]
data_n = G.nodes[nid]
print(f'NODE: {data_n.get(\"label\", nid)}')
print(f'  source: {data_n.get(\"source_file\",\"unknown\")}')
print(f'  type: {data_n.get(\"file_type\",\"unknown\")}')
print(f'  degree: {G.degree(nid)}')
print()
print('CONNECTIONS:')
for neighbor in G.neighbors(nid):
    _raw = G[nid][neighbor]; edge = next(iter(_raw.values()), {}) if isinstance(G, nx.MultiGraph) else _raw
    nlabel = G.nodes[neighbor].get('label', neighbor)
    rel = edge.get('relation', '')
    conf = edge.get('confidence', '')
    src_file = G.nodes[neighbor].get('source_file', '')
    print(f'  --{rel}--> {nlabel} [{conf}] ({src_file})')
"
```

Replace `NODE_NAME` with the concept the user asked about. Then write a 3-5 sentence explanation of what this node is, what it connects to, and why those connections are significant. Use the source locations as citations.

After writing the explanation, save it back:

```bash
eval "$(printf '%b' 'GRAPHIFY_PYTHON=""\n_GRAPHIFY_VERSION_CHECK='"'"'import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)'"'"'\n_GRAPHIFY_WORKSPACE=$(/bin/pwd -P) || exit 1\n_graphify_canonical_root() {\n    _gfy_root=$1; [ -n "$_gfy_root" ] || return 1\n    case "$_gfy_root" in /*) ;; *) _gfy_root=$_GRAPHIFY_WORKSPACE/$_gfy_root ;; esac\n    [ -d "$_gfy_root" ] && CDPATH= cd -P -- "$_gfy_root" 2>/dev/null && /bin/pwd -P\n}\n_GRAPHIFY_INPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_INPUT_PATH-}") || _GRAPHIFY_INPUT_ROOT=""; _GRAPHIFY_OUTPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_OUTPUT_ROOT-${GRAPHIFY_OUT-graphify-out}}") || _GRAPHIFY_OUTPUT_ROOT=""\n_graphify_path_denied() { _gfy_policy_path=$1; [ "$_GRAPHIFY_WORKSPACE" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_WORKSPACE"|"$_GRAPHIFY_WORKSPACE"/*) return 0 ;; esac; [ -z "$_GRAPHIFY_INPUT_ROOT" ] || { [ "$_GRAPHIFY_INPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_INPUT_ROOT"|"$_GRAPHIFY_INPUT_ROOT"/*) return 0 ;; esac; }; [ -z "$_GRAPHIFY_OUTPUT_ROOT" ] || { [ "$_GRAPHIFY_OUTPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_OUTPUT_ROOT"|"$_GRAPHIFY_OUTPUT_ROOT"/*) return 0 ;; esac; }; return 1; }\n_graphify_resolve_ambient() {\n    _gfy_lexical=$1; case "$_gfy_lexical" in /*) ;; *) return 1 ;; esac\n    _graphify_path_denied "$_gfy_lexical" && return 1; _gfy_path=$_gfy_lexical\n    _gfy_links=0\n    while [ -L "$_gfy_path" ]; do\n        _gfy_links=$((_gfy_links + 1))\n        [ "$_gfy_links" -le 40 ] || return 1\n        [ -x /usr/bin/readlink ] || return 1\n        _gfy_link=$(/usr/bin/readlink "$_gfy_path") || return 1\n        case "$_gfy_link" in\n            /*) _gfy_path=$_gfy_link ;;\n            *) _gfy_dir=${_gfy_path%/*}; _gfy_path=$_gfy_dir/$_gfy_link ;;\n        esac\n    done\n    _gfy_dir=${_gfy_path%/*}; _gfy_base=${_gfy_path##*/}\n    _gfy_dir=$(CDPATH= cd -P -- "$_gfy_dir" 2>/dev/null && /bin/pwd -P) || return 1\n    _gfy_path=$_gfy_dir/$_gfy_base\n    _graphify_path_denied "$_gfy_path" && return 1\n    [ -x "$_gfy_lexical" ] || return 1\n    GRAPHIFY_RESOLVED=$_gfy_lexical\n}\n_graphify_command() {\n    _gfy_found=$(command -v "$1" 2>/dev/null) || return 1\n    _graphify_resolve_ambient "$_gfy_found"\n}\n_graphify_supported() {\n    [ -n "$1" ] && "$1" -E -P -B -c "$_GRAPHIFY_VERSION_CHECK" >/dev/null 2>&1\n}\n_graphify_usable() {\n    _graphify_supported "$1" && "$1" -E -P -B -c '"'"'import graphify'"'"' >/dev/null 2>&1\n}\n# An explicit absolute active environment is caller-selected, including a\n# project-local venv. Keep its lexical path for invocation.\ncase "${VIRTUAL_ENV-}" in\n    /*) _gfy_venv_python=$VIRTUAL_ENV/bin/python\n        _graphify_usable "$_gfy_venv_python" && GRAPHIFY_PYTHON=$_gfy_venv_python ;;\nesac\n# Trusted uv and pipx metadata, then trusted candidates derived from it.\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command uv; then\n    _gfy_uv=$GRAPHIFY_RESOLVED\n    _gfy_uv_dir=$("$_gfy_uv" tool dir 2>/dev/null)\n    _gfy_candidate=${_gfy_uv_dir:+$_gfy_uv_dir/graphifyy/bin/python}\n    if _graphify_resolve_ambient "$_gfy_candidate"; then\n        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED\n    fi\nfi\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command pipx; then\n    _gfy_pipx=$GRAPHIFY_RESOLVED\n    _gfy_pipx_home=$("$_gfy_pipx" environment --value PIPX_LOCAL_VENVS 2>/dev/null)\n    _gfy_candidate=${_gfy_pipx_home:+$_gfy_pipx_home/graphifyy/bin/python}\n    if _graphify_resolve_ambient "$_gfy_candidate"; then\n        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED\n    fi\nfi\n# Console-script shebang covers direct and pipx installs without executing the launcher.\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command graphify; then\n    _gfy_graphify=$GRAPHIFY_RESOLVED\n    IFS= read -r _gfy_shebang < "$_gfy_graphify" || _gfy_shebang=""\n    _gfy_shebang=${_gfy_shebang#\\#!}\n    case "$_gfy_shebang" in\n        "/usr/bin/env "*)\n            _gfy_env_command=${_gfy_shebang#"/usr/bin/env "}\n            case "$_gfy_env_command" in\n                ""|*[!a-zA-Z0-9_.@+-]*) _gfy_shebang="" ;;\n                *) if _graphify_command "$_gfy_env_command"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;\n            esac ;;\n        *[!a-zA-Z0-9/_.@+-]*) _gfy_shebang="" ;;\n        *) if _graphify_resolve_ambient "$_gfy_shebang"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;\n    esac\n    _graphify_usable "$_gfy_shebang" && GRAPHIFY_PYTHON=$_gfy_shebang\nfi\nif [ -z "$GRAPHIFY_PYTHON" ]; then\n    for _gfy_name in python3.14 python3 python; do\n        if _graphify_command "$_gfy_name"; then\n            _graphify_usable "$GRAPHIFY_RESOLVED" && { GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; break; }\n        fi\n    done\nfi\nif [ -z "$GRAPHIFY_PYTHON" ] && [ "${GRAPHIFY_DISCOVERY_OPTIONAL-0}" != 1 ]; then\n    echo "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." >&2\n    exit 1\nfi')"
"$GRAPHIFY_PYTHON" -E -P -B -m graphify save-result --question "Explain NODE_NAME" --answer "ANSWER" --type explain --nodes NODE_NAME
```
