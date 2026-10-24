"""Corpus observations retain their reader and native descriptor boundaries."""
from __future__ import annotations

from contextlib import nullcontext
import os

import pytest

from graphify import transaction as tx
from graphify.source_io import SourceIO


@pytest.mark.parametrize("scoped", [False, True], ids=["native", "source-io"])
def test_corpus_directory_reads_preserve_files_and_writer_boundary(tmp_path, scoped):
    root = tmp_path.resolve()
    output = root / "output"
    output.mkdir()
    marker = output / "marker.json"
    marker.write_bytes(b'{"observed": true}\n')
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with SourceIO(root) if scoped else nullcontext() as inputs:
        observer = tx._OperationalCorpusScan(root, output, source_io=inputs, ambient_output=False)
        fingerprints = {}
        with observer._directory(output) as directory:
            assert directory.path == output
            assert observer._read(directory, "marker.json", fingerprints) == {"observed": True}
            assert str(marker) in fingerprints
            if scoped:
                assert isinstance(directory, tx._ScopedCorpusDirectory)
                assert not isinstance(directory, tx.OutputCapability)
                assert not hasattr(directory, "fd")
                with pytest.raises(TypeError, match="requires an OutputCapability"):
                    # Deliberately pass an invalid type to test runtime refusal.
                    getattr(tx, "begin_transaction")(
                        "full", output, output=root / "not-created", root_capability=directory,
                    )
                assert not (root / "not-created").exists()
            else:
                assert isinstance(directory, tx.OutputCapability)
                fd = directory.fd
                os.fstat(fd)
        assert observer.finish() == []
        if not scoped:
            with pytest.raises(OSError):
                os.fstat(fd)
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == [
        "output", "output/marker.json",
    ]
