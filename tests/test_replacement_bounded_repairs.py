"""Replacement regressions on disposable roots with inert maintenance only."""
import importlib
import json
import os
from pathlib import Path
import stat

import pytest

from graphify import canonical_replacement as replacement
from graphify import transaction as tx
from tests.test_canonical_replacement import case as case, modeled as modeled


@pytest.mark.parametrize("mask", [0o022, 0o777])
def test_sealed_stage_remains_private_and_readable_under_umask(case, mask):
    request, fragments = case
    sealed = replacement.build_data(request, fragments)
    previous = os.umask(mask)
    try:
        replacement._seal_data(request, sealed)
    finally:
        os.umask(previous)
    stage = Path(request["stage"])
    assert stat.S_IMODE(stage.stat().st_mode) == 0o600
    assert replacement._load(stage) == json.loads(replacement._bytes(sealed))
