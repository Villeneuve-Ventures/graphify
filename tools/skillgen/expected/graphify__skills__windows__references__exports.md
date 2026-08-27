# graphify reference: extra exports and benchmark

Load this when the user passed one of the export flags (`--wiki`, `--neo4j`, `--neo4j-push`, `--falkordb`, `--falkordb-push`, `--svg`, `--graphml`, `--mcp`), or when the corpus is large enough for the token-reduction benchmark. Each step runs only for its own flag.

### Step 6b - Wiki (only if --wiki flag)

**Only run this step if `--wiki` was explicitly given in the original command.**

Run this before Step 9 (cleanup) so `.graphify_labels.json` is still available.

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
& $GraphifyPython -E -P -B -m graphify export wiki
```

### Step 7 - Neo4j export (only if --neo4j or --neo4j-push flag)

**If `--neo4j`** - generate a Cypher file for manual import:

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
& $GraphifyPython -E -P -B -m graphify export neo4j
```

**If `--neo4j-push <uri>`** - push directly to a running Neo4j instance. Ask the user for credentials if not provided:

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
& $GraphifyPython -E -P -B -m graphify export neo4j --push bolt://localhost:7687 --user neo4j --password PASSWORD
```

Default URI is `bolt://localhost:7687`, default user is `neo4j`. Uses MERGE - safe to re-run without creating duplicates.

### Step 7a - FalkorDB export (only if --falkordb or --falkordb-push flag)

**If `--falkordb`** - generate a Cypher file. The statements are OpenCypher, but FalkorDB's `GRAPH.QUERY` runs one statement at a time (no bulk script import like Neo4j's `cypher-shell`), so prefer `--falkordb-push` to load a graph. Use this only when you want the portable `cypher.txt` artifact:

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
& $GraphifyPython -E -P -B -m graphify export falkordb
```

**If `--falkordb-push <uri>`** - push directly to a running FalkorDB instance. Credentials are optional; ask the user only if the instance requires auth:

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
& $GraphifyPython -E -P -B -m graphify export falkordb --push falkordb://localhost:6379
```

Default URI is `falkordb://localhost:6379` (the scheme is informational - `redis://` or a bare `host:port` work too), auth is optional, and the target graph defaults to `graphify`. Uses MERGE - safe to re-run without creating duplicates.

### Step 7b - SVG export (only if --svg flag)

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
& $GraphifyPython -E -P -B -m graphify export svg
```

### Step 7c - GraphML export (only if --graphml flag)

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
& $GraphifyPython -E -P -B -m graphify export graphml
```

### Step 7d - MCP server (only if --mcp flag)

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
& $GraphifyPython -E -P -B -m graphify.serve graphify-out/graph.json
```

This starts a stdio MCP server that exposes tools: `query_graph`, `get_node`, `get_neighbors`, `get_community`, `god_nodes`, `graph_stats`, `shortest_path`. Add to Claude Desktop or any MCP-compatible agent orchestrator so other agents can query the graph live.

To configure in Claude Desktop, add to `claude_desktop_config.json`. Claude Desktop can't run `$(...)`, and under `uv tool install` the system `python3` can't import graphify — so set `command` to the **absolute interpreter path** emitted by the fresh discovery block below:
```powershell
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
$graphPath = Join-Path ([IO.Path]::GetFullPath((Get-Location).Path)) "graphify-out\graph.json"
[ordered]@{
    mcpServers = [ordered]@{
        "graphify" = [ordered]@{
            command = $GraphifyPython
            args = @("-E", "-P", "-B", "-m", "graphify.serve", $graphPath)
        }
    }
} | ConvertTo-Json -Depth 4
```

### Step 8 - Token reduction benchmark (only if total_words > 5000)

If `total_words` from `graphify-out/.graphify_detect.json` is greater than 5,000, run:

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
& $GraphifyPython -E -P -B -m graphify benchmark
```

Print the output directly in chat. If `total_words <= 5000`, skip silently - the graph value is structural clarity, not token compression, for small corpora.
