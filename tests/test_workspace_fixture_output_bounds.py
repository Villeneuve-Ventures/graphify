"""Focused fixture output races and retained-payload bounds (stdlib runner)."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from graphify.workspace.contracts import ContractError
from tools.workspace_artifacts import candidate


@unittest.skipIf(os.name == 'nt', 'descriptor-relative POSIX staging')
class FixtureOutputRaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.checkout = self.root / 'checkout'
        self.checkout.mkdir()
        self.parent = self.root / 'external'
        self.parent.mkdir()
        self.detached = self.root / 'detached'
        self.output = self.parent / 'fixture'
        self.bundle = {name: b'{}' for name in ('source-manifest.json', 'compatibility.json', 'runtime-manifest.json')}

    def test_redirected_parent_never_receives_staging_writes(self):
        from graphify.transaction import PendingTransactionError
        self.parent.rename(self.detached)
        self.parent.symlink_to(self.checkout, target_is_directory=True)
        created = []
        mkdir = os.mkdir

        def record_mkdir(path, *args, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)):
                resolved = Path(os.fsdecode(path)).resolve()
                if resolved.is_relative_to(self.checkout):
                    created.append(str(resolved))
            return mkdir(path, *args, **kwargs)

        with patch.object(os, 'mkdir', record_mkdir), self.assertRaises((ContractError, PendingTransactionError)):
            candidate._write_fixture(self.output, self.bundle, b'{}')
        self.assertEqual(created, [])
        self.assertEqual(list(self.checkout.iterdir()), [])

    def test_redirected_ancestor_is_checked_against_checkout_before_mkdir(self):
        outer = self.root / 'outer'
        outer.mkdir()
        (outer / 'parent').mkdir()
        (self.checkout / 'parent').mkdir()
        output = outer / 'parent/fixture'
        self.assertFalse(output.resolve().is_relative_to(self.checkout))
        outer.rename(self.root / 'detached-outer')
        outer.symlink_to(self.checkout, target_is_directory=True)
        with patch.object(os, 'mkdir') as mkdir, self.assertRaisesRegex(ContractError, 'outside source'):
            candidate._write_fixture(output, self.bundle, b'{}', repo=self.checkout)
        mkdir.assert_not_called()
        self.assertEqual(list((self.checkout / 'parent').iterdir()), [])

    def test_parent_redirect_during_writes_does_not_redirect_cleanup(self):
        import zipfile
        write = zipfile.ZipFile.writestr
        redirected = False

        def redirect_then_write(*args, **kwargs):
            nonlocal redirected
            if not redirected:
                redirected = True
                self.parent.rename(self.detached)
                self.parent.symlink_to(self.checkout, target_is_directory=True)
            return write(*args, **kwargs)

        from graphify.transaction import PendingTransactionError
        with patch.object(zipfile.ZipFile, 'writestr', redirect_then_write), self.assertRaises(PendingTransactionError):
            candidate._write_fixture(self.output, self.bundle, b'{}')
        self.assertEqual(list(self.checkout.iterdir()), [])
        self.assertEqual(list(self.detached.iterdir()), [])

    def test_racing_public_directory_is_never_replaced(self):
        publish = candidate._publish_fixture

        def race(payload, output, **kwargs):
            output.mkdir()
            publish(payload, output, **kwargs)

        with patch.object(candidate, '_publish_fixture', race), self.assertRaises(FileExistsError):
            candidate._write_fixture(self.output, self.bundle, b'{}')
        self.assertTrue(self.output.is_dir())
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(list(self.parent.iterdir()), [self.output])

    def test_write_failure_cleans_stage_without_public_output(self):
        import zipfile
        with patch.object(zipfile.ZipFile, 'writestr', side_effect=OSError('injected')):
            with self.assertRaises(OSError):
                candidate._write_fixture(self.output, self.bundle, b'{}')
        self.assertEqual(list(self.parent.iterdir()), [])
        candidate._write_fixture(self.output, self.bundle, b'{}')
        self.assertTrue((self.output / 'fixture-bundle.zip').is_file())


class FixtureBundleLimitTests(unittest.TestCase):
    def setUp(self):
        from tests import test_workspace_fixture_inputs as fixture_inputs
        self.fixture = fixture_inputs.FixtureInputReviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.wheel = self.fixture.wheel()
        self.tests = self.fixture.repo / 'tests'
        self.tests.mkdir()

    def test_aggregate_refuses_before_reading_test_payloads(self):
        for index in range(12):
            (self.tests / f'test_workspace_{index}.py').write_bytes(b'x' * 4096)
        read = candidate._read_source
        test_reads = []

        def observed(repo, path, **kwargs):
            if path.parent == self.tests:
                test_reads.append(path)
            return read(repo, path, **kwargs)

        with patch.object(candidate, 'MAX_FIXTURE_BUNDLE_BYTES', 32768, create=True), patch.object(candidate, '_read_source', observed):
            with self.assertRaises(ContractError):
                self.fixture.build(self.wheel)
        self.assertEqual(test_reads, [])
        self.assertFalse(self.fixture.output.exists())

    def test_member_count_refuses_empty_files_before_reading(self):
        for index in range(12):
            (self.tests / f'test_workspace_{index}.py').touch()
        with patch.object(candidate, 'MAX_FIXTURE_BUNDLE_MEMBERS', 10, create=True):
            with self.assertRaises(ContractError):
                self.fixture.build(self.wheel)
        self.assertFalse(self.fixture.output.exists())

    def test_growth_after_preflight_refuses_before_payload_read(self):
        path = self.tests / 'test_workspace_growing.py'
        path.write_bytes(b'x')
        bundle = {'identity': b'x'}
        limit = 2 + sum((self.fixture.repo / 'graphify/workspace/schemas' / name).stat().st_size
                        for name in candidate.SCHEMA_FILES)
        read = candidate._read_source
        budgets = []

        def grow(repo, target, **kwargs):
            if target == path:
                budgets.append(kwargs['max_bytes'])
                target.write_bytes(b'xx')
                with patch.object(os, 'read', side_effect=AssertionError('over-budget payload read')):
                    return read(repo, target, **kwargs)
            return read(repo, target, **kwargs)

        with patch.object(candidate, 'MAX_FIXTURE_BUNDLE_BYTES', limit), patch.object(candidate, '_read_source', grow), self.assertRaises(ContractError):
            candidate._collect_bundle_sources(self.fixture.repo, bundle)
        self.assertEqual(budgets, [1])
        self.assertNotIn('tests/' + path.name, bundle)



class UnavailableMutationTests(unittest.TestCase):
    def test_windows_mutation_refuses_before_any_staging_write(self):
        from graphify import transaction
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'fixture'
            with patch.object(transaction, '_PLATFORM', 'windows'), patch.object(os, 'mkdir') as mkdir, patch.object(candidate, '_write_fixture_members') as write:
                with self.assertRaisesRegex(transaction.PendingTransactionError, 'Windows non-retargetable'):
                    candidate._write_fixture(output, {}, b'{}')
            mkdir.assert_not_called()
            write.assert_not_called()
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
