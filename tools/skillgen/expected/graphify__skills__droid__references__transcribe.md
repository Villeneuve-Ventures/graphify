# graphify reference: transcribe video and audio

Load this only when `detect` reported one or more `video` files. A corpus with no video never reads this.

### Step 2.5 - Transcribe video / audio files (only if video files detected)

Skip this step entirely if `detect` returned zero `video` files.

Video and audio files cannot be read directly. Transcribe them to text first, then treat the transcripts as doc files in Step 3.

**Strategy:** Read the god nodes from `graphify-out/.graphify_detect.json` (or the analysis file if it exists from a previous run). You are already a language model — write a one-sentence domain hint yourself from those labels. Then pass it to Whisper as the initial prompt. No separate API call needed.

**However**, if the corpus has *only* video files and no other docs/code, use the generic fallback prompt: `"Use proper punctuation and paragraph breaks."`

**Step 1 - Write the Whisper prompt yourself.**

Read the top god node labels from detect output or analysis, then compose a short domain hint sentence, for example:

- Labels: `transformer, attention, encoder, decoder` → `"Machine learning research on transformer architectures and attention mechanisms. Use proper punctuation and paragraph breaks."`
- Labels: `kubernetes, deployment, pod, helm` → `"DevOps discussion about Kubernetes deployments and Helm charts. Use proper punctuation and paragraph breaks."`

**Export** it as `GRAPHIFY_WHISPER_PROMPT` (the exact name the transcriber reads — and it must be `export`ed so the child Python process sees it) for the next command.

**Step 2 - Transcribe:**

```bash
eval "$(printf '%b' 'GRAPHIFY_PYTHON=""\n_GRAPHIFY_VERSION_CHECK='"'"'import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)'"'"'\n_GRAPHIFY_WORKSPACE=$(/bin/pwd -P) || exit 1\n_graphify_canonical_root() {\n    _gfy_root=$1; [ -n "$_gfy_root" ] || return 1\n    case "$_gfy_root" in /*) ;; *) _gfy_root=$_GRAPHIFY_WORKSPACE/$_gfy_root ;; esac\n    [ -d "$_gfy_root" ] && CDPATH= cd -P -- "$_gfy_root" 2>/dev/null && /bin/pwd -P\n}\n_GRAPHIFY_INPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_INPUT_PATH-}") || _GRAPHIFY_INPUT_ROOT=""; _GRAPHIFY_OUTPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_OUTPUT_ROOT-${GRAPHIFY_OUT-graphify-out}}") || _GRAPHIFY_OUTPUT_ROOT=""\n_graphify_path_denied() { _gfy_policy_path=$1; [ "$_GRAPHIFY_WORKSPACE" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_WORKSPACE"|"$_GRAPHIFY_WORKSPACE"/*) return 0 ;; esac; [ -z "$_GRAPHIFY_INPUT_ROOT" ] || { [ "$_GRAPHIFY_INPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_INPUT_ROOT"|"$_GRAPHIFY_INPUT_ROOT"/*) return 0 ;; esac; }; [ -z "$_GRAPHIFY_OUTPUT_ROOT" ] || { [ "$_GRAPHIFY_OUTPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_OUTPUT_ROOT"|"$_GRAPHIFY_OUTPUT_ROOT"/*) return 0 ;; esac; }; return 1; }\n_graphify_resolve_ambient() {\n    _gfy_lexical=$1; case "$_gfy_lexical" in /*) ;; *) return 1 ;; esac\n    _graphify_path_denied "$_gfy_lexical" && return 1; _gfy_path=$_gfy_lexical\n    _gfy_links=0\n    while [ -L "$_gfy_path" ]; do\n        _gfy_links=$((_gfy_links + 1))\n        [ "$_gfy_links" -le 40 ] || return 1\n        [ -x /usr/bin/readlink ] || return 1\n        _gfy_link=$(/usr/bin/readlink "$_gfy_path") || return 1\n        case "$_gfy_link" in\n            /*) _gfy_path=$_gfy_link ;;\n            *) _gfy_dir=${_gfy_path%/*}; _gfy_path=$_gfy_dir/$_gfy_link ;;\n        esac\n    done\n    _gfy_dir=${_gfy_path%/*}; _gfy_base=${_gfy_path##*/}\n    _gfy_dir=$(CDPATH= cd -P -- "$_gfy_dir" 2>/dev/null && /bin/pwd -P) || return 1\n    _gfy_path=$_gfy_dir/$_gfy_base\n    _graphify_path_denied "$_gfy_path" && return 1\n    [ -x "$_gfy_lexical" ] || return 1\n    GRAPHIFY_RESOLVED=$_gfy_lexical\n}\n_graphify_command() {\n    _gfy_found=$(command -v "$1" 2>/dev/null) || return 1\n    _graphify_resolve_ambient "$_gfy_found"\n}\n_graphify_supported() {\n    [ -n "$1" ] && "$1" -E -P -B -c "$_GRAPHIFY_VERSION_CHECK" >/dev/null 2>&1\n}\n_graphify_usable() {\n    _graphify_supported "$1" && "$1" -E -P -B -c '"'"'import graphify'"'"' >/dev/null 2>&1\n}\n# An explicit absolute active environment is caller-selected, including a\n# project-local venv. Keep its lexical path for invocation.\ncase "${VIRTUAL_ENV-}" in\n    /*) _gfy_venv_python=$VIRTUAL_ENV/bin/python\n        _graphify_usable "$_gfy_venv_python" && GRAPHIFY_PYTHON=$_gfy_venv_python ;;\nesac\n# Trusted uv and pipx metadata, then trusted candidates derived from it.\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command uv; then\n    _gfy_uv=$GRAPHIFY_RESOLVED\n    _gfy_uv_dir=$("$_gfy_uv" tool dir 2>/dev/null)\n    _gfy_candidate=${_gfy_uv_dir:+$_gfy_uv_dir/graphifyy/bin/python}\n    if _graphify_resolve_ambient "$_gfy_candidate"; then\n        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED\n    fi\nfi\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command pipx; then\n    _gfy_pipx=$GRAPHIFY_RESOLVED\n    _gfy_pipx_home=$("$_gfy_pipx" environment --value PIPX_LOCAL_VENVS 2>/dev/null)\n    _gfy_candidate=${_gfy_pipx_home:+$_gfy_pipx_home/graphifyy/bin/python}\n    if _graphify_resolve_ambient "$_gfy_candidate"; then\n        _graphify_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED\n    fi\nfi\n# Console-script shebang covers direct and pipx installs without executing the launcher.\nif [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command graphify; then\n    _gfy_graphify=$GRAPHIFY_RESOLVED\n    IFS= read -r _gfy_shebang < "$_gfy_graphify" || _gfy_shebang=""\n    _gfy_shebang=${_gfy_shebang#\\#!}\n    case "$_gfy_shebang" in\n        "/usr/bin/env "*)\n            _gfy_env_command=${_gfy_shebang#"/usr/bin/env "}\n            case "$_gfy_env_command" in\n                ""|*[!a-zA-Z0-9_.@+-]*) _gfy_shebang="" ;;\n                *) if _graphify_command "$_gfy_env_command"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;\n            esac ;;\n        *[!a-zA-Z0-9/_.@+-]*) _gfy_shebang="" ;;\n        *) if _graphify_resolve_ambient "$_gfy_shebang"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;;\n    esac\n    _graphify_usable "$_gfy_shebang" && GRAPHIFY_PYTHON=$_gfy_shebang\nfi\nif [ -z "$GRAPHIFY_PYTHON" ]; then\n    for _gfy_name in python3.14 python3 python; do\n        if _graphify_command "$_gfy_name"; then\n            _graphify_usable "$GRAPHIFY_RESOLVED" && { GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; break; }\n        fi\n    done\nfi\nif [ -z "$GRAPHIFY_PYTHON" ] && [ "${GRAPHIFY_DISCOVERY_OPTIONAL-0}" != 1 ]; then\n    echo "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." >&2\n    exit 1\nfi')"
export GRAPHIFY_WHISPER_MODEL=base  # or whatever --whisper-model the user passed (must be exported)
export GRAPHIFY_WHISPER_PROMPT="<the one-sentence domain hint you composed in Step 1>"
"$GRAPHIFY_PYTHON" -E -P -B -c "
import json, os, sys
from pathlib import Path
from graphify.transcribe import transcribe_all

detect = json.loads(Path('graphify-out/.graphify_detect.json').read_text(encoding=\"utf-8\"))
video_files = detect.get('files', {}).get('video', [])
prompt = os.environ.get('GRAPHIFY_WHISPER_PROMPT', 'Use proper punctuation and paragraph breaks.')

transcript_paths = transcribe_all(video_files, initial_prompt=prompt)
# Write the JSON from Python (NOT a shell '>' redirect): transcribe_all/Whisper
# print progress to stdout, which would otherwise corrupt the JSON file (#1392).
Path('graphify-out/.graphify_transcripts.json').write_text(json.dumps(transcript_paths, ensure_ascii=False), encoding=\"utf-8\")
print(f'Transcribed {len(transcript_paths)} file(s)', file=sys.stderr)
"
```

After transcription:
- Read the transcript paths from `graphify-out/.graphify_transcripts.json`
- Add them to the docs list before dispatching semantic subagents in Step 3B
- Print how many transcripts were created: `Transcribed N video file(s) -> treating as docs`
- If transcription fails for a file, print a warning and continue with the rest

**Whisper model:** Default is `base`. If the user passed `--whisper-model <name>`, `export GRAPHIFY_WHISPER_MODEL=<name>` (it must be exported, not just assigned) before running the command above.
