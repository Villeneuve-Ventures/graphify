"""S2 must preserve negative observations emitted by actual S1 traversal."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from graphify.extractors.resolution import _resolve_tsconfig_alias
from graphify.source_io import SourceIO, engine_inputs
from graphify.workspace.contracts import ContractError, InputManifest


class NegativeProbeTests(unittest.TestCase):
    def test_absent_or_nondirectory_ancestors_round_trip(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "existing").mkdir()
            (root / "file").write_text("not a directory")
            for relative in ("missing/deep/file.ts", "existing/missing/file.ts",
                             "file/deep/file.ts"):
                with self.subTest(relative=relative), SourceIO(root) as inputs:
                    self.assertIsNone(inputs.probe(root / relative))
                    manifest = InputManifest.from_engine(
                        inputs, phase="consumed", code_inputs=[])
                    self.assertTrue(manifest.complete)
                    self.assertEqual(InputManifest.from_json(manifest.canonical), manifest)
                    self.assertIn({"operation": "probe", "path": relative, "value": None},
                                  manifest.to_dict()["evidence"])

    def test_negative_probes_preserve_explicit_roots(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            for name in ("source", "policy", "git"):
                (base / name).mkdir()
            with SourceIO(base / "source", extra_roots={
                "policy": base / "policy", "git": base / "git",
            }) as inputs:
                for name in ("source", "policy", "git"):
                    self.assertIsNone(inputs.probe(base / name / "absent/deep/item"))
                manifest = InputManifest.from_engine(inputs, phase="consumed", code_inputs=[])
            self.assertTrue(manifest.complete)
            for label in (".", "policy:.", "git:."):
                changed = manifest.to_dict()
                changed["evidence"] = [r for r in changed["evidence"]
                                       if not (r["operation"] == "directory" and r["path"] == label)]
                with self.subTest(label=label), self.assertRaisesRegex(
                    ContractError, "missing pinned root binding"
                ):
                    InputManifest.from_mapping(changed)
            changed = manifest.to_dict()
            changed["roots"].remove("policy")
            with self.assertRaisesRegex(ContractError, "outside explicit root allowlist"):
                InputManifest.from_mapping(changed)

    def test_actual_tsconfig_fallback_records_missing_first_target(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "src").mkdir()
            target = root / "src/helper.ts"
            target.write_text("export const helper = 1;")
            with SourceIO(root) as inputs, engine_inputs(inputs):
                resolved = _resolve_tsconfig_alias("@app/helper", {"@app/*": [
                    str(root / "missing/*"), str(root / "src/*"),
                ]}, strict=True)
                self.assertEqual(resolved, target)
                self.assertEqual(inputs.read_bytes(target), target.read_bytes())
                manifest = InputManifest.from_engine(
                    inputs, phase="consumed", code_inputs=[target],
                    outcomes=[{"path": target, "status": "success"}])
            self.assertTrue(manifest.complete)
            self.assertTrue(any(r["operation"] == "probe" and r["value"] is None
                                and r["path"].startswith("missing/")
                                for r in manifest.to_dict()["evidence"]))

    def test_positive_observations_still_require_every_traversed_directory(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "parent/child").mkdir(parents=True)
            target = root / "parent/child/file.ts"
            target.write_text("export {};")
            for operation in ("probe", "read", "list", "directory"):
                with self.subTest(operation=operation), SourceIO(root) as inputs:
                    if operation == "read":
                        inputs.read_bytes(target)
                    elif operation == "list":
                        inputs.listdir(target.parent)
                    elif operation == "directory":
                        inputs.probe(target.parent / "absent/deep/file.ts")
                    else:
                        inputs.probe(target)
                    manifest = InputManifest.from_engine(inputs, phase="consumed", code_inputs=[])
                    changed = manifest.to_dict()
                    changed["evidence"] = [r for r in changed["evidence"] if not (
                        r["operation"] == "directory" and r["path"] == "parent")]
                    with self.assertRaisesRegex(ContractError, "missing traversed directory binding"):
                        InputManifest.from_mapping(changed)

    def test_negative_probe_conflicting_with_membership_is_rejected(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "parent").mkdir()
            (root / "parent/file.ts").write_text("export {};")
            with SourceIO(root) as inputs:
                inputs.listdir(root / "parent")
                inputs.probe(root / "parent/file.ts")
                manifest = InputManifest.from_engine(inputs, phase="consumed", code_inputs=[])
            changed = manifest.to_dict()
            next(r for r in changed["evidence"] if r["operation"] == "probe"
                 and r["path"] == "parent/file.ts")["value"] = None
            with self.assertRaisesRegex(ContractError, "inconsistent evidence binding"):
                InputManifest.from_mapping(changed)


if __name__ == "__main__":
    unittest.main()
