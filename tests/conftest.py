from __future__ import annotations

from typing import Any

import pytest

_ANALYZE_WARNING_FILTERS = (
    "ignore:Tensorflow not installed; ParametricUMAP will be unavailable:ImportWarning:umap",
    "ignore:Please import `random` from the `scipy\\.sparse` namespace.*:"
    "DeprecationWarning:hyppo\\.independence\\.hhg",
    "ignore:The keyword argument 'nopython=False' was supplied.*:Warning:numba\\.core\\.decorators",
)


@pytest.fixture(autouse=True)
def _isolate_transaction_process_authority(monkeypatch):
    """Keep process-local transaction authority scoped to one test."""
    from graphify import transaction as transaction_module

    transaction_module._AUTHORITY.set(None)
    for name in (*transaction_module._TRANSACTION_ENV_SIGNALS, "GRAPHIFY_PREPARED_OUTPUT"):
        monkeypatch.delenv(name, raising=False)
    yield
    transaction_module._AUTHORITY.set(None)


def pytest_collection_modifyitems(items: list[Any]) -> None:
    for item in items:
        if item.path.name != "test_analyze.py":
            continue
        for warning_filter in _ANALYZE_WARNING_FILTERS:
            item.add_marker(pytest.mark.filterwarnings(warning_filter))
