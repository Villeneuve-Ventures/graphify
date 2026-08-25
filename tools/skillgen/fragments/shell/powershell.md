```powershell
# Detect Python with graphify — uv/pipx-aware (fixes #831)
New-Item -ItemType Directory -Force -Path graphify-out | Out-Null
$GRAPHIFY_PYTHON = $null

function Get-Python314Candidates {
    $versionCheck = "import sys; ok = sys.implementation.name == 'cpython' and sys.version_info.releaselevel == 'final' and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0); print(sys.executable) if ok else sys.exit(1)"

    $py314 = Get-Command python3.14 -ErrorAction SilentlyContinue
    if ($py314) {
        $resolved = (& $py314.Source -c $versionCheck 2>$null)
        if ($LASTEXITCODE -eq 0 -and $resolved) { Write-Output ("$resolved".Trim()) }
    }

    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $resolved = (& $launcher.Source -3.14 -c $versionCheck 2>$null)
        if ($LASTEXITCODE -eq 0 -and $resolved) { Write-Output ("$resolved".Trim()) }
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        $resolved = (& $python.Source -c $versionCheck 2>$null)
        if ($LASTEXITCODE -eq 0 -and $resolved) { Write-Output ("$resolved".Trim()) }
    }
}

function Find-Python314 {
    return (Get-Python314Candidates | Select-Object -First 1)
}

function Test-GraphifyPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    & $Candidate -c "import graphify, sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and sys.version_info.releaselevel == 'final' and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)" 2>$null
    return $LASTEXITCODE -eq 0
}

function Find-GraphifyPython {
    # 1. uv tool install — 'uv tool dir' is authoritative, respects UV_TOOL_DIR automatically
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        $uvDir = (uv tool dir 2>$null).Trim()
        if ($uvDir) {
            $py = Join-Path $uvDir "graphifyy\Scripts\python.exe"
            if ((Test-Path $py) -and (Test-GraphifyPython $py)) { return $py }
        }
    }
    # 2. pipx install — 'pipx environment' respects PIPX_HOME automatically
    if (Get-Command pipx -ErrorAction SilentlyContinue) {
        $venvs = (pipx environment --value PIPX_LOCAL_VENVS 2>$null).Trim()
        if ($venvs) {
            $py = Join-Path $venvs "graphifyy\Scripts\python.exe"
            if ((Test-Path $py) -and (Test-GraphifyPython $py)) { return $py }
        }
    }
    # 3. Supported Python 3.14 install / active environment
    foreach ($py in @(Get-Python314Candidates)) {
        if (Test-GraphifyPython $py) { return $py }
    }
    return $null
}

# Try to find the right Python (uv → pipx → active env)
$GRAPHIFY_PYTHON = Find-GraphifyPython

# Not found — install then re-detect
if (-not $GRAPHIFY_PYTHON) {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        uv tool install --python ">=3.14.2,<3.15" --upgrade graphifyy -q 2>&1 | Select-Object -Last 3
    } else {
        $installPython = Find-Python314
        if (-not $installPython) {
            throw "Graphify requires Python 3.14.2 through the final 3.14.x release."
        }
        & $installPython -m pip install graphifyy -q 2>&1 | Select-Object -Last 3
    }
    $GRAPHIFY_PYTHON = Find-GraphifyPython
}
if (-not $GRAPHIFY_PYTHON) {
    throw "Graphify installation did not produce a usable Python 3.14 environment."
}

# Save interpreter path — all subsequent steps read this
$GRAPHIFY_PYTHON | Out-File -FilePath graphify-out\.graphify_python -Encoding utf8 -NoNewline
# Save scan root so `graphify update` (no args) knows where to look next time
(Resolve-Path INPUT_PATH).Path | Out-File -FilePath graphify-out\.graphify_root -Encoding utf8 -NoNewline
```

If the import succeeds, print nothing and move straight to Step 2.

**In every subsequent block, run Python through the saved interpreter — `& (Get-Content graphify-out\.graphify_python)` in place of a bare `python3` — so every step uses the interpreter that actually has graphify.**
