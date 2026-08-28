# graphify reference: add a URL and watch a folder

Load this when the user ran `/graphify add <url>` or passed `--watch`. Neither is part of the default build.

## For /graphify add

Fetch a URL and add it to the corpus, then update the graph.

```bash
GRAPHIFY_PYTHON=$(GRAPHIFY_INPUT_PATH="${GRAPHIFY_INPUT_PATH-}" /bin/sh -p -c 'GRAPHIFY_PYTHON=""; GRAPHIFY_PYTHON_EXPLICIT=0; _GRAPHIFY_VERSION_CHECK='"'"'import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final" and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)'"'"'; _GRAPHIFY_IDENTITY_CHECK='"'"'exec("import importlib.metadata\nimport importlib.util\nimport json\nimport os\nimport sys\nimport urllib.parse\nimport urllib.request\n\ndef contained(path, root):\n    try:\n        normalized_root = os.path.normcase(root)\n        return os.path.commonpath((os.path.normcase(path), normalized_root)) == normalized_root\n    except (OSError, ValueError):\n        return False\n\ntry:\n    distribution = importlib.metadata.distribution(\"graphifyy\")\n    if distribution.metadata.get(\"Name\") != \"graphifyy\":\n        raise ValueError\n    spec = importlib.util.find_spec(\"graphify\")\n    if spec is None or not spec.origin:\n        raise ValueError\n    origin = os.path.abspath(spec.origin)\n    real_origin = os.path.realpath(origin)\n    direct_url_text = distribution.read_text(\"direct_url.json\")\n    editable = False\n    if direct_url_text is not None:\n        direct_url = json.loads(direct_url_text)\n        parsed = urllib.parse.urlparse(direct_url[\"url\"])\n        if direct_url.get(\"dir_info\", {}).get(\"editable\") is True:\n            editable = True\n            if parsed.scheme != \"file\" or parsed.netloc not in (\"\", \"localhost\"):\n                raise ValueError\n            package_root = os.path.abspath(\n                urllib.request.url2pathname(parsed.path)\n            )\n    if editable:\n        real_package_root = os.path.realpath(package_root)\n        if not contained(origin, package_root) or not contained(real_origin, real_package_root):\n            raise ValueError\n    else:\n        owned = [\n            entry\n            for entry in (distribution.files or ())\n            if str(entry) == \"graphify/__init__.py\"\n        ]\n        if len(owned) != 1:\n            raise ValueError\n        recorded_origin = os.path.abspath(distribution.locate_file(owned[0]))\n        if os.path.normcase(recorded_origin) != os.path.normcase(origin):\n            raise ValueError\n    arguments = sys.argv[1:]\n    if arguments[0] == \"ambient\":\n        for root_arg in arguments[1:]:\n            if not root_arg:\n                continue\n            root = os.path.abspath(root_arg)\n            real_root = os.path.realpath(root)\n            if contained(origin, root) or contained(real_origin, real_root):\n                raise ValueError\nexcept (Exception, SystemExit):\n    raise SystemExit(1)\n")'"'"'; _GRAPHIFY_WORKSPACE=$(command pwd -P) || exit 1; _graphify_canonical_root() { _gfy_root=$1; [ -n "$_gfy_root" ] || return 1; case "$_gfy_root" in /*) ;; *) _gfy_root=$_GRAPHIFY_WORKSPACE/$_gfy_root ;; esac; [ -d "$_gfy_root" ] && CDPATH= cd -P -- "$_gfy_root" 2>/dev/null && command pwd -P; }; _GRAPHIFY_INPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_INPUT_PATH-}") || _GRAPHIFY_INPUT_ROOT=""; _GRAPHIFY_OUTPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_OUTPUT_ROOT-${GRAPHIFY_OUT-graphify-out}}") || _GRAPHIFY_OUTPUT_ROOT=""; _graphify_path_denied() { _gfy_policy_path=$1; [ "$_GRAPHIFY_WORKSPACE" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_WORKSPACE"|"$_GRAPHIFY_WORKSPACE"/*) return 0 ;; esac; [ -z "$_GRAPHIFY_INPUT_ROOT" ] || { [ "$_GRAPHIFY_INPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_INPUT_ROOT"|"$_GRAPHIFY_INPUT_ROOT"/*) return 0 ;; esac; }; [ -z "$_GRAPHIFY_OUTPUT_ROOT" ] || { [ "$_GRAPHIFY_OUTPUT_ROOT" = / ] && return 0; case "$_gfy_policy_path" in "$_GRAPHIFY_OUTPUT_ROOT"|"$_GRAPHIFY_OUTPUT_ROOT"/*) return 0 ;; esac; }; return 1; }; _graphify_resolve_ambient() { _gfy_lexical=$1; case "$_gfy_lexical" in /*) ;; *) return 1 ;; esac; _graphify_path_denied "$_gfy_lexical" && return 1; _gfy_path=$_gfy_lexical; _gfy_links=0; while [ -L "$_gfy_path" ]; do _gfy_links=$((_gfy_links + 1)); [ "$_gfy_links" -le 40 ] || return 1; [ -x /usr/bin/readlink ] || return 1; _gfy_link=$(/usr/bin/readlink "$_gfy_path") || return 1; case "$_gfy_link" in /*) _gfy_path=$_gfy_link ;; *) _gfy_dir=${_gfy_path%/*}; _gfy_path=$_gfy_dir/$_gfy_link ;; esac; done; _gfy_dir=${_gfy_path%/*}; _gfy_base=${_gfy_path##*/}; _gfy_dir=$(CDPATH= cd -P -- "$_gfy_dir" 2>/dev/null && command pwd -P) || return 1; _gfy_path=$_gfy_dir/$_gfy_base; _graphify_path_denied "$_gfy_path" && return 1; [ -x "$_gfy_lexical" ] || return 1; GRAPHIFY_RESOLVED=$_gfy_lexical; }; _graphify_command() { _gfy_found=$(command -v "$1" 2>/dev/null) || return 1; _graphify_resolve_ambient "$_gfy_found"; }; _graphify_supported() { [ -n "$1" ] && "$1" -E -P -B -c "$_GRAPHIFY_VERSION_CHECK" >/dev/null 2>&1; }; _graphify_usable() { _graphify_supported "$1" && "$1" -E -P -B -c "$_GRAPHIFY_IDENTITY_CHECK" trusted >/dev/null 2>&1; }; _graphify_ambient_usable() { _graphify_supported "$1" && "$1" -E -P -B -c "$_GRAPHIFY_IDENTITY_CHECK" ambient "$_GRAPHIFY_WORKSPACE" "$_GRAPHIFY_INPUT_ROOT" "$_GRAPHIFY_OUTPUT_ROOT" >/dev/null 2>&1; }; case "${VIRTUAL_ENV-}" in /*) _gfy_venv_python=$VIRTUAL_ENV/bin/python; _graphify_usable "$_gfy_venv_python" && { GRAPHIFY_PYTHON=$_gfy_venv_python; GRAPHIFY_PYTHON_EXPLICIT=1; } ;; esac; if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command uv; then _gfy_uv=$GRAPHIFY_RESOLVED; _gfy_uv_dir=$("$_gfy_uv" tool dir 2>/dev/null); _gfy_candidate=${_gfy_uv_dir:+$_gfy_uv_dir/graphifyy/bin/python}; if _graphify_resolve_ambient "$_gfy_candidate"; then _graphify_ambient_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; fi; fi; if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command pipx; then _gfy_pipx=$GRAPHIFY_RESOLVED; _gfy_pipx_home=$("$_gfy_pipx" environment --value PIPX_LOCAL_VENVS 2>/dev/null); _gfy_candidate=${_gfy_pipx_home:+$_gfy_pipx_home/graphifyy/bin/python}; if _graphify_resolve_ambient "$_gfy_candidate"; then _graphify_ambient_usable "$GRAPHIFY_RESOLVED" && GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; fi; fi; if [ -z "$GRAPHIFY_PYTHON" ] && _graphify_command graphify; then _gfy_graphify=$GRAPHIFY_RESOLVED; IFS= read -r _gfy_shebang < "$_gfy_graphify" || _gfy_shebang=""; _gfy_shebang=${_gfy_shebang#\#!}; case "$_gfy_shebang" in "/usr/bin/env "*) _gfy_env_command=${_gfy_shebang#"/usr/bin/env "}; case "$_gfy_env_command" in ""|*[!a-zA-Z0-9_.@+-]*) _gfy_shebang="" ;; *) if _graphify_command "$_gfy_env_command"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;; esac ;; *[!a-zA-Z0-9/_.@+-]*) _gfy_shebang="" ;; *) if _graphify_resolve_ambient "$_gfy_shebang"; then _gfy_shebang=$GRAPHIFY_RESOLVED; else _gfy_shebang=""; fi ;; esac; _graphify_ambient_usable "$_gfy_shebang" && GRAPHIFY_PYTHON=$_gfy_shebang; fi; if [ -z "$GRAPHIFY_PYTHON" ]; then for _gfy_name in python3.14 python3 python; do if _graphify_command "$_gfy_name"; then _graphify_ambient_usable "$GRAPHIFY_RESOLVED" && { GRAPHIFY_PYTHON=$GRAPHIFY_RESOLVED; break; }; fi; done; fi; if [ -z "$GRAPHIFY_PYTHON" ] && [ "${GRAPHIFY_DISCOVERY_OPTIONAL-0}" != 1 ]; then echo "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." >&2; exit 1; fi; [ -n "$GRAPHIFY_PYTHON" ] || exit 1; printf "%sx" "$GRAPHIFY_PYTHON"'); GRAPHIFY_PYTHON=${GRAPHIFY_PYTHON%x}; GRAPHIFY_PYTHON=${GRAPHIFY_PYTHON:?Graphify interpreter discovery failed}
"$GRAPHIFY_PYTHON" -E -P -B -c "
import sys
from graphify.ingest import ingest
from pathlib import Path

try:
    out = ingest('URL', Path('./raw'), author='AUTHOR', contributor='CONTRIBUTOR')
    print(f'Saved to {out}')
except ValueError as e:
    print(f'error: {e}', file=sys.stderr)
    sys.exit(1)
except RuntimeError as e:
    print(f'error: {e}', file=sys.stderr)
    sys.exit(1)
"
```

Replace `URL` with the actual URL, `AUTHOR` with the user's name if provided, `CONTRIBUTOR` likewise. If the command exits with an error, tell the user what went wrong - do not silently continue. After a successful save, automatically run the `--update` pipeline on `./raw` to merge the new file into the existing graph.

Supported URL types (auto-detected):
- YouTube / any video URL → audio downloaded via yt-dlp, transcribed to `.txt` on next run (requires `pip install 'graphifyy[video]'`)
- Twitter/X → fetched via oEmbed, saved as `.md` with tweet text and author
- arXiv → abstract + metadata saved as `.md`
- PDF → downloaded as `.pdf`
- Images (.png/.jpg/.webp) → downloaded, Claude vision extracts on next run
- Any webpage → converted to markdown via html2text

---

## For --watch

Start a background watcher that monitors a folder and auto-updates the graph when files change.

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
    param([string]$Path)
    if (-not (Test-GraphifyFullyQualifiedPath $Path)) { return $null }
    try {
        $full = [IO.Path]::GetFullPath($Path)
        $root = [IO.Path]::GetPathRoot($full)
        $current = $root
        $visited = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        foreach ($part in ($full.Substring($root.Length) -split '[\\/]' | Where-Object { $_ })) {
            $current = [IO.Path]::GetFullPath((Join-Path $current $part))
            $info = Get-Item -LiteralPath $current -Force -ErrorAction Stop
            if (($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                if ($info.PSObject.Methods.Name -notcontains "ResolveLinkTarget") { return $null }
                $target = $info.ResolveLinkTarget($true)
                if (-not $target) { return $null }
                $current = [IO.Path]::GetFullPath($target.FullName)
                if (-not $visited.Add($current)) { return $null }
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
    if (-not (Test-GraphifySupportedPython $Candidate)) { return $false }
    & $Candidate -E -P -B -c $GraphifyIdentityCheck trusted 2>$null
    return $LASTEXITCODE -eq 0
}
function Test-GraphifyAmbientPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-GraphifySupportedPython $Candidate)) { return $false }
    & $Candidate -E -P -B -c $GraphifyIdentityCheck ambient @GraphifyDenyRoots 2>$null
    return $LASTEXITCODE -eq 0
}
function Test-GraphifySupportedPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    & $Candidate -E -P -B -c "import sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and sys.version_info.releaselevel == 'final' and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)" 2>$null
    return $LASTEXITCODE -eq 0
}
if (Test-GraphifyFullyQualifiedPath "$env:VIRTUAL_ENV") {
    $activeVenv = Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $activeVenv)) { $activeVenv = Join-Path $env:VIRTUAL_ENV "bin/python" }
    if (Test-GraphifyPython $activeVenv) { $GraphifyPython = $activeVenv; $GraphifyPythonExplicit = $true }
}
if (-not $GraphifyPython) {
    $uv = Resolve-GraphifyAmbientCommand uv
    if ($uv) {
        $uvDir = (& $uv tool dir 2>$null).Trim()
        $candidate = Join-Path $uvDir "graphifyy\Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $candidate)) { $candidate = Join-Path $uvDir "graphifyy/bin/python" }
        if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyAmbientPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate) }
    }
}
if (-not $GraphifyPython) {
    $pipx = Resolve-GraphifyAmbientCommand pipx
    if ($pipx) {
        $venvs = (& $pipx environment --value PIPX_LOCAL_VENVS 2>$null).Trim()
        $candidate = Join-Path $venvs "graphifyy\Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $candidate)) { $candidate = Join-Path $venvs "graphifyy/bin/python" }
        if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyAmbientPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate) }
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
            $resolved = (& $candidate -3.14 -E -P -B -c "import sys; print(sys.executable)" 2>$null).Trim()
            if ($LASTEXITCODE -eq 0 -and -not (Test-GraphifyWorkspacePath $resolved) -and (Test-GraphifyAmbientPython $resolved)) { $GraphifyPython = [IO.Path]::GetFullPath($resolved); break }
        } elseif (Test-GraphifyAmbientPython $candidate) { $GraphifyPython = $candidate; break }
    }
}
if (-not $GraphifyPython -and -not $GraphifyDiscoveryOptional) { throw "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." }
& $GraphifyPython -E -P -B -m graphify watch INPUT_PATH --debounce 3
```

Replace INPUT_PATH with the folder to watch. Behavior depends on what changed:

- **Code files only (.py, .ts, .go, etc.):** re-runs AST extraction + rebuild + cluster immediately, no LLM needed. `graph.json` and `GRAPH_REPORT.md` are updated automatically.
- **Docs, papers, or images:** writes a `graphify-out/needs_update` flag and prints a notification to run `/graphify --update` (LLM semantic re-extraction required).

Debounce (default 3s): waits until file activity stops before triggering, so a wave of parallel agent writes doesn't trigger a rebuild per file.

Press Ctrl+C to stop.

For agentic workflows: run `--watch` in a background terminal. Code changes from agent waves are picked up automatically between waves. If agents are also writing docs or notes, you'll need a manual `/graphify --update` after those waves.
