"""The fixture models implicit typing data and refuses unmodeled build inputs."""
import unittest
from unittest.mock import patch

from graphify.workspace.contracts import ContractError
from tools.workspace_artifacts import candidate
from tests import test_workspace_fixture_inputs as fixtures


class BuildInputTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FixtureInputReviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_automatic_typing_files_refuse_stale_wheel(self):
        wheel = self.fixture.wheel()
        (self.fixture.repo / 'graphify/py.typed').write_bytes(b'')
        with self.assertRaises(ContractError):
            self.fixture.build(wheel)
        self.assertFalse(self.fixture.output.exists())

    def test_typing_selection_is_package_local(self):
        for name in ('graphify/py.typed', 'graphify/api.pyi', 'graphify/.hidden.pyi',
                     'graphify/unlisted/api.pyi', 'graphify/workspace/api.pyi', 'root.pyi', 'py.typed'):
            path = self.fixture.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'')
        members = candidate.package_members(self.fixture.repo, config=self.fixture.config)
        self.assertEqual({name for name in members if name.endswith(('.pyi', 'py.typed'))},
                         {'graphify/py.typed', 'graphify/api.pyi', 'graphify/workspace/api.pyi'})

    def test_fresh_wheel_with_independently_selected_typing_files_is_accepted(self):
        members = candidate.package_members(self.fixture.repo, config=self.fixture.config)
        for name in ('graphify/py.typed', 'graphify/api.pyi', 'graphify/workspace/api.pyi'):
            (self.fixture.repo / name).write_bytes(b'')
            members[name] = ''
        # Construct the backend's known typing inventory independently of the
        # candidate selector, so a missing model entry makes admission fail.
        with patch.object(candidate, 'package_members', return_value=members):
            wheel = self.fixture.wheel()
        self.fixture.build(wheel)
        self.assertTrue((self.fixture.output / 'fixture-manifest.json').is_file())

    def test_alternate_build_documents_refuse_before_wheel_read(self):
        wheel = self.fixture.wheel()
        project = self.fixture.repo / 'pyproject.toml'
        original = project.read_text()
        variants = (
            original.replace('setuptools.build_meta', 'other.backend'),
            original.replace('[build-system]\n', '[build-system]\nbackend-path=["."]\n'),
            original.replace('[build-system]\n', '[build-system]\nunknown=true\n'),
            original.replace('"setuptools>=68"', '"setuptools>=68", "plugin"'),
            original.replace('"setuptools>=68"', '"setuptools[plugin]"'),
            original.replace('"setuptools>=68"', '"setuptools @ https://example.invalid/build.whl"'),
            original.replace('"setuptools>=68"', '"setuptools; python_version < \'3\'"'),
            original.replace('[project]\n', '[project]\ndynamic=["version"]\n'),
            original + '\n[project.gui-scripts]\nextra="graphify:main"\n',
            original + '\n[project.entry-points.custom]\nextra="graphify:main"\n',
        )
        for document in variants:
            with self.subTest(document=document):
                project.write_text(document)
                with patch.object(candidate, '_read', side_effect=AssertionError('wheel opened')):
                    with self.assertRaises(ContractError):
                        self.fixture.build(wheel)
                self.assertFalse(self.fixture.output.exists())

    def test_setup_inputs_including_ignored_and_dangling_links_refuse(self):
        wheel = self.fixture.wheel()
        (self.fixture.repo / '.gitignore').write_text('setup.py\nsetup.cfg\n')
        for name in ('setup.py', 'setup.cfg'):
            path = self.fixture.repo / name
            for symlink in (False, True):
                with self.subTest(name=name, symlink=symlink):
                    if symlink:
                        path.symlink_to(self.fixture.repo / 'absent')
                    else:
                        path.write_text('raise AssertionError("must not execute")\n')
                    try:
                        with patch.object(candidate, '_read', side_effect=AssertionError('wheel opened')):
                            with self.assertRaises(ContractError):
                                self.fixture.build(wheel)
                        self.assertFalse(self.fixture.output.exists())
                    finally:
                        path.unlink()

    def test_setup_input_appearing_during_collection_refuses(self):
        wheel = self.fixture.wheel()
        collect = candidate._collect_bundle_sources

        def collect_then_add(*args):
            result = collect(*args)
            (self.fixture.repo / 'setup.cfg').write_text('[options]\npy_modules=extra\n')
            return result

        with patch.object(candidate, '_collect_bundle_sources', collect_then_add):
            with self.assertRaises(ContractError):
                self.fixture.build(wheel)
        self.assertFalse(self.fixture.output.exists())

    def test_fresh_wheel_for_setup_module_configuration_also_refuses(self):
        members = candidate.package_members(self.fixture.repo, config=self.fixture.config)
        (self.fixture.repo / 'extra.py').write_bytes(b'# alternate build module\n')
        members['extra.py'] = ''
        with patch.object(candidate, 'package_members', return_value=members):
            wheel = self.fixture.wheel(top_level=b'graphify\nextra\n')
        (self.fixture.repo / 'setup.py').write_text(
            'from setuptools import setup\nsetup(py_modules=["extra"])\n')
        with patch.object(candidate, '_read', side_effect=AssertionError('wheel opened')):
            with self.assertRaises(ContractError):
                self.fixture.build(wheel)
        self.assertFalse(self.fixture.output.exists())


if __name__ == '__main__':
    unittest.main()
