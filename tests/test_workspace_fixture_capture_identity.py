"""Current-review staging, inventory-stream, and extra-name regressions."""
import io
import os
import stat
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from graphify.workspace.contracts import ContractError
from tools.workspace_artifacts import candidate


@unittest.skipIf(os.name == 'nt', 'native POSIX fixture publication')
class StagingIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.output = self.root / 'output'
        self.original = self.root / 'original-stage'
        self.bundle = {name: b'{}' for name in ('source-manifest.json', 'compatibility.json', 'runtime-manifest.json')}

    def test_replaced_stage_is_refused_and_never_cleaned(self):
        publish = candidate._publish_fixture
        replacement = []

        def replace_then_publish(stage, output, **kwargs):
            stage.rename(self.original)
            stage.mkdir()
            (stage / 'foreign').write_bytes(b'keep')
            replacement.append(stage)
            publish(stage, output, **kwargs)

        with patch.object(candidate, '_publish_fixture', replace_then_publish), self.assertRaises(ContractError):
            candidate._write_fixture(self.output, self.bundle, b'{}')
        self.assertFalse(self.output.exists())
        self.assertEqual((replacement[0] / 'foreign').read_bytes(), b'keep')
        self.assertTrue((self.original / 'fixture-bundle.zip').is_file())

    def test_vacated_stage_replacement_survives_success(self):
        from graphify import transaction
        rename = transaction._atomic_rename_no_replace
        replacement = []

        def publish_then_replace(parent, source, target):
            rename(parent, source, target)
            stage = parent.path / source
            stage.mkdir()
            (stage / 'foreign').write_bytes(b'keep')
            replacement.append(stage)

        with patch.object(transaction, '_atomic_rename_no_replace', publish_then_replace):
            candidate._write_fixture(self.output, self.bundle, b'{}')
        self.assertTrue((self.output / 'fixture-bundle.zip').is_file())
        self.assertEqual((replacement[0] / 'foreign').read_bytes(), b'keep')

    def test_substitution_at_rename_never_reports_success(self):
        from graphify import transaction
        rename = transaction._atomic_rename_no_replace

        def replace_during_rename(parent, source, target):
            stage = parent.path / source
            stage.rename(self.original)
            stage.mkdir()
            (stage / 'foreign').write_bytes(b'keep')
            rename(parent, source, target)

        with patch.object(transaction, '_atomic_rename_no_replace', replace_during_rename), self.assertRaises(ContractError):
            candidate._write_fixture(self.output, self.bundle, b'{}')
        self.assertEqual((self.output / 'foreign').read_bytes(), b'keep')
        self.assertTrue((self.original / 'fixture-bundle.zip').is_file())

    def test_nonprivate_stage_is_refused_before_writes(self):
        original_open = os.open

        def make_nonprivate(path, flags, *args, **kwargs):
            if flags & os.O_DIRECTORY and str(path).startswith('.graphify-fixture-'):
                os.chmod(path, 0o755, dir_fd=kwargs['dir_fd'])
            return original_open(path, flags, *args, **kwargs)

        with patch.object(os, 'open', make_nonprivate), patch.object(candidate, '_write_fixture_members') as writer, self.assertRaises(ContractError):
            candidate._write_fixture(self.output, self.bundle, b'{}')
        writer.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertEqual(len(list(self.root.glob('.graphify-fixture-*'))), 1)

    def test_foreign_owned_stage_is_refused_before_writes(self):
        original_fstat = os.fstat

        def foreign_owner(fd):
            info = original_fstat(fd)
            if stat.S_ISDIR(info.st_mode):
                return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino,
                                       st_mode=info.st_mode, st_uid=os.geteuid() + 1)
            return info

        with patch.object(os, 'fstat', foreign_owner), patch.object(candidate, '_write_fixture_members') as writer, self.assertRaises(ContractError):
            candidate._write_fixture(self.output, self.bundle, b'{}')
        writer.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertEqual(len(list(self.root.glob('.graphify-fixture-*'))), 1)

    def test_write_failure_preserves_foreign_replacement_member(self):
        import zipfile
        original = zipfile.ZipFile.writestr
        changed = False
        stages = []

        def replace_then_fail(*args, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                stage = next(self.root.glob('.graphify-fixture-*'))
                member = stage / 'source-manifest.json'
                member.rename(self.root / 'original-source')
                member.write_bytes(b'foreign keep')
                stages.append(stage)
                raise OSError('injected')
            return original(*args, **kwargs)

        with patch.object(zipfile.ZipFile, 'writestr', replace_then_fail), self.assertRaises(OSError):
            candidate._write_fixture(self.output, self.bundle, b'{}')
        self.assertEqual((stages[0] / 'source-manifest.json').read_bytes(), b'foreign keep')
        self.assertEqual((self.root / 'original-source').read_bytes(), b'{}')
        self.assertFalse(self.output.exists())

    def test_write_failure_preserves_foreign_replacement_directory(self):
        import zipfile
        stages = []

        def replace_then_fail(*args, **kwargs):
            stage = next(self.root.glob('.graphify-fixture-*'))
            stage.rename(self.original)
            stage.mkdir()
            (stage / 'foreign').write_bytes(b'keep')
            stages.append(stage)
            raise OSError('injected')

        with patch.object(zipfile.ZipFile, 'writestr', replace_then_fail), self.assertRaises(OSError):
            candidate._write_fixture(self.output, self.bundle, b'{}')
        self.assertEqual((stages[0] / 'foreign').read_bytes(), b'keep')
        self.assertTrue((self.original / 'source-manifest.json').is_file())
        self.assertFalse(self.output.exists())



class FakeGitProcess:
    def __init__(self, payload):
        self.stdout = io.BytesIO(payload)
        self.returncode = None
        self.killed = False
        self.waited = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.wait()

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True

    def wait(self):
        self.waited = True
        self.returncode = -9 if self.killed else 0
        return self.returncode


class InventoryStreamTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name).resolve()
        self.names = [f'file-{index}.py' for index in range(20)]
        for name in self.names:
            (self.repo / name).write_bytes(b'x')
        self.payload = ('\0'.join(self.names) + '\0').encode()
        self.process = FakeGitProcess(self.payload)

    def test_output_byte_limit_precedes_all_payload_reads(self):
        with patch.object(candidate, 'MAX_SOURCE_LIST_BYTES', 32, create=True), patch.object(candidate.subprocess, 'check_output', side_effect=['f' * 40, self.payload]), patch.object(candidate.subprocess, 'run'), patch.object(candidate.subprocess, 'Popen', return_value=self.process), patch.object(candidate, '_capture_source', wraps=candidate._capture_source) as capture:
            with self.assertRaises(ContractError):
                candidate.source_manifest(self.repo)
        capture.assert_not_called()
        self.assertTrue(self.process.killed and self.process.waited)

    def test_entry_limit_precedes_all_payload_reads(self):
        with patch.object(candidate, 'MAX_ENTRIES', 10), patch.object(candidate.subprocess, 'check_output', side_effect=['f' * 40, self.payload]), patch.object(candidate.subprocess, 'run'), patch.object(candidate.subprocess, 'Popen', return_value=self.process), patch.object(candidate, '_capture_source', wraps=candidate._capture_source) as capture:
            with self.assertRaises(ContractError):
                candidate.source_manifest(self.repo)
        capture.assert_not_called()
        self.assertTrue(self.process.killed and self.process.waited)

    def test_valid_stream_deduplicates_before_capture(self):
        payload = b'file-0.py\0file-0.py\0file-1.py\0'
        process = FakeGitProcess(payload)
        with patch.object(candidate.subprocess, 'Popen', return_value=process):
            self.assertEqual(candidate._source_names(self.repo), ['file-0.py', 'file-1.py'])
        self.assertFalse(process.killed)
        self.assertTrue(process.waited)



class ExtraNormalizationTests(unittest.TestCase):
    def test_project_extras_match_actual_setuptools_metadata(self):
        from packaging.metadata import Metadata
        from setuptools.dist import Distribution
        from setuptools._core_metadata import write_pkg_file
        project = {'dependencies': [], 'optional-dependencies': {'foo_bar': ['sample>=1']}}
        distribution = Distribution({'name': 'graphifyy', 'version': '0.10.0', 'extras_require': project['optional-dependencies']})
        stream = io.StringIO()
        write_pkg_file(distribution.metadata, stream)
        metadata = Metadata.from_email(stream.getvalue(), validate=True)
        requirements, extras = candidate._project_requirements(project)
        assert metadata.provides_extra is not None and metadata.requires_dist is not None
        self.assertEqual(set(metadata.provides_extra), extras)
        self.assertEqual({str(value) for value in metadata.requires_dist}, requirements)


if __name__ == '__main__':
    unittest.main()
