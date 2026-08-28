# git hook integration - install/uninstall graphify post-commit and post-checkout hooks
from __future__ import annotations
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from graphify._interpreter_identity import _GRAPHIFY_IDENTITY_SOURCE

_HOOK_MARKER = "# graphify-hook-start"
_HOOK_MARKER_END = "# graphify-hook-end"
_CHECKOUT_MARKER = "# graphify-checkout-hook-start"
_CHECKOUT_MARKER_END = "# graphify-checkout-hook-end"

# __PINNED_PYTHON__ is replaced at install time with the absolute path of the
# Python interpreter that ran `graphify hook install`.  For uv-tool and pipx
# installs the interpreter lives inside an isolated venv, so the launcher on
# PATH is the only entry point — and GUI git clients / CI runners often have a
# minimal PATH that omits ~/.local/bin.  Pinning sys.executable at install time
# makes the hook work regardless of PATH at git-trigger time.
_PYTHON_DETECT = """\
# Detect a trusted Python interpreter (uv tool, pipx, venv, system installs).
# The install-time pin has trusted provenance: it is the interpreter already
# running graphify hook install. Dynamic fallbacks are lower-authority and may
# not come from the repository being processed.
_GFY_PROBE="import importlib.metadata as m, importlib.util as u, json, os, re, sys, urllib.parse as p, urllib.request as r; v=sys.version_info; s=u.find_spec('graphify'); d=m.distribution('graphifyy'); name=re.sub('[-_.]+', '-', d.metadata['Name']).lower(); actual=os.path.normcase(os.path.abspath(s.origin or '')) if s else ''; owned=[x for x in (d.files or ()) if str(x) == 'graphify/__init__.py']; installed=os.path.normcase(os.path.abspath(str(d.locate_file(owned[0])))) if len(owned) == 1 else ''; direct=json.loads(d.read_text('direct_url.json') or '{}'); parts=p.urlparse(direct.get('url', '')); is_editable=direct.get('dir_info', {}).get('editable') is True; editable=is_editable and parts.scheme == 'file' and parts.netloc in ('', 'localhost') and not parts.params and not parts.query and not parts.fragment; editable_init=os.path.normcase(os.path.abspath(os.path.join(r.url2pathname(parts.path), 'graphify', '__init__.py'))) if editable else ''; identity=(not is_editable and len(owned) == 1 and actual == installed) or (editable and actual == editable_init); ok=sys.implementation.name == 'cpython' and v.releaselevel == 'final' and (3, 14, 2) <= v[:3] < (3, 15, 0) and name == 'graphifyy' and identity; sys.exit(0 if ok else 1)"
_GFY_IDENTITY_CHECK=""" + shlex.quote(_GRAPHIFY_IDENTITY_SOURCE) + """
# Capture an absolute lexical invocation path. Resolve its symlinks only for
# the containment check so a venv's lexical Python path keeps venv semantics.
_GFY_WORKSPACE=$(pwd -P 2>/dev/null)
_gfy_canonical_root() {
    _GFY_ROOT_RAW=$1
    [ -n "$_GFY_ROOT_RAW" ] || return 1
    case "$_GFY_ROOT_RAW" in /*) ;; *) _GFY_ROOT_RAW=$_GFY_WORKSPACE/$_GFY_ROOT_RAW ;; esac
    [ -d "$_GFY_ROOT_RAW" ] || return 1
    cd -P "$_GFY_ROOT_RAW" 2>/dev/null && pwd
}
_GFY_INPUT_ROOT=$(_gfy_canonical_root "${GRAPHIFY_INPUT_PATH-}") || _GFY_INPUT_ROOT=""
_GFY_OUTPUT_ROOT=$(_gfy_canonical_root "${GRAPHIFY_OUTPUT_ROOT-${GRAPHIFY_OUT-graphify-out}}") || _GFY_OUTPUT_ROOT=""
GRAPHIFY_PYTHON=""
_PINNED=__PINNED_PYTHON__
if [ -n "$_PINNED" ] && [ -x "$_PINNED" ] && "$_PINNED" -E -P -B -c "$_GFY_PROBE" "$_GFY_WORKSPACE" "$_GFY_INPUT_ROOT" "$_GFY_OUTPUT_ROOT" 2>/dev/null; then
    GRAPHIFY_PYTHON="$_PINNED"
fi
# Persisted corpus state is denial-only and is inspected only after the trusted
# install-time pin fails. A same-user check/open race remains possible here.
_GFY_PERSISTED_ROOT=""
_GFY_PERSISTED_POLICY_INVALID=0
_gfy_native_root_to_posix() {
    if [ -x /usr/bin/cygpath.exe ]; then _GFY_CYGPATH=/usr/bin/cygpath.exe
    elif [ -x /usr/bin/cygpath ]; then _GFY_CYGPATH=/usr/bin/cygpath
    elif [ -x /bin/cygpath.exe ]; then _GFY_CYGPATH=/bin/cygpath.exe
    elif [ -x /bin/cygpath ]; then _GFY_CYGPATH=/bin/cygpath
    else return 1
    fi
    "$_GFY_CYGPATH" -u "$1"
}
if [ -z "$GRAPHIFY_PYTHON" ]; then
    if [ "${GRAPHIFY_OUT+x}" = x ]; then
        if [ -n "$GRAPHIFY_OUT" ]; then _GFY_ROOT_MARKER=$GRAPHIFY_OUT/.graphify_root
        else _GFY_ROOT_MARKER=./.graphify_root
        fi
    else
        _GFY_ROOT_MARKER=graphify-out/.graphify_root
    fi
    if [ -f "$_GFY_ROOT_MARKER" ] && [ ! -L "$_GFY_ROOT_MARKER" ]; then
        _GFY_ROOT_LINE=""
        _GFY_ROOT_EXTRA=""
        if exec 3< "$_GFY_ROOT_MARKER"; then
            IFS= read -r _GFY_ROOT_LINE <&3 || [ -n "$_GFY_ROOT_LINE" ]
            _GFY_ROOT_STATUS=$?
            if IFS= read -r _GFY_ROOT_EXTRA <&3 || [ -n "$_GFY_ROOT_EXTRA" ]; then
                _GFY_ROOT_STATUS=1
            fi
            exec 3<&-
            _GFY_BOM=$(printf '\\357\\273\\277')
            case "$_GFY_ROOT_LINE" in "$_GFY_BOM"*) _GFY_ROOT_LINE=${_GFY_ROOT_LINE#"$_GFY_BOM"} ;; esac
            _GFY_ROOT_NATIVE=0
            _GFY_BACKSLASH=$(printf '\\\\')
            case "$_GFY_ROOT_LINE" in
                [a-zA-Z]:*) _GFY_ROOT_TAIL=${_GFY_ROOT_LINE#??}
                    case "$_GFY_ROOT_TAIL" in /*|"$_GFY_BACKSLASH"*) _GFY_ROOT_NATIVE=1 ;; esac ;;
                "$_GFY_BACKSLASH"*) _GFY_ROOT_TAIL=${_GFY_ROOT_LINE#?}
                    case "$_GFY_ROOT_TAIL" in "$_GFY_BACKSLASH"*) _GFY_ROOT_NATIVE=1 ;; esac ;;
            esac
            case "$_GFY_ROOT_LINE" in
                /*) if [ "$_GFY_ROOT_STATUS" -eq 0 ]; then
                        _GFY_PERSISTED_ROOT=$(_gfy_canonical_root "$_GFY_ROOT_LINE") || _GFY_PERSISTED_ROOT=""
                    fi ;;
            esac
            if [ "$_GFY_ROOT_NATIVE" = 1 ] && [ "$_GFY_ROOT_STATUS" -eq 0 ]; then
                _GFY_ROOT_LINE=$(_gfy_native_root_to_posix "$_GFY_ROOT_LINE") || _GFY_PERSISTED_POLICY_INVALID=1
                case "$_GFY_ROOT_LINE" in
                    /*) _GFY_PERSISTED_ROOT=$(_gfy_canonical_root "$_GFY_ROOT_LINE") || _GFY_PERSISTED_ROOT="" ;;
                    *) _GFY_PERSISTED_POLICY_INVALID=1 ;;
                esac
            fi
        fi
    fi
fi
if [ "$_GFY_PERSISTED_POLICY_INVALID" != 0 ]; then
    exit 0
fi
_gfy_path_denied() {
    _GFY_DENY_PATH=$1
    [ "$_GFY_WORKSPACE" = / ] && return 0
    case "$_GFY_DENY_PATH" in "$_GFY_WORKSPACE"|"$_GFY_WORKSPACE"/*) return 0 ;; esac
    if [ -n "$_GFY_INPUT_ROOT" ]; then
        [ "$_GFY_INPUT_ROOT" = / ] && return 0
        case "$_GFY_DENY_PATH" in "$_GFY_INPUT_ROOT"|"$_GFY_INPUT_ROOT"/*) return 0 ;; esac
    fi
    if [ -n "$_GFY_OUTPUT_ROOT" ]; then
        [ "$_GFY_OUTPUT_ROOT" = / ] && return 0
        case "$_GFY_DENY_PATH" in "$_GFY_OUTPUT_ROOT"|"$_GFY_OUTPUT_ROOT"/*) return 0 ;; esac
    fi
    if [ -n "$_GFY_PERSISTED_ROOT" ]; then
        [ "$_GFY_PERSISTED_ROOT" = / ] && return 0
        case "$_GFY_DENY_PATH" in "$_GFY_PERSISTED_ROOT"|"$_GFY_PERSISTED_ROOT"/*) return 0 ;; esac
    fi
    return 1
}
_gfy_normalize_path() {
    _GFY_RAW=$1
    case "$_GFY_RAW" in
        /*) ;;
        *) return 1 ;;
    esac
    _GFY_DIR=${_GFY_RAW%/*}
    _GFY_BASE=${_GFY_RAW##*/}
    _GFY_DIR=$(cd -P "$_GFY_DIR" 2>/dev/null && pwd) || return 1
    printf '%s/%s\n' "$_GFY_DIR" "$_GFY_BASE"
}
_gfy_capture_command() {
    _GFY_FOUND=$(command -v "$1" 2>/dev/null) || return 1
    case "$_GFY_FOUND" in
        /*) ;;
        *) _GFY_FOUND="$(pwd -P)/$_GFY_FOUND" ;;
    esac
    printf '%s\n' "$_GFY_FOUND"
}
if [ -x /usr/bin/readlink ]; then
    _GFY_READLINK=/usr/bin/readlink
elif [ -x /bin/readlink ]; then
    _GFY_READLINK=/bin/readlink
else
    _GFY_READLINK=""
fi
_gfy_policy_path() {
    # Canonicalize the parent chain before applying policy. A candidate whose
    # leaf is ordinary but whose parent is a symlink must not retain a lexical
    # spelling that hides workspace/input/output containment.
    _GFY_POLICY=$(_gfy_normalize_path "$1") || return 1
    _GFY_LINKS=0
    while [ -L "$_GFY_POLICY" ]; do
        [ "$_GFY_LINKS" -lt 40 ] || return 1
        [ -n "$_GFY_READLINK" ] || return 1
        _GFY_TARGET=$("$_GFY_READLINK" "$_GFY_POLICY" 2>/dev/null) || return 1
        case "$_GFY_TARGET" in
            /*) _GFY_POLICY=$_GFY_TARGET ;;
            *) _GFY_POLICY="${_GFY_POLICY%/*}/$_GFY_TARGET" ;;
        esac
        _GFY_POLICY=$(_gfy_normalize_path "$_GFY_POLICY") || return 1
        _GFY_LINKS=$((_GFY_LINKS + 1))
    done
    printf '%s\n' "$_GFY_POLICY"
}
_gfy_accept_dynamic() {
    _GFY_CANDIDATE=$1
    case "$_GFY_CANDIDATE" in /*) ;; *) return 1 ;; esac
    _GFY_POLICY=$(_gfy_policy_path "$_GFY_CANDIDATE") || return 1
    _gfy_path_denied "$_GFY_CANDIDATE" && return 1
    _gfy_path_denied "$_GFY_POLICY" && return 1
    [ -x "$_GFY_CANDIDATE" ] || return 1
    printf '%s\n' "$_GFY_CANDIDATE"
}
_gfy_dynamic_usable() {
    [ -n "$1" ] && "$1" -E -P -B -S -c "$_GFY_IDENTITY_CHECK" ambient-identity "$_GFY_WORKSPACE" "$_GFY_INPUT_ROOT" "$_GFY_OUTPUT_ROOT" "$_GFY_PERSISTED_ROOT" >/dev/null 2>&1
}

# Resolve via the graphify launcher on PATH. The generated-output interpreter
# pointer is advisory state and is deliberately never a hook input.
if [ -z "$GRAPHIFY_PYTHON" ]; then
    GRAPHIFY_BIN=$(_gfy_capture_command graphify) || GRAPHIFY_BIN=""
    if [ -n "$GRAPHIFY_BIN" ]; then
        GRAPHIFY_BIN=$(_gfy_accept_dynamic "$GRAPHIFY_BIN") || GRAPHIFY_BIN=""
    fi
    if [ -n "$GRAPHIFY_BIN" ]; then
        # Windows pip layout: Scripts/graphify(.exe) sits beside ../python.exe
        # (or ./python.exe inside a venv's Scripts dir).
        _GFY_BINDIR=${GRAPHIFY_BIN%/*}
        _GFY_CANDIDATE=$(_gfy_accept_dynamic "$_GFY_BINDIR/../python.exe") || _GFY_CANDIDATE=""
        if [ -n "$_GFY_CANDIDATE" ] && _gfy_dynamic_usable "$_GFY_CANDIDATE"; then
            GRAPHIFY_PYTHON="$_GFY_CANDIDATE"
        else
            _GFY_CANDIDATE=$(_gfy_accept_dynamic "$_GFY_BINDIR/python.exe") || _GFY_CANDIDATE=""
            if [ -n "$_GFY_CANDIDATE" ] && _gfy_dynamic_usable "$_GFY_CANDIDATE"; then
                GRAPHIFY_PYTHON="$_GFY_CANDIDATE"
            fi
        fi
    fi
    if [ -z "$GRAPHIFY_PYTHON" ] && [ -n "$GRAPHIFY_BIN" ]; then
        # POSIX launcher: parse only a real shebang with the shell's read
        # builtin. This avoids PATH-resolved parsing helpers and avoids putting
        # binary NUL bytes through command substitution when command -v returns
        # a Windows launcher without its .exe suffix.
        case "$GRAPHIFY_BIN" in
            *.exe) _SHEBANG="" ;;
            *) _SHEBANG=""
               IFS= read -r _GFY_FIRST_LINE < "$GRAPHIFY_BIN" || true
               case "$_GFY_FIRST_LINE" in
                   '#'!*) _SHEBANG=${_GFY_FIRST_LINE#??}
                        while :; do
                            case "$_SHEBANG" in
                                [[:space:]]*) _SHEBANG=${_SHEBANG#?} ;;
                                *) break ;;
                            esac
                        done ;;
               esac ;;
        esac
        case "$_SHEBANG" in
            */env\\ *) _GFY_SHEBANG_COMMAND="${_SHEBANG#*/env }" ;;
            *) _GFY_SHEBANG_COMMAND="$_SHEBANG" ;;
        esac
        case "$_GFY_SHEBANG_COMMAND" in
            *[!a-zA-Z0-9/_.@-]*) _GFY_SHEBANG_COMMAND="" ;;
        esac
        case "$_GFY_SHEBANG_COMMAND" in
            /*) _GFY_CANDIDATE=$(_gfy_accept_dynamic "$_GFY_SHEBANG_COMMAND") || _GFY_CANDIDATE="" ;;
            "") _GFY_CANDIDATE="" ;;
            *) _GFY_CANDIDATE=$(_gfy_capture_command "$_GFY_SHEBANG_COMMAND") || _GFY_CANDIDATE=""
               if [ -n "$_GFY_CANDIDATE" ]; then
                   _GFY_CANDIDATE=$(_gfy_accept_dynamic "$_GFY_CANDIDATE") || _GFY_CANDIDATE=""
               fi ;;
        esac
        if [ -n "$_GFY_CANDIDATE" ] && _gfy_dynamic_usable "$_GFY_CANDIDATE"; then
            GRAPHIFY_PYTHON="$_GFY_CANDIDATE"
        fi
    fi
fi

# Last resort: resolve python3 / python from PATH before the first execution.
if [ -z "$GRAPHIFY_PYTHON" ]; then
    _GFY_CANDIDATE=$(_gfy_capture_command python3) || _GFY_CANDIDATE=""
    if [ -n "$_GFY_CANDIDATE" ]; then
        _GFY_CANDIDATE=$(_gfy_accept_dynamic "$_GFY_CANDIDATE") || _GFY_CANDIDATE=""
    fi
    if [ -n "$_GFY_CANDIDATE" ] && _gfy_dynamic_usable "$_GFY_CANDIDATE"; then
        GRAPHIFY_PYTHON="$_GFY_CANDIDATE"
    else
        _GFY_CANDIDATE=$(_gfy_capture_command python) || _GFY_CANDIDATE=""
        if [ -n "$_GFY_CANDIDATE" ]; then
            _GFY_CANDIDATE=$(_gfy_accept_dynamic "$_GFY_CANDIDATE") || _GFY_CANDIDATE=""
        fi
        if [ -n "$_GFY_CANDIDATE" ] && _gfy_dynamic_usable "$_GFY_CANDIDATE"; then
            GRAPHIFY_PYTHON="$_GFY_CANDIDATE"
        else
            echo "[graphify hook] could not locate a trusted final CPython 3.14.2+ with graphify installed. Re-run 'graphify hook install' from the environment where graphify lives." >&2
            exit 0
        fi
    fi
fi
"""

# The Python that the rebuild runs, shared by both hooks. Embedded verbatim into
# the launcher below and re-executed in the detached child. Must not contain the
# double-quote, $, backtick or backslash characters: it is carried inside a
# shell double-quoted `-c "..."` argument (see _detached_launch).
_REBUILD_BODY_COMMIT = """\
import os, signal, sys
from pathlib import Path

changed_raw = os.environ.get('GRAPHIFY_CHANGED', '')
changed = [Path(f.strip()) for f in changed_raw.strip().splitlines() if f.strip()]

if not changed:
    sys.exit(0)

print(f'[graphify hook] {len(changed)} file(s) changed - rebuilding graph...')

try:
    from graphify.watch import _rebuild_code, _apply_resource_limits
    _apply_resource_limits()
    _timeout = int(os.environ.get('GRAPHIFY_REBUILD_TIMEOUT', '600'))
    if _timeout > 0 and hasattr(signal, 'SIGALRM'):
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError(f'graphify rebuild exceeded {_timeout}s')))
        signal.alarm(_timeout)
    _force = os.environ.get('GRAPHIFY_FORCE', '').lower() in ('1', 'true', 'yes')
    _root = Path('.')
    _out = os.environ.get('GRAPHIFY_OUT', 'graphify-out')
    _saved = Path(_out) / '.graphify_root'
    if _saved.exists():
        _txt = _saved.read_text(encoding='utf-8').strip()
        if _txt:
            _root = Path(_txt)
    _rebuild_code(_root, changed_paths=changed, force=_force)
    # Refresh the work-memory lessons doc when saved Q&A outcomes exist
    # (best-effort; never fails the hook).
    try:
        _md = (_root / _out) / 'memory'
        if _md.is_dir() and any(_md.glob('*.md')):
            from graphify.reflect import reflect as _reflect
            _gj = (_root / _out) / 'graph.json'
            _reflect(memory_dir=_md, out_path=(_root / _out) / 'reflections' / 'LESSONS.md',
                     graph_path=_gj if _gj.exists() else None)
    except Exception:
        pass
except TimeoutError as exc:
    print(f'[graphify hook] {exc}')
    sys.exit(1)
except Exception as exc:
    print(f'[graphify hook] Rebuild failed: {exc}')
    sys.exit(1)
"""

_REBUILD_BODY_CHECKOUT = """\
from graphify.watch import _rebuild_code, _apply_resource_limits
from pathlib import Path
import os, signal, sys
try:
    _apply_resource_limits()
    _timeout = int(os.environ.get('GRAPHIFY_REBUILD_TIMEOUT', '600'))
    if _timeout > 0 and hasattr(signal, 'SIGALRM'):
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError(f'graphify rebuild exceeded {_timeout}s')))
        signal.alarm(_timeout)
    _force = os.environ.get('GRAPHIFY_FORCE', '').lower() in ('1', 'true', 'yes')
    # post-checkout: branch switch can touch arbitrary files; full rebuild path
    # (no changed_paths) is correct here. The flock inside _rebuild_code still
    # prevents pile-ups when commit + checkout fire back-to-back.
    _root = Path('.')
    _out = os.environ.get('GRAPHIFY_OUT', 'graphify-out')
    _saved = Path(_out) / '.graphify_root'
    if _saved.exists():
        _txt = _saved.read_text(encoding='utf-8').strip()
        if _txt:
            _root = Path(_txt)
    _rebuild_code(_root, force=_force)
    # Refresh the work-memory lessons doc when saved Q&A outcomes exist
    # (best-effort; never fails the hook).
    try:
        _md = (_root / _out) / 'memory'
        if _md.is_dir() and any(_md.glob('*.md')):
            from graphify.reflect import reflect as _reflect
            _gj = (_root / _out) / 'graph.json'
            _reflect(memory_dir=_md, out_path=(_root / _out) / 'reflections' / 'LESSONS.md',
                     graph_path=_gj if _gj.exists() else None)
    except Exception:
        pass
except TimeoutError as exc:
    print(f'[graphify] {exc}')
    sys.exit(1)
except Exception as exc:
    print(f'[graphify] Rebuild failed: {exc}')
    sys.exit(1)
"""

# Cross-platform detached-launch shim (#1161). The hooks used to background the
# rebuild with `nohup "$GRAPHIFY_PYTHON" -c "..." &`, but Git for Windows' bundled
# MSYS shell ships no nohup (nor setsid), so that line died with
# 'nohup: command not found' and the rebuild silently never ran — git commit/pull
# still returned 0, so the graph just went stale with no signal. graphify already
# requires Python, so we let Python do the detaching: a tiny outer process spawns
# the real rebuild fully detached and returns immediately, so the hook never
# blocks. POSIX uses start_new_session (the setsid equivalent); Windows uses
# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, breaking away from any job object
# when allowed. This payload is carried inside a shell double-quoted -c argument,
# so it deliberately uses only single-quoted Python strings (no ", $, ` or \\).
_LAUNCHER_TEMPLATE = """\
import os, subprocess, sys
_src = '''
__REBUILD_BODY__
'''
_log = os.environ.get('GRAPHIFY_REBUILD_LOG') or os.path.join(os.path.expanduser('~'), '.cache', 'graphify-rebuild.log')
try:
    os.makedirs(os.path.dirname(_log), exist_ok=True)
    _out = open(_log, 'a', buffering=1, encoding='utf-8', errors='replace')
except OSError:
    _out = subprocess.DEVNULL
_kw = dict(stdout=_out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=os.getcwd(), close_fds=True)
_cmd = [sys.executable, '-E', '-P', '-B', '-c', _src]
if os.name == 'nt':
    _flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(_cmd, creationflags=_flags | 0x01000000, **_kw)  # + CREATE_BREAKAWAY_FROM_JOB
    except OSError:
        subprocess.Popen(_cmd, creationflags=_flags, **_kw)
else:
    subprocess.Popen(_cmd, start_new_session=True, **_kw)
"""


def _detached_launch(rebuild_body: str) -> str:
    """Return a POSIX-sh line that runs ``rebuild_body`` as a detached background
    Python process via ``$GRAPHIFY_PYTHON``.

    Replaces the old ``nohup ... &`` form, which failed on Git for Windows'
    shell (no nohup/setsid) and let the rebuild silently never run (#1161).
    The launcher writes the child's output to ``$GRAPHIFY_REBUILD_LOG`` and
    returns the instant the child is spawned, so the git hook never blocks.
    """
    launcher = _LAUNCHER_TEMPLATE.replace("__REBUILD_BODY__", rebuild_body)
    return '"$GRAPHIFY_PYTHON" -E -P -B -c "' + launcher + '"\n'


# Skip the rebuild inside a linked worktree (git worktree add), shared by both
# hooks. With core.hooksPath shared across worktrees a commit in any worktree
# fires these hooks; the canonical graphify-out/ belongs to the primary checkout,
# so rebuilding from a worktree is wasteful, writes a rogue delta-only graph the
# user never asked for, and races deploy/CI `git clean` against the detached
# rebuild ("failed to remove graphify-out/: Directory not empty") (#1809, #1806).
# A linked worktree has git-dir != git-common-dir. Both are resolved to absolute
# via `cd ... && pwd` before comparing: git's exported GIT_DIR / --git-dir can be
# absolute while --git-common-dir is the relative ".git", and a raw compare would
# false-positive on the PRIMARY checkout and wrongly skip it.
_WORKTREE_GUARD = """\
_GFY_GITDIR=$(cd "$(git rev-parse --git-dir 2>/dev/null)" 2>/dev/null && pwd)
_GFY_COMMONDIR=$(cd "$(git rev-parse --git-common-dir 2>/dev/null)" 2>/dev/null && pwd)
if [ -n "$_GFY_COMMONDIR" ] && [ "$_GFY_GITDIR" != "$_GFY_COMMONDIR" ]; then
    exit 0
fi
"""


_HOOK_SCRIPT = """\
# graphify-hook-start
# Auto-rebuilds the knowledge graph after each commit (code files only, no LLM needed).
# Installed by: graphify hook install

# Deterministic clustering: networkx louvain iterates string-keyed sets whose
# order is randomized per-process by PYTHONHASHSEED, so community assignments
# churn run-to-run. Pinning it makes graphify-out reproducible.
export PYTHONHASHSEED=0

# Git for Windows/MSYS hooks can inherit fragile pipe handles from GUI clients
# and agent shells. Keep hook-triggered rebuilds sequential by default there;
# explicit GRAPHIFY_MAX_WORKERS still wins for users who want parallelism.
if [ -n "${WINDIR:-}" ] || [ -n "${MSYSTEM:-}" ]; then
    export GRAPHIFY_MAX_WORKERS="${GRAPHIFY_MAX_WORKERS:-1}"
fi

# Skip during rebase/merge/cherry-pick to avoid blocking --continue with unstaged changes
# git exports GIT_DIR to hooks; the rev-parse fallback only runs when invoked by
# hand (each git exec costs 1s+ on AV-scanned Windows machines).
GIT_DIR=${GIT_DIR:-$(git rev-parse --git-dir 2>/dev/null)}
[ -d "$GIT_DIR/rebase-merge" ] && exit 0
[ -d "$GIT_DIR/rebase-apply" ] && exit 0
[ -f "$GIT_DIR/MERGE_HEAD" ] && exit 0
[ -f "$GIT_DIR/CHERRY_PICK_HEAD" ] && exit 0

[ "${GRAPHIFY_SKIP_HOOK:-0}" = "1" ] && exit 0

""" + _WORKTREE_GUARD + """
CHANGED=$(git diff --name-only HEAD~1 HEAD 2>/dev/null || git diff --name-only HEAD 2>/dev/null)
if [ -z "$CHANGED" ]; then
    exit 0
fi

# Skip when only graphify-out/ artifacts changed (avoids rebuild loop when graph outputs are tracked in git)
_NON_GRAPH=$(echo "$CHANGED" | grep -v '^graphify-out/' || true)
if [ -z "$_NON_GRAPH" ]; then
    exit 0
fi

""" + _PYTHON_DETECT + """
export GRAPHIFY_CHANGED="$CHANGED"

# Run the rebuild detached so git commit returns immediately. Full-repo rebuilds
# can take hours; blocking the post-commit hook stalls the shell. The Python
# launcher below detaches the child cross-platform, so it works on Git for
# Windows' shell too (which lacks the coreutils backgrounding tools) (#1161).
_GRAPHIFY_LOG="${HOME}/.cache/graphify-rebuild.log"
mkdir -p "$(dirname "$_GRAPHIFY_LOG")"
export GRAPHIFY_REBUILD_LOG="$_GRAPHIFY_LOG"
echo "[graphify hook] launching background rebuild (log: $_GRAPHIFY_LOG)"
""" + _detached_launch(_REBUILD_BODY_COMMIT) + """# graphify-hook-end
"""


_CHECKOUT_SCRIPT = """\
# graphify-checkout-hook-start
# Auto-rebuilds the knowledge graph (code only) when switching branches.
# Installed by: graphify hook install

# Deterministic clustering: networkx louvain iterates string-keyed sets whose
# order is randomized per-process by PYTHONHASHSEED, so community assignments
# churn run-to-run. Pinning it makes graphify-out reproducible.
export PYTHONHASHSEED=0

# Git for Windows/MSYS hooks can inherit fragile pipe handles from GUI clients
# and agent shells. Keep hook-triggered rebuilds sequential by default there;
# explicit GRAPHIFY_MAX_WORKERS still wins for users who want parallelism.
if [ -n "${WINDIR:-}" ] || [ -n "${MSYSTEM:-}" ]; then
    export GRAPHIFY_MAX_WORKERS="${GRAPHIFY_MAX_WORKERS:-1}"
fi

PREV_HEAD=$1
NEW_HEAD=$2
BRANCH_SWITCH=$3

# Only run on branch switches, not file checkouts
if [ "$BRANCH_SWITCH" != "1" ]; then
    exit 0
fi

# Only run if graphify-out/ exists (graph has been built before)
if [ ! -d "graphify-out" ]; then
    exit 0
fi

# Skip during rebase/merge/cherry-pick
# git exports GIT_DIR to hooks; the rev-parse fallback only runs when invoked by
# hand (each git exec costs 1s+ on AV-scanned Windows machines).
GIT_DIR=${GIT_DIR:-$(git rev-parse --git-dir 2>/dev/null)}
[ -d "$GIT_DIR/rebase-merge" ] && exit 0
[ -d "$GIT_DIR/rebase-apply" ] && exit 0
[ -f "$GIT_DIR/MERGE_HEAD" ] && exit 0
[ -f "$GIT_DIR/CHERRY_PICK_HEAD" ] && exit 0

# Honor the same opt-out as post-commit: without this, GRAPHIFY_SKIP_HOOK=1
# suppressed commit-triggered rebuilds but not branch-switch ones (#1809).
[ "${GRAPHIFY_SKIP_HOOK:-0}" = "1" ] && exit 0

""" + _WORKTREE_GUARD + _PYTHON_DETECT + """
_GRAPHIFY_LOG="${HOME}/.cache/graphify-rebuild.log"
mkdir -p "$(dirname "$_GRAPHIFY_LOG")"
export GRAPHIFY_REBUILD_LOG="$_GRAPHIFY_LOG"
echo "[graphify] Branch switched - launching background rebuild (log: $_GRAPHIFY_LOG)"
""" + _detached_launch(_REBUILD_BODY_CHECKOUT) + """# graphify-checkout-hook-end
"""


def _git_root(path: Path) -> Path | None:
    """Walk up to find .git directory."""
    current = path.resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return None


_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _reject_windows_path(value: str, source: str) -> None:
    """Raise if a hooks path looks like a Windows absolute path (#1385).

    On POSIX/WSL ``Path("C:\\Users\\...").is_absolute()`` is False, so an absolute
    Windows hooks path gets joined under the repo root and mkdir'd as a literal
    junk directory (backslashes and all), while install reports success and the
    real ``.git/hooks`` gets nothing. Fail loudly instead so the user can fix it.
    """
    if os.name == "nt":
        return
    if _WINDOWS_DRIVE_RE.match(value) or "\\" in value:
        raise RuntimeError(
            f"git hooks path from {source} looks like a Windows path: {value!r}. "
            f"On WSL/POSIX this can't resolve to a real directory. Unset it with "
            f"`git config --local --unset core.hooksPath`, or set a POSIX path."
        )


def _hooks_dir(root: Path) -> Path:
    """Return the git hooks directory, respecting core.hooksPath if set (e.g. Husky).

    Asks git itself via ``rev-parse --git-path hooks`` rather than parsing
    ``.git/config`` with configparser: git legally allows duplicate keys and
    sections (VS Code writes such configs), which a strict configparser rejects
    with DuplicateOptionError/DuplicateSectionError, so every hook command
    printed a spurious "could not read core.hooksPath" warning (#1907). git
    resolves core.hooksPath, includeIf, and linked worktrees (where .git is a
    file, not a directory) correctly in one place. Genuinely corrupt configs
    are still surfaced: git itself fails on them, and its stderr is printed.
    """
    # NOTE: do NOT pass --path-format=absolute — added in git 2.31; older git
    # echoes it back as a literal argument, contaminating stdout and causing a
    # phantom directory to be created (#907). git -C <root> already returns an
    # absolute path for worktree/external-gitdir cases, and a path relative to
    # <root> for normal repos — anchoring on root covers both.
    import subprocess as _sp
    try:
        res = _sp.run(
            ["git", "-C", str(root), "rev-parse", "--git-path", "hooks"],
            capture_output=True, text=True,
        )
        if res.returncode != 0:
            # git failing here is a real signal (corrupt .git/config, tampering,
            # permission flips by another tool). Surface git's own stderr rather
            # than silently falling through to the default hooks directory.
            err = (res.stderr or "").strip()
            print(
                f"[graphify hooks] git could not resolve the hooks path for "
                f"{root}: {err or f'git exited with code {res.returncode}'}",
                file=sys.stderr,
            )
        else:
            raw = res.stdout.strip()
            # A valid hooks path can never contain newlines or NUL. Their presence
            # means git echoed an unrecognised flag back (old git behaviour).
            if raw and not any(c in raw for c in ("\n", "\r", "\x00")):
                _reject_windows_path(raw, "git rev-parse --git-path hooks")
                d = (root / raw).resolve()
                d.mkdir(parents=True, exist_ok=True)
                return d
    except (OSError, FileNotFoundError):
        pass
    d = root / ".git" / "hooks"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass(frozen=True)
class _HookInstallPlan:
    """A read-only decision for one hook installation."""

    hook_path: Path
    message: str
    text: str | None = None
    data: bytes | None = None
    create: bool = False


def _standalone_marker_spans(content: bytes, marker: str) -> list[tuple[int, int]]:
    """Return spans for exact ASCII marker lines, including their line ending."""
    marker_bytes = re.escape(marker.encode("ascii"))
    pattern = re.compile(rb"^" + marker_bytes + rb"(?:\r\n|\n|\Z)", re.MULTILINE)
    return [match.span() for match in pattern.finditer(content)]


def _owned_hook_span(
    content: bytes,
    marker: str,
    marker_end: str,
    context: str,
) -> tuple[int, int] | None:
    """Classify one exact standalone marker pair, failing closed if malformed."""
    starts = _standalone_marker_spans(content, marker)
    ends = _standalone_marker_spans(content, marker_end)
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or starts[0][0] >= ends[0][0]:
        raise RuntimeError(f"Malformed Graphify marker section in {context}")
    return starts[0][0], ends[0][1]


def _prepare_hook_install(
    hooks_dir: Path,
    name: str,
    script: str,
    marker: str,
    marker_end: str,
) -> _HookInstallPlan:
    """Prepare one hook installation without mutating the hook."""
    hook_path = hooks_dir / name
    if hook_path.exists():
        raw = hook_path.read_bytes()
        owned = _owned_hook_span(raw, marker, marker_end, f"{name} hook at {hook_path}")
        if owned is None:
            # Preserve the established append behavior, including UTF-8
            # validation, trailing-whitespace trimming, and LF output.
            content = hook_path.read_text(encoding="utf-8")
            return _HookInstallPlan(
                hook_path,
                f"appended to existing {name} hook at {hook_path}",
                text=content.rstrip() + "\n\n" + script,
            )

        owned_start, owned_end = owned
        rendered = script.encode("utf-8")
        if raw[owned_start:owned_end] == rendered:
            return _HookInstallPlan(
                hook_path,
                f"already installed at {hook_path}",
            )
        return _HookInstallPlan(
            hook_path,
            f"updated {name} hook at {hook_path}",
            data=raw[:owned_start] + rendered + raw[owned_end:],
        )

    return _HookInstallPlan(
        hook_path,
        f"installed at {hook_path}",
        text="#!/bin/sh\n" + script,
        create=True,
    )


def _apply_hook_install(plan: _HookInstallPlan) -> str:
    """Apply a previously prepared hook installation plan."""
    if plan.data is not None:
        plan.hook_path.write_bytes(plan.data)
    elif plan.text is not None:
        plan.hook_path.write_text(plan.text, encoding="utf-8", newline="\n")
    if plan.create:
        plan.hook_path.chmod(0o755)
    return plan.message


@dataclass(frozen=True)
class _HookUninstallPlan:
    """A read-only decision for one hook removal."""

    hook_path: Path
    message: str
    data: bytes | None = None
    delete: bool = False


def _prepare_hook_uninstall(
    hooks_dir: Path,
    name: str,
    marker: str,
    marker_end: str,
) -> _HookUninstallPlan:
    """Prepare removal of one exact owned hook interval without mutating it."""
    hook_path = hooks_dir / name
    if not hook_path.exists():
        return _HookUninstallPlan(
            hook_path,
            f"no {name} hook found - nothing to remove.",
        )

    raw = hook_path.read_bytes()
    owned = _owned_hook_span(raw, marker, marker_end, f"{name} hook at {hook_path}")
    if owned is None:
        return _HookUninstallPlan(
            hook_path,
            f"graphify hook not found in {name} - nothing to remove.",
        )

    owned_start, owned_end = owned
    remaining = raw[:owned_start] + raw[owned_end:]
    if remaining.strip() in (b"", b"#!/bin/bash", b"#!/bin/sh"):
        return _HookUninstallPlan(
            hook_path,
            f"removed {name} hook at {hook_path}",
            delete=True,
        )
    return _HookUninstallPlan(
        hook_path,
        f"graphify removed from {name} at {hook_path} (other hook content preserved)",
        data=remaining,
    )


def _apply_hook_uninstall(plan: _HookUninstallPlan) -> str:
    """Apply a previously prepared hook removal plan."""
    if plan.delete:
        plan.hook_path.unlink()
    elif plan.data is not None:
        plan.hook_path.write_bytes(plan.data)
    return plan.message


def _pinned_python() -> str:
    """Return an absolute ``sys.executable``, preserving its exact path text."""
    if not sys.executable or "\x00" in sys.executable:
        return ""
    if not Path(sys.executable).is_absolute() and not _WINDOWS_DRIVE_RE.match(sys.executable):
        return ""
    return sys.executable


def _merge_attr_line() -> str:
    """The .gitattributes line assigning the graphify merge driver to graph.json.

    The graph lives under the configured output directory (graphify.paths,
    GRAPHIFY_OUT env override). gitattributes patterns are repo-relative, so an
    absolute output-dir override cannot be expressed there — fall back to the
    default name in that case.
    """
    from graphify.paths import GRAPHIFY_OUT
    out = GRAPHIFY_OUT
    if not out or Path(out).is_absolute() or "\\" in out:
        out = "graphify-out"
    return f"{out.rstrip('/')}/graph.json merge=graphify"


def _has_merge_attr(content: str) -> bool:
    """True if a (non-comment) `<...>graph.json ... merge=graphify` line exists."""
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if fields and fields[0].endswith("graph.json") and "merge=graphify" in fields[1:]:
            return True
    return False


def _register_merge_driver(root: Path) -> str:
    """Register the graph.json union merge driver in git config + .gitattributes (#1902).

    README and CHANGELOG 0.7.0 document `graphify merge-driver` as being set up
    by `hook install`, but install never actually registered it. Writes go
    through `git config` (never hand-edit .git/config — in a linked worktree the
    effective config is not at root/.git/config). The interpreter is pinned the
    same way the hook scripts pin it, so the driver works even when the graphify
    launcher is not on PATH at merge time.
    """
    import subprocess as _sp
    pinned = _pinned_python()
    if pinned:
        # Git expands %O/%A/%B anywhere in the configured driver string,
        # including inside a quoted executable path. Keep a quote boundary
        # between each literal percent and the following path character; POSIX
        # shell concatenation reconstructs one exact token without executing a
        # command, while only the three placeholders below remain visible.
        percent = "'%'"
        executable = percent.join(shlex.quote(part) for part in pinned.split("%"))
        driver = (
            "unset -f printf 2>/dev/null; "
            f"{executable} -E -P -B -m graphify merge-driver %O %A %B"
        )
    else:
        driver = "graphify merge-driver %O %A %B"
    try:
        for key, value in (
            ("merge.graphify.name", "graphify graph.json union merge"),
            ("merge.graphify.driver", driver),
        ):
            _sp.run(
                ["git", "-C", str(root), "config", key, value],
                check=True, capture_output=True, text=True,
            )
    except (OSError, _sp.CalledProcessError) as exc:
        return f"not registered (git config failed: {exc})"

    line = _merge_attr_line()
    attrs = root / ".gitattributes"
    if attrs.exists():
        content = attrs.read_text(encoding="utf-8")
        if _has_merge_attr(content):
            return f"already registered ({line})"
        # Never clobber other entries; preserve a trailing newline.
        if content and not content.endswith("\n"):
            content += "\n"
        attrs.write_text(content + line + "\n", encoding="utf-8", newline="\n")
    else:
        attrs.write_text(line + "\n", encoding="utf-8", newline="\n")
    return f"registered ({line})"


def _unregister_merge_driver(root: Path) -> str:
    """Remove the merge-driver git config keys and the .gitattributes line."""
    import subprocess as _sp
    for key in ("merge.graphify.name", "merge.graphify.driver"):
        try:
            # --unset exits nonzero if the key is absent; that is fine.
            _sp.run(
                ["git", "-C", str(root), "config", "--unset", key],
                capture_output=True, text=True,
            )
        except OSError:
            pass
    attrs = root / ".gitattributes"
    if not attrs.exists():
        return "not registered - nothing to remove."
    content = attrs.read_text(encoding="utf-8")
    kept = [
        raw for raw in content.splitlines()
        if not _has_merge_attr(raw)
    ]
    if kept == content.splitlines():
        return "gitattributes entry not found - nothing to remove."
    if kept:
        # Other entries survive; the file stays.
        attrs.write_text("\n".join(kept) + "\n", encoding="utf-8", newline="\n")
        return "removed from .gitattributes (other entries preserved)"
    attrs.unlink()
    return "removed (.gitattributes deleted - no other entries)"


def _merge_driver_status(root: Path) -> str:
    """Report whether the merge driver is registered (config + gitattributes)."""
    import subprocess as _sp
    try:
        res = _sp.run(
            ["git", "-C", str(root), "config", "--get", "merge.graphify.driver"],
            capture_output=True, text=True,
        )
        cfg_ok = res.returncode == 0 and bool(res.stdout.strip())
    except OSError:
        cfg_ok = False
    attrs = root / ".gitattributes"
    attr_ok = attrs.exists() and _has_merge_attr(attrs.read_text(encoding="utf-8"))
    if cfg_ok and attr_ok:
        return "registered"
    if cfg_ok:
        return "partially registered (git config set, .gitattributes line missing)"
    if attr_ok:
        return "partially registered (.gitattributes line set, git config missing)"
    return "not registered"


def _user_hooks_dir(hooks_dir: Path) -> Path:
    """Return the user-editable hooks directory.

    Husky 9 sets core.hooksPath to .husky/_ (wrapper scripts auto-generated by
    Husky), while user-editable hooks live in the parent .husky/. Return the
    parent when the resolved dir ends in '_' so install/status/uninstall target
    the correct location (#987).
    """
    if hooks_dir.name == "_":
        return hooks_dir.parent
    return hooks_dir


def install(path: Path = Path(".")) -> str:
    """Install graphify post-commit and post-checkout hooks in the nearest git repo."""
    root = _git_root(path)
    if root is None:
        raise RuntimeError(f"No git repository found at or above {path.resolve()}")

    hooks_dir = _user_hooks_dir(_hooks_dir(root))

    # Pin the current interpreter so the hook works even when the graphify
    # launcher is not on PATH at git-trigger time (uv tool / pipx isolation).
    # sys.executable is the Python running this very install command, so it is
    # always the correct isolated-venv interpreter.  The placeholder is replaced
    # in both scripts before writing. Quote the complete executable token rather
    # than rejecting valid path punctuation; import verification catches a stale
    # pin so it safely falls through to dynamic detection.
    pinned = _pinned_python()
    quoted_pinned = shlex.quote(pinned)
    hook = _HOOK_SCRIPT.replace("__PINNED_PYTHON__", quoted_pinned)
    checkout = _CHECKOUT_SCRIPT.replace("__PINNED_PYTHON__", quoted_pinned)

    # Prepare both hooks before applying either so deterministic malformed
    # ownership in one hook cannot leave the other partially upgraded.
    commit_plan = _prepare_hook_install(
        hooks_dir, "post-commit", hook, _HOOK_MARKER, _HOOK_MARKER_END
    )
    checkout_plan = _prepare_hook_install(
        hooks_dir,
        "post-checkout",
        checkout,
        _CHECKOUT_MARKER,
        _CHECKOUT_MARKER_END,
    )
    commit_msg = _apply_hook_install(commit_plan)
    checkout_msg = _apply_hook_install(checkout_plan)
    merge_msg = _register_merge_driver(root)

    return f"post-commit: {commit_msg}\npost-checkout: {checkout_msg}\nmerge driver: {merge_msg}"


def uninstall(path: Path = Path(".")) -> str:
    """Remove graphify post-commit and post-checkout hooks."""
    root = _git_root(path)
    if root is None:
        raise RuntimeError(f"No git repository found at or above {path.resolve()}")

    hooks_dir = _user_hooks_dir(_hooks_dir(root))
    commit_plan = _prepare_hook_uninstall(
        hooks_dir, "post-commit", _HOOK_MARKER, _HOOK_MARKER_END
    )
    checkout_plan = _prepare_hook_uninstall(
        hooks_dir,
        "post-checkout",
        _CHECKOUT_MARKER,
        _CHECKOUT_MARKER_END,
    )
    commit_msg = _apply_hook_uninstall(commit_plan)
    checkout_msg = _apply_hook_uninstall(checkout_plan)
    merge_msg = _unregister_merge_driver(root)

    return f"post-commit: {commit_msg}\npost-checkout: {checkout_msg}\nmerge driver: {merge_msg}"


def status(path: Path = Path(".")) -> str:
    """Check if graphify hooks are installed."""
    root = _git_root(path)
    if root is None:
        return "Not in a git repository."
    hooks_dir = _user_hooks_dir(_hooks_dir(root))

    def _check(name: str, marker: str, marker_end: str) -> str:
        p = hooks_dir / name
        if not p.exists():
            return "not installed"
        try:
            owned = _owned_hook_span(
                p.read_bytes(), marker, marker_end, f"{name} hook at {p}"
            )
        except RuntimeError:
            return "not installed (malformed Graphify markers)"
        if owned is not None:
            return "installed"
        return "not installed (hook exists but graphify not found)"

    commit = _check("post-commit", _HOOK_MARKER, _HOOK_MARKER_END)
    checkout = _check("post-checkout", _CHECKOUT_MARKER, _CHECKOUT_MARKER_END)
    merge = _merge_driver_status(root)
    return f"post-commit: {commit}\npost-checkout: {checkout}\nmerge driver: {merge}"
