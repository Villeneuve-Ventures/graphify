"""Focused PR-review regressions, also runnable with the stdlib unittest runner."""
import hashlib
from importlib.metadata import PackagePath
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from graphify.workspace.adapters.base import QueryRejected, QueryRequest
from graphify.workspace.composition import (
    WorkspaceAuthorityInvalid, verify_installed_candidate,
)
from graphify.workspace.contracts import (
    CompletionBinding, ContractError, InputManifest, StateRootMarker,
)


class QueryEncodingTests(unittest.TestCase):
    def test_surrogates_use_query_refusal_for_both_fields(self):
        for surrogate in ("\ud800", "\udfff"):
            for question, filters in ((surrogate, ()), ("valid", (surrogate,))):
                with self.subTest(question=repr(question), filters=repr(filters)):
                    with self.assertRaises(QueryRejected):
                        QueryRequest(question, context_filters=filters)

    def test_valid_multibyte_text_remains_accepted(self):
        request = QueryRequest("café 😀", context_filters=("日本語",))
        self.assertEqual(request.question, "café 😀")
        self.assertEqual(request.context_filters, ("日本語",))


class InstalledMemberRefusalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "__init__.py"
        self.path.write_bytes(b"# installed fixture\n")
        member = PackagePath("graphify/__init__.py")
        self.expected = SimpleNamespace(to_dict=lambda: {
            "distribution_version": "0.10.0",
            "package_members": {str(member): hashlib.sha256(b"# installed fixture\n").hexdigest()},
            "installation_metadata": {},
        })
        dist = SimpleNamespace(version="0.10.0", files=[member],
                               locate_file=lambda _: self.path)
        for patcher in (patch("graphify.__file__", str(self.path)),
                        patch("graphify.workspace.composition.metadata.distribution", return_value=dist)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_matching_installed_member_is_accepted(self):
        verify_installed_candidate(self.expected)

    def test_stat_and_read_races_use_authority_refusal(self):
        for operation in ("stat", "read_bytes"):
            for error in (FileNotFoundError, PermissionError):
                with self.subTest(operation=operation, error=error):
                    # Reach the selected operation after the preliminary checks.
                    with patch.object(Path, "is_file", return_value=True), \
                         patch.object(Path, "is_symlink", return_value=False), \
                         patch.object(Path, operation, side_effect=error("fixture race")):
                        with self.assertRaisesRegex(WorkspaceAuthorityInvalid, "unreadable") as caught:
                            verify_installed_candidate(self.expected)
                        self.assertIsInstance(caught.exception.__cause__, error)

    def test_hash_mismatch_keeps_its_specific_refusal(self):
        self.path.write_bytes(b"tampered\n")
        with self.assertRaisesRegex(WorkspaceAuthorityInvalid, "member mismatch"):
            verify_installed_candidate(self.expected)

    def test_unrelated_programming_error_is_not_hidden(self):
        with patch.object(Path, "read_bytes", side_effect=ValueError("unrelated")):
            with self.assertRaisesRegex(ValueError, "unrelated"):
                verify_installed_candidate(self.expected)


class CompletionCompatibilityTests(unittest.TestCase):
    def test_unrelated_document_cannot_supply_the_compatibility_digest(self):
        common = {
            "contract": "graphify.workspace.source-inputs", "format_version": 1,
            "roots": ["source"], "code_inputs": [], "outcomes": [], "failure": None,
            "evidence": [{"operation": "directory", "path": ".", "value": [1, 2, 16832]}],
        }
        initial = InputManifest.from_mapping(dict(common, phase="detection"))
        consumed = InputManifest.from_mapping(dict(common, phase="consumed"))
        marker = StateRootMarker.from_mapping({
            "contract": "graphify.workspace.state-root", "state_schema_version": 2,
            "owner": "graphify.workspace",
        })
        for wrong in (marker, SimpleNamespace(sha256="a" * 64), None):
            with self.subTest(wrong=type(wrong).__name__):
                with self.assertRaisesRegex(ContractError, "compatibility manifest"):
                    CompletionBinding.bind(initial, consumed, compatibility=wrong,
                                           graph_sha256="b" * 64)


if __name__ == "__main__":
    unittest.main()
