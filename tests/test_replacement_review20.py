"""Bounded coordinator regressions use disposable files and modeled metadata."""
import contextlib
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from graphify import canonical_replacement as replacement
from graphify import transaction as tx
from tests.test_canonical_replacement import case as case, modeled as modeled


def test_preservation_inventory_includes_root_mode(tmp_path):
    output = tmp_path / "output"
    output.mkdir(mode=0o755)
    inventory = replacement._tree(output)
    assert stat.S_IMODE(inventory["."]["mode"]) == 0o755
    output.chmod(0o700)
    assert replacement._tree(output) != inventory


def test_root_mode_drift_refuses_before_operation_effects(case, modeled):
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    output = Path(request["output"])
    output.chmod(0o700 if stat.S_IMODE(output.stat().st_mode) != 0o700 else 0o755)
    before = (output / "graph.json").read_bytes()
    with pytest.raises(tx.PendingTransactionError, match="preview changed"):
        replacement.execute(request, seal, plan["preview_sha256"])
    assert not Path(request["archive"]).exists()
    assert not Path(request["journal"]).exists()
    assert (output / "graph.json").read_bytes() == before


@pytest.mark.parametrize("overflow_pass", [1, 2])
def test_preservation_enumeration_refuses_at_remaining_budget(tmp_path, monkeypatch, overflow_pass):
    output = tmp_path / "output"
    output.mkdir()
    (output / "one").write_text("one")
    identity = output.stat().st_ino
    real_scan = os.scandir
    calls, consumed = [], []
    monkeypatch.setattr(replacement, "MAX_PRESERVATION_ENTRIES", 2, raising=False)

    @contextlib.contextmanager
    def bounded_scan(path):
        if not isinstance(path, int) or os.fstat(path).st_ino != identity:
            with real_scan(path) as entries:
                yield entries
            return
        calls.append(True)

        def generated_entries():
            names = ["one"] if len(calls) != overflow_pass else ["one", "two", "three", "never"]
            for index, name in enumerate(names, 1):
                consumed.append(index)
                assert index <= 3, "enumeration exceeded the limit plus one"
                yield SimpleNamespace(name=name)

        yield generated_entries()

    monkeypatch.setattr(os, "scandir", bounded_scan)
    with pytest.raises(tx.PendingTransactionError, match="preservation entry bound"):
        replacement._tree(output)
    assert len(calls) == overflow_pass
    assert consumed[-1] == 3


@pytest.mark.parametrize("failure", ["device", "mount"])
def test_preview_refuses_mounted_output_without_effects(case, monkeypatch, failure):
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    output = Path(request["output"])
    real_stat = os.stat

    def metadata(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if failure != "device" or isinstance(path, int) or Path(path) != output:
            return info
        return SimpleNamespace(**{name: info.st_dev + 1 if name == "st_dev" else getattr(info, name)
                                  for name in dir(info) if name.startswith("st_")})

    monkeypatch.setattr(os, "stat", metadata)
    monkeypatch.setattr(os.path, "ismount", lambda path: failure == "mount" and Path(path) == output)
    before = (output / "graph.json").read_bytes()
    with pytest.raises(tx.PendingTransactionError, match="filesystem|mount"):
        replacement.preview(request, seal)
    assert not Path(request["journal"]).exists()
    assert not Path(request["archive"]).exists()
    assert (output / "graph.json").read_bytes() == before
