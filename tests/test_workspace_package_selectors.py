"""Bounded setuptools selection must match the fixture's source member model."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from graphify.workspace.contracts import ContractError
from tools.workspace_artifacts import candidate


class PackageSelectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name).resolve()
        (self.repo / 'demo').mkdir()
        (self.repo / 'demo/__init__.py').write_bytes(b'# demo\n')
        self.config = {'packages': ['demo'], 'package-data': {}, 'include-package-data': False}

    def test_unsupported_selectors_refuse_before_package_payload_reads(self):
        selectors = {
            'py-modules': ['extra'], 'package-dir': {'': 'src'},
            'exclude-package-data': {'demo': ['*.json']},
            'data-files': {'share': ['data.txt']}, 'script-files': ['run.py'],
            'ext-modules': [{'name': 'demo.native', 'sources': ['native.c']}],
            'namespace-packages': ['demo'], 'cmdclass': {'build_py': 'demo.Build'},
            'dynamic': {'version': {'attr': 'demo.VERSION'}},
        }
        for key, value in selectors.items():
            with self.subTest(key=key), patch.object(candidate, '_read_source', wraps=candidate._read_source) as read:
                with self.assertRaisesRegex(ContractError, 'unsupported setuptools'):
                    candidate.package_members(self.repo, config={**self.config, key: value})
                read.assert_not_called()

    def test_implicit_or_enabled_manifest_data_refuses_before_payload_reads(self):
        for value in (None, True, 0):
            config = dict(self.config)
            if value is None:
                config.pop('include-package-data')
            else:
                config['include-package-data'] = value
            with self.subTest(value=value), patch.object(candidate, '_read_source', wraps=candidate._read_source) as read:
                with self.assertRaisesRegex(ContractError, 'include-package-data'):
                    candidate.package_members(self.repo, config=config)
                read.assert_not_called()

    def test_explicit_supported_selection_succeeds(self):
        self.assertEqual(candidate.package_members(self.repo, config=self.config),
                         {'demo/__init__.py': hashlib.sha256(b'# demo\n').hexdigest()})

    def test_stale_wheel_with_new_module_selector_refuses_before_wheel_read_or_output(self):
        from tests import test_workspace_fixture_inputs as fixtures
        fixture = fixtures.FixtureInputReviewTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        wheel = fixture.wheel()
        project = fixture.repo / 'pyproject.toml'
        project.write_text(project.read_text().replace('[tool.setuptools]\n',
                           '[tool.setuptools]\npy-modules=["extra"]\n'))
        (fixture.repo / 'extra.py').write_bytes(b'new module omitted by stale wheel\n')
        read = candidate._read
        wheel_reads = []

        def capture(path, *args, **kwargs):
            if path == wheel:
                wheel_reads.append(path)
            return read(path, *args, **kwargs)

        with patch.object(candidate, '_read', capture), self.assertRaisesRegex(ContractError, 'unsupported setuptools'):
            fixture.build(wheel)
        self.assertEqual(wheel_reads, [])
        self.assertFalse(fixture.output.exists())

    def test_supported_fixture_still_publishes(self):
        from tests import test_workspace_fixture_inputs as fixtures
        fixture = fixtures.FixtureInputReviewTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.build(fixture.wheel())
        self.assertTrue((fixture.output / 'fixture-manifest.json').is_file())


if __name__ == '__main__':
    unittest.main()
