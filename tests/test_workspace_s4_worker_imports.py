"""Worker imports cannot inherit source paths from an embedding caller."""
import json
import os
import sys
import time

import pytest

from graphify.workspace import _readonly
from graphify.workspace._readonly import ReadOnlyFailure, run_readonly


@pytest.mark.parametrize("ambient", ["", ".", "source", "absolute"])
@pytest.mark.parametrize("module", ["graphify", "networkx", "statistics"])
def test_worker_excludes_ambient_source_imports(tmp_path, monkeypatch, ambient, module):
    source = tmp_path / "source"
    source.mkdir()
    if module == "statistics":
        target = source / "statistics.py"
    else:
        package = source / module
        package.mkdir()
        target = package / "__init__.py"
    target.write_text("import sys; sys.stdout.write('FORGED'); raise SystemExit(0)\n")
    if ambient in {"", "."}:
        monkeypatch.chdir(source)
        entry = ambient
    elif ambient == "source":
        monkeypatch.chdir(tmp_path)
        entry = ambient
    else:
        monkeypatch.chdir(tmp_path)
        entry = str(source)
    monkeypatch.setattr(sys, "path", [entry, *sys.path])

    output = run_readonly(
        f"import {module}; import json, sys; "
        f"sys.stdout.write(json.dumps({module}.__file__))",
        b"", deadline_ns=time.monotonic_ns() + 10_000_000_000,
        max_input_bytes=1024, max_output_bytes=65536,
    )
    assert output != b"FORGED"
    assert str(source) not in json.loads(output)


@pytest.mark.parametrize("unsafe", ["fifo", "oversized"])
def test_worker_refuses_unsafe_installation_path_file(tmp_path, monkeypatch, unsafe):
    site_root = tmp_path / "site-packages"
    site_root.mkdir()
    pth = site_root / "dependencies.pth"
    if unsafe == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO unavailable")
        os.mkfifo(pth)
    else:
        pth.write_bytes(b"x" * 65537)
    monkeypatch.setattr(_readonly.site, "getsitepackages", lambda: [str(site_root)])
    with pytest.raises(ReadOnlyFailure, match="unsafe installation path file"):
        run_readonly("pass", b"", deadline_ns=time.monotonic_ns() + 5_000_000_000,
                     max_input_bytes=1024, max_output_bytes=1024)


def test_worker_skips_missing_installation_site_root(tmp_path, monkeypatch):
    missing = tmp_path / "missing" / "site-packages"
    roots = _readonly.site.getsitepackages()
    monkeypatch.setattr(_readonly.site, "getsitepackages", lambda: [str(missing), *roots])
    output = run_readonly(
        "import networkx; print('ready')", b"",
        deadline_ns=time.monotonic_ns() + 10_000_000_000,
        max_input_bytes=1024, max_output_bytes=1024,
    )
    assert output == b"ready\n"
    assert not missing.exists()


def test_worker_reports_site_scan_failure(tmp_path, monkeypatch):
    site_root = tmp_path / "site-packages"
    site_root.mkdir()
    original = os.scandir

    def denied(path):
        if os.fspath(path) == os.fspath(site_root):
            raise PermissionError("site directory unreadable")
        return original(path)

    monkeypatch.setattr(_readonly.site, "getsitepackages", lambda: [str(site_root)])
    monkeypatch.setattr(_readonly.os, "scandir", denied)
    with pytest.raises(ReadOnlyFailure, match="installation site directory"):
        run_readonly("pass", b"", deadline_ns=time.monotonic_ns() + 5_000_000_000,
                     max_input_bytes=1024, max_output_bytes=1024)
