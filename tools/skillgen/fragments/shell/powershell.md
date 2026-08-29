```powershell
# Detect Python with graphify — uv/pipx-aware (fixes #831)
New-Item -ItemType Directory -Force -Path graphify-out | Out-Null
$GRAPHIFY_PYTHON = $null

function Get-Python314Candidates {
    $versionCheck = "import sys; ok = sys.implementation.name == 'cpython' and sys.version_info.releaselevel == 'final' and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0); print(sys.executable) if ok else sys.exit(1)"

    $py314 = Get-Command python3.14 -ErrorAction SilentlyContinue
    if ($py314) {
        $resolved = (& $py314.Source -E -P -B -c $versionCheck 2>$null)
        if ($LASTEXITCODE -eq 0 -and $resolved) { Write-Output ("$resolved".Trim()) }
    }

    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $resolved = (& $launcher.Source -3.14 -E -P -B -c $versionCheck 2>$null)
        if ($LASTEXITCODE -eq 0 -and $resolved) { Write-Output ("$resolved".Trim()) }
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        $resolved = (& $python.Source -E -P -B -c $versionCheck 2>$null)
        if ($LASTEXITCODE -eq 0 -and $resolved) { Write-Output ("$resolved".Trim()) }
    }
}

function Find-Python314 {
    return (Get-Python314Candidates | Select-Object -First 1)
}

function Test-GraphifyPython {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    & $Candidate -E -P -B -c "import graphify, sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and sys.version_info.releaselevel == 'final' and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)" 2>$null
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
        & $installPython -E -P -B -m pip install graphifyy -q 2>&1 | Select-Object -Last 3
    }
    $GRAPHIFY_PYTHON = Find-GraphifyPython
}
if (-not $GRAPHIFY_PYTHON) {
    throw "Graphify installation did not produce a usable Python 3.14 environment."
}

# Save interpreter path — all subsequent steps read this
& $GRAPHIFY_PYTHON -E -P -B -m graphify.interpreter_pointer write graphify-out\.graphify_python
if ($LASTEXITCODE -ne 0) { throw "Failed to publish the Graphify interpreter pointer." }

# Full-build transaction handoff. The exact token binds the canonical input
# root and actual output directory; environment ids/roots alone are not owner authority.
$GRAPHIFY_TRANSACTION_TOKEN = & $GRAPHIFY_PYTHON -E -P -B -c @'
import sys
from pathlib import Path
from graphify.transaction import begin_transaction, stage_transaction_handoff
root = Path(sys.argv[1]).resolve(strict=True)
output = (root / 'graphify-out').resolve()
print(stage_transaction_handoff(begin_transaction('full', root, output=output)).path, end='')
'@ INPUT_PATH
if ($LASTEXITCODE -ne 0) { throw "Graphify transaction handoff failed." }
$Env:GRAPHIFY_TRANSACTION_TOKEN = [string]$GRAPHIFY_TRANSACTION_TOKEN
function Invoke-GraphifyTransactionPython {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$PythonArgs)
    if (-not $Env:GRAPHIFY_TRANSACTION_TOKEN) {
        throw "Missing immutable Graphify transaction token."
    }
    & $GRAPHIFY_PYTHON -E -P -B -m graphify.transaction run-token `
        $Env:GRAPHIFY_TRANSACTION_TOKEN -- @PythonArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
```

If the import succeeds, print nothing and move straight to Step 2.

For a full build with an explicit `INPUT_PATH`, persist the scan root in a separate block:

```powershell
$GraphifyActiveTokenCode = 'from graphify.transaction import active_transaction_token_path; print(active_transaction_token_path())'; $GraphifyPreparedRootCode = 'import sys; from pathlib import Path; Path(".graphify_root").write_text(str(Path(sys.argv[1]).resolve(strict=True)), encoding="utf-8")'
$GraphifyTransactionToken = & $GraphifyPython -E -P -B -c $GraphifyActiveTokenCode
$GraphifyPreparedRoot = (Resolve-Path INPUT_PATH).Path
& $GraphifyPython -E -P -B -m graphify.transaction run-prepared-token $GraphifyTransactionToken '--' -c $GraphifyPreparedRootCode $GraphifyPreparedRoot
if ($LASTEXITCODE -ne 0) { throw "Failed to write the prepared Graphify scan root." }
```

Do not run that scan-root block for no-path subcommands such as `query`, `path`,
`explain`, hooks, installs, or exports. The interpreter bootstrap and
`.graphify_python` persistence are independent of `.graphify_root`.

**In every subsequent block, run Python through the saved interpreter — `& (Get-Content graphify-out\.graphify_python) -E -P -B` in place of a bare `python3` — so every step uses the interpreter that actually has graphify without importing project-local or `PYTHONPATH` shadows or writing bytecode.**

The saved interpreter and its user-site packages are trusted inputs outside the
inspected-corpus boundary. Pointer symlink and time-of-check/time-of-use hardening
remain separate work; these startup flags do not provide that identity guarantee.
