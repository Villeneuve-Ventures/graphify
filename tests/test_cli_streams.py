"""CLI stream setup supports optional, duck-typed encoding control."""
from __future__ import annotations

from io import BytesIO, StringIO, TextIOWrapper
import sys

import pytest

from graphify import __main__ as cli


@pytest.mark.parametrize("kind", ["text-wrapper", "custom", "missing", "noncallable", "raises", "none"])
def test_cli_reconfigures_available_streams_and_continues(monkeypatch, kind):
    class Stream:
        def __init__(self):
            self.calls = []

        def reconfigure(self, **kwargs):
            self.calls.append(kwargs)
            if kind == "raises":
                raise OSError("stream cannot change encoding")

    stderr = Stream()
    if kind == "text-wrapper":
        stdout = TextIOWrapper(BytesIO(), encoding="ascii", errors="strict")
    elif kind == "missing":
        stdout = StringIO()
    elif kind == "none":
        stdout = None
    else:
        stdout = Stream()
        if kind == "noncallable":
            monkeypatch.setattr(stdout, "reconfigure", None)

    # The workspace route exits after stream setup and avoids other CLI effects.
    def dispatch(args):
        assert args == ["status"]
        return 17

    monkeypatch.setattr("graphify.workspace.cli.run_workspace_cli", dispatch)
    monkeypatch.setattr(sys, "argv", ["graphify", "workspace", "status"])
    with monkeypatch.context() as streams:
        streams.setattr(sys, "stdout", stdout)
        streams.setattr(sys, "stderr", stderr)
        with pytest.raises(SystemExit) as result:
            cli._run_cli()
    assert result.value.code == 17
    expected = [{"encoding": "utf-8", "errors": "replace"}]
    assert stderr.calls == expected
    if kind == "text-wrapper":
        assert isinstance(stdout, TextIOWrapper)
        assert isinstance(stdout.buffer, BytesIO)
        assert stdout.encoding == "utf-8" and stdout.errors == "replace"
        stdout.write("→ ✅")
        stdout.flush()
        assert stdout.buffer.getvalue() == "→ ✅".encode("utf-8")
        stdout.close()
    elif kind in {"custom", "raises"}:
        assert isinstance(stdout, Stream)
        assert stdout.calls == expected
    elif kind == "missing":
        assert isinstance(stdout, StringIO)
        stdout.write("→ ✅")
        assert stdout.getvalue() == "→ ✅"
    elif kind == "noncallable":
        assert isinstance(stdout, Stream)
        assert stdout.calls == []
