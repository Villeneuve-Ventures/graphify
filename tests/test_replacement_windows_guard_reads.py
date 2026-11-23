"""Windows marker-only admission must not import the POSIX issuer reader."""
import builtins
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from graphify import canonical_replacement as replacement


def test_windows_read_guard_does_not_import_posix_issuer(monkeypatch, tmp_path):
    real_import = builtins.__import__
    calls = []

    def windows_import(name, *args, **kwargs):
        if name == "replacement_guard_issuers":
            raise ModuleNotFoundError("POSIX issuer imports fcntl")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", windows_import)
    monkeypatch.setattr(replacement, "os", SimpleNamespace(name="nt", path=os.path))
    monkeypatch.setattr(replacement, "_guard_path", lambda path, **kwargs: calls.append(kwargs))
    replacement.guard_output(tmp_path / "ordinary", mutation=False)
    assert calls and all(not call["mutation"] and call["issuer_policy"] is None for call in calls)


def test_fresh_guard_import_and_windows_output_admission_without_fcntl(tmp_path):
    script = """
import builtins
import os
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, sys.argv[1])
# Load the existing transaction platform adapter before blocking imports on this
# POSIX host. The coordinator and issuer reader must still be fresh imports.
from graphify import transaction
assert 'graphify.canonical_replacement' not in sys.modules
assert 'graphify.replacement_guard_issuers' not in sys.modules
assert 'graphify.hook_installation' not in sys.modules
real_import = builtins.__import__

def without_fcntl(name, *args, **kwargs):
    if name == 'fcntl':
        raise ModuleNotFoundError('fcntl is unavailable on Windows')
    return real_import(name, *args, **kwargs)

builtins.__import__ = without_fcntl
from graphify import canonical_replacement as replacement
from graphify import storage_guard
windows_os = SimpleNamespace(**{**vars(os), 'name': 'nt'})
replacement.os = windows_os
storage_guard.os = windows_os
# Windows directory handles need a native adapter. Intercept that traversal on
# this host while checking both public entry points and issuer-reader isolation.
calls = []
replacement._guard_path = lambda path, **kwargs: calls.append(kwargs)
candidate = Path(sys.argv[2])
replacement.guard_output(candidate, mutation=False)
storage_guard.require_ordinary_output(candidate)
assert calls and any(not call['mutation'] for call in calls)
assert any(call['mutation'] for call in calls)
assert all(call['issuer_policy'] is None for call in calls)
assert not candidate.exists()
assert 'graphify.hook_installation' not in sys.modules
print('windows-guard-import-ok')
"""
    candidate = tmp_path / "ordinary"
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(Path(__file__).resolve().parents[1]), str(candidate)],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "windows-guard-import-ok"
    assert list(tmp_path.iterdir()) == []
