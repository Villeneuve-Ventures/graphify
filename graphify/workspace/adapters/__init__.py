"""Exact candidate selection. PROBE never grants execution or promotion."""
from .base import AdapterIntent, AdapterSelection, CompatibilityTuple, UnsupportedCompatibility


def select_adapter(compatibility: CompatibilityTuple, *, expected: CompatibilityTuple,
                   intent: AdapterIntent) -> AdapterSelection:
    if not isinstance(intent, AdapterIntent):
        raise UnsupportedCompatibility("explicit adapter intent required")
    if (type(compatibility) is not CompatibilityTuple
            or type(expected) is not CompatibilityTuple or compatibility != expected):
        raise UnsupportedCompatibility("candidate is not the exact authorized tuple")
    if intent is not AdapterIntent.PROBE:
        members = compatibility.manifest.to_dict()["package_members"]
        if not {"graphify/workspace/adapters/v8.py", "graphify/workspace/sync.py",
                "graphify/workspace/query.py"} <= set(members):
            raise UnsupportedCompatibility("candidate lacks the S4 operational members")
    return AdapterSelection(compatibility, intent,
                            executable=intent in {AdapterIntent.EXECUTE, AdapterIntent.STAGE, AdapterIntent.QUERY},
                            promotable=intent is AdapterIntent.PROMOTE)
