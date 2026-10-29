# graphify reference: extraction subagent prompt

Load this in Step 3 Part B when the corpus has at least one doc, paper, or image chunk. A pure-code corpus skips Part B and never reads this file. Each semantic subagent receives the prompt below verbatim (substitute FILE_LIST, CHUNK_NUM, TOTAL_CHUNKS, DEEP_MODE, and CHUNK_PATH).

```
You are a graphify extraction subagent. Read the files listed and extract a knowledge graph fragment.
Output ONLY valid JSON matching the schema below - no explanation, no markdown fences, no preamble.

Files (chunk CHUNK_NUM of TOTAL_CHUNKS):
FILE_LIST

Rules:
- Completion: completed_sources lists only assigned files fully read and fully extracted in this run, including successful empty extraction. Omit failed, omitted, partial or truncated files; record ownership alone never establishes completion.
- EXTRACTED: relationship explicit in source (import, call, citation, "see §3.2")
- INFERRED: reasonable inference (shared data structure, implied dependency)
- AMBIGUOUS: uncertain - flag for review, do not omit

Code files: focus on semantic edges AST cannot find (call relationships, shared data, arch patterns).
  Do not re-extract imports - AST already has those.
Doc/paper files: extract named concepts, entities, citations. For rationale (WHY decisions were made, trade-offs, design intent): store as a `rationale` attribute on the relevant concept node — do NOT create a separate rationale node or fragment node. Only create a node for something that is itself a named entity or concept. Use `file_type:"rationale"` for concept-like nodes (ideas, principles, mechanisms, design patterns). `file_type` MUST be one of exactly these six values: `code`, `document`, `paper`, `image`, `rationale`, `concept`. Any other value is invalid and will be rejected.
Code files: when adding `calls` edges, source MUST be the caller (the function/class doing the calling), target MUST be the callee. Never reverse this direction. `calls` edges MUST stay within one language: a Python function cannot `calls` a JS/TS/Go/Rust/Java symbol and vice versa — cross-language call edges are phantom artifacts, never emit them.
Image files: use vision to understand what the image IS - do not just OCR.
  UI screenshot: layout patterns, design decisions, key elements, purpose.
  Chart: metric, trend/insight, data source.
  Tweet/post: claim as node, author, concepts mentioned.
  Diagram: components and connections.
  Research figure: what it demonstrates, method, result.
  Handwritten/whiteboard: ideas and arrows, mark uncertain readings AMBIGUOUS.

DEEP_MODE (if --mode deep was given): be aggressive with INFERRED edges - indirect deps,
  shared assumptions, latent couplings. Mark uncertain ones AMBIGUOUS instead of omitting.

Semantic similarity: if two concepts in this chunk solve the same problem or represent the same idea without any structural link (no import, no call, no citation), add a `semantically_similar_to` edge marked INFERRED with a confidence_score reflecting how similar they are (0.6-0.95). Examples:
- Two functions that both validate user input but never call each other
- A class in code and a concept in a paper that describe the same algorithm
- Two error types that handle the same failure mode differently
Only add these when the similarity is genuinely non-obvious and cross-cutting. Do not add them for trivially similar things.

Hyperedges: if 3 or more nodes clearly participate together in a shared concept, flow, or pattern that is not captured by pairwise edges alone, add a hyperedge to a top-level `hyperedges` array. Examples:
- All classes that implement a common protocol or interface
- All functions in an authentication flow (even if they don't all call each other)
- All concepts from a paper section that form one coherent idea
Use sparingly — only when the group relationship adds information beyond the pairwise edges. Maximum 3 hyperedges per chunk.

If a file has YAML frontmatter (--- ... ---), copy source_url, captured_at, author,
  contributor onto every node from that file.

confidence_score is REQUIRED on every edge - never omit it, never use 0.5 as a default:
- EXTRACTED edges: confidence_score = 1.0 always
- INFERRED edges: pick exactly ONE value from this set — never 0.5:
    0.95  direct structural evidence (shared data structure, named cross-file reference).
    0.85  strong inference (clear functional alignment, no direct symbol link).
    0.75  reasonable inference (shared problem domain + similar shape, requires interpretation).
    0.65  weak inference (thematically related, no shape evidence).
    0.55  speculative but plausible (surface-level co-occurrence only).
  Models follow discrete rubrics better than continuous ranges; the bimodal
  distribution observed in production (>50% at 0.5, >40% at 0.85+) shows the
  range guidance is being collapsed to a binary. If no value above fits, mark
  the edge AMBIGUOUS rather than picking 0.4 or below.
- AMBIGUOUS edges: 0.1-0.3

Node ID format: lowercase, only `[a-z0-9_]`, no dots or slashes. Format: `{stem}_{entity}` where stem is the **full repo-relative path with the extension dropped**, every path segment kept and joined with `_` (each segment lowercased with non-alphanumeric chars replaced by `_`), and entity is the symbol name similarly normalized. Use every directory level, not just the immediate parent — this keeps same-named files in different directories distinct. Examples: `src/auth/session.py` + `ValidateToken` → `src_auth_session_validatetoken`; `lib/utils/helpers.py` + `parse_url` → `lib_utils_helpers_parse_url`; `tests/test_foo.py` + `_helper` → `tests_test_foo_helper`; `docs/v1/api/README.md` + `getUser` → `docs_v1_api_readme_getuser`. Top-level files (no parent dir, e.g. `setup.py`) use just the filename stem: `setup_my_func`. This must match the ID the AST extractor generates — using just the filename (e.g., `session_validatetoken`) or only the immediate parent (e.g., `auth_session_validatetoken`) will create orphan ghost-duplicate nodes. If you are re-extracting a project built under the old immediate-parent format, the user should run `graphify extract --force` to rebuild cleanly. CRITICAL: never append chunk numbers, sequence numbers, or any suffix to an ID (no `_c1`, `_c2`, `_chunk2`, etc.). IDs must be deterministic from the label alone — the same entity must always produce the same ID regardless of which chunk processes it.

Generate the extraction JSON matching this schema exactly:
{"nodes":[{"id":"auth_session_validatetoken","label":"Human Readable Name","file_type":"code|document|paper|image|rationale|concept","source_file":"<FILE_LIST path verbatim>","source_location":null,"source_url":null,"captured_at":null,"author":null,"contributor":null}],"edges":[{"source":"node_id","target":"node_id","relation":"calls|implements|references|cites|conceptually_related_to|shares_data_with|semantically_similar_to|rationale_for","confidence":"EXTRACTED|INFERRED|AMBIGUOUS","confidence_score":1.0,"source_file":"<FILE_LIST path verbatim>","source_location":null,"weight":1.0}],"hyperedges":[{"id":"snake_case_id","label":"Human Readable Label","nodes":["node_id1","node_id2","node_id3"],"relation":"participate_in|implement|form","confidence":"EXTRACTED|INFERRED","confidence_score":0.75,"source_file":"<FILE_LIST path verbatim>"}],"completed_sources":["<FILE_LIST path verbatim>"],"input_tokens":0,"output_tokens":0}

source_file RULE (every node, edge, and hyperedge): set source_file to the path of the originating file EXACTLY as it appears in FILE_LIST — verbatim and absolute. Do NOT shorten to a basename, do NOT re-relativize, do NOT strip any directory prefix, and do NOT change separators (the engine canonicalizes separators and relativizes against the build root downstream). Copy the FILE_LIST entry character-for-character. This keeps the full build and incremental --update on the same base, so build_merge's replace-on-re-extract matches the existing node instead of accumulating a duplicate.

Then write the JSON to disk using the Write tool at this exact absolute path (no relative paths — Write resolves relative paths against an undefined cwd and the file will be silently lost):
CHUNK_PATH
```

## Disk result staging

Parent-only steps for disk/manual hosts. CHUNK_PATH is an untrusted intake file, never the published or prepared chunk path. Each command freshly admits its interpreter and binds the exact live token for INPUT_PATH and the configured output through v8's existing active-token admission, as B0/B3 do. A token retained from Step 1/B0 must match; stop if it differs, is refused or no live token is available. Shell exports do not persist across tool calls: bind the chunk number, assignment and usage data in each call that needs them. The runner alone can commit into its pinned prepared output.

Before dispatch, assign each current chunk a positive padded number. Bind GRAPHIFY_CHUNK_NUMBER and GRAPHIFY_CHUNK_FILES_JSON as environment data (the latter is the JSON array of that chunk's exact FILE_LIST paths). Set complete quoted data literals: POSIX single quotes with embedded apostrophes escaped as '\''; PowerShell single quotes with doubled apostrophes. Never substitute path or JSON bytes into Python code. Export those variables on POSIX; use $env: variables on PowerShell. Do not dispatch a file outside B0's uncached list or assign a file to multiple chunks. Run allocations serially, once per chunk, before dispatch; give the printed absolute CHUNK_PATH to the subagent's Write/file editor as data. The disk-host prompt directs it to write JSON to CHUNK_PATH and return only a completion notice.

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
    if ([IO.Path]::DirectorySeparatorChar -eq '\' -and $Path -match '^[\\/](?![\\/])') { return $false }
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
exec('import sys\nif sys.flags.no_site != 1 and sys.argv[1] != "trusted": raise SystemExit(1)\nimport importlib.machinery\nimport importlib.metadata\nimport importlib.util\nimport json\nimport os\nimport site\nimport stat\nimport urllib.parse\nimport urllib.request\n\ndef contained(path, root):\n    try:\n        normalized_root = os.path.normcase(root)\n        return os.path.commonpath((os.path.normcase(path), normalized_root)) == normalized_root\n    except (OSError, ValueError):\n        return False\n\ndef unique_paths(paths):\n    result = []\n    seen = set()\n    for path in paths:\n        if not isinstance(path, str) or not path:\n            raise ValueError\n        absolute = os.path.abspath(path)\n        key = os.path.normcase(absolute)\n        if key not in seen:\n            seen.add(key)\n            result.append(absolute)\n    return result\n\ndef venv_system_site_enabled():\n    config = os.path.join(sys.prefix, "pyvenv.cfg")\n    try:\n        text = open(config, encoding="utf-8").read()\n    except (OSError, UnicodeError):\n        raise ValueError\n    values = []\n    for raw_line in text.splitlines():\n        key, separator, value = raw_line.partition("=")\n        if key.strip().lower() != "include-system-site-packages":\n            continue\n        if not separator or value.strip().lower() not in ("true", "false"):\n            raise ValueError\n        values.append(value.strip().lower() == "true")\n    if len(values) != 1:\n        raise ValueError\n    return values[0]\n\ndef normal_site_roots():\n    roots = []\n    is_venv = sys.prefix != sys.base_prefix or sys.exec_prefix != sys.base_exec_prefix\n    user_enabled = False\n    if is_venv:\n        roots.extend(site.getsitepackages(unique_paths((sys.prefix, sys.exec_prefix))))\n        include_system = venv_system_site_enabled()\n        if include_system:\n            user_enabled = site.check_enableusersite() is True\n            if user_enabled:\n                roots.append(site.getusersitepackages())\n            roots.extend(\n                site.getsitepackages(\n                    unique_paths((sys.base_prefix, sys.base_exec_prefix))\n                )\n            )\n    else:\n        user_enabled = site.check_enableusersite() is True\n        if user_enabled:\n            roots.append(site.getusersitepackages())\n        roots.extend(site.getsitepackages(unique_paths((sys.prefix, sys.exec_prefix))))\n    return [path for path in unique_paths(roots) if os.path.isdir(path)], user_enabled\n\ndef path_denied(path, deny_roots):\n    absolute = os.path.abspath(path)\n    real = os.path.realpath(absolute)\n    for root_arg in deny_roots:\n        if not root_arg:\n            continue\n        root = os.path.abspath(root_arg)\n        real_root = os.path.realpath(root)\n        if contained(absolute, root) or contained(real, real_root):\n            return True\n    return False\n\ndef inert_startup_paths(roots, deny_roots, strict):\n    accepted = []\n    unsafe = False\n    for root in roots:\n        try:\n            entries = sorted(os.scandir(root), key=lambda entry: entry.name)\n        except OSError:\n            raise ValueError\n        for entry in entries:\n            if entry.name.startswith(".") or not entry.name.endswith(".pth"):\n                continue\n            try:\n                entry_stat = entry.stat(follow_symlinks=False)\n                if (\n                    getattr(entry_stat, "st_flags", 0) & getattr(stat, "UF_HIDDEN", 0)\n                    or getattr(entry_stat, "st_file_attributes", 0)\n                    & getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 2)\n                ):\n                    continue\n                if (\n                    entry.is_symlink()\n                    or not entry.is_file(follow_symlinks=False)\n                ):\n                    unsafe = True\n                    continue\n                text = open(entry.path, encoding="utf-8-sig").read()\n            except (OSError, UnicodeError):\n                unsafe = True\n                continue\n            file_accepted = []\n            file_unsafe = False\n            for raw_line in text.splitlines():\n                line = raw_line.rstrip()\n                if not line or line.startswith("#"):\n                    continue\n                if "\\x00" in line or line.startswith(("import ", "import\\t")):\n                    file_unsafe = True\n                    continue\n                target = os.path.abspath(os.path.join(root, line))\n                if not os.path.exists(target):\n                    continue\n                try:\n                    target_mode = os.stat(target).st_mode\n                except OSError:\n                    file_unsafe = True\n                    continue\n                if not (stat.S_ISDIR(target_mode) or stat.S_ISREG(target_mode)):\n                    file_unsafe = True\n                    continue\n                if path_denied(target, deny_roots):\n                    file_unsafe = True\n                    continue\n                file_accepted.append(target)\n            if file_unsafe:\n                unsafe = True\n            else:\n                accepted.extend(file_accepted)\n    if strict and unsafe:\n        raise ValueError\n    return unique_paths(accepted)\n\ndef ambient_paths(deny_roots, strict):\n    roots, user_enabled = normal_site_roots()\n    sanitized = list(sys.path)\n    for root in roots:\n        sanitized.extend((root, *inert_startup_paths([root], deny_roots, strict)))\n    sanitized = unique_paths(sanitized)\n    if strict:\n        if importlib.machinery.PathFinder.find_spec("sitecustomize", sanitized) is not None:\n            raise ValueError\n        if user_enabled and importlib.machinery.PathFinder.find_spec(\n            "usercustomize", sanitized\n        ) is not None:\n            raise ValueError\n    return roots, sanitized\n\ndef supported():\n    return (\n        sys.implementation.name == "cpython"\n        and sys.version_info.releaselevel == "final"\n        and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0)\n    )\n\ntry:\n    arguments = sys.argv[1:]\n    action = arguments[0]\n    if not supported():\n        raise ValueError\n    if action == "executable":\n        print(sys.executable)\n        raise SystemExit(0)\n    if action == "trusted":\n        deny_roots = []\n        distribution = importlib.metadata.distribution("graphifyy")\n        if distribution.metadata.get("Name") != "graphifyy":\n            raise ValueError\n    elif action not in ("ambient-supported", "ambient-identity"):\n        raise ValueError\n    else:\n        deny_roots = arguments[1:]\n        roots, sanitized = ambient_paths(\n            deny_roots, strict=action == "ambient-supported"\n        )\n        if action == "ambient-supported":\n            raise SystemExit(0)\n        sys.path[:] = sanitized\n        distribution = next(\n            distribution\n            for distribution in importlib.metadata.distributions(path=roots)\n            if distribution.metadata.get("Name") == "graphifyy"\n        )\n    spec = importlib.util.find_spec("graphify")\n    if spec is None or not spec.origin:\n        raise ValueError\n    origin = os.path.abspath(spec.origin)\n    real_origin = os.path.realpath(origin)\n    direct_url_text = distribution.read_text("direct_url.json")\n    editable = False\n    if direct_url_text is not None:\n        direct_url = json.loads(direct_url_text)\n        parsed = urllib.parse.urlparse(direct_url["url"])\n        if direct_url.get("dir_info", {}).get("editable") is True:\n            editable = True\n            if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):\n                raise ValueError\n            package_root = os.path.abspath(\n                urllib.request.url2pathname(parsed.path)\n            )\n    if editable:\n        real_package_root = os.path.realpath(package_root)\n        if not contained(origin, package_root) or not contained(real_origin, real_package_root):\n            raise ValueError\n    else:\n        owned = [\n            entry\n            for entry in (distribution.files or ())\n            if str(entry) == "graphify/__init__.py"\n        ]\n        if len(owned) != 1:\n            raise ValueError\n        recorded_origin = os.path.abspath(distribution.locate_file(owned[0]))\n        if os.path.normcase(recorded_origin) != os.path.normcase(origin):\n            raise ValueError\n    for root_arg in deny_roots:\n        if not root_arg:\n            continue\n        root = os.path.abspath(root_arg)\n        real_root = os.path.realpath(root)\n        if contained(origin, root) or contained(real_origin, real_root):\n            raise ValueError\nexcept (Exception, SystemExit) as error:\n    if isinstance(error, SystemExit) and error.code == 0:\n        raise\n    raise SystemExit(1)\n')
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
    & $Candidate -E -P -B -S -c $GraphifyIdentityCheck ambient-identity @GraphifyDenyRoots 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    return $invocationSucceeded -and $exitCode -eq 0
}
function Test-GraphifyAmbientSupportedPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    & $Candidate -E -P -B -S -c $GraphifyIdentityCheck ambient-supported @GraphifyDenyRoots 2>$null
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
            $resolved = Invoke-GraphifyNativeText $candidate @("-3.14", "-E", "-P", "-B", "-S", "-c", $GraphifyIdentityCheck, "executable")
            if ($resolved -and -not (Test-GraphifyWorkspacePath $resolved) -and (Test-GraphifyAmbientPython $resolved)) { $GraphifyPython = [IO.Path]::GetFullPath($resolved); break }
        } elseif (Test-GraphifyAmbientPython $candidate) { $GraphifyPython = $candidate; break }
    }
}
if (-not $GraphifyPython -and -not $GraphifyDiscoveryOptional) { throw "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." }
$Env:GRAPHIFY_TRANSACTION_TOKEN = & $GraphifyPython -E -P -B -c 'import os, sys
from pathlib import Path
from graphify.paths import GRAPHIFY_OUT
from graphify.transaction import active_transaction_token_path
root = Path(sys.argv[1]).resolve(strict=True)
configured = Path(GRAPHIFY_OUT).expanduser()
output = (configured if configured.is_absolute() else root / configured).resolve()
token = active_transaction_token_path(output)
retained = os.environ.get(''GRAPHIFY_TRANSACTION_TOKEN'')
if retained and Path(retained).absolute() != token:
    raise ValueError(''retained transaction token does not match the live owner'')
print(token)
' INPUT_PATH
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$GraphifyPythonSource = @'
import json, os, re, tempfile
from pathlib import Path
from graphify.llm import _completed_source_paths
from graphify.transaction import current_transaction, commit_prepared_bytes, pin_output
owner = current_transaction()
number = os.environ['GRAPHIFY_CHUNK_NUMBER']
if not re.fullmatch('[0-9]{2,}', number) or int(number) < 1:
    raise ValueError('chunk number must be a positive padded decimal')
assigned = json.loads(os.environ['GRAPHIFY_CHUNK_FILES_JSON'])
uncached = set(Path('.graphify_uncached.txt').read_text(encoding='utf-8').splitlines())
if (not isinstance(assigned, list) or not assigned or len(assigned) > 25
        or any(not isinstance(item, str) for item in assigned)
        or len(set(assigned)) != len(assigned) or not set(assigned) <= uncached):
    raise ValueError('chunk assignment must name only current uncached input files')
batch = Path('.graphify_dispatch_batch.txt').read_text(encoding='utf-8')
plan_name = '.graphify_intake_plan_' + number + '.json'
if Path(plan_name).exists() and json.loads(Path(plan_name).read_text())['batch'] == batch:
    raise ValueError('this chunk already has an intake; retain it for diagnosis')
assigned_paths = _completed_source_paths({'completed_sources': assigned}, Path(owner.root))
for previous_path in Path.cwd().glob('.graphify_intake_plan_*.json'):
    previous = json.loads(previous_path.read_text())
    if previous['owner'] == owner.id and previous['batch'] == batch:
        previous_paths = _completed_source_paths({'completed_sources': previous['assigned']}, Path(owner.root))
        if assigned_paths & previous_paths:
            raise ValueError('source already assigned to another chunk in this batch')
intake_root = Path(tempfile.gettempdir()).resolve(strict=True)
if any(intake_root.is_relative_to(path.resolve()) for path in (Path(owner.output), Path.cwd())):
    raise ValueError('intake storage must be outside public and prepared output')
intake = Path(tempfile.mkdtemp(prefix='graphify-chunk-', dir=intake_root))
with pin_output(intake, mutation=False) as capability:
    identity = capability.identity.json()
plan = {'owner': owner.id, 'batch': batch, 'number': number,
        'assigned': assigned, 'intake': str(intake), 'identity': identity}
print('Intake retained for this chunk: ' + str(intake), file=__import__('sys').stderr)
commit_prepared_bytes(owner, plan_name, json.dumps(plan).encode('utf-8'))
print(json.dumps({'CHUNK_PATH': str(intake / 'result.json'), 'CHUNK_NUM': number}))
'@
& $GraphifyPython -E -P -B -m graphify.transaction run-prepared-token $Env:GRAPHIFY_TRANSACTION_TOKEN '--' -c $GraphifyPythonSource
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
```

Wait for all dispatches. If any fails, has no result or returns invalid/truncated output, retain its intake and diagnostics and stop before B3; do not import siblings, save cache or publish. Treat this stop rule as overriding B3's older skip-failed-chunk advice. Check explicit completed_sources against the assigned files and actual full reads; omit failed, unreadable, partial and truncated sources. A successful empty extraction is valid, but an absent result is not.

Then the parent runs this import serially for each successfully completed chunk, with GRAPHIFY_CHUNK_NUMBER bound to its trusted number. The existing bounded native reader rejects symlink/nonregular results and reads at most 512 MiB (the graph artifact limit). The intake and its JSON are untrusted data. Foreign record ownership refuses import; explicit completion is intersected with the assigned files and excludes incomplete or partial results. No record ownership or intake path grants completion. B3 keeps v8's existing cache and merge contracts: only explicit validated completion can certify a complete cache entry; retained partial claims cannot. Only verified prepared readback is registered in the current success manifest. Do not write .graphify_semantic_new.json here.

Before each import, bind GRAPHIFY_CHUNK_USAGE_JSON using the same environment-data quoting rules. Copy that dispatch's actual parent-observed tool usage into an object with nonnegative integer input_tokens and output_tokens; never use estimates or the subagent's placeholder counts. For a manual host or a tool that does not expose usage, explicitly bind JSON null, report usage as unavailable, and keep v8's numeric zero placeholders for merge compatibility. Unavailable counts are not evidence of zero use. Import applies these counts before the prepared commit; do not edit registered chunks afterward. These read-only intake pins do not enable native Windows mutation: v8's existing transaction refusal still applies.

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
    if ([IO.Path]::DirectorySeparatorChar -eq '\' -and $Path -match '^[\\/](?![\\/])') { return $false }
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
exec('import sys\nif sys.flags.no_site != 1 and sys.argv[1] != "trusted": raise SystemExit(1)\nimport importlib.machinery\nimport importlib.metadata\nimport importlib.util\nimport json\nimport os\nimport site\nimport stat\nimport urllib.parse\nimport urllib.request\n\ndef contained(path, root):\n    try:\n        normalized_root = os.path.normcase(root)\n        return os.path.commonpath((os.path.normcase(path), normalized_root)) == normalized_root\n    except (OSError, ValueError):\n        return False\n\ndef unique_paths(paths):\n    result = []\n    seen = set()\n    for path in paths:\n        if not isinstance(path, str) or not path:\n            raise ValueError\n        absolute = os.path.abspath(path)\n        key = os.path.normcase(absolute)\n        if key not in seen:\n            seen.add(key)\n            result.append(absolute)\n    return result\n\ndef venv_system_site_enabled():\n    config = os.path.join(sys.prefix, "pyvenv.cfg")\n    try:\n        text = open(config, encoding="utf-8").read()\n    except (OSError, UnicodeError):\n        raise ValueError\n    values = []\n    for raw_line in text.splitlines():\n        key, separator, value = raw_line.partition("=")\n        if key.strip().lower() != "include-system-site-packages":\n            continue\n        if not separator or value.strip().lower() not in ("true", "false"):\n            raise ValueError\n        values.append(value.strip().lower() == "true")\n    if len(values) != 1:\n        raise ValueError\n    return values[0]\n\ndef normal_site_roots():\n    roots = []\n    is_venv = sys.prefix != sys.base_prefix or sys.exec_prefix != sys.base_exec_prefix\n    user_enabled = False\n    if is_venv:\n        roots.extend(site.getsitepackages(unique_paths((sys.prefix, sys.exec_prefix))))\n        include_system = venv_system_site_enabled()\n        if include_system:\n            user_enabled = site.check_enableusersite() is True\n            if user_enabled:\n                roots.append(site.getusersitepackages())\n            roots.extend(\n                site.getsitepackages(\n                    unique_paths((sys.base_prefix, sys.base_exec_prefix))\n                )\n            )\n    else:\n        user_enabled = site.check_enableusersite() is True\n        if user_enabled:\n            roots.append(site.getusersitepackages())\n        roots.extend(site.getsitepackages(unique_paths((sys.prefix, sys.exec_prefix))))\n    return [path for path in unique_paths(roots) if os.path.isdir(path)], user_enabled\n\ndef path_denied(path, deny_roots):\n    absolute = os.path.abspath(path)\n    real = os.path.realpath(absolute)\n    for root_arg in deny_roots:\n        if not root_arg:\n            continue\n        root = os.path.abspath(root_arg)\n        real_root = os.path.realpath(root)\n        if contained(absolute, root) or contained(real, real_root):\n            return True\n    return False\n\ndef inert_startup_paths(roots, deny_roots, strict):\n    accepted = []\n    unsafe = False\n    for root in roots:\n        try:\n            entries = sorted(os.scandir(root), key=lambda entry: entry.name)\n        except OSError:\n            raise ValueError\n        for entry in entries:\n            if entry.name.startswith(".") or not entry.name.endswith(".pth"):\n                continue\n            try:\n                entry_stat = entry.stat(follow_symlinks=False)\n                if (\n                    getattr(entry_stat, "st_flags", 0) & getattr(stat, "UF_HIDDEN", 0)\n                    or getattr(entry_stat, "st_file_attributes", 0)\n                    & getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 2)\n                ):\n                    continue\n                if (\n                    entry.is_symlink()\n                    or not entry.is_file(follow_symlinks=False)\n                ):\n                    unsafe = True\n                    continue\n                text = open(entry.path, encoding="utf-8-sig").read()\n            except (OSError, UnicodeError):\n                unsafe = True\n                continue\n            file_accepted = []\n            file_unsafe = False\n            for raw_line in text.splitlines():\n                line = raw_line.rstrip()\n                if not line or line.startswith("#"):\n                    continue\n                if "\\x00" in line or line.startswith(("import ", "import\\t")):\n                    file_unsafe = True\n                    continue\n                target = os.path.abspath(os.path.join(root, line))\n                if not os.path.exists(target):\n                    continue\n                try:\n                    target_mode = os.stat(target).st_mode\n                except OSError:\n                    file_unsafe = True\n                    continue\n                if not (stat.S_ISDIR(target_mode) or stat.S_ISREG(target_mode)):\n                    file_unsafe = True\n                    continue\n                if path_denied(target, deny_roots):\n                    file_unsafe = True\n                    continue\n                file_accepted.append(target)\n            if file_unsafe:\n                unsafe = True\n            else:\n                accepted.extend(file_accepted)\n    if strict and unsafe:\n        raise ValueError\n    return unique_paths(accepted)\n\ndef ambient_paths(deny_roots, strict):\n    roots, user_enabled = normal_site_roots()\n    sanitized = list(sys.path)\n    for root in roots:\n        sanitized.extend((root, *inert_startup_paths([root], deny_roots, strict)))\n    sanitized = unique_paths(sanitized)\n    if strict:\n        if importlib.machinery.PathFinder.find_spec("sitecustomize", sanitized) is not None:\n            raise ValueError\n        if user_enabled and importlib.machinery.PathFinder.find_spec(\n            "usercustomize", sanitized\n        ) is not None:\n            raise ValueError\n    return roots, sanitized\n\ndef supported():\n    return (\n        sys.implementation.name == "cpython"\n        and sys.version_info.releaselevel == "final"\n        and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0)\n    )\n\ntry:\n    arguments = sys.argv[1:]\n    action = arguments[0]\n    if not supported():\n        raise ValueError\n    if action == "executable":\n        print(sys.executable)\n        raise SystemExit(0)\n    if action == "trusted":\n        deny_roots = []\n        distribution = importlib.metadata.distribution("graphifyy")\n        if distribution.metadata.get("Name") != "graphifyy":\n            raise ValueError\n    elif action not in ("ambient-supported", "ambient-identity"):\n        raise ValueError\n    else:\n        deny_roots = arguments[1:]\n        roots, sanitized = ambient_paths(\n            deny_roots, strict=action == "ambient-supported"\n        )\n        if action == "ambient-supported":\n            raise SystemExit(0)\n        sys.path[:] = sanitized\n        distribution = next(\n            distribution\n            for distribution in importlib.metadata.distributions(path=roots)\n            if distribution.metadata.get("Name") == "graphifyy"\n        )\n    spec = importlib.util.find_spec("graphify")\n    if spec is None or not spec.origin:\n        raise ValueError\n    origin = os.path.abspath(spec.origin)\n    real_origin = os.path.realpath(origin)\n    direct_url_text = distribution.read_text("direct_url.json")\n    editable = False\n    if direct_url_text is not None:\n        direct_url = json.loads(direct_url_text)\n        parsed = urllib.parse.urlparse(direct_url["url"])\n        if direct_url.get("dir_info", {}).get("editable") is True:\n            editable = True\n            if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):\n                raise ValueError\n            package_root = os.path.abspath(\n                urllib.request.url2pathname(parsed.path)\n            )\n    if editable:\n        real_package_root = os.path.realpath(package_root)\n        if not contained(origin, package_root) or not contained(real_origin, real_package_root):\n            raise ValueError\n    else:\n        owned = [\n            entry\n            for entry in (distribution.files or ())\n            if str(entry) == "graphify/__init__.py"\n        ]\n        if len(owned) != 1:\n            raise ValueError\n        recorded_origin = os.path.abspath(distribution.locate_file(owned[0]))\n        if os.path.normcase(recorded_origin) != os.path.normcase(origin):\n            raise ValueError\n    for root_arg in deny_roots:\n        if not root_arg:\n            continue\n        root = os.path.abspath(root_arg)\n        real_root = os.path.realpath(root)\n        if contained(origin, root) or contained(real_origin, real_root):\n            raise ValueError\nexcept (Exception, SystemExit) as error:\n    if isinstance(error, SystemExit) and error.code == 0:\n        raise\n    raise SystemExit(1)\n')
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
    & $Candidate -E -P -B -S -c $GraphifyIdentityCheck ambient-identity @GraphifyDenyRoots 2>$null
    $invocationSucceeded = $?
    $exitCode = $LASTEXITCODE
    return $invocationSucceeded -and $exitCode -eq 0
}
function Test-GraphifyAmbientSupportedPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    & $Candidate -E -P -B -S -c $GraphifyIdentityCheck ambient-supported @GraphifyDenyRoots 2>$null
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
            $resolved = Invoke-GraphifyNativeText $candidate @("-3.14", "-E", "-P", "-B", "-S", "-c", $GraphifyIdentityCheck, "executable")
            if ($resolved -and -not (Test-GraphifyWorkspacePath $resolved) -and (Test-GraphifyAmbientPython $resolved)) { $GraphifyPython = [IO.Path]::GetFullPath($resolved); break }
        } elseif (Test-GraphifyAmbientPython $candidate) { $GraphifyPython = $candidate; break }
    }
}
if (-not $GraphifyPython -and -not $GraphifyDiscoveryOptional) { throw "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." }
$Env:GRAPHIFY_TRANSACTION_TOKEN = & $GraphifyPython -E -P -B -c 'import os, sys
from pathlib import Path
from graphify.paths import GRAPHIFY_OUT
from graphify.transaction import active_transaction_token_path
root = Path(sys.argv[1]).resolve(strict=True)
configured = Path(GRAPHIFY_OUT).expanduser()
output = (configured if configured.is_absolute() else root / configured).resolve()
token = active_transaction_token_path(output)
retained = os.environ.get(''GRAPHIFY_TRANSACTION_TOKEN'')
if retained and Path(retained).absolute() != token:
    raise ValueError(''retained transaction token does not match the live owner'')
print(token)
' INPUT_PATH
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$GraphifyPythonSource = @'
import json, os, re
from pathlib import Path
from graphify.build import _admit_string_identities, _hashable, _merge_source_file
from graphify.llm import _completed_source_paths
from graphify.transaction import (current_transaction, commit_prepared_bytes,
    pin_output, _read_relative_bytes)
owner = current_transaction()
number = os.environ['GRAPHIFY_CHUNK_NUMBER']
if not re.fullmatch('[0-9]{2,}', number) or int(number) < 1:
    raise ValueError('invalid chunk number')
plan = json.loads(Path('.graphify_intake_plan_' + number + '.json').read_text())
if (plan['owner'] != owner.id or plan['number'] != number
        or plan['batch'] != Path('.graphify_dispatch_batch.txt').read_text()):
    raise ValueError('intake belongs to a different owner or dispatch batch')
with pin_output(Path(plan['intake']), mutation=False) as capability:
    if capability.identity.json() != plan['identity']:
        raise ValueError('intake directory identity changed')
    # Existing native no-follow regular-file reader; capture immutable bytes once.
    payload = _read_relative_bytes(capability, 'result.json', 512 * 1024 * 1024)
    capability.validate()
result = json.loads(payload)
if (not isinstance(result, dict)
        or any(not isinstance(result.get(key), list) for key in ('nodes', 'edges'))
        or not isinstance(result.get('hyperedges', []), list)
        or any(not isinstance(item, dict) for key in ('nodes', 'edges', 'hyperedges')
               for item in result.get(key, []))
        or not isinstance(result.get('completed_sources', []), list)
        or any(not isinstance(item, str) for item in result.get('completed_sources', []))
        or not isinstance(result.get('incomplete_sources', []), list)
        or any(not isinstance(item, str) for item in result.get('incomplete_sources', []))):
    raise ValueError('chunk must be an extraction object with record lists')
nodes, edges = _admit_string_identities(result['nodes'], result['edges'], keep_incomplete_nodes=False)
if len(nodes) != len(result['nodes']) or len(edges) != len(result['edges']):
    raise ValueError('chunk contains invalid record identities')
for group in result.get('hyperedges', []):
    members = next((group[key] for key in ('nodes', 'members', 'node_ids')
                    if isinstance(group.get(key), list)), None)
    if (not _hashable(group.get('id')) or members is None
            or any(not isinstance(member, str) for member in members)):
        raise ValueError('chunk contains invalid hyperedge identities or members')
if (result.get('error') or result.get('failed_chunks')
        or result.get('finish_reason') not in (None, 'stop')):
    raise ValueError('failed chunk cannot be registered')
usage = json.loads(os.environ['GRAPHIFY_CHUNK_USAGE_JSON'])
if usage is None:
    print('Token usage unavailable; retaining numeric zero placeholders, not measured zero use.')
    usage = {'input_tokens': 0, 'output_tokens': 0}
if (not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0
        for key in ('input_tokens', 'output_tokens'))):
    raise ValueError('parent usage must contain nonnegative integer token counts or be null')
for key in ('input_tokens', 'output_tokens'):
    result[key] = usage[key]
root = Path(owner.root)
assigned = _completed_source_paths({'completed_sources': plan['assigned']}, root)
# Reuse v8's ownership keys and explicit-completion validator. A foreign claim
# refuses the entire intake instead of importing the donor's graph-scope filter.
for key in ('nodes', 'edges', 'hyperedges'):
    for record in result.get(key, []):
        source = record.get('source_file', record.get('source') if key == 'nodes' else None)
        if source:
            source = _merge_source_file(source, str(root))
            if str(Path(os.path.abspath(root / source))) not in assigned:
                raise ValueError('record ownership is outside this chunk assignment')
complete = _completed_source_paths(result, root) & assigned
incomplete = _completed_source_paths({'completed_sources': result.get('incomplete_sources', [])}, root)
complete -= incomplete
if (result.get('_partial') or any(
        record.get('_partial') or not record.get('source_file', record.get('source') if key == 'nodes' else None)
        for key in ('nodes', 'edges', 'hyperedges') for record in result.get(key, []))):
    complete.clear()
result['completed_sources'] = sorted(complete)
# JSON stays disk-backed across the host/subagent boundary; never interpolate it into code.
payload = json.dumps(result, ensure_ascii=False).encode('utf-8')
chunk_name = '.graphify_chunk_' + number + '.json'
commit_prepared_bytes(owner, chunk_name, payload)
if Path(chunk_name).read_bytes() != payload:
    raise ValueError('prepared chunk readback changed')
manifest_name = '.graphify_completed_chunks.json'
completed = json.loads(Path(manifest_name).read_text())
chunk_path = str(Path(chunk_name).absolute())
if chunk_path not in completed:
    completed.append(chunk_path)
commit_prepared_bytes(owner, manifest_name, json.dumps(completed).encode('utf-8'))
print('Staged and registered chunk ' + number + '; retain intake until consumers finish.')
'@
& $GraphifyPython -E -P -B -m graphify.transaction run-prepared-token $Env:GRAPHIFY_TRANSACTION_TOKEN '--' -c $GraphifyPythonSource
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
```

Keep failed/partial intake bytes and diagnostics for recovery. Once import/readback and all dependent consumers finish, the parent may remove only that successful intake file/directory after rechecking its recorded identity, content, links and consumers. Do not run an unconditional cleanup on failure or follow intake symlinks.
