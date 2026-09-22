"""Paired observation evidence regressions, runnable with stdlib unittest."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from graphify.source_io import SourceIO
from graphify.workspace.adapters.base import SourceObservation
from graphify.workspace.contracts import CompletionBinding, ContractError, InputManifest
from tests.test_workspace_installed_identity import installed_manifest


class ObservationBindingTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name).resolve()
        self.compatibility = installed_manifest(base / "distribution")
        root = base / "source"
        root.mkdir()
        code = root / "code.py"
        code.write_bytes(b"answer = 42\n")
        (root / "support.txt").write_bytes(b"support\n")
        (root / "extra.txt").write_bytes(b"extra\n")
        with SourceIO(root) as inputs:
            inputs.probe(code)
            inputs.probe(root / "support.txt")
            inputs.probe(root / "missing.txt")
            self.initial = InputManifest.from_engine(inputs, phase="detection", code_inputs=[code])
            inputs.read_bytes(code)
            inputs.probe(root / "extra.txt")
            self.consumed = InputManifest.from_engine(
                inputs, phase="consumed", code_inputs=[code],
                outcomes=[{"path": code, "status": "success"}])

    def bind(self, initial, consumed):
        return CompletionBinding.bind(initial, consumed, compatibility=self.compatibility,
                                      graph_sha256="a" * 64)

    def assert_pair_refused(self, consumed, message):
        # Each document is individually valid; only their relationship is wrong.
        changed = InputManifest.from_mapping(consumed)
        for boundary in (lambda: SourceObservation(self.initial, changed, 2),
                         lambda: self.bind(self.initial, changed)):
            with self.subTest(boundary=boundary):
                with self.assertRaisesRegex(ContractError, message):
                    boundary()

    def test_root_mismatch_is_refused_by_both_boundaries(self):
        changed = self.consumed.to_dict()
        changed["roots"].append("policy")
        changed["roots"].sort()
        changed["evidence"].append({"operation": "directory", "path": "policy:.",
                                    "value": [1, 2, 16832]})
        changed["evidence"].sort(key=lambda item: (item["operation"], item["path"]))
        self.assert_pair_refused(changed, "detection authority changed")

    def test_code_inventory_mismatch_is_refused_by_both_boundaries(self):
        changed = self.consumed.to_dict()
        changed["code_inputs"] = []
        changed["outcomes"] = []
        self.assert_pair_refused(changed, "detection authority changed")

    def test_changed_or_missing_positive_and_negative_evidence_is_refused(self):
        for path in ("support.txt", "missing.txt"):
            for missing in (False, True):
                with self.subTest(path=path, missing=missing):
                    changed = self.consumed.to_dict()
                    evidence = next(item for item in changed["evidence"]
                                    if item["operation"] == "probe" and item["path"] == path)
                    if missing:
                        changed["evidence"].remove(evidence)
                    elif evidence["value"] is None:
                        evidence["value"] = [1, 2, 33188, 0, 0, 0]
                    else:
                        evidence["value"][1] += 1
                    self.assert_pair_refused(changed, "initial evidence missing or changed")

    def test_valid_supporting_evidence_extension_is_accepted(self):
        observation = SourceObservation(self.initial, self.consumed, 2)
        self.assertIs(observation.initial_detection, self.initial)
        self.assertIs(observation.consumed_inputs, self.consumed)
        self.assertGreater(len(self.consumed.to_dict()["evidence"]),
                           len(self.initial.to_dict()["evidence"]))
        binding = self.bind(self.initial, self.consumed).to_dict()
        self.assertEqual(binding["initial_detection_sha256"], self.initial.sha256)
        self.assertEqual(binding["consumed_inputs_sha256"], self.consumed.sha256)

    def test_detection_only_observation_remains_supported(self):
        observation = SourceObservation(self.initial, None, 2)
        self.assertIsNone(observation.consumed_inputs)

    def assert_invalid_pair(self, initial, consumed):
        with self.assertRaises(ContractError):
            SourceObservation(initial, consumed, 2)
        with self.assertRaises(ContractError):
            self.bind(initial, consumed)

    def test_types_phases_completeness_and_pass_count_remain_checked(self):
        value = self.consumed.to_dict()
        value["failure"] = "failed_extraction"
        incomplete = InputManifest.from_mapping(value)
        for initial, consumed in ((None, self.consumed),
                                  (SimpleNamespace(to_dict=self.initial.to_dict), self.consumed),
                                  (self.consumed, self.consumed),
                                  (self.initial, self.initial),
                                  (self.initial, incomplete)):
            with self.subTest(initial=type(initial).__name__, consumed=consumed):
                self.assert_invalid_pair(initial, consumed)
        for passes in (True, 1, 7):
            with self.subTest(passes=passes), self.assertRaises(ContractError):
                SourceObservation(self.initial, self.consumed, passes)


if __name__ == "__main__":
    unittest.main()
