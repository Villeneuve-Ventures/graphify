"""Completed fixture membership, content binding, and safe refusal regressions."""
import os
import stat
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from graphify.workspace.contracts import ContractError
from tools.workspace_artifacts import candidate


@unittest.skipIf(os.name == 'nt', 'native POSIX fixture publication')
class FixtureMemberBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.bundle = {name: b'{"expected":true}' for name in
                       ('source-manifest.json', 'compatibility.json', 'runtime-manifest.json')}

    def damage(self, stage, kind):
        path = stage / 'source-manifest.json'
        if kind == 'add':
            (stage / 'foreign').write_bytes(b'preserve foreign')
        elif kind == 'remove':
            path.unlink()
        elif kind == 'replace':
            path.rename(stage.parent / ('saved-' + stage.name))
            path.write_bytes(b'foreign replacement')
        elif kind == 'in-place':
            path.write_bytes(b'foreign in-place')
        elif kind == 'mode':
            path.chmod(0o644)
        elif kind == 'fifo':
            path.unlink()
            os.mkfifo(path)
        elif kind == 'hardlink':
            os.link(path, stage.parent / ('link-' + stage.name))
        elif kind == 'symlink':
            path.unlink()
            path.symlink_to(self.root / 'outside')

    def assert_preserved(self, stage, kind):
        if kind == 'add':
            self.assertEqual((stage / 'foreign').read_bytes(), b'preserve foreign')
        elif kind in {'replace', 'in-place'}:
            self.assertTrue((stage / 'source-manifest.json').read_bytes().startswith(b'foreign'))
        elif kind == 'mode':
            self.assertEqual((stage / 'source-manifest.json').stat().st_mode & 0o777, 0o644)
        elif kind == 'fifo':
            self.assertTrue(stat.S_ISFIFO((stage / 'source-manifest.json').lstat().st_mode))
        elif kind == 'hardlink':
            self.assertEqual((stage / 'source-manifest.json').stat().st_nlink, 2)
        elif kind == 'symlink':
            self.assertTrue((stage / 'source-manifest.json').is_symlink())

    def test_all_member_drifts_before_publication_refuse(self):
        publish = candidate._publish_fixture
        for kind in ('add', 'remove', 'replace', 'in-place', 'mode', 'symlink', 'fifo', 'hardlink'):
            with self.subTest(kind=kind):
                directory = self.root / kind
                directory.mkdir()
                output = directory / 'output'
                stages = []

                def damage_then_publish(stage, target, **kwargs):
                    stages.append(stage)
                    self.damage(stage, kind)
                    return publish(stage, target, **kwargs)

                with patch.object(candidate, '_publish_fixture', damage_then_publish), self.assertRaises(ContractError):
                    candidate._write_fixture(output, self.bundle, b'{}')
                self.assertFalse(output.exists())
                self.assert_preserved(stages[0], kind)

    def test_member_drifts_during_rename_refuse_without_rollback(self):
        from graphify import transaction
        rename = transaction._atomic_rename_no_replace
        for kind in ('add', 'remove', 'replace', 'in-place', 'mode', 'symlink', 'fifo', 'hardlink'):
            with self.subTest(kind=kind):
                directory = self.root / kind
                directory.mkdir()
                output = directory / 'output'

                def rename_then_damage(parent, source, target):
                    rename(parent, source, target)
                    self.damage(parent.path / target, kind)

                with patch.object(transaction, '_atomic_rename_no_replace', rename_then_damage), self.assertRaises(ContractError):
                    candidate._write_fixture(output, self.bundle, b'{}')
                self.assertTrue(output.exists())
                self.assert_preserved(output, kind)

    def test_member_mutation_after_publication_helper_refuses(self):
        publish = candidate._publish_fixture
        output = self.root / 'output'

        def publish_then_damage(stage, target, **kwargs):
            publish(stage, target, **kwargs)
            self.damage(target, 'in-place')

        with patch.object(candidate, '_publish_fixture', publish_then_damage), self.assertRaises(ContractError):
            candidate._write_fixture(output, self.bundle, b'{}')
        self.assert_preserved(output, 'in-place')

    def test_same_inode_drift_on_failed_write_is_preserved(self):
        write = candidate._write_fixture_members
        stages = []

        def write_then_damage(*args):
            write(*args)
            stage = next(self.root.glob('.graphify-fixture-*'))
            stages.append(stage)
            self.damage(stage, 'in-place')
            raise OSError('injected completion failure')

        with patch.object(candidate, '_write_fixture_members', write_then_damage), self.assertRaises(OSError):
            candidate._write_fixture(self.root / 'output', self.bundle, b'{}')
        self.assert_preserved(stages[0], 'in-place')

    def test_zip_completion_tamper_is_not_adopted_as_expected(self):
        close = zipfile.ZipFile.close
        damaged = []

        def close_then_damage(archive):
            stream = archive.fp
            close(archive)
            if stream is not None and not damaged:
                stream.flush()
                stage = next(self.root.glob('.graphify-fixture-*'))
                path = stage / 'fixture-bundle.zip'
                with path.open('r+b') as target:
                    target.write(b'X')
                damaged.append(path)

        with patch.object(zipfile.ZipFile, 'close', close_then_damage), self.assertRaises(ContractError):
            candidate._write_fixture(self.root / 'output', self.bundle, b'{}')
        self.assertTrue(damaged[0].read_bytes().startswith(b'X'))

    def test_close_time_tamper_refuses_and_is_preserved(self):
        fdopen = os.fdopen
        damaged = []
        case = self

        class CloseTamper:
            def __init__(self, stream):
                self.stream = stream
            def __getattr__(self, name):
                return getattr(self.stream, name)
            def __enter__(self):
                self.stream.__enter__()
                return self
            def __exit__(self, *args):
                result = self.stream.__exit__(*args)
                if not damaged:
                    stage = next(case.root.glob('.graphify-fixture-*'))
                    path = stage / 'source-manifest.json'
                    path.write_bytes(b'foreign close mutation')
                    damaged.append(path)
                return result

        with patch.object(os, 'fdopen', lambda *args, **kwargs: CloseTamper(fdopen(*args, **kwargs))), self.assertRaises(ContractError):
            candidate._write_fixture(self.root / 'output', self.bundle, b'{}')
        self.assertEqual(damaged[0].read_bytes(), b'foreign close mutation')

    def test_valid_outputs_remain_complete_and_reproducible(self):
        first, second = self.root / 'first', self.root / 'second'
        for output in (first, second):
            candidate._write_fixture(output, self.bundle, b'{}')
        self.assertEqual({path.name: path.read_bytes() for path in first.iterdir()},
                         {path.name: path.read_bytes() for path in second.iterdir()})
        with zipfile.ZipFile(first / 'fixture-bundle.zip') as archive:
            self.assertEqual({name: archive.read(name) for name in archive.namelist()}, self.bundle)

    def test_close_exception_cleans_only_verified_owned_bytes(self):
        fdopen = os.fdopen

        class CloseFailure:
            def __init__(self, stream):
                self.stream = stream
            def __getattr__(self, name):
                return getattr(self.stream, name)
            def __enter__(self):
                self.stream.__enter__()
                return self
            def __exit__(self, *args):
                self.stream.__exit__(*args)
                raise OSError('injected close failure')

        with patch.object(os, 'fdopen', lambda *args, **kwargs: CloseFailure(fdopen(*args, **kwargs))), self.assertRaisesRegex(OSError, 'close failure'):
            candidate._write_fixture(self.root / 'output', self.bundle, b'{}')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_interrupted_partial_write_preserves_uncertified_bytes(self):
        fdopen = os.fdopen

        class PartialFailure:
            def __init__(self, stream):
                self.stream = stream
            def __getattr__(self, name):
                return getattr(self.stream, name)
            def __enter__(self):
                self.stream.__enter__()
                return self
            def __exit__(self, *args):
                return self.stream.__exit__(*args)
            def write(self, payload):
                self.stream.write(payload[:3])
                raise OSError('interrupted partial write')

        with patch.object(os, 'fdopen', lambda *args, **kwargs: PartialFailure(fdopen(*args, **kwargs))), self.assertRaises(ContractError):
            candidate._write_fixture(self.root / 'output', self.bundle, b'{}')
        self.assertFalse((self.root / 'output').exists())
        stage = next(self.root.glob('.graphify-fixture-*'))
        self.assertEqual((stage / 'source-manifest.json').read_bytes(), self.bundle['source-manifest.json'][:3])

    def test_pre_write_failure_leaves_no_public_output_or_stage(self):
        with patch.object(candidate, '_write_fixture_members', side_effect=OSError('before write')), self.assertRaises(OSError):
            candidate._write_fixture(self.root / 'output', self.bundle, b'{}')
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
