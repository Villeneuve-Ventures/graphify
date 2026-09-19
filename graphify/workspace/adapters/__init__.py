"""Exact fixture selection. PROBE never grants execution or promotion."""
from .base import AdapterIntent, AdapterSelection, CompatibilityTuple, UnsupportedCompatibility


def select_adapter(compatibility: CompatibilityTuple, *, expected: CompatibilityTuple,
                   intent: AdapterIntent) -> AdapterSelection:
    if not isinstance(intent, AdapterIntent):
        raise UnsupportedCompatibility("explicit adapter intent required")
    if (type(compatibility) is not CompatibilityTuple
            or type(expected) is not CompatibilityTuple or compatibility != expected):
        raise UnsupportedCompatibility("candidate is not the exact authorized tuple")
    if intent is not AdapterIntent.PROBE:
        raise UnsupportedCompatibility("S2 fixture is non-executable and non-promoting")
    return AdapterSelection(compatibility, intent)
