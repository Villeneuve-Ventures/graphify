# graphify reference: query, path, explain

Load this when the user asks a question against an existing graph, or runs `/graphify path` or `/graphify explain`. The core's query stub points here for the full traversal flow. These flows use the `graphify query` CLI when it is available and fall back to an inline NetworkX traversal otherwise.

Two traversal modes - choose based on the question:

| Mode | Flag | Best for |
|------|------|----------|
| BFS (default) | _(none)_ | "What is X connected to?" - broad context, nearest neighbors first |
| DFS | `--dfs` | "How does X reach Y?" - trace a specific chain or dependency path |

First check the graph exists:
```bash
GRAPHIFY_PYTHON=$(GRAPHIFY_INPUT_PATH="${GRAPHIFY_INPUT_PATH-}" /bin/sh -p -c 'GRAPHIFY_PYTHON=""; GRAPHIFY_PYTHON_EXPLICIT=0; _GRAPHIFY_VERSION_CHECK='"'"'import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)'"'"'; _GRAPHIFY_IDENTITY_CHECK='"'"'exec("import importlib.metadata\nimport importlib.util\nimport json\nimport os\nimport sys\nimport urllib.parse\nimport urllib.request\n\ndef contained(path, root):\n    try:\n        normalized_root = os.path.normcase(root)\n        return os.path.commonpath((os.path.normcase(path), normalized_root)) == normalized_root\n    except (OSError, ValueError):\n        return False\n\ntry:\n    distribution = importlib.metadata.distribution(\"graphifyy\")\n    if distribution.metadata.get(\"Name\") != \"graphifyy\":\n        raise ValueError\n    spec = importlib.util.find_spec(\"graphify\")\n    if spec is None or not spec.origin:\n        raise ValueError\n    origin = os.path.abspath(spec.origin)\n    real_origin = os.path.realpath(origin)\n    direct_url_text = distribution.read_text(\"direct_url.json\")\n    editable = False\n    if direct_url_text is not None:\n        direct_url = json.loads(direct_url_text)\n        parsed = urllib.parse.urlparse(direct_url[\"url\"])\n        if direct_url.get(\"dir_info\", {}).get(\"editable\") is True:\n            editable = True\n            if parsed.scheme != \"file\" or parsed.netloc not in (\"\", \"localhost\"):\n                raise ValueError\n            package_root = os.path.abspath(\n                urllib.request.url2pathname(parsed.path)\n            )\n    if editable:\n        real_package_root = os.path.realpath(package_root)\n        if not contained(origin, package_root) or not contained(real_origin, real_package_root):\n            raise ValueError\n    else:\n        owned = [\n            entry\n            for entry in (distribution.files or ())\n            if str(entry) == \"graphify/__init__.py\"\n        ]\n        if len(owned) != 1:\n            raise ValueError\n        recorded_origin = os.path.abspath(distribution.locate_file(owned[0]))\n        if os.path.normcase(recorded_origin) != os.path.normcase(origin):\n            raise ValueError\n    arguments = sys.argv[1:]\n    if arguments[0] == \"ambient\":\n        for root_arg in arguments[1:]:\n            if not root_arg:\n                continue\n            root = os.path.abspath(root_arg)\n            real_root = os.path.realpath(root)\n            if contained(origin, root) or contained(real_origin, real_root):\n                raise ValueError\nexcept (Exception, SystemExit):\n    raise SystemExit(1)\n")'"'"'; if [ -x /usr/bin/cygpath.exe ]; then _GRAPHIFY_CYGPATH=/usr/bin/cygpath.exe; elif [ -x /usr/bin/cygpath ]; then _GRAPHIFY_CYGPATH=/usr/bin/cygpath; elif [ -x /bin/cygpath.exe ]; then _GRAPHIFY_CYGPATH=/bin/cygpath.exe; elif [ -x /bin/cygpath ]; then _GRAPHIFY_CYGPATH=/bin/cygpath; else exit 1; fi; _graphify_to_posix() { _gfy_native=$1; case "$_gfy_native" in [a-zA-Z]:|[a-zA-Z]:[!\\/]*|\\|\\[!\\]*) return 1 ;; esac; "$_GRAPHIFY_CYGPATH" -u "$_gfy_native"; }; _graphify_to_native() { [ -n "$1" ] || return 0; "$_GRAPHIFY_CYGPATH" -w "$1"; }; _GRAPHIFY_WORKSPACE=$(command pwd -P) || exit 1; _GRAPHIFY_WORKSPACE_NATIVE=$(_graphify_to_native "$_GRAPHIFY_WORKSPACE") || exit 1; _graphify_canonical_root() { _gfy_root=$1; [ -n "$_gfy_root" ] || return 1; case "$_gfy_root" in [a-zA-Z]:|[a-zA-Z]:[!\\/]*|\\|\\[!\\]*) return 1 ;; /*) ;; *) _gfy_root=$(_graphify_to_posix "$_gfy_root") || return 1 ;; esac; case "$_gfy_root" in /*) ;; *) _gfy_root=$_GRAPHIFY_WORKSPACE/$_gfy_root ;; esac; if [ -d "$_gfy_root" ]; then CDPATH= cd -P -- "$_gfy_root" 2>/dev/null && command pwd -P; else printf '"'"'%s\n'"'"' "$_gfy_root"; fi; }; _graphify_absolute_command() { _gfy_command=$1; case "$_gfy_command" in /*) case "$_gfy_command" in */./*|*/../*|*/.|*/..) ;; *) GRAPHIFY_COMMAND_PATH=$_gfy_command; return 0 ;; esac; _gfy_command_dir=${_gfy_command%/*}; _gfy_command_base=${_gfy_command##*/} ;; */*) _gfy_command_dir=$_GRAPHIFY_WORKSPACE/${_gfy_command%/*}; _gfy_command_base=${_gfy_command##*/} ;; *) _gfy_command_dir=$_GRAPHIFY_WORKSPACE; _gfy_command_base=$_gfy_command ;; esac; case "$_gfy_command_base" in ""|.|..) return 1 ;; esac; _gfy_command_dir=$(CDPATH= cd -L -- "$_gfy_command_dir" 2>/dev/null && command pwd -L) || return 1; GRAPHIFY_COMMAND_PATH=$_gfy_command_dir/$_gfy_command_base; }; _GRAPHIFY_DENY_POLICY_INVALID=0; _gfy_input_raw=${GRAPHIFY_INPUT_PATH-}; _gfy_output_raw=${GRAPHIFY_OUTPUT_ROOT-${GRAPHIFY_OUT-graphify-out}}; _GRAPHIFY_INPUT_ROOT=$(_graphify_canonical_root "$_gfy_input_raw") || { _GRAPHIFY_INPUT_ROOT=""; [ -z "$_gfy_input_raw" ] || _GRAPHIFY_DENY_POLICY_INVALID=1; }; _GRAPHIFY_OUTPUT_ROOT=$(_graphify_canonical_root "$_gfy_output_raw") || { _GRAPHIFY_OUTPUT_ROOT=""; [ -z "$_gfy_output_raw" ] || _GRAPHIFY_DENY_POLICY_INVALID=1; }; [ "$_GRAPHIFY_DENY_POLICY_INVALID" = 0 ] || exit 1; _GRAPHIFY_INPUT_ROOT_NATIVE=$(_graphify_to_native "$_GRAPHIFY_INPUT_ROOT") || exit 1; _GRAPHIFY_OUTPUT_ROOT_NATIVE=$(_graphify_to_native "$_GRAPHIFY_OUTPUT_ROOT") || exit 1; _graphify_path_denied() { _gfy_policy_path=$1; [ "$_GRAPHIFY_WORKSPACE" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_WORKSPACE"|"$_GRAPHIFY_WORKSPACE"/*) return 0 ;; esac; [ -z "$_GRAPHIFY_INPUT_ROOT" ] || { [ "$_GRAPHIFY_INPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_INPUT_ROOT"|"$_GRAPHIFY_INPUT_ROOT"/*) return 0 ;; esac; }; [ -z "$_GRAPHIFY_OUTPUT_ROOT" ] || { [ "$_GRAPHIFY_OUTPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_OUTPUT_ROOT"|"$_GRAPHIFY_OUTPUT_ROOT"/*) return 0 ;; esac; }; return 1; }; _graphify_readlink() { if [ -x /usr/bin/readlink ]; then /usr/bin/readlink "$1"; elif [ -x /bin/readlink ]; then /bin/readlink "$1"; elif [ -x /run/current-system/sw/bin/readlink ]; then /run/current-system/sw/bin/readlink "$1"; else command -p readlink "$1"; fi; }; _graphify_resolve_ambient() { _gfy_lexical=$1; _gfy_lexical=$(_graphify_to_posix "$_gfy_lexical") || return 1; case "$_gfy_lexical" in /*) ;; *) return 1 ;; esac; _graphify_path_denied "$_gfy_lexical" && return 1; _gfy_path=$_gfy_lexical; _gfy_links=0; while [ -L "$_gfy_path" ]; do _gfy_links=$((_gfy_links + 1)); [ "$_gfy_links" -le 40 ] || return 1; _gfy_link=$(_graphify_readlink "$_gfy_path") || return 1; case "$_gfy_link" in /*) _gfy_path=$_gfy_link ;; *) _gfy_dir=${_gfy_path%/*}; _gfy_path=$_gfy_dir/$_gfy_link ;; esac; done; _gfy_dir=${_gfy_path%/*}; _gfy_base=${_gfy_path##*/}; _gfy_dir=$(CDPATH= cd -P -- "$_gfy_dir" 2>/dev/null && command pwd -P) || return 1; _gfy_path=$_gfy_dir/$_gfy_base; _graphify_path_denied "$_gfy_path" && return 1; [ -x "$_gfy_lexical" ] || return 1; GRAPHIFY_RESOLVED=$_gfy_lexical; }; _graphify_command() { _gfy_found=$(command -v "$1" 2>/dev/null) || return 1; _gfy_found=$(_graphify_to_posix "$_gfy_found") || return 1; _graphify_absolute_command "$_gfy_found" || return 1; _graphify_resolve_ambient "$GRAPHIFY_COMMAND_PATH"; }; _graphify_supported() { [ -n "$1" ] && "$1" -E -P -B -c "$_GRAPHIFY_VERSION_CHECK" >/dev/null 2>&1; }; _graphify_usable() { _graphify_supported "$1" && "$1" -E -P -B -c "$_GRAPHIFY_IDENTITY_CHECK" trusted >/dev/null 2>&1; }; _graphify_ambient_usable() { _graphify_supported "$1" && "$1" -E -P -B -c "$_GRAPHIFY_IDENTITY_CHECK" ambient "$_GRAPHIFY_WORKSPACE_NATIVE" "$_GRAPHIFY_INPUT_ROOT_NATIVE" "$_GRAPHIFY_OUTPUT_ROOT_NATIVE" >/dev/null 2>&1; }; case "${VIRTUAL_ENV-}" in "") ;; *) _gfy_venv=$(_graphify_to_posix "$VIRTUAL_ENV") || exit 1; _gfy_venv_python=$_gfy_venv/Scripts/python.exe; _graphify_usable "$_gfy_venv_python" && { GRAPHIFY_PYTHON=$_gfy_venv_python; GRAPHIFY_PYTHON_EXPLICIT=1; } ;; esac; if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command uv; then _gfy_uv=$GRAPHIFY_RESOLVED; _gfy_uv_dir=$("$_gfy_uv" tool dir 2>/dev/null); _gfy_uv_dir=$(_graphify_to_posix "$_gfy_uv_dir") || _gfy_uv_dir=""; _gfy_candidate=${_gfy_uv_dir:+$_gfy_uv_dir/graphifyy/Scripts/python.exe}; if _graphify_resolve_ambient "$_gfy_candidate"; then _graphify_ambient_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; fi; fi; if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command pipx; then _gfy_pipx=$GRAPHIFY_RESOLVED; _gfy_pipx_home=$("$_gfy_pipx" environment --value PIPX_LOCAL_VENVS 2>/dev/null); _gfy_pipx_home=$(_graphify_to_posix "$_gfy_pipx_home") || _gfy_pipx_home=""; _gfy_candidate=${_gfy_pipx_home:+$_gfy_pipx_home/graphifyy/Scripts/python.exe}; if _graphify_resolve_ambient "$_gfy_candidate"; then _graphify_ambient_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; fi; fi; if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command graphify; then _gfy_graphify=$(_graphify_to_posix "$GRAPHIFY_RESOLVED") || _gfy_graphify=""; _gfy_bindir=${_gfy_graphify%/*}; for _gfy_candidate in "$_gfy_bindir/python.exe" "$_gfy_bindir/../python.exe"; do if _graphify_resolve_ambient "$_gfy_candidate" && _graphify_ambient_usable "$GRAPHIFY_RESOLVED"; then GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; break; fi; done; fi; if [ -z "$GRAPHIFY_PYTHON" ]; then for _gfy_name in python3.14 python3 python; do if _graphify_command "$_gfy_name"; then _graphify_ambient_usable "$GRAPHIFY_RESOLVED" && { GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; break; }; fi; done; fi; if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command py; then _gfy_py=$GRAPHIFY_RESOLVED; _gfy_candidate=$("$_gfy_py" -3.14 -E -P -B -c '"'"'import sys; print(sys.executable)'"'"' 2>/dev/null); _gfy_candidate=$(_graphify_to_posix "$_gfy_candidate") || _gfy_candidate=""; if _graphify_resolve_ambient "$_gfy_candidate"; then _graphify_ambient_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; fi; fi; if [ -z "$GRAPHIFY_PYTHON" ] && [ "${GRAPHIFY_DISCOVERY_OPTIONAL-0}" != 1 ]; then echo "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." >&2; exit 1; fi; [ -n "$GRAPHIFY_PYTHON" ] || exit 1; printf "%sx" "$GRAPHIFY_PYTHON"'); GRAPHIFY_PYTHON=${GRAPHIFY_PYTHON%x}; GRAPHIFY_PYTHON=${GRAPHIFY_PYTHON:?Graphify interpreter discovery failed}
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
GRAPHIFY_PYTHON=$(GRAPHIFY_INPUT_PATH="${GRAPHIFY_INPUT_PATH-}" /bin/sh -p -c 'GRAPHIFY_PYTHON=""; GRAPHIFY_PYTHON_EXPLICIT=0; _GRAPHIFY_VERSION_CHECK='"'"'import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)'"'"'; _GRAPHIFY_IDENTITY_CHECK='"'"'exec("import importlib.metadata\nimport importlib.util\nimport json\nimport os\nimport sys\nimport urllib.parse\nimport urllib.request\n\ndef contained(path, root):\n    try:\n        normalized_root = os.path.normcase(root)\n        return os.path.commonpath((os.path.normcase(path), normalized_root)) == normalized_root\n    except (OSError, ValueError):\n        return False\n\ntry:\n    distribution = importlib.metadata.distribution(\"graphifyy\")\n    if distribution.metadata.get(\"Name\") != \"graphifyy\":\n        raise ValueError\n    spec = importlib.util.find_spec(\"graphify\")\n    if spec is None or not spec.origin:\n        raise ValueError\n    origin = os.path.abspath(spec.origin)\n    real_origin = os.path.realpath(origin)\n    direct_url_text = distribution.read_text(\"direct_url.json\")\n    editable = False\n    if direct_url_text is not None:\n        direct_url = json.loads(direct_url_text)\n        parsed = urllib.parse.urlparse(direct_url[\"url\"])\n        if direct_url.get(\"dir_info\", {}).get(\"editable\") is True:\n            editable = True\n            if parsed.scheme != \"file\" or parsed.netloc not in (\"\", \"localhost\"):\n                raise ValueError\n            package_root = os.path.abspath(\n                urllib.request.url2pathname(parsed.path)\n            )\n    if editable:\n        real_package_root = os.path.realpath(package_root)\n        if not contained(origin, package_root) or not contained(real_origin, real_package_root):\n            raise ValueError\n    else:\n        owned = [\n            entry\n            for entry in (distribution.files or ())\n            if str(entry) == \"graphify/__init__.py\"\n        ]\n        if len(owned) != 1:\n            raise ValueError\n        recorded_origin = os.path.abspath(distribution.locate_file(owned[0]))\n        if os.path.normcase(recorded_origin) != os.path.normcase(origin):\n            raise ValueError\n    arguments = sys.argv[1:]\n    if arguments[0] == \"ambient\":\n        for root_arg in arguments[1:]:\n            if not root_arg:\n                continue\n            root = os.path.abspath(root_arg)\n            real_root = os.path.realpath(root)\n            if contained(origin, root) or contained(real_origin, real_root):\n                raise ValueError\nexcept (Exception, SystemExit):\n    raise SystemExit(1)\n")'"'"'; if [ -x /usr/bin/cygpath.exe ]; then _GRAPHIFY_CYGPATH=/usr/bin/cygpath.exe; elif [ -x /usr/bin/cygpath ]; then _GRAPHIFY_CYGPATH=/usr/bin/cygpath; elif [ -x /bin/cygpath.exe ]; then _GRAPHIFY_CYGPATH=/bin/cygpath.exe; elif [ -x /bin/cygpath ]; then _GRAPHIFY_CYGPATH=/bin/cygpath; else exit 1; fi; _graphify_to_posix() { _gfy_native=$1; case "$_gfy_native" in [a-zA-Z]:|[a-zA-Z]:[!\\/]*|\\|\\[!\\]*) return 1 ;; esac; "$_GRAPHIFY_CYGPATH" -u "$_gfy_native"; }; _graphify_to_native() { [ -n "$1" ] || return 0; "$_GRAPHIFY_CYGPATH" -w "$1"; }; _GRAPHIFY_WORKSPACE=$(command pwd -P) || exit 1; _GRAPHIFY_WORKSPACE_NATIVE=$(_graphify_to_native "$_GRAPHIFY_WORKSPACE") || exit 1; _graphify_canonical_root() { _gfy_root=$1; [ -n "$_gfy_root" ] || return 1; case "$_gfy_root" in [a-zA-Z]:|[a-zA-Z]:[!\\/]*|\\|\\[!\\]*) return 1 ;; /*) ;; *) _gfy_root=$(_graphify_to_posix "$_gfy_root") || return 1 ;; esac; case "$_gfy_root" in /*) ;; *) _gfy_root=$_GRAPHIFY_WORKSPACE/$_gfy_root ;; esac; if [ -d "$_gfy_root" ]; then CDPATH= cd -P -- "$_gfy_root" 2>/dev/null && command pwd -P; else printf '"'"'%s\n'"'"' "$_gfy_root"; fi; }; _graphify_absolute_command() { _gfy_command=$1; case "$_gfy_command" in /*) case "$_gfy_command" in */./*|*/../*|*/.|*/..) ;; *) GRAPHIFY_COMMAND_PATH=$_gfy_command; return 0 ;; esac; _gfy_command_dir=${_gfy_command%/*}; _gfy_command_base=${_gfy_command##*/} ;; */*) _gfy_command_dir=$_GRAPHIFY_WORKSPACE/${_gfy_command%/*}; _gfy_command_base=${_gfy_command##*/} ;; *) _gfy_command_dir=$_GRAPHIFY_WORKSPACE; _gfy_command_base=$_gfy_command ;; esac; case "$_gfy_command_base" in ""|.|..) return 1 ;; esac; _gfy_command_dir=$(CDPATH= cd -L -- "$_gfy_command_dir" 2>/dev/null && command pwd -L) || return 1; GRAPHIFY_COMMAND_PATH=$_gfy_command_dir/$_gfy_command_base; }; _GRAPHIFY_DENY_POLICY_INVALID=0; _gfy_input_raw=${GRAPHIFY_INPUT_PATH-}; _gfy_output_raw=${GRAPHIFY_OUTPUT_ROOT-${GRAPHIFY_OUT-graphify-out}}; _GRAPHIFY_INPUT_ROOT=$(_graphify_canonical_root "$_gfy_input_raw") || { _GRAPHIFY_INPUT_ROOT=""; [ -z "$_gfy_input_raw" ] || _GRAPHIFY_DENY_POLICY_INVALID=1; }; _GRAPHIFY_OUTPUT_ROOT=$(_graphify_canonical_root "$_gfy_output_raw") || { _GRAPHIFY_OUTPUT_ROOT=""; [ -z "$_gfy_output_raw" ] || _GRAPHIFY_DENY_POLICY_INVALID=1; }; [ "$_GRAPHIFY_DENY_POLICY_INVALID" = 0 ] || exit 1; _GRAPHIFY_INPUT_ROOT_NATIVE=$(_graphify_to_native "$_GRAPHIFY_INPUT_ROOT") || exit 1; _GRAPHIFY_OUTPUT_ROOT_NATIVE=$(_graphify_to_native "$_GRAPHIFY_OUTPUT_ROOT") || exit 1; _graphify_path_denied() { _gfy_policy_path=$1; [ "$_GRAPHIFY_WORKSPACE" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_WORKSPACE"|"$_GRAPHIFY_WORKSPACE"/*) return 0 ;; esac; [ -z "$_GRAPHIFY_INPUT_ROOT" ] || { [ "$_GRAPHIFY_INPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_INPUT_ROOT"|"$_GRAPHIFY_INPUT_ROOT"/*) return 0 ;; esac; }; [ -z "$_GRAPHIFY_OUTPUT_ROOT" ] || { [ "$_GRAPHIFY_OUTPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_OUTPUT_ROOT"|"$_GRAPHIFY_OUTPUT_ROOT"/*) return 0 ;; esac; }; return 1; }; _graphify_readlink() { if [ -x /usr/bin/readlink ]; then /usr/bin/readlink "$1"; elif [ -x /bin/readlink ]; then /bin/readlink "$1"; elif [ -x /run/current-system/sw/bin/readlink ]; then /run/current-system/sw/bin/readlink "$1"; else command -p readlink "$1"; fi; }; _graphify_resolve_ambient() { _gfy_lexical=$1; _gfy_lexical=$(_graphify_to_posix "$_gfy_lexical") || return 1; case "$_gfy_lexical" in /*) ;; *) return 1 ;; esac; _graphify_path_denied "$_gfy_lexical" && return 1; _gfy_path=$_gfy_lexical; _gfy_links=0; while [ -L "$_gfy_path" ]; do _gfy_links=$((_gfy_links + 1)); [ "$_gfy_links" -le 40 ] || return 1; _gfy_link=$(_graphify_readlink "$_gfy_path") || return 1; case "$_gfy_link" in /*) _gfy_path=$_gfy_link ;; *) _gfy_dir=${_gfy_path%/*}; _gfy_path=$_gfy_dir/$_gfy_link ;; esac; done; _gfy_dir=${_gfy_path%/*}; _gfy_base=${_gfy_path##*/}; _gfy_dir=$(CDPATH= cd -P -- "$_gfy_dir" 2>/dev/null && command pwd -P) || return 1; _gfy_path=$_gfy_dir/$_gfy_base; _graphify_path_denied "$_gfy_path" && return 1; [ -x "$_gfy_lexical" ] || return 1; GRAPHIFY_RESOLVED=$_gfy_lexical; }; _graphify_command() { _gfy_found=$(command -v "$1" 2>/dev/null) || return 1; _gfy_found=$(_graphify_to_posix "$_gfy_found") || return 1; _graphify_absolute_command "$_gfy_found" || return 1; _graphify_resolve_ambient "$GRAPHIFY_COMMAND_PATH"; }; _graphify_supported() { [ -n "$1" ] && "$1" -E -P -B -c "$_GRAPHIFY_VERSION_CHECK" >/dev/null 2>&1; }; _graphify_usable() { _graphify_supported "$1" && "$1" -E -P -B -c "$_GRAPHIFY_IDENTITY_CHECK" trusted >/dev/null 2>&1; }; _graphify_ambient_usable() { _graphify_supported "$1" && "$1" -E -P -B -c "$_GRAPHIFY_IDENTITY_CHECK" ambient "$_GRAPHIFY_WORKSPACE_NATIVE" "$_GRAPHIFY_INPUT_ROOT_NATIVE" "$_GRAPHIFY_OUTPUT_ROOT_NATIVE" >/dev/null 2>&1; }; case "${VIRTUAL_ENV-}" in "") ;; *) _gfy_venv=$(_graphify_to_posix "$VIRTUAL_ENV") || exit 1; _gfy_venv_python=$_gfy_venv/Scripts/python.exe; _graphify_usable "$_gfy_venv_python" && { GRAPHIFY_PYTHON=$_gfy_venv_python; GRAPHIFY_PYTHON_EXPLICIT=1; } ;; esac; if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command uv; then _gfy_uv=$GRAPHIFY_RESOLVED; _gfy_uv_dir=$("$_gfy_uv" tool dir 2>/dev/null); _gfy_uv_dir=$(_graphify_to_posix "$_gfy_uv_dir") || _gfy_uv_dir=""; _gfy_candidate=${_gfy_uv_dir:+$_gfy_uv_dir/graphifyy/Scripts/python.exe}; if _graphify_resolve_ambient "$_gfy_candidate"; then _graphify_ambient_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; fi; fi; if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command pipx; then _gfy_pipx=$GRAPHIFY_RESOLVED; _gfy_pipx_home=$("$_gfy_pipx" environment --value PIPX_LOCAL_VENVS 2>/dev/null); _gfy_pipx_home=$(_graphify_to_posix "$_gfy_pipx_home") || _gfy_pipx_home=""; _gfy_candidate=${_gfy_pipx_home:+$_gfy_pipx_home/graphifyy/Scripts/python.exe}; if _graphify_resolve_ambient "$_gfy_candidate"; then _graphify_ambient_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; fi; fi; if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command graphify; then _gfy_graphify=$(_graphify_to_posix "$GRAPHIFY_RESOLVED") || _gfy_graphify=""; _gfy_bindir=${_gfy_graphify%/*}; for _gfy_candidate in "$_gfy_bindir/python.exe" "$_gfy_bindir/../python.exe"; do if _graphify_resolve_ambient "$_gfy_candidate" && _graphify_ambient_usable "$GRAPHIFY_RESOLVED"; then GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; break; fi; done; fi; if [ -z "$GRAPHIFY_PYTHON" ]; then for _gfy_name in python3.14 python3 python; do if _graphify_command "$_gfy_name"; then _graphify_ambient_usable "$GRAPHIFY_RESOLVED" && { GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; break; }; fi; done; fi; if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command py; then _gfy_py=$GRAPHIFY_RESOLVED; _gfy_candidate=$("$_gfy_py" -3.14 -E -P -B -c '"'"'import sys; print(sys.executable)'"'"' 2>/dev/null); _gfy_candidate=$(_graphify_to_posix "$_gfy_candidate") || _gfy_candidate=""; if _graphify_resolve_ambient "$_gfy_candidate"; then _graphify_ambient_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; fi; fi; if [ -z "$GRAPHIFY_PYTHON" ] && [ "${GRAPHIFY_DISCOVERY_OPTIONAL-0}" != 1 ]; then echo "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." >&2; exit 1; fi; [ -n "$GRAPHIFY_PYTHON" ] || exit 1; printf "%sx" "$GRAPHIFY_PYTHON"'); GRAPHIFY_PYTHON=${GRAPHIFY_PYTHON%x}; GRAPHIFY_PYTHON=${GRAPHIFY_PYTHON:?Graphify interpreter discovery failed}
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
```powershell
$env:GRAPHIFY_INPUT_PATH = "INPUT_PATH"
$GraphifyPython = $null
$GraphifyPythonExplicit = $false
$GraphifyWorkspace = [IO.Path]::GetFullPath((Get-Location).Path)
if ($GraphifyWorkspace -ne [IO.Path]::GetPathRoot($GraphifyWorkspace)) { $GraphifyWorkspace = $GraphifyWorkspace.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) }
$GraphifyDenyRoots = [Collections.Generic.List[string]]::new()
$GraphifyDenyPolicyInvalid = $false
function Test-GraphifyFullyQualifiedPath {
    param([string]$Path)
    if (-not $Path -or -not [IO.Path]::IsPathRooted($Path)) { return $false }
    $root = [IO.Path]::GetPathRoot($Path)
    if (-not $root -or $root -match '^[A-Za-z]:$') { return $false }
    if ($Path -match '^[\\/](?![\\/])') { return $false }
    return $true
}
function Resolve-GraphifyPolicyPath {
    param(
        [string]$Path,
        [Collections.Generic.HashSet[string]]$Visited = $null,
        [int]$Hops = 0
    )
    if (-not (Test-GraphifyFullyQualifiedPath $Path)) { return $null }
    try {
        $full = [IO.Path]::GetFullPath($Path)
        $root = [IO.Path]::GetPathRoot($full)
        if (-not $root) { return $null }
        if ($null -eq $Visited) {
            $Visited = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        }
        $current = $root
        $parts = @($full.Substring($root.Length) -split '[\\/]' | Where-Object { $_ })
        for ($index = 0; $index -lt $parts.Count; $index++) {
            $part = $parts[$index]
            $current = [IO.Path]::GetFullPath((Join-Path $current $part))
            $info = Get-Item -LiteralPath $current -Force -ErrorAction Stop
            if (($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                $sourcePath = [IO.Path]::GetFullPath($current)
                if (-not $Visited.Add($sourcePath)) { return $null }
                $nextHops = $Hops + 1
                if ($nextHops -gt 63) { return $null }
                if ($info.PSObject.Methods.Name -notcontains "ResolveLinkTarget") {
                    if ($info.PSObject.Properties.Name -notcontains "Target") { return $null }
                    $targets = @(
                        @($info.Target) | Where-Object {
                            $_ -is [string] -and -not [string]::IsNullOrWhiteSpace([string]$_)
                        }
                    )
                    if ($targets.Count -ne 1) { return $null }
                    $targetText = [string]$targets[0]
                } else {
                    $target = $info.ResolveLinkTarget($false)
                    if (-not $target -or -not $target.FullName) { return $null }
                    $targetText = [string]$target.FullName
                }
                if (Test-GraphifyFullyQualifiedPath $targetText) {
                    $targetPath = [IO.Path]::GetFullPath($targetText)
                } else {
                    $linkParent = Split-Path -Parent $sourcePath
                    if (-not $linkParent) { return $null }
                    $targetPath = [IO.Path]::GetFullPath((Join-Path $linkParent $targetText))
                }
                if ($index + 1 -lt $parts.Count) {
                    $remainingSuffix = [string]::Join(
                        [IO.Path]::DirectorySeparatorChar,
                        [string[]]$parts[($index + 1)..($parts.Count - 1)]
                    )
                    $targetPath = Join-Path $targetPath $remainingSuffix
                }
                return Resolve-GraphifyPolicyPath $targetPath $Visited $nextHops
            }
        }
        return [IO.Path]::GetFullPath($current)
    } catch { return $null }
}
function Add-GraphifyDenyRoot {
    param([string]$Path, [bool]$Required = $false)
    if (-not $Path) { return }
    try {
        $full = if (Test-GraphifyFullyQualifiedPath $path) { [IO.Path]::GetFullPath($Path) } else { [IO.Path]::GetFullPath((Join-Path $GraphifyWorkspace $Path)) }
        if (-not (Test-Path -LiteralPath $full -PathType Container)) { if ($Required) { $script:GraphifyDenyPolicyInvalid = $true }; return }
        $resolved = Resolve-GraphifyPolicyPath $full
        if (-not $resolved) { $script:GraphifyDenyPolicyInvalid = $true; return }
        if ($resolved -ne [IO.Path]::GetPathRoot($resolved)) { $resolved = $resolved.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) }
        $lexical = [IO.Path]::GetFullPath($full)
        if ($lexical -ne [IO.Path]::GetPathRoot($lexical)) { $lexical = $lexical.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) }
        if (-not $GraphifyDenyRoots.Contains($lexical)) { $GraphifyDenyRoots.Add($lexical) }
        if (-not $GraphifyDenyRoots.Contains($resolved)) { $GraphifyDenyRoots.Add($resolved) }
    } catch { $script:GraphifyDenyPolicyInvalid = $true; return }
}
Add-GraphifyDenyRoot $GraphifyWorkspace $true
if ($env:GRAPHIFY_INPUT_PATH -and $env:GRAPHIFY_INPUT_PATH -ne "INPUT_PATH") { Add-GraphifyDenyRoot $env:GRAPHIFY_INPUT_PATH $true }
$GraphifySelectedOutput = if ($env:GRAPHIFY_OUTPUT_ROOT) { $env:GRAPHIFY_OUTPUT_ROOT } elseif ($env:GRAPHIFY_OUT) { $env:GRAPHIFY_OUT } else { "graphify-out" }
Add-GraphifyDenyRoot $GraphifySelectedOutput ([bool]($env:GRAPHIFY_OUTPUT_ROOT -or $env:GRAPHIFY_OUT))
function Test-GraphifyWorkspacePath {
    param([string]$Path)
    if ($GraphifyDenyPolicyInvalid) { return $true }
    if (-not (Test-GraphifyFullyQualifiedPath $Path)) { return $true }
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    try {
        $lexical = [IO.Path]::GetFullPath($Path)
        $resolved = Resolve-GraphifyPolicyPath $lexical
        if (-not $resolved) { return $true }
    }
    catch { return $true }
    foreach ($root in $GraphifyDenyRoots) {
        $prefix = if ($root -eq [IO.Path]::GetPathRoot($root)) { $root } else { $root + [IO.Path]::DirectorySeparatorChar }
        if ($lexical -eq $root -or $lexical.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { return $true }
        if ($resolved -eq $root -or $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}
function Resolve-GraphifyAmbientCommand {
    param([string]$Name)
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $command) { return $null }
    $path = [IO.Path]::GetFullPath($command.Source)
    if (Test-GraphifyWorkspacePath $path) { return $null }
    return $path
}
$GraphifyIdentityCheck = @'
exec('import importlib.metadata\nimport importlib.util\nimport json\nimport os\nimport sys\nimport urllib.parse\nimport urllib.request\n\ndef contained(path, root):\n    try:\n        normalized_root = os.path.normcase(root)\n        return os.path.commonpath((os.path.normcase(path), normalized_root)) == normalized_root\n    except (OSError, ValueError):\n        return False\n\ntry:\n    distribution = importlib.metadata.distribution("graphifyy")\n    if distribution.metadata.get("Name") != "graphifyy":\n        raise ValueError\n    spec = importlib.util.find_spec("graphify")\n    if spec is None or not spec.origin:\n        raise ValueError\n    origin = os.path.abspath(spec.origin)\n    real_origin = os.path.realpath(origin)\n    direct_url_text = distribution.read_text("direct_url.json")\n    editable = False\n    if direct_url_text is not None:\n        direct_url = json.loads(direct_url_text)\n        parsed = urllib.parse.urlparse(direct_url["url"])\n        if direct_url.get("dir_info", {}).get("editable") is True:\n            editable = True\n            if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):\n                raise ValueError\n            package_root = os.path.abspath(\n                urllib.request.url2pathname(parsed.path)\n            )\n    if editable:\n        real_package_root = os.path.realpath(package_root)\n        if not contained(origin, package_root) or not contained(real_origin, real_package_root):\n            raise ValueError\n    else:\n        owned = [\n            entry\n            for entry in (distribution.files or ())\n            if str(entry) == "graphify/__init__.py"\n        ]\n        if len(owned) != 1:\n            raise ValueError\n        recorded_origin = os.path.abspath(distribution.locate_file(owned[0]))\n        if os.path.normcase(recorded_origin) != os.path.normcase(origin):\n            raise ValueError\n    arguments = sys.argv[1:]\n    if arguments[0] == "ambient":\n        for root_arg in arguments[1:]:\n            if not root_arg:\n                continue\n            root = os.path.abspath(root_arg)\n            real_root = os.path.realpath(root)\n            if contained(origin, root) or contained(real_origin, real_root):\n                raise ValueError\nexcept (Exception, SystemExit):\n    raise SystemExit(1)\n')
'@
function Test-GraphifyPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    if (-not (Test-GraphifySupportedPython $Candidate)) { return $false }
    & $Candidate -E -P -B -c $GraphifyIdentityCheck trusted 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    return $invocationSucceeded -and $exitCode -eq 0
}
function Test-GraphifyAmbientPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    if (-not (Test-GraphifySupportedPython $Candidate)) { return $false }
    & $Candidate -E -P -B -c $GraphifyIdentityCheck ambient @GraphifyDenyRoots 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    return $invocationSucceeded -and $exitCode -eq 0
}
function Test-GraphifySupportedPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    & $Candidate -E -P -B -c "import sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and sys.version_info.releaselevel == 'final' and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)" 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    return $invocationSucceeded -and $exitCode -eq 0
}
function Invoke-GraphifyNativeText {
    param([string]$Candidate, [string[]]$Arguments)
    if (-not $Candidate) { return $null }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $null }
    $output = & $Candidate @Arguments 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    if (-not ($invocationSucceeded -and $exitCode -eq 0)) { return $null }
    $lines = @(
        @($output) | Where-Object {
            $_ -is [string] -and -not [string]::IsNullOrWhiteSpace([string]$_)
        }
    )
    if ($lines.Count -ne 1) { return $null }
    return ([string]$lines[0]).Trim()
}
if (Test-GraphifyFullyQualifiedPath "$env:VIRTUAL_ENV") {
    $activeVenv = Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $activeVenv)) { $activeVenv = Join-Path $env:VIRTUAL_ENV "bin/python" }
    if (Test-GraphifyPython $activeVenv) { $GraphifyPython = $activeVenv; $GraphifyPythonExplicit = $true }
}
if (-not $GraphifyPython) {
    $uv = Resolve-GraphifyAmbientCommand uv
    if ($uv) {
        $uvDir = Invoke-GraphifyNativeText $uv @("tool", "dir")
        if ($uvDir) {
            $candidate = Join-Path $uvDir "graphifyy\Scripts\python.exe"
            if (-not (Test-Path -LiteralPath $candidate)) { $candidate = Join-Path $uvDir "graphifyy/bin/python" }
            if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyAmbientPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate) }
        }
    }
}
if (-not $GraphifyPython) {
    $pipx = Resolve-GraphifyAmbientCommand pipx
    if ($pipx) {
        $venvs = Invoke-GraphifyNativeText $pipx @("environment", "--value", "PIPX_LOCAL_VENVS")
        if ($venvs) {
            $candidate = Join-Path $venvs "graphifyy\Scripts\python.exe"
            if (-not (Test-Path -LiteralPath $candidate)) { $candidate = Join-Path $venvs "graphifyy/bin/python" }
            if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyAmbientPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate) }
        }
    }
}
if (-not $GraphifyPython) {
    $graphify = Resolve-GraphifyAmbientCommand graphify
    if ($graphify) {
        $bindir = Split-Path -Parent $graphify
        foreach ($candidate in @((Join-Path $bindir "python.exe"), (Join-Path $bindir "../python.exe"))) {
            if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyAmbientPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate); break }
        }
    }
}
if (-not $GraphifyPython) {
    foreach ($name in @("python3.14", "python3", "py", "python")) {
        $candidate = Resolve-GraphifyAmbientCommand $name
        if (-not $candidate) { continue }
        if ($name -eq "py") {
            $resolved = Invoke-GraphifyNativeText $candidate @("-3.14", "-E", "-P", "-B", "-c", "import sys; print(sys.executable)")
            if ($resolved -and -not (Test-GraphifyWorkspacePath $resolved) -and (Test-GraphifyAmbientPython $resolved)) { $GraphifyPython = [IO.Path]::GetFullPath($resolved); break }
        } elseif (Test-GraphifyAmbientPython $candidate) { $GraphifyPython = $candidate; break }
    }
}
if (-not $GraphifyPython -and -not $GraphifyDiscoveryOptional) { throw "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." }
& $GraphifyPython -E -P -B -m graphify query "QUESTION"
# or: & $GraphifyPython -E -P -B -m graphify query "QUESTION" --dfs --budget 3000
```

If the CLI is unavailable, load `graphify-out/graph.json` and run the traversal inline:

1. Find the 1-3 nodes whose label best matches the expanded tokens.
2. Run the appropriate traversal from each starting node.
3. Read the subgraph - node labels, edge relations, confidence tags, source locations.
4. Answer using **only** what the graph contains. Quote `source_location` when citing a specific fact.
5. If the graph lacks enough information, say so - do not hallucinate edges.

```bash
GRAPHIFY_PYTHON=$(GRAPHIFY_INPUT_PATH="${GRAPHIFY_INPUT_PATH-}" /bin/sh -p -c 'GRAPHIFY_PYTHON=""; GRAPHIFY_PYTHON_EXPLICIT=0; _GRAPHIFY_VERSION_CHECK='"'"'import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)'"'"'; _GRAPHIFY_IDENTITY_CHECK='"'"'exec("import importlib.metadata\nimport importlib.util\nimport json\nimport os\nimport sys\nimport urllib.parse\nimport urllib.request\n\ndef contained(path, root):\n    try:\n        normalized_root = os.path.normcase(root)\n        return os.path.commonpath((os.path.normcase(path), normalized_root)) == normalized_root\n    except (OSError, ValueError):\n        return False\n\ntry:\n    distribution = importlib.metadata.distribution(\"graphifyy\")\n    if distribution.metadata.get(\"Name\") != \"graphifyy\":\n        raise ValueError\n    spec = importlib.util.find_spec(\"graphify\")\n    if spec is None or not spec.origin:\n        raise ValueError\n    origin = os.path.abspath(spec.origin)\n    real_origin = os.path.realpath(origin)\n    direct_url_text = distribution.read_text(\"direct_url.json\")\n    editable = False\n    if direct_url_text is not None:\n        direct_url = json.loads(direct_url_text)\n        parsed = urllib.parse.urlparse(direct_url[\"url\"])\n        if direct_url.get(\"dir_info\", {}).get(\"editable\") is True:\n            editable = True\n            if parsed.scheme != \"file\" or parsed.netloc not in (\"\", \"localhost\"):\n                raise ValueError\n            package_root = os.path.abspath(\n                urllib.request.url2pathname(parsed.path)\n            )\n    if editable:\n        real_package_root = os.path.realpath(package_root)\n        if not contained(origin, package_root) or not contained(real_origin, real_package_root):\n            raise ValueError\n    else:\n        owned = [\n            entry\n            for entry in (distribution.files or ())\n            if str(entry) == \"graphify/__init__.py\"\n        ]\n        if len(owned) != 1:\n            raise ValueError\n        recorded_origin = os.path.abspath(distribution.locate_file(owned[0]))\n        if os.path.normcase(recorded_origin) != os.path.normcase(origin):\n            raise ValueError\n    arguments = sys.argv[1:]\n    if arguments[0] == \"ambient\":\n        for root_arg in arguments[1:]:\n            if not root_arg:\n                continue\n            root = os.path.abspath(root_arg)\n            real_root = os.path.realpath(root)\n            if contained(origin, root) or contained(real_origin, real_root):\n                raise ValueError\nexcept (Exception, SystemExit):\n    raise SystemExit(1)\n")'"'"'; if [ -x /usr/bin/cygpath.exe ]; then _GRAPHIFY_CYGPATH=/usr/bin/cygpath.exe; elif [ -x /usr/bin/cygpath ]; then _GRAPHIFY_CYGPATH=/usr/bin/cygpath; elif [ -x /bin/cygpath.exe ]; then _GRAPHIFY_CYGPATH=/bin/cygpath.exe; elif [ -x /bin/cygpath ]; then _GRAPHIFY_CYGPATH=/bin/cygpath; else exit 1; fi; _graphify_to_posix() { _gfy_native=$1; case "$_gfy_native" in [a-zA-Z]:|[a-zA-Z]:[!\\/]*|\\|\\[!\\]*) return 1 ;; esac; "$_GRAPHIFY_CYGPATH" -u "$_gfy_native"; }; _graphify_to_native() { [ -n "$1" ] || return 0; "$_GRAPHIFY_CYGPATH" -w "$1"; }; _GRAPHIFY_WORKSPACE=$(command pwd -P) || exit 1; _GRAPHIFY_WORKSPACE_NATIVE=$(_graphify_to_native "$_GRAPHIFY_WORKSPACE") || exit 1; _graphify_canonical_root() { _gfy_root=$1; [ -n "$_gfy_root" ] || return 1; case "$_gfy_root" in [a-zA-Z]:|[a-zA-Z]:[!\\/]*|\\|\\[!\\]*) return 1 ;; /*) ;; *) _gfy_root=$(_graphify_to_posix "$_gfy_root") || return 1 ;; esac; case "$_gfy_root" in /*) ;; *) _gfy_root=$_GRAPHIFY_WORKSPACE/$_gfy_root ;; esac; if [ -d "$_gfy_root" ]; then CDPATH= cd -P -- "$_gfy_root" 2>/dev/null && command pwd -P; else printf '"'"'%s\n'"'"' "$_gfy_root"; fi; }; _graphify_absolute_command() { _gfy_command=$1; case "$_gfy_command" in /*) case "$_gfy_command" in */./*|*/../*|*/.|*/..) ;; *) GRAPHIFY_COMMAND_PATH=$_gfy_command; return 0 ;; esac; _gfy_command_dir=${_gfy_command%/*}; _gfy_command_base=${_gfy_command##*/} ;; */*) _gfy_command_dir=$_GRAPHIFY_WORKSPACE/${_gfy_command%/*}; _gfy_command_base=${_gfy_command##*/} ;; *) _gfy_command_dir=$_GRAPHIFY_WORKSPACE; _gfy_command_base=$_gfy_command ;; esac; case "$_gfy_command_base" in ""|.|..) return 1 ;; esac; _gfy_command_dir=$(CDPATH= cd -L -- "$_gfy_command_dir" 2>/dev/null && command pwd -L) || return 1; GRAPHIFY_COMMAND_PATH=$_gfy_command_dir/$_gfy_command_base; }; _GRAPHIFY_DENY_POLICY_INVALID=0; _gfy_input_raw=${GRAPHIFY_INPUT_PATH-}; _gfy_output_raw=${GRAPHIFY_OUTPUT_ROOT-${GRAPHIFY_OUT-graphify-out}}; _GRAPHIFY_INPUT_ROOT=$(_graphify_canonical_root "$_gfy_input_raw") || { _GRAPHIFY_INPUT_ROOT=""; [ -z "$_gfy_input_raw" ] || _GRAPHIFY_DENY_POLICY_INVALID=1; }; _GRAPHIFY_OUTPUT_ROOT=$(_graphify_canonical_root "$_gfy_output_raw") || { _GRAPHIFY_OUTPUT_ROOT=""; [ -z "$_gfy_output_raw" ] || _GRAPHIFY_DENY_POLICY_INVALID=1; }; [ "$_GRAPHIFY_DENY_POLICY_INVALID" = 0 ] || exit 1; _GRAPHIFY_INPUT_ROOT_NATIVE=$(_graphify_to_native "$_GRAPHIFY_INPUT_ROOT") || exit 1; _GRAPHIFY_OUTPUT_ROOT_NATIVE=$(_graphify_to_native "$_GRAPHIFY_OUTPUT_ROOT") || exit 1; _graphify_path_denied() { _gfy_policy_path=$1; [ "$_GRAPHIFY_WORKSPACE" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_WORKSPACE"|"$_GRAPHIFY_WORKSPACE"/*) return 0 ;; esac; [ -z "$_GRAPHIFY_INPUT_ROOT" ] || { [ "$_GRAPHIFY_INPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_INPUT_ROOT"|"$_GRAPHIFY_INPUT_ROOT"/*) return 0 ;; esac; }; [ -z "$_GRAPHIFY_OUTPUT_ROOT" ] || { [ "$_GRAPHIFY_OUTPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_OUTPUT_ROOT"|"$_GRAPHIFY_OUTPUT_ROOT"/*) return 0 ;; esac; }; return 1; }; _graphify_readlink() { if [ -x /usr/bin/readlink ]; then /usr/bin/readlink "$1"; elif [ -x /bin/readlink ]; then /bin/readlink "$1"; elif [ -x /run/current-system/sw/bin/readlink ]; then /run/current-system/sw/bin/readlink "$1"; else command -p readlink "$1"; fi; }; _graphify_resolve_ambient() { _gfy_lexical=$1; _gfy_lexical=$(_graphify_to_posix "$_gfy_lexical") || return 1; case "$_gfy_lexical" in /*) ;; *) return 1 ;; esac; _graphify_path_denied "$_gfy_lexical" && return 1; _gfy_path=$_gfy_lexical; _gfy_links=0; while [ -L "$_gfy_path" ]; do _gfy_links=$((_gfy_links + 1)); [ "$_gfy_links" -le 40 ] || return 1; _gfy_link=$(_graphify_readlink "$_gfy_path") || return 1; case "$_gfy_link" in /*) _gfy_path=$_gfy_link ;; *) _gfy_dir=${_gfy_path%/*}; _gfy_path=$_gfy_dir/$_gfy_link ;; esac; done; _gfy_dir=${_gfy_path%/*}; _gfy_base=${_gfy_path##*/}; _gfy_dir=$(CDPATH= cd -P -- "$_gfy_dir" 2>/dev/null && command pwd -P) || return 1; _gfy_path=$_gfy_dir/$_gfy_base; _graphify_path_denied "$_gfy_path" && return 1; [ -x "$_gfy_lexical" ] || return 1; GRAPHIFY_RESOLVED=$_gfy_lexical; }; _graphify_command() { _gfy_found=$(command -v "$1" 2>/dev/null) || return 1; _gfy_found=$(_graphify_to_posix "$_gfy_found") || return 1; _graphify_absolute_command "$_gfy_found" || return 1; _graphify_resolve_ambient "$GRAPHIFY_COMMAND_PATH"; }; _graphify_supported() { [ -n "$1" ] && "$1" -E -P -B -c "$_GRAPHIFY_VERSION_CHECK" >/dev/null 2>&1; }; _graphify_usable() { _graphify_supported "$1" && "$1" -E -P -B -c "$_GRAPHIFY_IDENTITY_CHECK" trusted >/dev/null 2>&1; }; _graphify_ambient_usable() { _graphify_supported "$1" && "$1" -E -P -B -c "$_GRAPHIFY_IDENTITY_CHECK" ambient "$_GRAPHIFY_WORKSPACE_NATIVE" "$_GRAPHIFY_INPUT_ROOT_NATIVE" "$_GRAPHIFY_OUTPUT_ROOT_NATIVE" >/dev/null 2>&1; }; case "${VIRTUAL_ENV-}" in "") ;; *) _gfy_venv=$(_graphify_to_posix "$VIRTUAL_ENV") || exit 1; _gfy_venv_python=$_gfy_venv/Scripts/python.exe; _graphify_usable "$_gfy_venv_python" && { GRAPHIFY_PYTHON=$_gfy_venv_python; GRAPHIFY_PYTHON_EXPLICIT=1; } ;; esac; if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command uv; then _gfy_uv=$GRAPHIFY_RESOLVED; _gfy_uv_dir=$("$_gfy_uv" tool dir 2>/dev/null); _gfy_uv_dir=$(_graphify_to_posix "$_gfy_uv_dir") || _gfy_uv_dir=""; _gfy_candidate=${_gfy_uv_dir:+$_gfy_uv_dir/graphifyy/Scripts/python.exe}; if _graphify_resolve_ambient "$_gfy_candidate"; then _graphify_ambient_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; fi; fi; if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command pipx; then _gfy_pipx=$GRAPHIFY_RESOLVED; _gfy_pipx_home=$("$_gfy_pipx" environment --value PIPX_LOCAL_VENVS 2>/dev/null); _gfy_pipx_home=$(_graphify_to_posix "$_gfy_pipx_home") || _gfy_pipx_home=""; _gfy_candidate=${_gfy_pipx_home:+$_gfy_pipx_home/graphifyy/Scripts/python.exe}; if _graphify_resolve_ambient "$_gfy_candidate"; then _graphify_ambient_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; fi; fi; if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command graphify; then _gfy_graphify=$(_graphify_to_posix "$GRAPHIFY_RESOLVED") || _gfy_graphify=""; _gfy_bindir=${_gfy_graphify%/*}; for _gfy_candidate in "$_gfy_bindir/python.exe" "$_gfy_bindir/../python.exe"; do if _graphify_resolve_ambient "$_gfy_candidate" && _graphify_ambient_usable "$GRAPHIFY_RESOLVED"; then GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; break; fi; done; fi; if [ -z "$GRAPHIFY_PYTHON" ]; then for _gfy_name in python3.14 python3 python; do if _graphify_command "$_gfy_name"; then _graphify_ambient_usable "$GRAPHIFY_RESOLVED" && { GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; break; }; fi; done; fi; if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command py; then _gfy_py=$GRAPHIFY_RESOLVED; _gfy_candidate=$("$_gfy_py" -3.14 -E -P -B -c '"'"'import sys; print(sys.executable)'"'"' 2>/dev/null); _gfy_candidate=$(_graphify_to_posix "$_gfy_candidate") || _gfy_candidate=""; if _graphify_resolve_ambient "$_gfy_candidate"; then _graphify_ambient_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; fi; fi; if [ -z "$GRAPHIFY_PYTHON" ] && [ "${GRAPHIFY_DISCOVERY_OPTIONAL-0}" != 1 ]; then echo "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." >&2; exit 1; fi; [ -n "$GRAPHIFY_PYTHON" ] || exit 1; printf "%sx" "$GRAPHIFY_PYTHON"'); GRAPHIFY_PYTHON=${GRAPHIFY_PYTHON%x}; GRAPHIFY_PYTHON=${GRAPHIFY_PYTHON:?Graphify interpreter discovery failed}
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

```powershell
$env:GRAPHIFY_INPUT_PATH = "INPUT_PATH"
$GraphifyPython = $null
$GraphifyPythonExplicit = $false
$GraphifyWorkspace = [IO.Path]::GetFullPath((Get-Location).Path)
if ($GraphifyWorkspace -ne [IO.Path]::GetPathRoot($GraphifyWorkspace)) { $GraphifyWorkspace = $GraphifyWorkspace.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) }
$GraphifyDenyRoots = [Collections.Generic.List[string]]::new()
$GraphifyDenyPolicyInvalid = $false
function Test-GraphifyFullyQualifiedPath {
    param([string]$Path)
    if (-not $Path -or -not [IO.Path]::IsPathRooted($Path)) { return $false }
    $root = [IO.Path]::GetPathRoot($Path)
    if (-not $root -or $root -match '^[A-Za-z]:$') { return $false }
    if ($Path -match '^[\\/](?![\\/])') { return $false }
    return $true
}
function Resolve-GraphifyPolicyPath {
    param(
        [string]$Path,
        [Collections.Generic.HashSet[string]]$Visited = $null,
        [int]$Hops = 0
    )
    if (-not (Test-GraphifyFullyQualifiedPath $Path)) { return $null }
    try {
        $full = [IO.Path]::GetFullPath($Path)
        $root = [IO.Path]::GetPathRoot($full)
        if (-not $root) { return $null }
        if ($null -eq $Visited) {
            $Visited = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        }
        $current = $root
        $parts = @($full.Substring($root.Length) -split '[\\/]' | Where-Object { $_ })
        for ($index = 0; $index -lt $parts.Count; $index++) {
            $part = $parts[$index]
            $current = [IO.Path]::GetFullPath((Join-Path $current $part))
            $info = Get-Item -LiteralPath $current -Force -ErrorAction Stop
            if (($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                $sourcePath = [IO.Path]::GetFullPath($current)
                if (-not $Visited.Add($sourcePath)) { return $null }
                $nextHops = $Hops + 1
                if ($nextHops -gt 63) { return $null }
                if ($info.PSObject.Methods.Name -notcontains "ResolveLinkTarget") {
                    if ($info.PSObject.Properties.Name -notcontains "Target") { return $null }
                    $targets = @(
                        @($info.Target) | Where-Object {
                            $_ -is [string] -and -not [string]::IsNullOrWhiteSpace([string]$_)
                        }
                    )
                    if ($targets.Count -ne 1) { return $null }
                    $targetText = [string]$targets[0]
                } else {
                    $target = $info.ResolveLinkTarget($false)
                    if (-not $target -or -not $target.FullName) { return $null }
                    $targetText = [string]$target.FullName
                }
                if (Test-GraphifyFullyQualifiedPath $targetText) {
                    $targetPath = [IO.Path]::GetFullPath($targetText)
                } else {
                    $linkParent = Split-Path -Parent $sourcePath
                    if (-not $linkParent) { return $null }
                    $targetPath = [IO.Path]::GetFullPath((Join-Path $linkParent $targetText))
                }
                if ($index + 1 -lt $parts.Count) {
                    $remainingSuffix = [string]::Join(
                        [IO.Path]::DirectorySeparatorChar,
                        [string[]]$parts[($index + 1)..($parts.Count - 1)]
                    )
                    $targetPath = Join-Path $targetPath $remainingSuffix
                }
                return Resolve-GraphifyPolicyPath $targetPath $Visited $nextHops
            }
        }
        return [IO.Path]::GetFullPath($current)
    } catch { return $null }
}
function Add-GraphifyDenyRoot {
    param([string]$Path, [bool]$Required = $false)
    if (-not $Path) { return }
    try {
        $full = if (Test-GraphifyFullyQualifiedPath $path) { [IO.Path]::GetFullPath($Path) } else { [IO.Path]::GetFullPath((Join-Path $GraphifyWorkspace $Path)) }
        if (-not (Test-Path -LiteralPath $full -PathType Container)) { if ($Required) { $script:GraphifyDenyPolicyInvalid = $true }; return }
        $resolved = Resolve-GraphifyPolicyPath $full
        if (-not $resolved) { $script:GraphifyDenyPolicyInvalid = $true; return }
        if ($resolved -ne [IO.Path]::GetPathRoot($resolved)) { $resolved = $resolved.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) }
        $lexical = [IO.Path]::GetFullPath($full)
        if ($lexical -ne [IO.Path]::GetPathRoot($lexical)) { $lexical = $lexical.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) }
        if (-not $GraphifyDenyRoots.Contains($lexical)) { $GraphifyDenyRoots.Add($lexical) }
        if (-not $GraphifyDenyRoots.Contains($resolved)) { $GraphifyDenyRoots.Add($resolved) }
    } catch { $script:GraphifyDenyPolicyInvalid = $true; return }
}
Add-GraphifyDenyRoot $GraphifyWorkspace $true
if ($env:GRAPHIFY_INPUT_PATH -and $env:GRAPHIFY_INPUT_PATH -ne "INPUT_PATH") { Add-GraphifyDenyRoot $env:GRAPHIFY_INPUT_PATH $true }
$GraphifySelectedOutput = if ($env:GRAPHIFY_OUTPUT_ROOT) { $env:GRAPHIFY_OUTPUT_ROOT } elseif ($env:GRAPHIFY_OUT) { $env:GRAPHIFY_OUT } else { "graphify-out" }
Add-GraphifyDenyRoot $GraphifySelectedOutput ([bool]($env:GRAPHIFY_OUTPUT_ROOT -or $env:GRAPHIFY_OUT))
function Test-GraphifyWorkspacePath {
    param([string]$Path)
    if ($GraphifyDenyPolicyInvalid) { return $true }
    if (-not (Test-GraphifyFullyQualifiedPath $Path)) { return $true }
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    try {
        $lexical = [IO.Path]::GetFullPath($Path)
        $resolved = Resolve-GraphifyPolicyPath $lexical
        if (-not $resolved) { return $true }
    }
    catch { return $true }
    foreach ($root in $GraphifyDenyRoots) {
        $prefix = if ($root -eq [IO.Path]::GetPathRoot($root)) { $root } else { $root + [IO.Path]::DirectorySeparatorChar }
        if ($lexical -eq $root -or $lexical.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { return $true }
        if ($resolved -eq $root -or $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}
function Resolve-GraphifyAmbientCommand {
    param([string]$Name)
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $command) { return $null }
    $path = [IO.Path]::GetFullPath($command.Source)
    if (Test-GraphifyWorkspacePath $path) { return $null }
    return $path
}
$GraphifyIdentityCheck = @'
exec('import importlib.metadata\nimport importlib.util\nimport json\nimport os\nimport sys\nimport urllib.parse\nimport urllib.request\n\ndef contained(path, root):\n    try:\n        normalized_root = os.path.normcase(root)\n        return os.path.commonpath((os.path.normcase(path), normalized_root)) == normalized_root\n    except (OSError, ValueError):\n        return False\n\ntry:\n    distribution = importlib.metadata.distribution("graphifyy")\n    if distribution.metadata.get("Name") != "graphifyy":\n        raise ValueError\n    spec = importlib.util.find_spec("graphify")\n    if spec is None or not spec.origin:\n        raise ValueError\n    origin = os.path.abspath(spec.origin)\n    real_origin = os.path.realpath(origin)\n    direct_url_text = distribution.read_text("direct_url.json")\n    editable = False\n    if direct_url_text is not None:\n        direct_url = json.loads(direct_url_text)\n        parsed = urllib.parse.urlparse(direct_url["url"])\n        if direct_url.get("dir_info", {}).get("editable") is True:\n            editable = True\n            if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):\n                raise ValueError\n            package_root = os.path.abspath(\n                urllib.request.url2pathname(parsed.path)\n            )\n    if editable:\n        real_package_root = os.path.realpath(package_root)\n        if not contained(origin, package_root) or not contained(real_origin, real_package_root):\n            raise ValueError\n    else:\n        owned = [\n            entry\n            for entry in (distribution.files or ())\n            if str(entry) == "graphify/__init__.py"\n        ]\n        if len(owned) != 1:\n            raise ValueError\n        recorded_origin = os.path.abspath(distribution.locate_file(owned[0]))\n        if os.path.normcase(recorded_origin) != os.path.normcase(origin):\n            raise ValueError\n    arguments = sys.argv[1:]\n    if arguments[0] == "ambient":\n        for root_arg in arguments[1:]:\n            if not root_arg:\n                continue\n            root = os.path.abspath(root_arg)\n            real_root = os.path.realpath(root)\n            if contained(origin, root) or contained(real_origin, real_root):\n                raise ValueError\nexcept (Exception, SystemExit):\n    raise SystemExit(1)\n')
'@
function Test-GraphifyPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    if (-not (Test-GraphifySupportedPython $Candidate)) { return $false }
    & $Candidate -E -P -B -c $GraphifyIdentityCheck trusted 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    return $invocationSucceeded -and $exitCode -eq 0
}
function Test-GraphifyAmbientPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    if (-not (Test-GraphifySupportedPython $Candidate)) { return $false }
    & $Candidate -E -P -B -c $GraphifyIdentityCheck ambient @GraphifyDenyRoots 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    return $invocationSucceeded -and $exitCode -eq 0
}
function Test-GraphifySupportedPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    & $Candidate -E -P -B -c "import sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and sys.version_info.releaselevel == 'final' and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)" 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    return $invocationSucceeded -and $exitCode -eq 0
}
function Invoke-GraphifyNativeText {
    param([string]$Candidate, [string[]]$Arguments)
    if (-not $Candidate) { return $null }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $null }
    $output = & $Candidate @Arguments 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    if (-not ($invocationSucceeded -and $exitCode -eq 0)) { return $null }
    $lines = @(
        @($output) | Where-Object {
            $_ -is [string] -and -not [string]::IsNullOrWhiteSpace([string]$_)
        }
    )
    if ($lines.Count -ne 1) { return $null }
    return ([string]$lines[0]).Trim()
}
if (Test-GraphifyFullyQualifiedPath "$env:VIRTUAL_ENV") {
    $activeVenv = Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $activeVenv)) { $activeVenv = Join-Path $env:VIRTUAL_ENV "bin/python" }
    if (Test-GraphifyPython $activeVenv) { $GraphifyPython = $activeVenv; $GraphifyPythonExplicit = $true }
}
if (-not $GraphifyPython) {
    $uv = Resolve-GraphifyAmbientCommand uv
    if ($uv) {
        $uvDir = Invoke-GraphifyNativeText $uv @("tool", "dir")
        if ($uvDir) {
            $candidate = Join-Path $uvDir "graphifyy\Scripts\python.exe"
            if (-not (Test-Path -LiteralPath $candidate)) { $candidate = Join-Path $uvDir "graphifyy/bin/python" }
            if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyAmbientPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate) }
        }
    }
}
if (-not $GraphifyPython) {
    $pipx = Resolve-GraphifyAmbientCommand pipx
    if ($pipx) {
        $venvs = Invoke-GraphifyNativeText $pipx @("environment", "--value", "PIPX_LOCAL_VENVS")
        if ($venvs) {
            $candidate = Join-Path $venvs "graphifyy\Scripts\python.exe"
            if (-not (Test-Path -LiteralPath $candidate)) { $candidate = Join-Path $venvs "graphifyy/bin/python" }
            if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyAmbientPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate) }
        }
    }
}
if (-not $GraphifyPython) {
    $graphify = Resolve-GraphifyAmbientCommand graphify
    if ($graphify) {
        $bindir = Split-Path -Parent $graphify
        foreach ($candidate in @((Join-Path $bindir "python.exe"), (Join-Path $bindir "../python.exe"))) {
            if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyAmbientPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate); break }
        }
    }
}
if (-not $GraphifyPython) {
    foreach ($name in @("python3.14", "python3", "py", "python")) {
        $candidate = Resolve-GraphifyAmbientCommand $name
        if (-not $candidate) { continue }
        if ($name -eq "py") {
            $resolved = Invoke-GraphifyNativeText $candidate @("-3.14", "-E", "-P", "-B", "-c", "import sys; print(sys.executable)")
            if ($resolved -and -not (Test-GraphifyWorkspacePath $resolved) -and (Test-GraphifyAmbientPython $resolved)) { $GraphifyPython = [IO.Path]::GetFullPath($resolved); break }
        } elseif (Test-GraphifyAmbientPython $candidate) { $GraphifyPython = $candidate; break }
    }
}
if (-not $GraphifyPython -and -not $GraphifyDiscoveryOptional) { throw "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." }
& $GraphifyPython -E -P -B -m graphify save-result --question "ORIGINAL_QUESTION" --answer "ANSWER" --type query --nodes NODE1 NODE2
```

Replace `ORIGINAL_QUESTION` with the user's verbatim question, `ANSWER` with your full answer text (containing the expanded-token trace), `NODE1 NODE2` with the list of node labels you cited. This closes the feedback loop: the next `--update` will extract this Q&A as a node in the graph.

**Work memory (self-improving loop).** Add an `--outcome` so future sessions learn from this one — append `--outcome useful|dead_end|corrected` to the `save-result` command (and `--correction "the right answer"` when correcting):

- `useful` — the cited nodes answered the question well (they become *preferred sources*).
- `dead_end` — the question/path led nowhere; don't re-derive it next time.
- `corrected` — the saved answer was wrong; `--correction` records what was right.

At the **start** of graph work, refresh and read the lessons with `& $GraphifyPython -E -P -B -m graphify reflect --if-stale` (cheap, deterministic, no LLM; `--if-stale` makes it a no-op when `LESSONS.md` is already newer than every input, e.g. when the git hook just refreshed it), then read `graphify-out/reflections/LESSONS.md`. It lists **preferred sources** (start there), **known dead ends** (skip them), and prior **corrections**. Running `reflect` yourself keeps the lessons current even without the git hook installed; if the post-commit hook *is* installed, `--if-stale` means your session-start run costs almost nothing.

---

## For /graphify path

Find the shortest path between two named concepts in the graph. Prefer the CLI when installed:

```powershell
$env:GRAPHIFY_INPUT_PATH = "INPUT_PATH"
$GraphifyPython = $null
$GraphifyPythonExplicit = $false
$GraphifyWorkspace = [IO.Path]::GetFullPath((Get-Location).Path)
if ($GraphifyWorkspace -ne [IO.Path]::GetPathRoot($GraphifyWorkspace)) { $GraphifyWorkspace = $GraphifyWorkspace.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) }
$GraphifyDenyRoots = [Collections.Generic.List[string]]::new()
$GraphifyDenyPolicyInvalid = $false
function Test-GraphifyFullyQualifiedPath {
    param([string]$Path)
    if (-not $Path -or -not [IO.Path]::IsPathRooted($Path)) { return $false }
    $root = [IO.Path]::GetPathRoot($Path)
    if (-not $root -or $root -match '^[A-Za-z]:$') { return $false }
    if ($Path -match '^[\\/](?![\\/])') { return $false }
    return $true
}
function Resolve-GraphifyPolicyPath {
    param(
        [string]$Path,
        [Collections.Generic.HashSet[string]]$Visited = $null,
        [int]$Hops = 0
    )
    if (-not (Test-GraphifyFullyQualifiedPath $Path)) { return $null }
    try {
        $full = [IO.Path]::GetFullPath($Path)
        $root = [IO.Path]::GetPathRoot($full)
        if (-not $root) { return $null }
        if ($null -eq $Visited) {
            $Visited = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        }
        $current = $root
        $parts = @($full.Substring($root.Length) -split '[\\/]' | Where-Object { $_ })
        for ($index = 0; $index -lt $parts.Count; $index++) {
            $part = $parts[$index]
            $current = [IO.Path]::GetFullPath((Join-Path $current $part))
            $info = Get-Item -LiteralPath $current -Force -ErrorAction Stop
            if (($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                $sourcePath = [IO.Path]::GetFullPath($current)
                if (-not $Visited.Add($sourcePath)) { return $null }
                $nextHops = $Hops + 1
                if ($nextHops -gt 63) { return $null }
                if ($info.PSObject.Methods.Name -notcontains "ResolveLinkTarget") {
                    if ($info.PSObject.Properties.Name -notcontains "Target") { return $null }
                    $targets = @(
                        @($info.Target) | Where-Object {
                            $_ -is [string] -and -not [string]::IsNullOrWhiteSpace([string]$_)
                        }
                    )
                    if ($targets.Count -ne 1) { return $null }
                    $targetText = [string]$targets[0]
                } else {
                    $target = $info.ResolveLinkTarget($false)
                    if (-not $target -or -not $target.FullName) { return $null }
                    $targetText = [string]$target.FullName
                }
                if (Test-GraphifyFullyQualifiedPath $targetText) {
                    $targetPath = [IO.Path]::GetFullPath($targetText)
                } else {
                    $linkParent = Split-Path -Parent $sourcePath
                    if (-not $linkParent) { return $null }
                    $targetPath = [IO.Path]::GetFullPath((Join-Path $linkParent $targetText))
                }
                if ($index + 1 -lt $parts.Count) {
                    $remainingSuffix = [string]::Join(
                        [IO.Path]::DirectorySeparatorChar,
                        [string[]]$parts[($index + 1)..($parts.Count - 1)]
                    )
                    $targetPath = Join-Path $targetPath $remainingSuffix
                }
                return Resolve-GraphifyPolicyPath $targetPath $Visited $nextHops
            }
        }
        return [IO.Path]::GetFullPath($current)
    } catch { return $null }
}
function Add-GraphifyDenyRoot {
    param([string]$Path, [bool]$Required = $false)
    if (-not $Path) { return }
    try {
        $full = if (Test-GraphifyFullyQualifiedPath $path) { [IO.Path]::GetFullPath($Path) } else { [IO.Path]::GetFullPath((Join-Path $GraphifyWorkspace $Path)) }
        if (-not (Test-Path -LiteralPath $full -PathType Container)) { if ($Required) { $script:GraphifyDenyPolicyInvalid = $true }; return }
        $resolved = Resolve-GraphifyPolicyPath $full
        if (-not $resolved) { $script:GraphifyDenyPolicyInvalid = $true; return }
        if ($resolved -ne [IO.Path]::GetPathRoot($resolved)) { $resolved = $resolved.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) }
        $lexical = [IO.Path]::GetFullPath($full)
        if ($lexical -ne [IO.Path]::GetPathRoot($lexical)) { $lexical = $lexical.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) }
        if (-not $GraphifyDenyRoots.Contains($lexical)) { $GraphifyDenyRoots.Add($lexical) }
        if (-not $GraphifyDenyRoots.Contains($resolved)) { $GraphifyDenyRoots.Add($resolved) }
    } catch { $script:GraphifyDenyPolicyInvalid = $true; return }
}
Add-GraphifyDenyRoot $GraphifyWorkspace $true
if ($env:GRAPHIFY_INPUT_PATH -and $env:GRAPHIFY_INPUT_PATH -ne "INPUT_PATH") { Add-GraphifyDenyRoot $env:GRAPHIFY_INPUT_PATH $true }
$GraphifySelectedOutput = if ($env:GRAPHIFY_OUTPUT_ROOT) { $env:GRAPHIFY_OUTPUT_ROOT } elseif ($env:GRAPHIFY_OUT) { $env:GRAPHIFY_OUT } else { "graphify-out" }
Add-GraphifyDenyRoot $GraphifySelectedOutput ([bool]($env:GRAPHIFY_OUTPUT_ROOT -or $env:GRAPHIFY_OUT))
function Test-GraphifyWorkspacePath {
    param([string]$Path)
    if ($GraphifyDenyPolicyInvalid) { return $true }
    if (-not (Test-GraphifyFullyQualifiedPath $Path)) { return $true }
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    try {
        $lexical = [IO.Path]::GetFullPath($Path)
        $resolved = Resolve-GraphifyPolicyPath $lexical
        if (-not $resolved) { return $true }
    }
    catch { return $true }
    foreach ($root in $GraphifyDenyRoots) {
        $prefix = if ($root -eq [IO.Path]::GetPathRoot($root)) { $root } else { $root + [IO.Path]::DirectorySeparatorChar }
        if ($lexical -eq $root -or $lexical.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { return $true }
        if ($resolved -eq $root -or $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}
function Resolve-GraphifyAmbientCommand {
    param([string]$Name)
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $command) { return $null }
    $path = [IO.Path]::GetFullPath($command.Source)
    if (Test-GraphifyWorkspacePath $path) { return $null }
    return $path
}
$GraphifyIdentityCheck = @'
exec('import importlib.metadata\nimport importlib.util\nimport json\nimport os\nimport sys\nimport urllib.parse\nimport urllib.request\n\ndef contained(path, root):\n    try:\n        normalized_root = os.path.normcase(root)\n        return os.path.commonpath((os.path.normcase(path), normalized_root)) == normalized_root\n    except (OSError, ValueError):\n        return False\n\ntry:\n    distribution = importlib.metadata.distribution("graphifyy")\n    if distribution.metadata.get("Name") != "graphifyy":\n        raise ValueError\n    spec = importlib.util.find_spec("graphify")\n    if spec is None or not spec.origin:\n        raise ValueError\n    origin = os.path.abspath(spec.origin)\n    real_origin = os.path.realpath(origin)\n    direct_url_text = distribution.read_text("direct_url.json")\n    editable = False\n    if direct_url_text is not None:\n        direct_url = json.loads(direct_url_text)\n        parsed = urllib.parse.urlparse(direct_url["url"])\n        if direct_url.get("dir_info", {}).get("editable") is True:\n            editable = True\n            if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):\n                raise ValueError\n            package_root = os.path.abspath(\n                urllib.request.url2pathname(parsed.path)\n            )\n    if editable:\n        real_package_root = os.path.realpath(package_root)\n        if not contained(origin, package_root) or not contained(real_origin, real_package_root):\n            raise ValueError\n    else:\n        owned = [\n            entry\n            for entry in (distribution.files or ())\n            if str(entry) == "graphify/__init__.py"\n        ]\n        if len(owned) != 1:\n            raise ValueError\n        recorded_origin = os.path.abspath(distribution.locate_file(owned[0]))\n        if os.path.normcase(recorded_origin) != os.path.normcase(origin):\n            raise ValueError\n    arguments = sys.argv[1:]\n    if arguments[0] == "ambient":\n        for root_arg in arguments[1:]:\n            if not root_arg:\n                continue\n            root = os.path.abspath(root_arg)\n            real_root = os.path.realpath(root)\n            if contained(origin, root) or contained(real_origin, real_root):\n                raise ValueError\nexcept (Exception, SystemExit):\n    raise SystemExit(1)\n')
'@
function Test-GraphifyPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    if (-not (Test-GraphifySupportedPython $Candidate)) { return $false }
    & $Candidate -E -P -B -c $GraphifyIdentityCheck trusted 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    return $invocationSucceeded -and $exitCode -eq 0
}
function Test-GraphifyAmbientPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    if (-not (Test-GraphifySupportedPython $Candidate)) { return $false }
    & $Candidate -E -P -B -c $GraphifyIdentityCheck ambient @GraphifyDenyRoots 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    return $invocationSucceeded -and $exitCode -eq 0
}
function Test-GraphifySupportedPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    & $Candidate -E -P -B -c "import sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and sys.version_info.releaselevel == 'final' and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)" 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    return $invocationSucceeded -and $exitCode -eq 0
}
function Invoke-GraphifyNativeText {
    param([string]$Candidate, [string[]]$Arguments)
    if (-not $Candidate) { return $null }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $null }
    $output = & $Candidate @Arguments 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    if (-not ($invocationSucceeded -and $exitCode -eq 0)) { return $null }
    $lines = @(
        @($output) | Where-Object {
            $_ -is [string] -and -not [string]::IsNullOrWhiteSpace([string]$_)
        }
    )
    if ($lines.Count -ne 1) { return $null }
    return ([string]$lines[0]).Trim()
}
if (Test-GraphifyFullyQualifiedPath "$env:VIRTUAL_ENV") {
    $activeVenv = Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $activeVenv)) { $activeVenv = Join-Path $env:VIRTUAL_ENV "bin/python" }
    if (Test-GraphifyPython $activeVenv) { $GraphifyPython = $activeVenv; $GraphifyPythonExplicit = $true }
}
if (-not $GraphifyPython) {
    $uv = Resolve-GraphifyAmbientCommand uv
    if ($uv) {
        $uvDir = Invoke-GraphifyNativeText $uv @("tool", "dir")
        if ($uvDir) {
            $candidate = Join-Path $uvDir "graphifyy\Scripts\python.exe"
            if (-not (Test-Path -LiteralPath $candidate)) { $candidate = Join-Path $uvDir "graphifyy/bin/python" }
            if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyAmbientPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate) }
        }
    }
}
if (-not $GraphifyPython) {
    $pipx = Resolve-GraphifyAmbientCommand pipx
    if ($pipx) {
        $venvs = Invoke-GraphifyNativeText $pipx @("environment", "--value", "PIPX_LOCAL_VENVS")
        if ($venvs) {
            $candidate = Join-Path $venvs "graphifyy\Scripts\python.exe"
            if (-not (Test-Path -LiteralPath $candidate)) { $candidate = Join-Path $venvs "graphifyy/bin/python" }
            if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyAmbientPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate) }
        }
    }
}
if (-not $GraphifyPython) {
    $graphify = Resolve-GraphifyAmbientCommand graphify
    if ($graphify) {
        $bindir = Split-Path -Parent $graphify
        foreach ($candidate in @((Join-Path $bindir "python.exe"), (Join-Path $bindir "../python.exe"))) {
            if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyAmbientPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate); break }
        }
    }
}
if (-not $GraphifyPython) {
    foreach ($name in @("python3.14", "python3", "py", "python")) {
        $candidate = Resolve-GraphifyAmbientCommand $name
        if (-not $candidate) { continue }
        if ($name -eq "py") {
            $resolved = Invoke-GraphifyNativeText $candidate @("-3.14", "-E", "-P", "-B", "-c", "import sys; print(sys.executable)")
            if ($resolved -and -not (Test-GraphifyWorkspacePath $resolved) -and (Test-GraphifyAmbientPython $resolved)) { $GraphifyPython = [IO.Path]::GetFullPath($resolved); break }
        } elseif (Test-GraphifyAmbientPython $candidate) { $GraphifyPython = $candidate; break }
    }
}
if (-not $GraphifyPython -and -not $GraphifyDiscoveryOptional) { throw "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." }
& $GraphifyPython -E -P -B -m graphify path "NODE_A" "NODE_B"
```

If the CLI is unavailable, run it inline:

```bash
GRAPHIFY_PYTHON=$(GRAPHIFY_INPUT_PATH="${GRAPHIFY_INPUT_PATH-}" /bin/sh -p -c 'GRAPHIFY_PYTHON=""; GRAPHIFY_PYTHON_EXPLICIT=0; _GRAPHIFY_VERSION_CHECK='"'"'import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)'"'"'; _GRAPHIFY_IDENTITY_CHECK='"'"'exec("import importlib.metadata\nimport importlib.util\nimport json\nimport os\nimport sys\nimport urllib.parse\nimport urllib.request\n\ndef contained(path, root):\n    try:\n        normalized_root = os.path.normcase(root)\n        return os.path.commonpath((os.path.normcase(path), normalized_root)) == normalized_root\n    except (OSError, ValueError):\n        return False\n\ntry:\n    distribution = importlib.metadata.distribution(\"graphifyy\")\n    if distribution.metadata.get(\"Name\") != \"graphifyy\":\n        raise ValueError\n    spec = importlib.util.find_spec(\"graphify\")\n    if spec is None or not spec.origin:\n        raise ValueError\n    origin = os.path.abspath(spec.origin)\n    real_origin = os.path.realpath(origin)\n    direct_url_text = distribution.read_text(\"direct_url.json\")\n    editable = False\n    if direct_url_text is not None:\n        direct_url = json.loads(direct_url_text)\n        parsed = urllib.parse.urlparse(direct_url[\"url\"])\n        if direct_url.get(\"dir_info\", {}).get(\"editable\") is True:\n            editable = True\n            if parsed.scheme != \"file\" or parsed.netloc not in (\"\", \"localhost\"):\n                raise ValueError\n            package_root = os.path.abspath(\n                urllib.request.url2pathname(parsed.path)\n            )\n    if editable:\n        real_package_root = os.path.realpath(package_root)\n        if not contained(origin, package_root) or not contained(real_origin, real_package_root):\n            raise ValueError\n    else:\n        owned = [\n            entry\n            for entry in (distribution.files or ())\n            if str(entry) == \"graphify/__init__.py\"\n        ]\n        if len(owned) != 1:\n            raise ValueError\n        recorded_origin = os.path.abspath(distribution.locate_file(owned[0]))\n        if os.path.normcase(recorded_origin) != os.path.normcase(origin):\n            raise ValueError\n    arguments = sys.argv[1:]\n    if arguments[0] == \"ambient\":\n        for root_arg in arguments[1:]:\n            if not root_arg:\n                continue\n            root = os.path.abspath(root_arg)\n            real_root = os.path.realpath(root)\n            if contained(origin, root) or contained(real_origin, real_root):\n                raise ValueError\nexcept (Exception, SystemExit):\n    raise SystemExit(1)\n")'"'"'; if [ -x /usr/bin/cygpath.exe ]; then _GRAPHIFY_CYGPATH=/usr/bin/cygpath.exe; elif [ -x /usr/bin/cygpath ]; then _GRAPHIFY_CYGPATH=/usr/bin/cygpath; elif [ -x /bin/cygpath.exe ]; then _GRAPHIFY_CYGPATH=/bin/cygpath.exe; elif [ -x /bin/cygpath ]; then _GRAPHIFY_CYGPATH=/bin/cygpath; else exit 1; fi; _graphify_to_posix() { _gfy_native=$1; case "$_gfy_native" in [a-zA-Z]:|[a-zA-Z]:[!\\/]*|\\|\\[!\\]*) return 1 ;; esac; "$_GRAPHIFY_CYGPATH" -u "$_gfy_native"; }; _graphify_to_native() { [ -n "$1" ] || return 0; "$_GRAPHIFY_CYGPATH" -w "$1"; }; _GRAPHIFY_WORKSPACE=$(command pwd -P) || exit 1; _GRAPHIFY_WORKSPACE_NATIVE=$(_graphify_to_native "$_GRAPHIFY_WORKSPACE") || exit 1; _graphify_canonical_root() { _gfy_root=$1; [ -n "$_gfy_root" ] || return 1; case "$_gfy_root" in [a-zA-Z]:|[a-zA-Z]:[!\\/]*|\\|\\[!\\]*) return 1 ;; /*) ;; *) _gfy_root=$(_graphify_to_posix "$_gfy_root") || return 1 ;; esac; case "$_gfy_root" in /*) ;; *) _gfy_root=$_GRAPHIFY_WORKSPACE/$_gfy_root ;; esac; if [ -d "$_gfy_root" ]; then CDPATH= cd -P -- "$_gfy_root" 2>/dev/null && command pwd -P; else printf '"'"'%s\n'"'"' "$_gfy_root"; fi; }; _graphify_absolute_command() { _gfy_command=$1; case "$_gfy_command" in /*) case "$_gfy_command" in */./*|*/../*|*/.|*/..) ;; *) GRAPHIFY_COMMAND_PATH=$_gfy_command; return 0 ;; esac; _gfy_command_dir=${_gfy_command%/*}; _gfy_command_base=${_gfy_command##*/} ;; */*) _gfy_command_dir=$_GRAPHIFY_WORKSPACE/${_gfy_command%/*}; _gfy_command_base=${_gfy_command##*/} ;; *) _gfy_command_dir=$_GRAPHIFY_WORKSPACE; _gfy_command_base=$_gfy_command ;; esac; case "$_gfy_command_base" in ""|.|..) return 1 ;; esac; _gfy_command_dir=$(CDPATH= cd -L -- "$_gfy_command_dir" 2>/dev/null && command pwd -L) || return 1; GRAPHIFY_COMMAND_PATH=$_gfy_command_dir/$_gfy_command_base; }; _GRAPHIFY_DENY_POLICY_INVALID=0; _gfy_input_raw=${GRAPHIFY_INPUT_PATH-}; _gfy_output_raw=${GRAPHIFY_OUTPUT_ROOT-${GRAPHIFY_OUT-graphify-out}}; _GRAPHIFY_INPUT_ROOT=$(_graphify_canonical_root "$_gfy_input_raw") || { _GRAPHIFY_INPUT_ROOT=""; [ -z "$_gfy_input_raw" ] || _GRAPHIFY_DENY_POLICY_INVALID=1; }; _GRAPHIFY_OUTPUT_ROOT=$(_graphify_canonical_root "$_gfy_output_raw") || { _GRAPHIFY_OUTPUT_ROOT=""; [ -z "$_gfy_output_raw" ] || _GRAPHIFY_DENY_POLICY_INVALID=1; }; [ "$_GRAPHIFY_DENY_POLICY_INVALID" = 0 ] || exit 1; _GRAPHIFY_INPUT_ROOT_NATIVE=$(_graphify_to_native "$_GRAPHIFY_INPUT_ROOT") || exit 1; _GRAPHIFY_OUTPUT_ROOT_NATIVE=$(_graphify_to_native "$_GRAPHIFY_OUTPUT_ROOT") || exit 1; _graphify_path_denied() { _gfy_policy_path=$1; [ "$_GRAPHIFY_WORKSPACE" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_WORKSPACE"|"$_GRAPHIFY_WORKSPACE"/*) return 0 ;; esac; [ -z "$_GRAPHIFY_INPUT_ROOT" ] || { [ "$_GRAPHIFY_INPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_INPUT_ROOT"|"$_GRAPHIFY_INPUT_ROOT"/*) return 0 ;; esac; }; [ -z "$_GRAPHIFY_OUTPUT_ROOT" ] || { [ "$_GRAPHIFY_OUTPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_OUTPUT_ROOT"|"$_GRAPHIFY_OUTPUT_ROOT"/*) return 0 ;; esac; }; return 1; }; _graphify_readlink() { if [ -x /usr/bin/readlink ]; then /usr/bin/readlink "$1"; elif [ -x /bin/readlink ]; then /bin/readlink "$1"; elif [ -x /run/current-system/sw/bin/readlink ]; then /run/current-system/sw/bin/readlink "$1"; else command -p readlink "$1"; fi; }; _graphify_resolve_ambient() { _gfy_lexical=$1; _gfy_lexical=$(_graphify_to_posix "$_gfy_lexical") || return 1; case "$_gfy_lexical" in /*) ;; *) return 1 ;; esac; _graphify_path_denied "$_gfy_lexical" && return 1; _gfy_path=$_gfy_lexical; _gfy_links=0; while [ -L "$_gfy_path" ]; do _gfy_links=$((_gfy_links + 1)); [ "$_gfy_links" -le 40 ] || return 1; _gfy_link=$(_graphify_readlink "$_gfy_path") || return 1; case "$_gfy_link" in /*) _gfy_path=$_gfy_link ;; *) _gfy_dir=${_gfy_path%/*}; _gfy_path=$_gfy_dir/$_gfy_link ;; esac; done; _gfy_dir=${_gfy_path%/*}; _gfy_base=${_gfy_path##*/}; _gfy_dir=$(CDPATH= cd -P -- "$_gfy_dir" 2>/dev/null && command pwd -P) || return 1; _gfy_path=$_gfy_dir/$_gfy_base; _graphify_path_denied "$_gfy_path" && return 1; [ -x "$_gfy_lexical" ] || return 1; GRAPHIFY_RESOLVED=$_gfy_lexical; }; _graphify_command() { _gfy_found=$(command -v "$1" 2>/dev/null) || return 1; _gfy_found=$(_graphify_to_posix "$_gfy_found") || return 1; _graphify_absolute_command "$_gfy_found" || return 1; _graphify_resolve_ambient "$GRAPHIFY_COMMAND_PATH"; }; _graphify_supported() { [ -n "$1" ] && "$1" -E -P -B -c "$_GRAPHIFY_VERSION_CHECK" >/dev/null 2>&1; }; _graphify_usable() { _graphify_supported "$1" && "$1" -E -P -B -c "$_GRAPHIFY_IDENTITY_CHECK" trusted >/dev/null 2>&1; }; _graphify_ambient_usable() { _graphify_supported "$1" && "$1" -E -P -B -c "$_GRAPHIFY_IDENTITY_CHECK" ambient "$_GRAPHIFY_WORKSPACE_NATIVE" "$_GRAPHIFY_INPUT_ROOT_NATIVE" "$_GRAPHIFY_OUTPUT_ROOT_NATIVE" >/dev/null 2>&1; }; case "${VIRTUAL_ENV-}" in "") ;; *) _gfy_venv=$(_graphify_to_posix "$VIRTUAL_ENV") || exit 1; _gfy_venv_python=$_gfy_venv/Scripts/python.exe; _graphify_usable "$_gfy_venv_python" && { GRAPHIFY_PYTHON=$_gfy_venv_python; GRAPHIFY_PYTHON_EXPLICIT=1; } ;; esac; if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command uv; then _gfy_uv=$GRAPHIFY_RESOLVED; _gfy_uv_dir=$("$_gfy_uv" tool dir 2>/dev/null); _gfy_uv_dir=$(_graphify_to_posix "$_gfy_uv_dir") || _gfy_uv_dir=""; _gfy_candidate=${_gfy_uv_dir:+$_gfy_uv_dir/graphifyy/Scripts/python.exe}; if _graphify_resolve_ambient "$_gfy_candidate"; then _graphify_ambient_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; fi; fi; if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command pipx; then _gfy_pipx=$GRAPHIFY_RESOLVED; _gfy_pipx_home=$("$_gfy_pipx" environment --value PIPX_LOCAL_VENVS 2>/dev/null); _gfy_pipx_home=$(_graphify_to_posix "$_gfy_pipx_home") || _gfy_pipx_home=""; _gfy_candidate=${_gfy_pipx_home:+$_gfy_pipx_home/graphifyy/Scripts/python.exe}; if _graphify_resolve_ambient "$_gfy_candidate"; then _graphify_ambient_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; fi; fi; if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command graphify; then _gfy_graphify=$(_graphify_to_posix "$GRAPHIFY_RESOLVED") || _gfy_graphify=""; _gfy_bindir=${_gfy_graphify%/*}; for _gfy_candidate in "$_gfy_bindir/python.exe" "$_gfy_bindir/../python.exe"; do if _graphify_resolve_ambient "$_gfy_candidate" && _graphify_ambient_usable "$GRAPHIFY_RESOLVED"; then GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; break; fi; done; fi; if [ -z "$GRAPHIFY_PYTHON" ]; then for _gfy_name in python3.14 python3 python; do if _graphify_command "$_gfy_name"; then _graphify_ambient_usable "$GRAPHIFY_RESOLVED" && { GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; break; }; fi; done; fi; if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command py; then _gfy_py=$GRAPHIFY_RESOLVED; _gfy_candidate=$("$_gfy_py" -3.14 -E -P -B -c '"'"'import sys; print(sys.executable)'"'"' 2>/dev/null); _gfy_candidate=$(_graphify_to_posix "$_gfy_candidate") || _gfy_candidate=""; if _graphify_resolve_ambient "$_gfy_candidate"; then _graphify_ambient_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; fi; fi; if [ -z "$GRAPHIFY_PYTHON" ] && [ "${GRAPHIFY_DISCOVERY_OPTIONAL-0}" != 1 ]; then echo "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." >&2; exit 1; fi; [ -n "$GRAPHIFY_PYTHON" ] || exit 1; printf "%sx" "$GRAPHIFY_PYTHON"'); GRAPHIFY_PYTHON=${GRAPHIFY_PYTHON%x}; GRAPHIFY_PYTHON=${GRAPHIFY_PYTHON:?Graphify interpreter discovery failed}
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

```powershell
$env:GRAPHIFY_INPUT_PATH = "INPUT_PATH"
$GraphifyPython = $null
$GraphifyPythonExplicit = $false
$GraphifyWorkspace = [IO.Path]::GetFullPath((Get-Location).Path)
if ($GraphifyWorkspace -ne [IO.Path]::GetPathRoot($GraphifyWorkspace)) { $GraphifyWorkspace = $GraphifyWorkspace.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) }
$GraphifyDenyRoots = [Collections.Generic.List[string]]::new()
$GraphifyDenyPolicyInvalid = $false
function Test-GraphifyFullyQualifiedPath {
    param([string]$Path)
    if (-not $Path -or -not [IO.Path]::IsPathRooted($Path)) { return $false }
    $root = [IO.Path]::GetPathRoot($Path)
    if (-not $root -or $root -match '^[A-Za-z]:$') { return $false }
    if ($Path -match '^[\\/](?![\\/])') { return $false }
    return $true
}
function Resolve-GraphifyPolicyPath {
    param(
        [string]$Path,
        [Collections.Generic.HashSet[string]]$Visited = $null,
        [int]$Hops = 0
    )
    if (-not (Test-GraphifyFullyQualifiedPath $Path)) { return $null }
    try {
        $full = [IO.Path]::GetFullPath($Path)
        $root = [IO.Path]::GetPathRoot($full)
        if (-not $root) { return $null }
        if ($null -eq $Visited) {
            $Visited = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        }
        $current = $root
        $parts = @($full.Substring($root.Length) -split '[\\/]' | Where-Object { $_ })
        for ($index = 0; $index -lt $parts.Count; $index++) {
            $part = $parts[$index]
            $current = [IO.Path]::GetFullPath((Join-Path $current $part))
            $info = Get-Item -LiteralPath $current -Force -ErrorAction Stop
            if (($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                $sourcePath = [IO.Path]::GetFullPath($current)
                if (-not $Visited.Add($sourcePath)) { return $null }
                $nextHops = $Hops + 1
                if ($nextHops -gt 63) { return $null }
                if ($info.PSObject.Methods.Name -notcontains "ResolveLinkTarget") {
                    if ($info.PSObject.Properties.Name -notcontains "Target") { return $null }
                    $targets = @(
                        @($info.Target) | Where-Object {
                            $_ -is [string] -and -not [string]::IsNullOrWhiteSpace([string]$_)
                        }
                    )
                    if ($targets.Count -ne 1) { return $null }
                    $targetText = [string]$targets[0]
                } else {
                    $target = $info.ResolveLinkTarget($false)
                    if (-not $target -or -not $target.FullName) { return $null }
                    $targetText = [string]$target.FullName
                }
                if (Test-GraphifyFullyQualifiedPath $targetText) {
                    $targetPath = [IO.Path]::GetFullPath($targetText)
                } else {
                    $linkParent = Split-Path -Parent $sourcePath
                    if (-not $linkParent) { return $null }
                    $targetPath = [IO.Path]::GetFullPath((Join-Path $linkParent $targetText))
                }
                if ($index + 1 -lt $parts.Count) {
                    $remainingSuffix = [string]::Join(
                        [IO.Path]::DirectorySeparatorChar,
                        [string[]]$parts[($index + 1)..($parts.Count - 1)]
                    )
                    $targetPath = Join-Path $targetPath $remainingSuffix
                }
                return Resolve-GraphifyPolicyPath $targetPath $Visited $nextHops
            }
        }
        return [IO.Path]::GetFullPath($current)
    } catch { return $null }
}
function Add-GraphifyDenyRoot {
    param([string]$Path, [bool]$Required = $false)
    if (-not $Path) { return }
    try {
        $full = if (Test-GraphifyFullyQualifiedPath $path) { [IO.Path]::GetFullPath($Path) } else { [IO.Path]::GetFullPath((Join-Path $GraphifyWorkspace $Path)) }
        if (-not (Test-Path -LiteralPath $full -PathType Container)) { if ($Required) { $script:GraphifyDenyPolicyInvalid = $true }; return }
        $resolved = Resolve-GraphifyPolicyPath $full
        if (-not $resolved) { $script:GraphifyDenyPolicyInvalid = $true; return }
        if ($resolved -ne [IO.Path]::GetPathRoot($resolved)) { $resolved = $resolved.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) }
        $lexical = [IO.Path]::GetFullPath($full)
        if ($lexical -ne [IO.Path]::GetPathRoot($lexical)) { $lexical = $lexical.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) }
        if (-not $GraphifyDenyRoots.Contains($lexical)) { $GraphifyDenyRoots.Add($lexical) }
        if (-not $GraphifyDenyRoots.Contains($resolved)) { $GraphifyDenyRoots.Add($resolved) }
    } catch { $script:GraphifyDenyPolicyInvalid = $true; return }
}
Add-GraphifyDenyRoot $GraphifyWorkspace $true
if ($env:GRAPHIFY_INPUT_PATH -and $env:GRAPHIFY_INPUT_PATH -ne "INPUT_PATH") { Add-GraphifyDenyRoot $env:GRAPHIFY_INPUT_PATH $true }
$GraphifySelectedOutput = if ($env:GRAPHIFY_OUTPUT_ROOT) { $env:GRAPHIFY_OUTPUT_ROOT } elseif ($env:GRAPHIFY_OUT) { $env:GRAPHIFY_OUT } else { "graphify-out" }
Add-GraphifyDenyRoot $GraphifySelectedOutput ([bool]($env:GRAPHIFY_OUTPUT_ROOT -or $env:GRAPHIFY_OUT))
function Test-GraphifyWorkspacePath {
    param([string]$Path)
    if ($GraphifyDenyPolicyInvalid) { return $true }
    if (-not (Test-GraphifyFullyQualifiedPath $Path)) { return $true }
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    try {
        $lexical = [IO.Path]::GetFullPath($Path)
        $resolved = Resolve-GraphifyPolicyPath $lexical
        if (-not $resolved) { return $true }
    }
    catch { return $true }
    foreach ($root in $GraphifyDenyRoots) {
        $prefix = if ($root -eq [IO.Path]::GetPathRoot($root)) { $root } else { $root + [IO.Path]::DirectorySeparatorChar }
        if ($lexical -eq $root -or $lexical.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { return $true }
        if ($resolved -eq $root -or $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}
function Resolve-GraphifyAmbientCommand {
    param([string]$Name)
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $command) { return $null }
    $path = [IO.Path]::GetFullPath($command.Source)
    if (Test-GraphifyWorkspacePath $path) { return $null }
    return $path
}
$GraphifyIdentityCheck = @'
exec('import importlib.metadata\nimport importlib.util\nimport json\nimport os\nimport sys\nimport urllib.parse\nimport urllib.request\n\ndef contained(path, root):\n    try:\n        normalized_root = os.path.normcase(root)\n        return os.path.commonpath((os.path.normcase(path), normalized_root)) == normalized_root\n    except (OSError, ValueError):\n        return False\n\ntry:\n    distribution = importlib.metadata.distribution("graphifyy")\n    if distribution.metadata.get("Name") != "graphifyy":\n        raise ValueError\n    spec = importlib.util.find_spec("graphify")\n    if spec is None or not spec.origin:\n        raise ValueError\n    origin = os.path.abspath(spec.origin)\n    real_origin = os.path.realpath(origin)\n    direct_url_text = distribution.read_text("direct_url.json")\n    editable = False\n    if direct_url_text is not None:\n        direct_url = json.loads(direct_url_text)\n        parsed = urllib.parse.urlparse(direct_url["url"])\n        if direct_url.get("dir_info", {}).get("editable") is True:\n            editable = True\n            if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):\n                raise ValueError\n            package_root = os.path.abspath(\n                urllib.request.url2pathname(parsed.path)\n            )\n    if editable:\n        real_package_root = os.path.realpath(package_root)\n        if not contained(origin, package_root) or not contained(real_origin, real_package_root):\n            raise ValueError\n    else:\n        owned = [\n            entry\n            for entry in (distribution.files or ())\n            if str(entry) == "graphify/__init__.py"\n        ]\n        if len(owned) != 1:\n            raise ValueError\n        recorded_origin = os.path.abspath(distribution.locate_file(owned[0]))\n        if os.path.normcase(recorded_origin) != os.path.normcase(origin):\n            raise ValueError\n    arguments = sys.argv[1:]\n    if arguments[0] == "ambient":\n        for root_arg in arguments[1:]:\n            if not root_arg:\n                continue\n            root = os.path.abspath(root_arg)\n            real_root = os.path.realpath(root)\n            if contained(origin, root) or contained(real_origin, real_root):\n                raise ValueError\nexcept (Exception, SystemExit):\n    raise SystemExit(1)\n')
'@
function Test-GraphifyPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    if (-not (Test-GraphifySupportedPython $Candidate)) { return $false }
    & $Candidate -E -P -B -c $GraphifyIdentityCheck trusted 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    return $invocationSucceeded -and $exitCode -eq 0
}
function Test-GraphifyAmbientPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    if (-not (Test-GraphifySupportedPython $Candidate)) { return $false }
    & $Candidate -E -P -B -c $GraphifyIdentityCheck ambient @GraphifyDenyRoots 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    return $invocationSucceeded -and $exitCode -eq 0
}
function Test-GraphifySupportedPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    & $Candidate -E -P -B -c "import sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and sys.version_info.releaselevel == 'final' and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)" 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    return $invocationSucceeded -and $exitCode -eq 0
}
function Invoke-GraphifyNativeText {
    param([string]$Candidate, [string[]]$Arguments)
    if (-not $Candidate) { return $null }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $null }
    $output = & $Candidate @Arguments 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    if (-not ($invocationSucceeded -and $exitCode -eq 0)) { return $null }
    $lines = @(
        @($output) | Where-Object {
            $_ -is [string] -and -not [string]::IsNullOrWhiteSpace([string]$_)
        }
    )
    if ($lines.Count -ne 1) { return $null }
    return ([string]$lines[0]).Trim()
}
if (Test-GraphifyFullyQualifiedPath "$env:VIRTUAL_ENV") {
    $activeVenv = Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $activeVenv)) { $activeVenv = Join-Path $env:VIRTUAL_ENV "bin/python" }
    if (Test-GraphifyPython $activeVenv) { $GraphifyPython = $activeVenv; $GraphifyPythonExplicit = $true }
}
if (-not $GraphifyPython) {
    $uv = Resolve-GraphifyAmbientCommand uv
    if ($uv) {
        $uvDir = Invoke-GraphifyNativeText $uv @("tool", "dir")
        if ($uvDir) {
            $candidate = Join-Path $uvDir "graphifyy\Scripts\python.exe"
            if (-not (Test-Path -LiteralPath $candidate)) { $candidate = Join-Path $uvDir "graphifyy/bin/python" }
            if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyAmbientPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate) }
        }
    }
}
if (-not $GraphifyPython) {
    $pipx = Resolve-GraphifyAmbientCommand pipx
    if ($pipx) {
        $venvs = Invoke-GraphifyNativeText $pipx @("environment", "--value", "PIPX_LOCAL_VENVS")
        if ($venvs) {
            $candidate = Join-Path $venvs "graphifyy\Scripts\python.exe"
            if (-not (Test-Path -LiteralPath $candidate)) { $candidate = Join-Path $venvs "graphifyy/bin/python" }
            if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyAmbientPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate) }
        }
    }
}
if (-not $GraphifyPython) {
    $graphify = Resolve-GraphifyAmbientCommand graphify
    if ($graphify) {
        $bindir = Split-Path -Parent $graphify
        foreach ($candidate in @((Join-Path $bindir "python.exe"), (Join-Path $bindir "../python.exe"))) {
            if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyAmbientPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate); break }
        }
    }
}
if (-not $GraphifyPython) {
    foreach ($name in @("python3.14", "python3", "py", "python")) {
        $candidate = Resolve-GraphifyAmbientCommand $name
        if (-not $candidate) { continue }
        if ($name -eq "py") {
            $resolved = Invoke-GraphifyNativeText $candidate @("-3.14", "-E", "-P", "-B", "-c", "import sys; print(sys.executable)")
            if ($resolved -and -not (Test-GraphifyWorkspacePath $resolved) -and (Test-GraphifyAmbientPython $resolved)) { $GraphifyPython = [IO.Path]::GetFullPath($resolved); break }
        } elseif (Test-GraphifyAmbientPython $candidate) { $GraphifyPython = $candidate; break }
    }
}
if (-not $GraphifyPython -and -not $GraphifyDiscoveryOptional) { throw "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." }
& $GraphifyPython -E -P -B -m graphify save-result --question "Path from NODE_A to NODE_B" --answer "ANSWER" --type path_query --nodes NODE_A NODE_B
```

---

## For /graphify explain

Give a plain-language explanation of a single node - everything connected to it. Prefer the CLI when installed:

```powershell
$env:GRAPHIFY_INPUT_PATH = "INPUT_PATH"
$GraphifyPython = $null
$GraphifyPythonExplicit = $false
$GraphifyWorkspace = [IO.Path]::GetFullPath((Get-Location).Path)
if ($GraphifyWorkspace -ne [IO.Path]::GetPathRoot($GraphifyWorkspace)) { $GraphifyWorkspace = $GraphifyWorkspace.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) }
$GraphifyDenyRoots = [Collections.Generic.List[string]]::new()
$GraphifyDenyPolicyInvalid = $false
function Test-GraphifyFullyQualifiedPath {
    param([string]$Path)
    if (-not $Path -or -not [IO.Path]::IsPathRooted($Path)) { return $false }
    $root = [IO.Path]::GetPathRoot($Path)
    if (-not $root -or $root -match '^[A-Za-z]:$') { return $false }
    if ($Path -match '^[\\/](?![\\/])') { return $false }
    return $true
}
function Resolve-GraphifyPolicyPath {
    param(
        [string]$Path,
        [Collections.Generic.HashSet[string]]$Visited = $null,
        [int]$Hops = 0
    )
    if (-not (Test-GraphifyFullyQualifiedPath $Path)) { return $null }
    try {
        $full = [IO.Path]::GetFullPath($Path)
        $root = [IO.Path]::GetPathRoot($full)
        if (-not $root) { return $null }
        if ($null -eq $Visited) {
            $Visited = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        }
        $current = $root
        $parts = @($full.Substring($root.Length) -split '[\\/]' | Where-Object { $_ })
        for ($index = 0; $index -lt $parts.Count; $index++) {
            $part = $parts[$index]
            $current = [IO.Path]::GetFullPath((Join-Path $current $part))
            $info = Get-Item -LiteralPath $current -Force -ErrorAction Stop
            if (($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                $sourcePath = [IO.Path]::GetFullPath($current)
                if (-not $Visited.Add($sourcePath)) { return $null }
                $nextHops = $Hops + 1
                if ($nextHops -gt 63) { return $null }
                if ($info.PSObject.Methods.Name -notcontains "ResolveLinkTarget") {
                    if ($info.PSObject.Properties.Name -notcontains "Target") { return $null }
                    $targets = @(
                        @($info.Target) | Where-Object {
                            $_ -is [string] -and -not [string]::IsNullOrWhiteSpace([string]$_)
                        }
                    )
                    if ($targets.Count -ne 1) { return $null }
                    $targetText = [string]$targets[0]
                } else {
                    $target = $info.ResolveLinkTarget($false)
                    if (-not $target -or -not $target.FullName) { return $null }
                    $targetText = [string]$target.FullName
                }
                if (Test-GraphifyFullyQualifiedPath $targetText) {
                    $targetPath = [IO.Path]::GetFullPath($targetText)
                } else {
                    $linkParent = Split-Path -Parent $sourcePath
                    if (-not $linkParent) { return $null }
                    $targetPath = [IO.Path]::GetFullPath((Join-Path $linkParent $targetText))
                }
                if ($index + 1 -lt $parts.Count) {
                    $remainingSuffix = [string]::Join(
                        [IO.Path]::DirectorySeparatorChar,
                        [string[]]$parts[($index + 1)..($parts.Count - 1)]
                    )
                    $targetPath = Join-Path $targetPath $remainingSuffix
                }
                return Resolve-GraphifyPolicyPath $targetPath $Visited $nextHops
            }
        }
        return [IO.Path]::GetFullPath($current)
    } catch { return $null }
}
function Add-GraphifyDenyRoot {
    param([string]$Path, [bool]$Required = $false)
    if (-not $Path) { return }
    try {
        $full = if (Test-GraphifyFullyQualifiedPath $path) { [IO.Path]::GetFullPath($Path) } else { [IO.Path]::GetFullPath((Join-Path $GraphifyWorkspace $Path)) }
        if (-not (Test-Path -LiteralPath $full -PathType Container)) { if ($Required) { $script:GraphifyDenyPolicyInvalid = $true }; return }
        $resolved = Resolve-GraphifyPolicyPath $full
        if (-not $resolved) { $script:GraphifyDenyPolicyInvalid = $true; return }
        if ($resolved -ne [IO.Path]::GetPathRoot($resolved)) { $resolved = $resolved.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) }
        $lexical = [IO.Path]::GetFullPath($full)
        if ($lexical -ne [IO.Path]::GetPathRoot($lexical)) { $lexical = $lexical.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) }
        if (-not $GraphifyDenyRoots.Contains($lexical)) { $GraphifyDenyRoots.Add($lexical) }
        if (-not $GraphifyDenyRoots.Contains($resolved)) { $GraphifyDenyRoots.Add($resolved) }
    } catch { $script:GraphifyDenyPolicyInvalid = $true; return }
}
Add-GraphifyDenyRoot $GraphifyWorkspace $true
if ($env:GRAPHIFY_INPUT_PATH -and $env:GRAPHIFY_INPUT_PATH -ne "INPUT_PATH") { Add-GraphifyDenyRoot $env:GRAPHIFY_INPUT_PATH $true }
$GraphifySelectedOutput = if ($env:GRAPHIFY_OUTPUT_ROOT) { $env:GRAPHIFY_OUTPUT_ROOT } elseif ($env:GRAPHIFY_OUT) { $env:GRAPHIFY_OUT } else { "graphify-out" }
Add-GraphifyDenyRoot $GraphifySelectedOutput ([bool]($env:GRAPHIFY_OUTPUT_ROOT -or $env:GRAPHIFY_OUT))
function Test-GraphifyWorkspacePath {
    param([string]$Path)
    if ($GraphifyDenyPolicyInvalid) { return $true }
    if (-not (Test-GraphifyFullyQualifiedPath $Path)) { return $true }
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    try {
        $lexical = [IO.Path]::GetFullPath($Path)
        $resolved = Resolve-GraphifyPolicyPath $lexical
        if (-not $resolved) { return $true }
    }
    catch { return $true }
    foreach ($root in $GraphifyDenyRoots) {
        $prefix = if ($root -eq [IO.Path]::GetPathRoot($root)) { $root } else { $root + [IO.Path]::DirectorySeparatorChar }
        if ($lexical -eq $root -or $lexical.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { return $true }
        if ($resolved -eq $root -or $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}
function Resolve-GraphifyAmbientCommand {
    param([string]$Name)
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $command) { return $null }
    $path = [IO.Path]::GetFullPath($command.Source)
    if (Test-GraphifyWorkspacePath $path) { return $null }
    return $path
}
$GraphifyIdentityCheck = @'
exec('import importlib.metadata\nimport importlib.util\nimport json\nimport os\nimport sys\nimport urllib.parse\nimport urllib.request\n\ndef contained(path, root):\n    try:\n        normalized_root = os.path.normcase(root)\n        return os.path.commonpath((os.path.normcase(path), normalized_root)) == normalized_root\n    except (OSError, ValueError):\n        return False\n\ntry:\n    distribution = importlib.metadata.distribution("graphifyy")\n    if distribution.metadata.get("Name") != "graphifyy":\n        raise ValueError\n    spec = importlib.util.find_spec("graphify")\n    if spec is None or not spec.origin:\n        raise ValueError\n    origin = os.path.abspath(spec.origin)\n    real_origin = os.path.realpath(origin)\n    direct_url_text = distribution.read_text("direct_url.json")\n    editable = False\n    if direct_url_text is not None:\n        direct_url = json.loads(direct_url_text)\n        parsed = urllib.parse.urlparse(direct_url["url"])\n        if direct_url.get("dir_info", {}).get("editable") is True:\n            editable = True\n            if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):\n                raise ValueError\n            package_root = os.path.abspath(\n                urllib.request.url2pathname(parsed.path)\n            )\n    if editable:\n        real_package_root = os.path.realpath(package_root)\n        if not contained(origin, package_root) or not contained(real_origin, real_package_root):\n            raise ValueError\n    else:\n        owned = [\n            entry\n            for entry in (distribution.files or ())\n            if str(entry) == "graphify/__init__.py"\n        ]\n        if len(owned) != 1:\n            raise ValueError\n        recorded_origin = os.path.abspath(distribution.locate_file(owned[0]))\n        if os.path.normcase(recorded_origin) != os.path.normcase(origin):\n            raise ValueError\n    arguments = sys.argv[1:]\n    if arguments[0] == "ambient":\n        for root_arg in arguments[1:]:\n            if not root_arg:\n                continue\n            root = os.path.abspath(root_arg)\n            real_root = os.path.realpath(root)\n            if contained(origin, root) or contained(real_origin, real_root):\n                raise ValueError\nexcept (Exception, SystemExit):\n    raise SystemExit(1)\n')
'@
function Test-GraphifyPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    if (-not (Test-GraphifySupportedPython $Candidate)) { return $false }
    & $Candidate -E -P -B -c $GraphifyIdentityCheck trusted 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    return $invocationSucceeded -and $exitCode -eq 0
}
function Test-GraphifyAmbientPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    if (-not (Test-GraphifySupportedPython $Candidate)) { return $false }
    & $Candidate -E -P -B -c $GraphifyIdentityCheck ambient @GraphifyDenyRoots 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    return $invocationSucceeded -and $exitCode -eq 0
}
function Test-GraphifySupportedPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    & $Candidate -E -P -B -c "import sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and sys.version_info.releaselevel == 'final' and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)" 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    return $invocationSucceeded -and $exitCode -eq 0
}
function Invoke-GraphifyNativeText {
    param([string]$Candidate, [string[]]$Arguments)
    if (-not $Candidate) { return $null }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $null }
    $output = & $Candidate @Arguments 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    if (-not ($invocationSucceeded -and $exitCode -eq 0)) { return $null }
    $lines = @(
        @($output) | Where-Object {
            $_ -is [string] -and -not [string]::IsNullOrWhiteSpace([string]$_)
        }
    )
    if ($lines.Count -ne 1) { return $null }
    return ([string]$lines[0]).Trim()
}
if (Test-GraphifyFullyQualifiedPath "$env:VIRTUAL_ENV") {
    $activeVenv = Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $activeVenv)) { $activeVenv = Join-Path $env:VIRTUAL_ENV "bin/python" }
    if (Test-GraphifyPython $activeVenv) { $GraphifyPython = $activeVenv; $GraphifyPythonExplicit = $true }
}
if (-not $GraphifyPython) {
    $uv = Resolve-GraphifyAmbientCommand uv
    if ($uv) {
        $uvDir = Invoke-GraphifyNativeText $uv @("tool", "dir")
        if ($uvDir) {
            $candidate = Join-Path $uvDir "graphifyy\Scripts\python.exe"
            if (-not (Test-Path -LiteralPath $candidate)) { $candidate = Join-Path $uvDir "graphifyy/bin/python" }
            if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyAmbientPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate) }
        }
    }
}
if (-not $GraphifyPython) {
    $pipx = Resolve-GraphifyAmbientCommand pipx
    if ($pipx) {
        $venvs = Invoke-GraphifyNativeText $pipx @("environment", "--value", "PIPX_LOCAL_VENVS")
        if ($venvs) {
            $candidate = Join-Path $venvs "graphifyy\Scripts\python.exe"
            if (-not (Test-Path -LiteralPath $candidate)) { $candidate = Join-Path $venvs "graphifyy/bin/python" }
            if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyAmbientPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate) }
        }
    }
}
if (-not $GraphifyPython) {
    $graphify = Resolve-GraphifyAmbientCommand graphify
    if ($graphify) {
        $bindir = Split-Path -Parent $graphify
        foreach ($candidate in @((Join-Path $bindir "python.exe"), (Join-Path $bindir "../python.exe"))) {
            if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyAmbientPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate); break }
        }
    }
}
if (-not $GraphifyPython) {
    foreach ($name in @("python3.14", "python3", "py", "python")) {
        $candidate = Resolve-GraphifyAmbientCommand $name
        if (-not $candidate) { continue }
        if ($name -eq "py") {
            $resolved = Invoke-GraphifyNativeText $candidate @("-3.14", "-E", "-P", "-B", "-c", "import sys; print(sys.executable)")
            if ($resolved -and -not (Test-GraphifyWorkspacePath $resolved) -and (Test-GraphifyAmbientPython $resolved)) { $GraphifyPython = [IO.Path]::GetFullPath($resolved); break }
        } elseif (Test-GraphifyAmbientPython $candidate) { $GraphifyPython = $candidate; break }
    }
}
if (-not $GraphifyPython -and -not $GraphifyDiscoveryOptional) { throw "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." }
& $GraphifyPython -E -P -B -m graphify explain "NODE_NAME"
```

If the CLI is unavailable, run it inline:

```bash
GRAPHIFY_PYTHON=$(GRAPHIFY_INPUT_PATH="${GRAPHIFY_INPUT_PATH-}" /bin/sh -p -c 'GRAPHIFY_PYTHON=""; GRAPHIFY_PYTHON_EXPLICIT=0; _GRAPHIFY_VERSION_CHECK='"'"'import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)'"'"'; _GRAPHIFY_IDENTITY_CHECK='"'"'exec("import importlib.metadata\nimport importlib.util\nimport json\nimport os\nimport sys\nimport urllib.parse\nimport urllib.request\n\ndef contained(path, root):\n    try:\n        normalized_root = os.path.normcase(root)\n        return os.path.commonpath((os.path.normcase(path), normalized_root)) == normalized_root\n    except (OSError, ValueError):\n        return False\n\ntry:\n    distribution = importlib.metadata.distribution(\"graphifyy\")\n    if distribution.metadata.get(\"Name\") != \"graphifyy\":\n        raise ValueError\n    spec = importlib.util.find_spec(\"graphify\")\n    if spec is None or not spec.origin:\n        raise ValueError\n    origin = os.path.abspath(spec.origin)\n    real_origin = os.path.realpath(origin)\n    direct_url_text = distribution.read_text(\"direct_url.json\")\n    editable = False\n    if direct_url_text is not None:\n        direct_url = json.loads(direct_url_text)\n        parsed = urllib.parse.urlparse(direct_url[\"url\"])\n        if direct_url.get(\"dir_info\", {}).get(\"editable\") is True:\n            editable = True\n            if parsed.scheme != \"file\" or parsed.netloc not in (\"\", \"localhost\"):\n                raise ValueError\n            package_root = os.path.abspath(\n                urllib.request.url2pathname(parsed.path)\n            )\n    if editable:\n        real_package_root = os.path.realpath(package_root)\n        if not contained(origin, package_root) or not contained(real_origin, real_package_root):\n            raise ValueError\n    else:\n        owned = [\n            entry\n            for entry in (distribution.files or ())\n            if str(entry) == \"graphify/__init__.py\"\n        ]\n        if len(owned) != 1:\n            raise ValueError\n        recorded_origin = os.path.abspath(distribution.locate_file(owned[0]))\n        if os.path.normcase(recorded_origin) != os.path.normcase(origin):\n            raise ValueError\n    arguments = sys.argv[1:]\n    if arguments[0] == \"ambient\":\n        for root_arg in arguments[1:]:\n            if not root_arg:\n                continue\n            root = os.path.abspath(root_arg)\n            real_root = os.path.realpath(root)\n            if contained(origin, root) or contained(real_origin, real_root):\n                raise ValueError\nexcept (Exception, SystemExit):\n    raise SystemExit(1)\n")'"'"'; if [ -x /usr/bin/cygpath.exe ]; then _GRAPHIFY_CYGPATH=/usr/bin/cygpath.exe; elif [ -x /usr/bin/cygpath ]; then _GRAPHIFY_CYGPATH=/usr/bin/cygpath; elif [ -x /bin/cygpath.exe ]; then _GRAPHIFY_CYGPATH=/bin/cygpath.exe; elif [ -x /bin/cygpath ]; then _GRAPHIFY_CYGPATH=/bin/cygpath; else exit 1; fi; _graphify_to_posix() { _gfy_native=$1; case "$_gfy_native" in [a-zA-Z]:|[a-zA-Z]:[!\\/]*|\\|\\[!\\]*) return 1 ;; esac; "$_GRAPHIFY_CYGPATH" -u "$_gfy_native"; }; _graphify_to_native() { [ -n "$1" ] || return 0; "$_GRAPHIFY_CYGPATH" -w "$1"; }; _GRAPHIFY_WORKSPACE=$(command pwd -P) || exit 1; _GRAPHIFY_WORKSPACE_NATIVE=$(_graphify_to_native "$_GRAPHIFY_WORKSPACE") || exit 1; _graphify_canonical_root() { _gfy_root=$1; [ -n "$_gfy_root" ] || return 1; case "$_gfy_root" in [a-zA-Z]:|[a-zA-Z]:[!\\/]*|\\|\\[!\\]*) return 1 ;; /*) ;; *) _gfy_root=$(_graphify_to_posix "$_gfy_root") || return 1 ;; esac; case "$_gfy_root" in /*) ;; *) _gfy_root=$_GRAPHIFY_WORKSPACE/$_gfy_root ;; esac; if [ -d "$_gfy_root" ]; then CDPATH= cd -P -- "$_gfy_root" 2>/dev/null && command pwd -P; else printf '"'"'%s\n'"'"' "$_gfy_root"; fi; }; _graphify_absolute_command() { _gfy_command=$1; case "$_gfy_command" in /*) case "$_gfy_command" in */./*|*/../*|*/.|*/..) ;; *) GRAPHIFY_COMMAND_PATH=$_gfy_command; return 0 ;; esac; _gfy_command_dir=${_gfy_command%/*}; _gfy_command_base=${_gfy_command##*/} ;; */*) _gfy_command_dir=$_GRAPHIFY_WORKSPACE/${_gfy_command%/*}; _gfy_command_base=${_gfy_command##*/} ;; *) _gfy_command_dir=$_GRAPHIFY_WORKSPACE; _gfy_command_base=$_gfy_command ;; esac; case "$_gfy_command_base" in ""|.|..) return 1 ;; esac; _gfy_command_dir=$(CDPATH= cd -L -- "$_gfy_command_dir" 2>/dev/null && command pwd -L) || return 1; GRAPHIFY_COMMAND_PATH=$_gfy_command_dir/$_gfy_command_base; }; _GRAPHIFY_DENY_POLICY_INVALID=0; _gfy_input_raw=${GRAPHIFY_INPUT_PATH-}; _gfy_output_raw=${GRAPHIFY_OUTPUT_ROOT-${GRAPHIFY_OUT-graphify-out}}; _GRAPHIFY_INPUT_ROOT=$(_graphify_canonical_root "$_gfy_input_raw") || { _GRAPHIFY_INPUT_ROOT=""; [ -z "$_gfy_input_raw" ] || _GRAPHIFY_DENY_POLICY_INVALID=1; }; _GRAPHIFY_OUTPUT_ROOT=$(_graphify_canonical_root "$_gfy_output_raw") || { _GRAPHIFY_OUTPUT_ROOT=""; [ -z "$_gfy_output_raw" ] || _GRAPHIFY_DENY_POLICY_INVALID=1; }; [ "$_GRAPHIFY_DENY_POLICY_INVALID" = 0 ] || exit 1; _GRAPHIFY_INPUT_ROOT_NATIVE=$(_graphify_to_native "$_GRAPHIFY_INPUT_ROOT") || exit 1; _GRAPHIFY_OUTPUT_ROOT_NATIVE=$(_graphify_to_native "$_GRAPHIFY_OUTPUT_ROOT") || exit 1; _graphify_path_denied() { _gfy_policy_path=$1; [ "$_GRAPHIFY_WORKSPACE" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_WORKSPACE"|"$_GRAPHIFY_WORKSPACE"/*) return 0 ;; esac; [ -z "$_GRAPHIFY_INPUT_ROOT" ] || { [ "$_GRAPHIFY_INPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_INPUT_ROOT"|"$_GRAPHIFY_INPUT_ROOT"/*) return 0 ;; esac; }; [ -z "$_GRAPHIFY_OUTPUT_ROOT" ] || { [ "$_GRAPHIFY_OUTPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_OUTPUT_ROOT"|"$_GRAPHIFY_OUTPUT_ROOT"/*) return 0 ;; esac; }; return 1; }; _graphify_readlink() { if [ -x /usr/bin/readlink ]; then /usr/bin/readlink "$1"; elif [ -x /bin/readlink ]; then /bin/readlink "$1"; elif [ -x /run/current-system/sw/bin/readlink ]; then /run/current-system/sw/bin/readlink "$1"; else command -p readlink "$1"; fi; }; _graphify_resolve_ambient() { _gfy_lexical=$1; _gfy_lexical=$(_graphify_to_posix "$_gfy_lexical") || return 1; case "$_gfy_lexical" in /*) ;; *) return 1 ;; esac; _graphify_path_denied "$_gfy_lexical" && return 1; _gfy_path=$_gfy_lexical; _gfy_links=0; while [ -L "$_gfy_path" ]; do _gfy_links=$((_gfy_links + 1)); [ "$_gfy_links" -le 40 ] || return 1; _gfy_link=$(_graphify_readlink "$_gfy_path") || return 1; case "$_gfy_link" in /*) _gfy_path=$_gfy_link ;; *) _gfy_dir=${_gfy_path%/*}; _gfy_path=$_gfy_dir/$_gfy_link ;; esac; done; _gfy_dir=${_gfy_path%/*}; _gfy_base=${_gfy_path##*/}; _gfy_dir=$(CDPATH= cd -P -- "$_gfy_dir" 2>/dev/null && command pwd -P) || return 1; _gfy_path=$_gfy_dir/$_gfy_base; _graphify_path_denied "$_gfy_path" && return 1; [ -x "$_gfy_lexical" ] || return 1; GRAPHIFY_RESOLVED=$_gfy_lexical; }; _graphify_command() { _gfy_found=$(command -v "$1" 2>/dev/null) || return 1; _gfy_found=$(_graphify_to_posix "$_gfy_found") || return 1; _graphify_absolute_command "$_gfy_found" || return 1; _graphify_resolve_ambient "$GRAPHIFY_COMMAND_PATH"; }; _graphify_supported() { [ -n "$1" ] && "$1" -E -P -B -c "$_GRAPHIFY_VERSION_CHECK" >/dev/null 2>&1; }; _graphify_usable() { _graphify_supported "$1" && "$1" -E -P -B -c "$_GRAPHIFY_IDENTITY_CHECK" trusted >/dev/null 2>&1; }; _graphify_ambient_usable() { _graphify_supported "$1" && "$1" -E -P -B -c "$_GRAPHIFY_IDENTITY_CHECK" ambient "$_GRAPHIFY_WORKSPACE_NATIVE" "$_GRAPHIFY_INPUT_ROOT_NATIVE" "$_GRAPHIFY_OUTPUT_ROOT_NATIVE" >/dev/null 2>&1; }; case "${VIRTUAL_ENV-}" in "") ;; *) _gfy_venv=$(_graphify_to_posix "$VIRTUAL_ENV") || exit 1; _gfy_venv_python=$_gfy_venv/Scripts/python.exe; _graphify_usable "$_gfy_venv_python" && { GRAPHIFY_PYTHON=$_gfy_venv_python; GRAPHIFY_PYTHON_EXPLICIT=1; } ;; esac; if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command uv; then _gfy_uv=$GRAPHIFY_RESOLVED; _gfy_uv_dir=$("$_gfy_uv" tool dir 2>/dev/null); _gfy_uv_dir=$(_graphify_to_posix "$_gfy_uv_dir") || _gfy_uv_dir=""; _gfy_candidate=${_gfy_uv_dir:+$_gfy_uv_dir/graphifyy/Scripts/python.exe}; if _graphify_resolve_ambient "$_gfy_candidate"; then _graphify_ambient_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; fi; fi; if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command pipx; then _gfy_pipx=$GRAPHIFY_RESOLVED; _gfy_pipx_home=$("$_gfy_pipx" environment --value PIPX_LOCAL_VENVS 2>/dev/null); _gfy_pipx_home=$(_graphify_to_posix "$_gfy_pipx_home") || _gfy_pipx_home=""; _gfy_candidate=${_gfy_pipx_home:+$_gfy_pipx_home/graphifyy/Scripts/python.exe}; if _graphify_resolve_ambient "$_gfy_candidate"; then _graphify_ambient_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; fi; fi; if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command graphify; then _gfy_graphify=$(_graphify_to_posix "$GRAPHIFY_RESOLVED") || _gfy_graphify=""; _gfy_bindir=${_gfy_graphify%/*}; for _gfy_candidate in "$_gfy_bindir/python.exe" "$_gfy_bindir/../python.exe"; do if _graphify_resolve_ambient "$_gfy_candidate" && _graphify_ambient_usable "$GRAPHIFY_RESOLVED"; then GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; break; fi; done; fi; if [ -z "$GRAPHIFY_PYTHON" ]; then for _gfy_name in python3.14 python3 python; do if _graphify_command "$_gfy_name"; then _graphify_ambient_usable "$GRAPHIFY_RESOLVED" && { GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; break; }; fi; done; fi; if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command py; then _gfy_py=$GRAPHIFY_RESOLVED; _gfy_candidate=$("$_gfy_py" -3.14 -E -P -B -c '"'"'import sys; print(sys.executable)'"'"' 2>/dev/null); _gfy_candidate=$(_graphify_to_posix "$_gfy_candidate") || _gfy_candidate=""; if _graphify_resolve_ambient "$_gfy_candidate"; then _graphify_ambient_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; fi; fi; if [ -z "$GRAPHIFY_PYTHON" ] && [ "${GRAPHIFY_DISCOVERY_OPTIONAL-0}" != 1 ]; then echo "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." >&2; exit 1; fi; [ -n "$GRAPHIFY_PYTHON" ] || exit 1; printf "%sx" "$GRAPHIFY_PYTHON"'); GRAPHIFY_PYTHON=${GRAPHIFY_PYTHON%x}; GRAPHIFY_PYTHON=${GRAPHIFY_PYTHON:?Graphify interpreter discovery failed}
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

```powershell
$env:GRAPHIFY_INPUT_PATH = "INPUT_PATH"
$GraphifyPython = $null
$GraphifyPythonExplicit = $false
$GraphifyWorkspace = [IO.Path]::GetFullPath((Get-Location).Path)
if ($GraphifyWorkspace -ne [IO.Path]::GetPathRoot($GraphifyWorkspace)) { $GraphifyWorkspace = $GraphifyWorkspace.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) }
$GraphifyDenyRoots = [Collections.Generic.List[string]]::new()
$GraphifyDenyPolicyInvalid = $false
function Test-GraphifyFullyQualifiedPath {
    param([string]$Path)
    if (-not $Path -or -not [IO.Path]::IsPathRooted($Path)) { return $false }
    $root = [IO.Path]::GetPathRoot($Path)
    if (-not $root -or $root -match '^[A-Za-z]:$') { return $false }
    if ($Path -match '^[\\/](?![\\/])') { return $false }
    return $true
}
function Resolve-GraphifyPolicyPath {
    param(
        [string]$Path,
        [Collections.Generic.HashSet[string]]$Visited = $null,
        [int]$Hops = 0
    )
    if (-not (Test-GraphifyFullyQualifiedPath $Path)) { return $null }
    try {
        $full = [IO.Path]::GetFullPath($Path)
        $root = [IO.Path]::GetPathRoot($full)
        if (-not $root) { return $null }
        if ($null -eq $Visited) {
            $Visited = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        }
        $current = $root
        $parts = @($full.Substring($root.Length) -split '[\\/]' | Where-Object { $_ })
        for ($index = 0; $index -lt $parts.Count; $index++) {
            $part = $parts[$index]
            $current = [IO.Path]::GetFullPath((Join-Path $current $part))
            $info = Get-Item -LiteralPath $current -Force -ErrorAction Stop
            if (($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                $sourcePath = [IO.Path]::GetFullPath($current)
                if (-not $Visited.Add($sourcePath)) { return $null }
                $nextHops = $Hops + 1
                if ($nextHops -gt 63) { return $null }
                if ($info.PSObject.Methods.Name -notcontains "ResolveLinkTarget") {
                    if ($info.PSObject.Properties.Name -notcontains "Target") { return $null }
                    $targets = @(
                        @($info.Target) | Where-Object {
                            $_ -is [string] -and -not [string]::IsNullOrWhiteSpace([string]$_)
                        }
                    )
                    if ($targets.Count -ne 1) { return $null }
                    $targetText = [string]$targets[0]
                } else {
                    $target = $info.ResolveLinkTarget($false)
                    if (-not $target -or -not $target.FullName) { return $null }
                    $targetText = [string]$target.FullName
                }
                if (Test-GraphifyFullyQualifiedPath $targetText) {
                    $targetPath = [IO.Path]::GetFullPath($targetText)
                } else {
                    $linkParent = Split-Path -Parent $sourcePath
                    if (-not $linkParent) { return $null }
                    $targetPath = [IO.Path]::GetFullPath((Join-Path $linkParent $targetText))
                }
                if ($index + 1 -lt $parts.Count) {
                    $remainingSuffix = [string]::Join(
                        [IO.Path]::DirectorySeparatorChar,
                        [string[]]$parts[($index + 1)..($parts.Count - 1)]
                    )
                    $targetPath = Join-Path $targetPath $remainingSuffix
                }
                return Resolve-GraphifyPolicyPath $targetPath $Visited $nextHops
            }
        }
        return [IO.Path]::GetFullPath($current)
    } catch { return $null }
}
function Add-GraphifyDenyRoot {
    param([string]$Path, [bool]$Required = $false)
    if (-not $Path) { return }
    try {
        $full = if (Test-GraphifyFullyQualifiedPath $path) { [IO.Path]::GetFullPath($Path) } else { [IO.Path]::GetFullPath((Join-Path $GraphifyWorkspace $Path)) }
        if (-not (Test-Path -LiteralPath $full -PathType Container)) { if ($Required) { $script:GraphifyDenyPolicyInvalid = $true }; return }
        $resolved = Resolve-GraphifyPolicyPath $full
        if (-not $resolved) { $script:GraphifyDenyPolicyInvalid = $true; return }
        if ($resolved -ne [IO.Path]::GetPathRoot($resolved)) { $resolved = $resolved.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) }
        $lexical = [IO.Path]::GetFullPath($full)
        if ($lexical -ne [IO.Path]::GetPathRoot($lexical)) { $lexical = $lexical.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) }
        if (-not $GraphifyDenyRoots.Contains($lexical)) { $GraphifyDenyRoots.Add($lexical) }
        if (-not $GraphifyDenyRoots.Contains($resolved)) { $GraphifyDenyRoots.Add($resolved) }
    } catch { $script:GraphifyDenyPolicyInvalid = $true; return }
}
Add-GraphifyDenyRoot $GraphifyWorkspace $true
if ($env:GRAPHIFY_INPUT_PATH -and $env:GRAPHIFY_INPUT_PATH -ne "INPUT_PATH") { Add-GraphifyDenyRoot $env:GRAPHIFY_INPUT_PATH $true }
$GraphifySelectedOutput = if ($env:GRAPHIFY_OUTPUT_ROOT) { $env:GRAPHIFY_OUTPUT_ROOT } elseif ($env:GRAPHIFY_OUT) { $env:GRAPHIFY_OUT } else { "graphify-out" }
Add-GraphifyDenyRoot $GraphifySelectedOutput ([bool]($env:GRAPHIFY_OUTPUT_ROOT -or $env:GRAPHIFY_OUT))
function Test-GraphifyWorkspacePath {
    param([string]$Path)
    if ($GraphifyDenyPolicyInvalid) { return $true }
    if (-not (Test-GraphifyFullyQualifiedPath $Path)) { return $true }
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    try {
        $lexical = [IO.Path]::GetFullPath($Path)
        $resolved = Resolve-GraphifyPolicyPath $lexical
        if (-not $resolved) { return $true }
    }
    catch { return $true }
    foreach ($root in $GraphifyDenyRoots) {
        $prefix = if ($root -eq [IO.Path]::GetPathRoot($root)) { $root } else { $root + [IO.Path]::DirectorySeparatorChar }
        if ($lexical -eq $root -or $lexical.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { return $true }
        if ($resolved -eq $root -or $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}
function Resolve-GraphifyAmbientCommand {
    param([string]$Name)
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $command) { return $null }
    $path = [IO.Path]::GetFullPath($command.Source)
    if (Test-GraphifyWorkspacePath $path) { return $null }
    return $path
}
$GraphifyIdentityCheck = @'
exec('import importlib.metadata\nimport importlib.util\nimport json\nimport os\nimport sys\nimport urllib.parse\nimport urllib.request\n\ndef contained(path, root):\n    try:\n        normalized_root = os.path.normcase(root)\n        return os.path.commonpath((os.path.normcase(path), normalized_root)) == normalized_root\n    except (OSError, ValueError):\n        return False\n\ntry:\n    distribution = importlib.metadata.distribution("graphifyy")\n    if distribution.metadata.get("Name") != "graphifyy":\n        raise ValueError\n    spec = importlib.util.find_spec("graphify")\n    if spec is None or not spec.origin:\n        raise ValueError\n    origin = os.path.abspath(spec.origin)\n    real_origin = os.path.realpath(origin)\n    direct_url_text = distribution.read_text("direct_url.json")\n    editable = False\n    if direct_url_text is not None:\n        direct_url = json.loads(direct_url_text)\n        parsed = urllib.parse.urlparse(direct_url["url"])\n        if direct_url.get("dir_info", {}).get("editable") is True:\n            editable = True\n            if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):\n                raise ValueError\n            package_root = os.path.abspath(\n                urllib.request.url2pathname(parsed.path)\n            )\n    if editable:\n        real_package_root = os.path.realpath(package_root)\n        if not contained(origin, package_root) or not contained(real_origin, real_package_root):\n            raise ValueError\n    else:\n        owned = [\n            entry\n            for entry in (distribution.files or ())\n            if str(entry) == "graphify/__init__.py"\n        ]\n        if len(owned) != 1:\n            raise ValueError\n        recorded_origin = os.path.abspath(distribution.locate_file(owned[0]))\n        if os.path.normcase(recorded_origin) != os.path.normcase(origin):\n            raise ValueError\n    arguments = sys.argv[1:]\n    if arguments[0] == "ambient":\n        for root_arg in arguments[1:]:\n            if not root_arg:\n                continue\n            root = os.path.abspath(root_arg)\n            real_root = os.path.realpath(root)\n            if contained(origin, root) or contained(real_origin, real_root):\n                raise ValueError\nexcept (Exception, SystemExit):\n    raise SystemExit(1)\n')
'@
function Test-GraphifyPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    if (-not (Test-GraphifySupportedPython $Candidate)) { return $false }
    & $Candidate -E -P -B -c $GraphifyIdentityCheck trusted 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    return $invocationSucceeded -and $exitCode -eq 0
}
function Test-GraphifyAmbientPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    if (-not (Test-GraphifySupportedPython $Candidate)) { return $false }
    & $Candidate -E -P -B -c $GraphifyIdentityCheck ambient @GraphifyDenyRoots 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    return $invocationSucceeded -and $exitCode -eq 0
}
function Test-GraphifySupportedPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    & $Candidate -E -P -B -c "import sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and sys.version_info.releaselevel == 'final' and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)" 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    return $invocationSucceeded -and $exitCode -eq 0
}
function Invoke-GraphifyNativeText {
    param([string]$Candidate, [string[]]$Arguments)
    if (-not $Candidate) { return $null }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $null }
    $output = & $Candidate @Arguments 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    if (-not ($invocationSucceeded -and $exitCode -eq 0)) { return $null }
    $lines = @(
        @($output) | Where-Object {
            $_ -is [string] -and -not [string]::IsNullOrWhiteSpace([string]$_)
        }
    )
    if ($lines.Count -ne 1) { return $null }
    return ([string]$lines[0]).Trim()
}
if (Test-GraphifyFullyQualifiedPath "$env:VIRTUAL_ENV") {
    $activeVenv = Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $activeVenv)) { $activeVenv = Join-Path $env:VIRTUAL_ENV "bin/python" }
    if (Test-GraphifyPython $activeVenv) { $GraphifyPython = $activeVenv; $GraphifyPythonExplicit = $true }
}
if (-not $GraphifyPython) {
    $uv = Resolve-GraphifyAmbientCommand uv
    if ($uv) {
        $uvDir = Invoke-GraphifyNativeText $uv @("tool", "dir")
        if ($uvDir) {
            $candidate = Join-Path $uvDir "graphifyy\Scripts\python.exe"
            if (-not (Test-Path -LiteralPath $candidate)) { $candidate = Join-Path $uvDir "graphifyy/bin/python" }
            if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyAmbientPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate) }
        }
    }
}
if (-not $GraphifyPython) {
    $pipx = Resolve-GraphifyAmbientCommand pipx
    if ($pipx) {
        $venvs = Invoke-GraphifyNativeText $pipx @("environment", "--value", "PIPX_LOCAL_VENVS")
        if ($venvs) {
            $candidate = Join-Path $venvs "graphifyy\Scripts\python.exe"
            if (-not (Test-Path -LiteralPath $candidate)) { $candidate = Join-Path $venvs "graphifyy/bin/python" }
            if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyAmbientPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate) }
        }
    }
}
if (-not $GraphifyPython) {
    $graphify = Resolve-GraphifyAmbientCommand graphify
    if ($graphify) {
        $bindir = Split-Path -Parent $graphify
        foreach ($candidate in @((Join-Path $bindir "python.exe"), (Join-Path $bindir "../python.exe"))) {
            if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyAmbientPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate); break }
        }
    }
}
if (-not $GraphifyPython) {
    foreach ($name in @("python3.14", "python3", "py", "python")) {
        $candidate = Resolve-GraphifyAmbientCommand $name
        if (-not $candidate) { continue }
        if ($name -eq "py") {
            $resolved = Invoke-GraphifyNativeText $candidate @("-3.14", "-E", "-P", "-B", "-c", "import sys; print(sys.executable)")
            if ($resolved -and -not (Test-GraphifyWorkspacePath $resolved) -and (Test-GraphifyAmbientPython $resolved)) { $GraphifyPython = [IO.Path]::GetFullPath($resolved); break }
        } elseif (Test-GraphifyAmbientPython $candidate) { $GraphifyPython = $candidate; break }
    }
}
if (-not $GraphifyPython -and -not $GraphifyDiscoveryOptional) { throw "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." }
& $GraphifyPython -E -P -B -m graphify save-result --question "Explain NODE_NAME" --answer "ANSWER" --type explain --nodes NODE_NAME
```
