"""Global storage mutations refuse retained namespaces; all storage is disposable."""
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import networkx as nx
import pytest

from graphify import canonical_replacement as replacement
from graphify import global_graph as global_store
from graphify import transaction as tx


@pytest.fixture
def registration(tmp_path):
    pytest.importorskip("fcntl")
    journal = tmp_path / "journal.json"
    journal.write_bytes(replacement._bytes({
        "request_digest": "prior-operation", "state": "COMMITTED",
    }))

    def register(path):
        replacement._register(path, journal, "prior-operation", "archive")

    return register
