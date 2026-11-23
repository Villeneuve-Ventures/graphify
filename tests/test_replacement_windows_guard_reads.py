"""Windows marker-only admission must not import the POSIX issuer reader."""
import builtins
import os
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
