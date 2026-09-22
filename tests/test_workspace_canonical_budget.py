"""Focused canonical encoding budget regressions, runnable with unittest."""
import json
import unittest
from unittest.mock import patch

from graphify.workspace import contracts


class CanonicalBudgetTests(unittest.TestCase):
    def test_aggregate_budget_stops_before_full_copy_or_encoding(self):
        visited = []

        class ObservedList(list):
            def __iter__(self):
                for item in super().__iter__():
                    visited.append(item)
                    yield item

        value = ObservedList(["x" * 32] * 20)
        encode = contracts.json.dumps
        with (patch.object(contracts, "MAX_DOCUMENT_BYTES", 128),
              patch.object(contracts.json, "dumps", wraps=encode) as dumps):
            with self.assertRaises(contracts.ContractError):
                contracts.canonical_json_bytes(value)
        self.assertLess(len(visited), len(value))
        dumps.assert_not_called()

    def test_exact_boundaries_preserve_canonical_bytes(self):
        values = [None, True, False, 0, -123456789, 10**100,
                  "", '"\\\b\f\n\r\t\x00\x1f', "café 😀", "e\u0301",
                  "α\u0313\u0300\u0345", [], {}, (1, "é"),
                  {"z": [True, None], "e\u0301": {"a": -1}}]
        import unicodedata

        def normalized(value):
            if isinstance(value, str):
                return unicodedata.normalize("NFC", value)
            if isinstance(value, dict):
                return {normalized(k): normalized(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [normalized(v) for v in value]
            return value

        for value in values:
            expected = (json.dumps(normalized(value), ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), allow_nan=False) + "\n").encode()
            with self.subTest(value=repr(value)):
                with patch.object(contracts, "MAX_DOCUMENT_BYTES", len(expected)):
                    self.assertEqual(contracts.canonical_json_bytes(value), expected)
                    self.assertEqual(contracts.decode_canonical(expected), json.loads(expected))
                with patch.object(contracts, "MAX_DOCUMENT_BYTES", len(expected) - 1):
                    with self.assertRaises(contracts.ContractError):
                        contracts.canonical_json_bytes(value)

    def test_oversized_string_is_refused_before_normalization(self):
        with (patch.object(contracts, "MAX_DOCUMENT_BYTES", 32),
              patch.object(contracts.unicodedata, "normalize") as normalize,
              patch.object(contracts.json, "dumps") as dumps):
            with self.assertRaises(contracts.ContractError):
                contracts.canonical_json_bytes("x" * 1024)
        normalize.assert_not_called()
        dumps.assert_not_called()

    def test_oversized_integer_is_refused_before_encoding(self):
        with (patch.object(contracts, "MAX_DOCUMENT_BYTES", 32),
              patch.object(contracts.json, "dumps") as dumps):
            with self.assertRaisesRegex(contracts.ContractError, "byte limit"):
                contracts.canonical_json_bytes(1 << 10000)
        dumps.assert_not_called()

    def test_oversized_containers_are_refused_before_iteration(self):
        class UnvisitedList(list):
            def __iter__(self):
                self.fail_if_visited()
                return super().__iter__()

            def fail_if_visited(self):
                raise AssertionError("oversized list was traversed")

        class UnvisitedDict(dict):
            def items(self):
                raise AssertionError("oversized mapping was traversed")

        for value in (UnvisitedList([0] * 100), UnvisitedDict({str(i): 0 for i in range(100)})):
            with self.subTest(kind=type(value).__name__):
                with patch.object(contracts, "MAX_DOCUMENT_BYTES", 32):
                    with self.assertRaises(contracts.ContractError):
                        contracts.canonical_json_bytes(value)

    def test_existing_invalid_values_remain_refused(self):
        nested = 0
        for _ in range(34):
            nested = [nested]
        for value in ({"é": 1, "e\u0301": 2}, "\ud800", {1: "bad key"},
                      1.0, float("nan"), b"bytes", {1, 2}, nested):
            with self.subTest(value=repr(value)):
                with self.assertRaises(contracts.ContractError):
                    contracts.canonical_json_bytes(value)

    def test_from_json_rejects_oversized_bytes_before_parsing(self):
        payload = b" " * (contracts.MAX_DOCUMENT_BYTES + 1)
        with patch.object(contracts.json, "loads") as loads:
            with self.assertRaises(contracts.ContractError):
                contracts.StateRootMarker.from_json(payload)
        loads.assert_not_called()

    def test_from_mapping_bounds_before_validator_allocations(self):
        with (patch.object(contracts, "MAX_DOCUMENT_BYTES", 128),
              patch.object(contracts.InputManifest, "validate") as validate):
            with self.assertRaises(contracts.ContractError):
                contracts.InputManifest.from_mapping({"evidence": ["x" * 32] * 20})
        validate.assert_not_called()

    def test_from_mapping_still_validates_original_filesystem_labels(self):
        value = {"contract": "graphify.workspace.source-inputs", "format_version": 1,
                 "phase": "detection", "roots": ["source"],
                 "evidence": [{"operation": "directory", "path": ".", "value": [1, 2, 16832]}],
                 "code_inputs": ["e\u0301.py"], "outcomes": [], "failure": None}
        with self.assertRaisesRegex(contracts.ContractError, "invalid input label"):
            contracts.InputManifest.from_mapping(value)
        self.assertEqual(value["code_inputs"], ["e\u0301.py"])

    def test_concrete_document_paths_keep_identical_canonical_bytes(self):
        value = {"contract": "graphify.workspace.state-root", "state_schema_version": 2,
                 "owner": "graphify.workspace"}
        document = contracts.StateRootMarker.from_mapping(value)
        self.assertEqual(contracts.StateRootMarker.from_json(document.canonical), document)
        self.assertEqual(document.to_dict(), value)


if __name__ == "__main__":
    unittest.main()
