"""Installed-identity review regressions; run only with stdlib unittest."""
import hashlib
import base64
import csv
import io
import compileall
from importlib import metadata, util
import os
import marshal
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from graphify.workspace.composition import WorkspaceAuthorityInvalid, verify_installed_candidate


from graphify.workspace.contracts import (
    CompatibilityManifest, DETECTOR_ID, DISTRIBUTION_VERSION, ENGINE_BASELINE,
    EXTRACTOR_CACHE_ABI, INSTALLATION_METADATA, SCHEMA_FILES,
)


def installed_manifest(root):
    """Create all structural members required by a real validated test manifest."""
    required = {"__init__.py", "__main__.py", "source_io.py", "workspace/contracts.py",
                "workspace/composition.py", "workspace/__init__.py",
                "workspace/adapters/base.py", "workspace/adapters/__init__.py"}
    required.update("workspace/schemas/" + name for name in SCHEMA_FILES)
    for name in required:
        path = root / "graphify" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(b"# structural fixture\n" if name.endswith(".py") else b"{}\n")
    metadata_root = root / f"graphifyy-{DISTRIBUTION_VERSION}.dist-info"
    for name in (*INSTALLATION_METADATA, "RECORD"):
        path = metadata_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(b"Name: graphifyy\nVersion: 0.10.0\n"
                             if name == "METADATA" else b"fixture metadata\n")
    return CompatibilityManifest.from_mapping({
        "contract": "graphify.workspace.compatibility", "schema_version": 2,
        "distribution": "graphifyy", "distribution_version": DISTRIBUTION_VERSION,
        "distribution_build": "fixture:sha256:" + "a" * 64,
        "source_manifest_sha256": "a" * 64, "wheel_sha256": "b" * 64,
        "engine_baseline": ENGINE_BASELINE, "extractor_cache_abi": EXTRACTOR_CACHE_ABI,
        "adapter_contract_version": 2, "state_schema_version": 2,
        "detector_id": DETECTOR_ID, "graph_payload_version": 1, "input_manifest_version": 1,
        "candidate_kind": "local-fixture", "certified": False,
        "package_members": {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in (root / "graphify").rglob("*") if path.is_file()},
        "installation_metadata": {name: hashlib.sha256((metadata_root / name).read_bytes()).hexdigest()
                                  for name in INSTALLATION_METADATA},
    })


def write_installed_record(root, files):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    for path in files:
        name = str(path)
        if name.endswith(("/RECORD", ".pyc")):
            writer.writerow((name, "", ""))
        else:
            payload = (root / path).read_bytes()
            digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
            writer.writerow((name, "sha256=" + digest, str(len(payload))))
    (root / "graphifyy-0.10.0.dist-info/RECORD").write_text(stream.getvalue(), encoding="utf-8")


class InstalledIdentityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.package = self.root / "graphify"
        self.package.mkdir()
        self.init = self.package / "__init__.py"
        self.init.write_bytes(b"# fixture package\n")
        self.module = self.package / "marker.py"
        self.source = (b'"""Fixture marker."""\nMARKER = 1\nassert MARKER\n'
                       b'def outer(value="repeated"):\n'
                       b'    def inner():\n        return value, "repeated"\n'
                       b'    return [inner() for _ in range(2)]\n')
        self.module.write_bytes(self.source)
        self.dist_info = self.root / "graphifyy-0.10.0.dist-info"
        self.dist_info.mkdir()
        self.metadata_file = self.dist_info / "METADATA"
        self.metadata_file.write_bytes(b"Name: graphifyy\nVersion: 0.10.0\n")
        (self.dist_info / "RECORD").write_bytes(b"")
        entry_points = b"[console_scripts]\ngraphify = graphify.__main__:main\ngraphify-mcp = graphify.serve:_main\n"
        (self.dist_info / "entry_points.txt").write_bytes(entry_points)
        self.expected = installed_manifest(self.root)
        self.files = [metadata.PackagePath(path.relative_to(self.root).as_posix())
                      for path in self.root.rglob("*") if path.is_file()]
        self.scripts = self.root / "bin"
        self.scripts.mkdir()
        for name in ("graphify", "graphify-mcp"):
            (self.scripts / name).write_bytes(b"# fixture console script\n")
            self.files.append(metadata.PackagePath("bin/" + name))
        write_installed_record(self.root, self.files)
        self.dist = metadata.PathDistribution(self.dist_info)
        for patcher in (patch("graphify.__file__", str(self.init)),
                        patch("graphify.workspace.composition.metadata.distribution", return_value=self.dist),
                        patch("graphify.workspace.composition.sysconfig.get_path", return_value=str(self.scripts)),
                        patch("sys.pycache_prefix", None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_public_verifier_refuses_unvalidated_compatibility(self):
        class DerivedManifest(CompatibilityManifest):
            pass

        invalid = self.expected.to_dict()
        invalid["certified"] = True
        invalid["installation_metadata"] = {}
        for expected in (None, invalid, SimpleNamespace(to_dict=lambda: invalid),
                         DerivedManifest.from_mapping(self.expected.to_dict())):
            with self.subTest(expected=type(expected).__name__):
                with self.assertRaisesRegex(WorkspaceAuthorityInvalid, "compatibility manifest"):
                    verify_installed_candidate(expected)

    def cache(self, *, optimize=0, mode=py_compile.PycInvalidationMode.TIMESTAMP):
        filename = py_compile.compile(str(self.module), doraise=True, optimize=optimize,
                                      invalidation_mode=mode)
        assert filename is not None
        return Path(filename)

    def poison_cache(self, *, optimize=0, mode=py_compile.PycInvalidationMode.TIMESTAMP):
        before = self.module.stat()
        self.module.write_bytes(self.source.replace(b"MARKER = 1", b"MARKER = 2"))
        os.utime(self.module, ns=(before.st_atime_ns, before.st_mtime_ns))
        cached = self.cache(optimize=optimize, mode=mode)
        self.module.write_bytes(self.source)
        os.utime(self.module, ns=(before.st_atime_ns, before.st_mtime_ns))
        # A valid authorized-source header is not proof of the executable payload.
        if mode != py_compile.PycInvalidationMode.TIMESTAMP:
            raw = cached.read_bytes()
            cached.write_bytes(raw[:8] + util.source_hash(self.source) + raw[16:])
        return cached

    def test_symlinked_scripts_directory_is_accepted(self):
        alias = self.root / "linked-bin"
        alias.symlink_to(self.scripts, target_is_directory=True)
        with patch("graphify.workspace.composition.sysconfig.get_path", return_value=str(alias)):
            verify_installed_candidate(self.expected)

    def test_missing_recorded_console_script_is_refused(self):
        (self.scripts / "graphify").unlink()
        with self.assertRaisesRegex(WorkspaceAuthorityInvalid, "console script"):
            verify_installed_candidate(self.expected)

    def test_missing_declared_console_script_rows_are_refused(self):
        for names in (("graphify",), ("graphify-mcp",), ("graphify", "graphify-mcp")):
            for remove_files in (False, True):
                with self.subTest(names=names, remove_files=remove_files):
                    rows = [metadata.PackagePath("bin/" + name) for name in names]
                    for row in rows:
                        self.files.remove(row)
                        if remove_files:
                            (self.root / row).unlink()
                    write_installed_record(self.root, self.files)
                    try:
                        with self.assertRaisesRegex(WorkspaceAuthorityInvalid, "console script"):
                            verify_installed_candidate(self.expected)
                    finally:
                        self.files.extend(rows)
                        for row in rows:
                            (self.root / row).write_bytes(b"# fixture console script\n")
                        write_installed_record(self.root, self.files)

    def test_supported_exe_launcher_variants_remain_accepted(self):
        for name in ("graphify", "graphify-mcp"):
            (self.scripts / name).rename(self.scripts / (name + ".exe"))
            self.files.remove(metadata.PackagePath("bin/" + name))
            self.files.append(metadata.PackagePath("bin/" + name + ".exe"))
        write_installed_record(self.root, self.files)
        verify_installed_candidate(self.expected)

    def test_console_script_disappearance_before_final_admission_is_refused(self):
        from graphify.workspace import composition
        verify_tree = composition._verify_package_tree
        def remove_script(*args):
            verify_tree(*args)
            (self.scripts / "graphify").unlink()
        with (patch.object(composition, "_verify_package_tree", side_effect=remove_script),
              self.assertRaisesRegex(WorkspaceAuthorityInvalid, "console script")):
            verify_installed_candidate(self.expected)

    def test_normal_caches_preserve_identity_and_bytes(self):
        for optimize in (0, 1, 2):
            for mode in py_compile.PycInvalidationMode:
                with self.subTest(optimize=optimize, mode=mode):
                    cached = self.cache(optimize=optimize, mode=mode)
                    before = cached.read_bytes()
                    verify_installed_candidate(self.expected)
                    self.assertEqual(cached.read_bytes(), before)

    def test_fresh_process_import_generated_caches_are_accepted(self):
        for optimize in ([], ["-O"], ["-OO"]):
            with self.subTest(optimize=optimize):
                result = subprocess.run(
                    [sys.executable, "-I", *optimize, "-c",
                     "import importlib.util, sys; "
                     "s = importlib.util.spec_from_file_location('fixture_marker', sys.argv[1]); "
                     "m = importlib.util.module_from_spec(s); s.loader.exec_module(m); print(m.MARKER)",
                     str(self.module)], text=True, capture_output=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "1")
                verify_installed_candidate(self.expected)

    def test_timestamp_valid_forgery_executes_under_B_but_is_refused(self):
        cached = self.poison_cache()
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c",
             "import importlib.util, sys; "
             "s = importlib.util.spec_from_file_location('fixture_marker', sys.argv[1]); "
             "m = importlib.util.module_from_spec(s); s.loader.exec_module(m); print(m.MARKER)",
             str(self.module)], text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "2")
        before = cached.read_bytes()
        with self.assertRaisesRegex(WorkspaceAuthorityInvalid, "bytecode"):
            verify_installed_candidate(self.expected)
        self.assertEqual(cached.read_bytes(), before)

    def test_recorded_and_unrecorded_forged_caches_are_refused(self):
        for recorded in (False, True):
            for optimize in (0, 1, 2):
                for mode in py_compile.PycInvalidationMode:
                    with self.subTest(recorded=recorded, optimize=optimize, mode=mode):
                        cached = self.poison_cache(optimize=optimize, mode=mode)
                        member = metadata.PackagePath(str(cached.relative_to(self.root)))
                        if recorded and member not in self.files:
                            self.files.append(member)
                            write_installed_record(self.root, self.files)
                        with self.assertRaisesRegex(WorkspaceAuthorityInvalid, "bytecode"):
                            verify_installed_candidate(self.expected)
                        cached.unlink()

    def test_unrecorded_shadow_package_is_refused(self):
        shadow = self.package / "marker"
        shadow.mkdir()
        (shadow / "__init__.py").write_text("MARKER = 99\n")
        with self.assertRaisesRegex(WorkspaceAuthorityInvalid, "unrecorded"):
            verify_installed_candidate(self.expected)

    def test_package_member_replacement_after_hash_is_refused(self):
        import graphify.workspace.composition as composition
        inspect_tree = composition._verify_package_tree

        def replace_then_inspect(*args):
            replacement = self.root / "replacement.py"
            replacement.write_bytes(self.source.replace(b"MARKER = 1", b"MARKER = 9"))
            replacement.replace(self.module)
            return inspect_tree(*args)

        with (patch.object(composition, "_verify_package_tree", replace_then_inspect),
              self.assertRaisesRegex(WorkspaceAuthorityInvalid, "changed after verification")):
            verify_installed_candidate(self.expected)

    def test_package_member_deletion_after_hash_is_refused(self):
        import graphify.workspace.composition as composition
        inspect_tree = composition._verify_package_tree

        def delete_then_inspect(*args):
            self.module.unlink()
            return inspect_tree(*args)

        with (patch.object(composition, "_verify_package_tree", delete_then_inspect),
              self.assertRaisesRegex(WorkspaceAuthorityInvalid, "disappeared")):
            verify_installed_candidate(self.expected)

    def test_metadata_replacement_after_hash_is_refused(self):
        import graphify.workspace.composition as composition
        inspect_tree = composition._verify_package_tree

        def replace_then_inspect(*args):
            replacement = self.root / "replacement-metadata"
            replacement.write_bytes(self.metadata_file.read_bytes())
            replacement.replace(self.metadata_file)
            return inspect_tree(*args)

        with (patch.object(composition, "_verify_package_tree", replace_then_inspect),
              self.assertRaisesRegex(WorkspaceAuthorityInvalid, "metadata changed")):
            verify_installed_candidate(self.expected)

    def test_metadata_properties_do_not_reopen_captured_files(self):
        with patch.object(metadata.PathDistribution, "read_text",
                          side_effect=AssertionError("unbounded metadata reread")):
            verify_installed_candidate(self.expected)

    def test_malformed_record_size_is_authority_refusal(self):
        record = self.dist_info / "RECORD"
        record.write_bytes(b"graphify/__init__.py,,not-an-integer\n")
        with self.assertRaises(WorkspaceAuthorityInvalid):
            verify_installed_candidate(self.expected)

    def test_malformed_captured_record_is_authority_refusal(self):
        record = self.dist_info / "RECORD"
        invalid_records = [b"\xff", b'"unterminated,,\n', b"path,hash\n",
                           b"path,,1,extra\n", b"path,,1\npath,,1\n",
                           b"path,sha256=,1\n", b"path,invalid,1\n", b"bad\x00path,,1\n",
                           b"path,,1\n./path,,1\n"]
        invalid_records.extend(b"path,," + size + b"\n"
                               for size in (b"-1", b"+1", b"1.5", b"\xd9\xa1", b" 1"))
        for payload in invalid_records:
            with self.subTest(payload=payload):
                record.write_bytes(payload)
                with self.assertRaises(WorkspaceAuthorityInvalid):
                    verify_installed_candidate(self.expected)

    def test_malformed_captured_metadata_is_authority_refusal(self):
        for payload in (b"\xff", b"Name: graphifyy\n", b"Version: 1.0\n",
                        b"Version: 0.10.0\nVersion: 0.10.0\n", b"bad header\nVersion: 0.10.0\n"):
            with self.subTest(payload=payload):
                self.metadata_file.write_bytes(payload)
                with self.assertRaises(WorkspaceAuthorityInvalid):
                    verify_installed_candidate(self.expected)

    def test_optional_record_hash_and_size_fields_remain_accepted(self):
        record = self.dist_info / "RECORD"
        cached = self.cache()
        self.files.append(metadata.PackagePath(cached.relative_to(self.root).as_posix()))
        write_installed_record(self.root, self.files)
        original_rows = list(csv.reader(io.StringIO(record.read_text())))
        for omit_hash, omit_size in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(omit_hash=omit_hash, omit_size=omit_size):
                stream = io.StringIO(newline="")
                csv.writer(stream).writerows((name, "" if omit_hash else digest,
                                              "" if omit_size else size)
                                             for name, digest, size in original_rows)
                record.write_text(stream.getvalue(), encoding="utf-8")
                verify_installed_candidate(self.expected)

    def test_captured_metadata_is_parsed_before_final_drift_refusal(self):
        from graphify.workspace import composition
        capture = composition._read_installed_member
        inspect_tree = composition._verify_package_tree
        for name in ("METADATA", "RECORD"):
            path = self.dist_info / name
            original = path.read_bytes()
            reads = []
            def mutate_after_capture(member):
                result = capture(member)
                if member == path:
                    reads.append(member)
                    member.write_bytes(b"\xff" * 1024 * 1024)
                return result
            try:
                with (self.subTest(name=name),
                      patch.object(composition, "_read_installed_member", mutate_after_capture),
                      patch.object(composition, "_verify_package_tree", wraps=inspect_tree) as tree,
                      patch.object(metadata.PathDistribution, "read_text",
                                   side_effect=AssertionError("unbounded metadata reread"))):
                    with self.assertRaisesRegex(WorkspaceAuthorityInvalid, "metadata changed"):
                        verify_installed_candidate(self.expected)
                    tree.assert_called_once()
                    self.assertEqual(reads, [path])
            finally:
                path.write_bytes(original)

    def test_external_pycache_prefix_is_checked(self):
        with patch("sys.pycache_prefix", str(self.root / "external-cache")):
            cached = self.cache()
            verify_installed_candidate(self.expected)
            self.poison_cache()
            with self.assertRaisesRegex(WorkspaceAuthorityInvalid, "bytecode"):
                verify_installed_candidate(self.expected)
            self.assertTrue(cached.is_file())

    def test_external_cache_replacement_after_comparison_is_refused(self):
        import graphify.workspace.composition as composition
        inspect_tree = composition._verify_package_tree
        with patch("sys.pycache_prefix", str(self.root / "external-cache")):
            cached = self.cache()

            def replace_then_inspect(*args):
                replacement = self.root / "replacement-cache"
                replacement.write_bytes(cached.read_bytes())
                replacement.replace(cached)
                return inspect_tree(*args)

            with (patch.object(composition, "_verify_package_tree", replace_then_inspect),
                  self.assertRaisesRegex(WorkspaceAuthorityInvalid,
                                         "bytecode cache changed")):
                verify_installed_candidate(self.expected)

    def test_cache_symlink_and_oversized_file_are_refused(self):
        cached = self.cache()
        target = self.root / "cache-target"
        cached.rename(target)
        cached.symlink_to(target)
        with self.assertRaisesRegex(WorkspaceAuthorityInvalid, "bytecode"):
            verify_installed_candidate(self.expected)
        cached.unlink()
        with cached.open("wb") as stream:
            stream.truncate(64 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(WorkspaceAuthorityInvalid, "bytecode"):
            verify_installed_candidate(self.expected)

    def test_cache_resolution_races_use_authority_refusal(self):
        cached = self.cache()
        resolve = Path.resolve

        def racing_resolve(path, *args, **kwargs):
            if path == cached:
                raise FileNotFoundError("cache removed during verification")
            return resolve(path, *args, **kwargs)

        with (patch.object(Path, "resolve", racing_resolve),
              self.assertRaisesRegex(WorkspaceAuthorityInvalid, "bytecode cache unreadable") as caught):
            verify_installed_candidate(self.expected)
        self.assertIsInstance(caught.exception.__cause__, FileNotFoundError)

    def test_cache_initial_lookup_errors_use_authority_refusal(self):
        cached = self.cache()
        lstat = Path.lstat

        def failing_lstat(path, *args, **kwargs):
            if path == cached:
                raise PermissionError("cache lookup refused")
            return lstat(path, *args, **kwargs)

        with (patch.object(Path, "lstat", failing_lstat),
              self.assertRaisesRegex(WorkspaceAuthorityInvalid,
                                     "bytecode cache unreadable") as caught):
            verify_installed_candidate(self.expected)
        self.assertIsInstance(caught.exception.__cause__, PermissionError)

    def test_symlinked_installation_ancestors_accept_either_compile_filename(self):
        alias = self.root / "alias"
        alias.symlink_to(self.package, target_is_directory=True)
        self.dist.locate_file = lambda path: (
            alias / Path(path).relative_to("graphify") if str(path).startswith("graphify/") else self.root / path
        )
        for filename in (self.module, alias / self.module.name):
            with self.subTest(filename=str(filename)):
                py_compile.compile(str(filename), doraise=True)
                verify_installed_candidate(self.expected)

    def test_prefix_does_not_select_local_cache(self):
        self.poison_cache()
        with patch("sys.pycache_prefix", str(self.root / "external-cache")):
            self.cache()
            verify_installed_candidate(self.expected)

    def test_nonstandard_cache_filename_is_explicitly_refused(self):
        py_compile.compile(str(self.module), dfile="unrelated-source.py", doraise=True)
        with self.assertRaisesRegex(WorkspaceAuthorityInvalid, "bytecode"):
            verify_installed_candidate(self.expected)

    def test_compileall_transaction_module_caches_are_accepted(self):
        # This actual module exposed cross-compilation string-interning changes
        # that small fixtures do not exercise. It is compiled, never imported.
        self.source = (Path(__file__).resolve().parents[1] / "graphify/transaction.py").read_bytes()
        self.module.write_bytes(self.source)
        value = self.expected.to_dict()
        value["package_members"]["graphify/marker.py"] = hashlib.sha256(self.source).hexdigest()
        self.expected = CompatibilityManifest.from_mapping(value)
        for optimize in (0, 1, 2):
            self.assertTrue(compileall.compile_file(str(self.module), quiet=1, optimize=optimize))
        verify_installed_candidate(self.expected)

    def test_execution_metadata_forgery_is_refused(self):
        for field in ("co_stacksize", "co_exceptiontable", "co_linetable"):
            with self.subTest(field=field):
                cached = self.cache()
                code = compile(self.source, str(self.module), "exec", dont_inherit=True)
                if field == "co_stacksize":
                    changed = code.replace(co_stacksize=code.co_stacksize + 1)
                elif field == "co_exceptiontable":
                    changed = code.replace(co_exceptiontable=code.co_exceptiontable + b"\x00")
                else:
                    changed = code.replace(co_linetable=code.co_linetable + b"\x00")
                cached.write_bytes(cached.read_bytes()[:16] + marshal.dumps(changed))
                with self.assertRaisesRegex(WorkspaceAuthorityInvalid, "bytecode"):
                    verify_installed_candidate(self.expected)

    def test_windows_child_does_not_require_resource_module(self):
        from graphify.workspace import composition
        self.cache()
        # Exercise the Windows limit branch without claiming a Windows run.
        script = composition._CACHE_COMPARISON.replace(
            "try:\n", "sys.platform = 'win32'\ntry:\n", 1)
        with patch.object(composition, "_CACHE_COMPARISON", script):
            verify_installed_candidate(self.expected)

    def test_decomposed_unicode_installation_path_preserves_cache_identity(self):
        location = self.root / "cafe\u0301"
        self.package.rename(location)
        self.package = location
        self.init = location / "__init__.py"
        self.module = location / "marker.py"
        self.dist.locate_file = lambda path: (
            location / Path(path).relative_to("graphify") if str(path).startswith("graphify/") else self.root / path
        )
        with patch("graphify.__file__", str(self.init)):
            self.cache()
            verify_installed_candidate(self.expected)

    def test_constant_reference_sharing_is_outside_field_equivalence(self):
        import types
        self.source = (b'value = "a long value with spaces"\n'
                       b'def get_value():\n    return "a long value with spaces"\n'
                       b'MARKER = value is get_value()\n')
        self.module.write_bytes(self.source)
        value = self.expected.to_dict()
        value["package_members"]["graphify/marker.py"] = hashlib.sha256(self.source).hexdigest()
        self.expected = CompatibilityManifest.from_mapping(value)
        cached = self.cache()
        original = compile(self.source, str(self.module), "exec", dont_inherit=True)
        nested = next(value for value in original.co_consts if isinstance(value, types.CodeType))
        literal = next(value for value in nested.co_consts if isinstance(value, str))
        distinct = literal.encode().decode()
        self.assertIsNot(literal, distinct)
        changed = nested.replace(co_consts=tuple(distinct if value is literal else value
                                                 for value in nested.co_consts))
        forged = original.replace(co_consts=tuple(changed if value is nested else value
                                                  for value in original.co_consts))
        for code, wanted in ((original, True), (forged, False)):
            # Only this self-authored harmless marker fixture executes.
            namespace = {}
            exec(marshal.loads(marshal.dumps(code)), namespace)
            self.assertIs(namespace["MARKER"], wanted)
        cached.write_bytes(cached.read_bytes()[:16] + marshal.dumps(forged))
        # Ordinary compilation can also vary immutable constant sharing. This
        # verifier promises field/value equality, not identity-sensitive behavior.
        verify_installed_candidate(self.expected)


if __name__ == "__main__":
    unittest.main()
