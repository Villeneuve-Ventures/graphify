"""Installed-identity review regressions; run only with stdlib unittest."""
import hashlib
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
        self.files = [metadata.PackagePath("graphify/__init__.py"),
                      metadata.PackagePath("graphify/marker.py")]
        members = {str(p): hashlib.sha256((self.root / p).read_bytes()).hexdigest()
                   for p in self.files}
        self.expected = SimpleNamespace(to_dict=lambda: {
            "distribution_version": "0.10.0",
            "package_members": members,
            "installation_metadata": {},
        })
        self.dist = SimpleNamespace(version="0.10.0", files=self.files,
                                    locate_file=lambda p: self.root / p)
        for patcher in (patch("graphify.__file__", str(self.init)),
                        patch("graphify.workspace.composition.metadata.distribution", return_value=self.dist),
                        patch("sys.pycache_prefix", None)):
            patcher.start()
            self.addCleanup(patcher.stop)

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
        scripts = self.root / "bin"
        scripts.mkdir()
        alias = self.root / "linked-bin"
        alias.symlink_to(scripts, target_is_directory=True)
        (scripts / "graphify").write_bytes(b"# fixture console script\n")
        self.files.append(metadata.PackagePath("linked-bin/graphify"))
        with patch("graphify.workspace.composition.sysconfig.get_path", return_value=str(alias)):
            verify_installed_candidate(self.expected)

    def test_missing_recorded_console_script_is_refused(self):
        scripts = self.root / "bin"
        scripts.mkdir()
        self.files.append(metadata.PackagePath("bin/graphify"))
        with (patch("graphify.workspace.composition.sysconfig.get_path", return_value=str(scripts)),
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

    def test_external_pycache_prefix_is_checked(self):
        with patch("sys.pycache_prefix", str(self.root / "external-cache")):
            cached = self.cache()
            verify_installed_candidate(self.expected)
            self.poison_cache()
            with self.assertRaisesRegex(WorkspaceAuthorityInvalid, "bytecode"):
                verify_installed_candidate(self.expected)
            self.assertTrue(cached.is_file())

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

    def test_symlinked_installation_ancestors_accept_either_compile_filename(self):
        alias = self.root / "alias"
        alias.symlink_to(self.package, target_is_directory=True)
        self.dist.locate_file = lambda p: alias / Path(p).name
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
        self.expected.to_dict()["package_members"]["graphify/marker.py"] = hashlib.sha256(self.source).hexdigest()
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
        self.dist.locate_file = lambda p: location / Path(p).name
        with patch("graphify.__file__", str(self.init)):
            self.cache()
            verify_installed_candidate(self.expected)

    def test_constant_reference_sharing_is_outside_field_equivalence(self):
        import types
        self.source = (b'value = "a long value with spaces"\n'
                       b'def get_value():\n    return "a long value with spaces"\n'
                       b'MARKER = value is get_value()\n')
        self.module.write_bytes(self.source)
        self.expected.to_dict()["package_members"]["graphify/marker.py"] = hashlib.sha256(self.source).hexdigest()
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
