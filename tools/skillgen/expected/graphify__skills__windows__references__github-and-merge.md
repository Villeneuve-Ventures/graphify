# graphify reference: GitHub clone and cross-repo merge

Load this when the user passed one or more `https://github.com/...` URLs, or named several local subfolders to merge into one graph.

### Step 0 - Clone GitHub repo(s) (only if a GitHub URL was given)

**Single repo:**
```powershell
$env:GRAPHIFY_INPUT_PATH = "INPUT_PATH"
$GraphifyPython = $null
$GraphifyWorkspace = [IO.Path]::GetFullPath((Get-Location).Path)
if ($GraphifyWorkspace -ne [IO.Path]::GetPathRoot($GraphifyWorkspace)) { $GraphifyWorkspace = $GraphifyWorkspace.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) }
$GraphifyDenyRoots = [Collections.Generic.List[string]]::new()
$GraphifyDenyPolicyInvalid = $false
function Resolve-GraphifyPolicyPath {
    param([string]$Path)
    if (-not $Path -or -not [IO.Path]::IsPathFullyQualified($Path)) { return $null }
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
        $full = if ([IO.Path]::IsPathFullyQualified($Path)) { [IO.Path]::GetFullPath($Path) } else { [IO.Path]::GetFullPath((Join-Path $GraphifyWorkspace $Path)) }
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
    if (-not $Path -or -not [IO.Path]::IsPathFullyQualified($Path)) { return $true }
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
function Test-GraphifyPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-GraphifySupportedPython $Candidate)) { return $false }
    & $Candidate -E -P -B -c "import graphify" 2>$null
    return $LASTEXITCODE -eq 0
}
function Test-GraphifySupportedPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    & $Candidate -E -P -B -c "import sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and sys.version_info.releaselevel == 'final' and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)" 2>$null
    return $LASTEXITCODE -eq 0
}
if ([IO.Path]::IsPathFullyQualified("$env:VIRTUAL_ENV")) {
    $activeVenv = Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $activeVenv)) { $activeVenv = Join-Path $env:VIRTUAL_ENV "bin/python" }
    if (Test-GraphifyPython $activeVenv) { $GraphifyPython = $activeVenv }
}
if (-not $GraphifyPython) {
    $uv = Resolve-GraphifyAmbientCommand uv
    if ($uv) {
        $uvDir = (& $uv tool dir 2>$null).Trim()
        $candidate = Join-Path $uvDir "graphifyy\Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $candidate)) { $candidate = Join-Path $uvDir "graphifyy/bin/python" }
        if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate) }
    }
}
if (-not $GraphifyPython) {
    $pipx = Resolve-GraphifyAmbientCommand pipx
    if ($pipx) {
        $venvs = (& $pipx environment --value PIPX_LOCAL_VENVS 2>$null).Trim()
        $candidate = Join-Path $venvs "graphifyy\Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $candidate)) { $candidate = Join-Path $venvs "graphifyy/bin/python" }
        if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate) }
    }
}
if (-not $GraphifyPython) {
    $graphify = Resolve-GraphifyAmbientCommand graphify
    if ($graphify) {
        $bindir = Split-Path -Parent $graphify
        foreach ($candidate in @((Join-Path $bindir "python.exe"), (Join-Path $bindir "../python.exe"))) {
            if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate); break }
        }
    }
}
if (-not $GraphifyPython) {
    foreach ($name in @("python3.14", "python3", "py", "python")) {
        $candidate = Resolve-GraphifyAmbientCommand $name
        if (-not $candidate) { continue }
        if ($name -eq "py") {
            $resolved = (& $candidate -3.14 -E -P -B -c "import sys; print(sys.executable)" 2>$null).Trim()
            if ($LASTEXITCODE -eq 0 -and -not (Test-GraphifyWorkspacePath $resolved) -and (Test-GraphifyPython $resolved)) { $GraphifyPython = [IO.Path]::GetFullPath($resolved); break }
        } elseif (Test-GraphifyPython $candidate) { $GraphifyPython = $candidate; break }
    }
}
if (-not $GraphifyPython -and -not $GraphifyDiscoveryOptional) { throw "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." }
& $GraphifyPython -E -P -B -m graphify clone <github-url> [--branch <branch>]
# Use the printed local path as the target for all subsequent steps.
```

**Multiple repos (cross-repo graph):**
```powershell
$env:GRAPHIFY_INPUT_PATH = "INPUT_PATH"
$GraphifyPython = $null
$GraphifyWorkspace = [IO.Path]::GetFullPath((Get-Location).Path)
if ($GraphifyWorkspace -ne [IO.Path]::GetPathRoot($GraphifyWorkspace)) { $GraphifyWorkspace = $GraphifyWorkspace.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) }
$GraphifyDenyRoots = [Collections.Generic.List[string]]::new()
$GraphifyDenyPolicyInvalid = $false
function Resolve-GraphifyPolicyPath {
    param([string]$Path)
    if (-not $Path -or -not [IO.Path]::IsPathFullyQualified($Path)) { return $null }
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
        $full = if ([IO.Path]::IsPathFullyQualified($Path)) { [IO.Path]::GetFullPath($Path) } else { [IO.Path]::GetFullPath((Join-Path $GraphifyWorkspace $Path)) }
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
    if (-not $Path -or -not [IO.Path]::IsPathFullyQualified($Path)) { return $true }
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
function Test-GraphifyPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-GraphifySupportedPython $Candidate)) { return $false }
    & $Candidate -E -P -B -c "import graphify" 2>$null
    return $LASTEXITCODE -eq 0
}
function Test-GraphifySupportedPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    & $Candidate -E -P -B -c "import sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and sys.version_info.releaselevel == 'final' and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)" 2>$null
    return $LASTEXITCODE -eq 0
}
if ([IO.Path]::IsPathFullyQualified("$env:VIRTUAL_ENV")) {
    $activeVenv = Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $activeVenv)) { $activeVenv = Join-Path $env:VIRTUAL_ENV "bin/python" }
    if (Test-GraphifyPython $activeVenv) { $GraphifyPython = $activeVenv }
}
if (-not $GraphifyPython) {
    $uv = Resolve-GraphifyAmbientCommand uv
    if ($uv) {
        $uvDir = (& $uv tool dir 2>$null).Trim()
        $candidate = Join-Path $uvDir "graphifyy\Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $candidate)) { $candidate = Join-Path $uvDir "graphifyy/bin/python" }
        if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate) }
    }
}
if (-not $GraphifyPython) {
    $pipx = Resolve-GraphifyAmbientCommand pipx
    if ($pipx) {
        $venvs = (& $pipx environment --value PIPX_LOCAL_VENVS 2>$null).Trim()
        $candidate = Join-Path $venvs "graphifyy\Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $candidate)) { $candidate = Join-Path $venvs "graphifyy/bin/python" }
        if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate) }
    }
}
if (-not $GraphifyPython) {
    $graphify = Resolve-GraphifyAmbientCommand graphify
    if ($graphify) {
        $bindir = Split-Path -Parent $graphify
        foreach ($candidate in @((Join-Path $bindir "python.exe"), (Join-Path $bindir "../python.exe"))) {
            if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate); break }
        }
    }
}
if (-not $GraphifyPython) {
    foreach ($name in @("python3.14", "python3", "py", "python")) {
        $candidate = Resolve-GraphifyAmbientCommand $name
        if (-not $candidate) { continue }
        if ($name -eq "py") {
            $resolved = (& $candidate -3.14 -E -P -B -c "import sys; print(sys.executable)" 2>$null).Trim()
            if ($LASTEXITCODE -eq 0 -and -not (Test-GraphifyWorkspacePath $resolved) -and (Test-GraphifyPython $resolved)) { $GraphifyPython = [IO.Path]::GetFullPath($resolved); break }
        } elseif (Test-GraphifyPython $candidate) { $GraphifyPython = $candidate; break }
    }
}
if (-not $GraphifyPython -and -not $GraphifyDiscoveryOptional) { throw "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." }
# Clone each repo, run the full pipeline on each, then merge
& $GraphifyPython -E -P -B -m graphify clone <url1>   # → ~/.graphify/repos/<owner1>/<repo1>
& $GraphifyPython -E -P -B -m graphify clone <url2>   # → ~/.graphify/repos/<owner2>/<repo2>
# Run /graphify on each local path to produce their graph.json files
# Then merge:
& $GraphifyPython -E -P -B -m graphify merge-graphs ~/.graphify/repos/<owner1>/<repo1>/graphify-out/graph.json ~/.graphify/repos/<owner2>/<repo2>/graphify-out/graph.json --out graphify-out/cross-repo-graph.json
```

Graphify clones into `~/.graphify/repos/<owner>/<repo>` and reuses existing clones on repeat runs. Each node in the merged graph carries a `repo` attribute so you can filter by origin.

**Multiple local subfolders (monorepo or multi-service layout):**

The skill pipeline writes all intermediate and final outputs to `graphify-out/` in the current working directory. Running the skill on each subfolder separately will clobber the same output dir. Instead, use the CLI directly for each subfolder — it places `graphify-out/` *inside* the scanned path:

```powershell
$env:GRAPHIFY_INPUT_PATH = "INPUT_PATH"
$GraphifyPython = $null
$GraphifyWorkspace = [IO.Path]::GetFullPath((Get-Location).Path)
if ($GraphifyWorkspace -ne [IO.Path]::GetPathRoot($GraphifyWorkspace)) { $GraphifyWorkspace = $GraphifyWorkspace.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) }
$GraphifyDenyRoots = [Collections.Generic.List[string]]::new()
$GraphifyDenyPolicyInvalid = $false
function Resolve-GraphifyPolicyPath {
    param([string]$Path)
    if (-not $Path -or -not [IO.Path]::IsPathFullyQualified($Path)) { return $null }
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
        $full = if ([IO.Path]::IsPathFullyQualified($Path)) { [IO.Path]::GetFullPath($Path) } else { [IO.Path]::GetFullPath((Join-Path $GraphifyWorkspace $Path)) }
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
    if (-not $Path -or -not [IO.Path]::IsPathFullyQualified($Path)) { return $true }
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
function Test-GraphifyPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-GraphifySupportedPython $Candidate)) { return $false }
    & $Candidate -E -P -B -c "import graphify" 2>$null
    return $LASTEXITCODE -eq 0
}
function Test-GraphifySupportedPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    & $Candidate -E -P -B -c "import sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and sys.version_info.releaselevel == 'final' and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)" 2>$null
    return $LASTEXITCODE -eq 0
}
if ([IO.Path]::IsPathFullyQualified("$env:VIRTUAL_ENV")) {
    $activeVenv = Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $activeVenv)) { $activeVenv = Join-Path $env:VIRTUAL_ENV "bin/python" }
    if (Test-GraphifyPython $activeVenv) { $GraphifyPython = $activeVenv }
}
if (-not $GraphifyPython) {
    $uv = Resolve-GraphifyAmbientCommand uv
    if ($uv) {
        $uvDir = (& $uv tool dir 2>$null).Trim()
        $candidate = Join-Path $uvDir "graphifyy\Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $candidate)) { $candidate = Join-Path $uvDir "graphifyy/bin/python" }
        if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate) }
    }
}
if (-not $GraphifyPython) {
    $pipx = Resolve-GraphifyAmbientCommand pipx
    if ($pipx) {
        $venvs = (& $pipx environment --value PIPX_LOCAL_VENVS 2>$null).Trim()
        $candidate = Join-Path $venvs "graphifyy\Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $candidate)) { $candidate = Join-Path $venvs "graphifyy/bin/python" }
        if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate) }
    }
}
if (-not $GraphifyPython) {
    $graphify = Resolve-GraphifyAmbientCommand graphify
    if ($graphify) {
        $bindir = Split-Path -Parent $graphify
        foreach ($candidate in @((Join-Path $bindir "python.exe"), (Join-Path $bindir "../python.exe"))) {
            if ((-not (Test-GraphifyWorkspacePath $candidate)) -and (Test-GraphifyPython $candidate)) { $GraphifyPython = [IO.Path]::GetFullPath($candidate); break }
        }
    }
}
if (-not $GraphifyPython) {
    foreach ($name in @("python3.14", "python3", "py", "python")) {
        $candidate = Resolve-GraphifyAmbientCommand $name
        if (-not $candidate) { continue }
        if ($name -eq "py") {
            $resolved = (& $candidate -3.14 -E -P -B -c "import sys; print(sys.executable)" 2>$null).Trim()
            if ($LASTEXITCODE -eq 0 -and -not (Test-GraphifyWorkspacePath $resolved) -and (Test-GraphifyPython $resolved)) { $GraphifyPython = [IO.Path]::GetFullPath($resolved); break }
        } elseif (Test-GraphifyPython $candidate) { $GraphifyPython = $candidate; break }
    }
}
if (-not $GraphifyPython -and -not $GraphifyDiscoveryOptional) { throw "No trusted Graphify Python 3.14.2-final interpreter found; rerun Step 1." }
& $GraphifyPython -E -P -B -m graphify extract ./core/     # → ./core/graphify-out/graph.json
& $GraphifyPython -E -P -B -m graphify extract ./service/  # → ./service/graphify-out/graph.json
& $GraphifyPython -E -P -B -m graphify extract ./platform/ # → ./platform/graphify-out/graph.json
# Add --backend gemini|kimi|openai|deepseek|claude-cli depending on which API key you have set

# Then merge at the project root:
& $GraphifyPython -E -P -B -m graphify merge-graphs ./core/graphify-out/graph.json ./service/graphify-out/graph.json ./platform/graphify-out/graph.json --out graphify-out/graph.json
```

Once `graphify-out/graph.json` exists, the fast path above takes over: any codebase question runs `graphify query` directly on the merged graph — no re-extraction, no size gate.
