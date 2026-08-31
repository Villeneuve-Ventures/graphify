# graphify reference: commit hook and native CLAUDE.md integration

Load this when the user asked to install the post-commit hook or wire graphify into a project's CLAUDE.md.

## For git commit hook

Install a post-commit hook that auto-rebuilds the graph after every commit. No background process needed - triggers once per commit, works with any editor.

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
& $GraphifyPython -E -P -B -m graphify hook install    # install
& $GraphifyPython -E -P -B -m graphify hook uninstall  # remove
& $GraphifyPython -E -P -B -m graphify hook status     # check
```

After every `git commit`, the hook detects which code files changed (via `git diff HEAD~1`), re-runs AST extraction on those files, and rebuilds `graph.json` and `GRAPH_REPORT.md`. Doc/image changes are ignored by the hook - run `/graphify --update` manually for those.

If a post-commit hook already exists, graphify appends to it rather than replacing it.

---

## For native CLAUDE.md integration

Run once per project to make graphify always-on in Claude Code sessions:

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
& $GraphifyPython -E -P -B -m graphify claude install
```

This writes a `## graphify` section to the local `CLAUDE.md` that instructs Claude to check the graph before answering codebase questions and rebuild it after code changes. No manual `/graphify` needed in future sessions.

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
& $GraphifyPython -E -P -B -m graphify claude uninstall  # remove the section
```
