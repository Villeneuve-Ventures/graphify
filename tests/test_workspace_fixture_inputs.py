"""Focused current-review fixture input/metadata regressions (stdlib runner)."""
import base64
import csv
import hashlib
import io
import os
import stat
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from graphify.workspace.composition import StructuralPolicy
from graphify.workspace.contracts import ContractError, SCHEMA_FILES
from tools.workspace_artifacts import candidate
from tests.test_workspace_fixture_capture_identity import FakeGitProcess


class FixtureInputReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        modules = ("__init__.py", "__main__.py", "source_io.py", "workspace/contracts.py",
                   "workspace/composition.py", "workspace/__init__.py",
                   "workspace/adapters/base.py", "workspace/adapters/__init__.py")
        for name in modules:
            path = self.repo / "graphify" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"# fixture\n")
        for name in SCHEMA_FILES:
            path = self.repo / "graphify/workspace/schemas" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"{}")
        (self.repo / "LICENSE").write_bytes(b"fixture license")
        self.config = {'packages': ['graphify', 'graphify.workspace', 'graphify.workspace.adapters'],
                       'include-package-data': False,
                       'package-data': {'graphify.workspace': ['schemas/*.schema.json']}}
        self.output = self.root / "output"

    def wheel(self, *, extra_script=False, missing_script=False, top_level=b"graphify\n", entry_points=None):
        extra = 'foo = "graphify.__main__:main"\n' if extra_script else ''
        project = ('[build-system]\nrequires=["setuptools>=68"]\nbuild-backend="setuptools.build_meta"\n'
                   '[project]\nname="graphifyy"\nversion="0.10.0"\n'
                   'requires-python=">=3.14.2,==3.14.*"\ndependencies=[]\n'
                   '[project.optional-dependencies]\n[project.scripts]\n'
                   'graphify="graphify.__main__:main"\ngraphify-mcp="graphify.serve:_main"\n'
                   + extra + '[tool.setuptools]\ninclude-package-data=false\npackages=["graphify", "graphify.workspace", "graphify.workspace.adapters"]\n'
                   '[tool.setuptools.package-data]\n"graphify.workspace"=["schemas/*.schema.json"]\n')
        if missing_script:
            project = project.replace('graphify-mcp="graphify.serve:_main"\n', '')
        (self.repo / 'pyproject.toml').write_text(project)
        members = candidate.package_members(self.repo, config=self.config)
        payloads = {name: (self.repo / name).read_bytes() for name in members}
        prefix = 'graphifyy-0.10.0.dist-info/'
        payloads.update({prefix + 'METADATA': b'Metadata-Version: 2.4\nName: graphifyy\nVersion: 0.10.0\nRequires-Python: ==3.14.*,>=3.14.2\n',
                         prefix + 'WHEEL': b'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
                         prefix + 'entry_points.txt': b'[console_scripts]\ngraphify = graphify.__main__:main\ngraphify-mcp = graphify.serve:_main\n' + (b'foo = graphify.__main__:main\n' if extra_script else b''),
                         prefix + 'top_level.txt': top_level,
                         prefix + 'licenses/LICENSE': b'fixture license'})
        if missing_script:
            payloads[prefix + 'entry_points.txt'] = payloads[prefix + 'entry_points.txt'].replace(b'graphify-mcp = graphify.serve:_main\n', b'')
        if entry_points is not None:
            payloads[prefix + 'entry_points.txt'] = entry_points
        rows = [[name, 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode(), str(len(data))]
                for name, data in payloads.items()]
        rows.append([prefix + 'RECORD', '', ''])
        stream = io.StringIO()
        csv.writer(stream).writerows(rows)
        payloads[prefix + 'RECORD'] = stream.getvalue().encode()
        wheel = self.root / 'graphifyy-0.10.0-py3-none-any.whl'
        with zipfile.ZipFile(wheel, 'w') as archive:
            for name, data in payloads.items():
                archive.writestr(name, data)
        return wheel

    def build(self, wheel):
        inventory = {'files': {path.relative_to(self.repo).as_posix(): {'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                               for path in self.repo.rglob('*') if path.is_file()}}
        with patch.object(candidate, 'source_manifest', return_value=inventory):
            return candidate.build_fixture(repo_root=self.repo, wheel=wheel, output_root=self.output,
                                           policy=StructuralPolicy(1, 1, 1, 1, 1))

    def test_supported_fixture_publishes(self):
        self.build(self.wheel())
        self.assertTrue((self.output / 'fixture-manifest.json').is_file())

    def test_unsupported_matching_script_refuses_before_output(self):
        with self.assertRaises(ContractError):
            self.build(self.wheel(extra_script=True))
        self.assertFalse(self.output.exists())

    def test_matching_project_and_wheel_missing_supported_script_refuse(self):
        with self.assertRaises(ContractError):
            self.build(self.wheel(missing_script=True))
        self.assertFalse(self.output.exists())

    def test_malformed_entry_points_use_contract_refusal(self):
        for payload in (b'not an INI header', b'\xff'):
            with self.subTest(payload=payload), self.assertRaises(ContractError):
                self.build(self.wheel(entry_points=payload))
            self.assertFalse(self.output.exists())

    def test_wrong_top_level_with_correct_record_refuses_before_output(self):
        with self.assertRaises(ContractError):
            self.build(self.wheel(top_level=b'other\n'))
        self.assertFalse(self.output.exists())

    def test_missing_configured_package_refuses(self):
        self.config['packages'].append('graphify.missing')
        with self.assertRaises(ContractError):
            candidate.package_members(self.repo, config=self.config)

    def test_symlinked_package_data_ancestor_refuses(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'data.bin').write_bytes(b'external')
        (self.repo / 'graphify/.venv').symlink_to(outside, target_is_directory=True)
        self.config['package-data']['graphify'] = ['.venv/data.bin']
        with self.assertRaises(ContractError):
            candidate.package_members(self.repo, config=self.config)

    def test_nonnfc_git_name_refuses_before_capture(self):
        name = 'notes/e\u0301.txt'
        path = self.repo / name
        path.parent.mkdir()
        path.write_bytes(b'raw filename')
        with patch.object(candidate.subprocess, 'check_output', return_value='f' * 40), patch.object(candidate.subprocess, 'Popen', return_value=FakeGitProcess((name + '\0').encode())), patch.object(candidate.subprocess, 'run'):
            with self.assertRaises(ContractError):
                candidate.source_manifest(self.repo)

    def test_non_utf8_git_name_refuses(self):
        with patch.object(candidate.subprocess, 'check_output', return_value='f' * 40), patch.object(candidate.subprocess, 'Popen', return_value=FakeGitProcess(b'notes/\xff.txt\0')), patch.object(candidate.subprocess, 'run'):
            with self.assertRaises(ContractError):
                candidate.source_manifest(self.repo)

    def test_source_parent_replaced_during_capture_refuses(self):
        directory = self.repo / 'graphify/data'
        directory.mkdir()
        path = directory / 'member.bin'
        path.write_bytes(b'original')
        original_capture = candidate._capture

        def replace_then_read(target):
            directory.rename(self.repo / 'graphify/old-data')
            directory.mkdir()
            (directory / 'member.bin').write_bytes(b'replacement')
            return original_capture(target)

        with patch.object(candidate, '_capture', replace_then_read), self.assertRaises(ContractError):
            candidate._read_source(self.repo, path)

    def test_external_wheel_path_parent_alias_is_allowed(self):
        wheel = self.wheel()
        alias = self.root / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        self.build(alias / wheel.name)
        self.assertTrue((self.output / 'fixture-manifest.json').is_file())

    def test_project_snapshot_hook_reaches_capture_boundary(self):
        self.wheel()
        path = self.repo / 'pyproject.toml'
        capture = candidate._capture
        reads = 0

        def racing_capture(target, **kwargs):
            nonlocal reads
            payload, info = capture(target, **kwargs)
            if target == path:
                reads += 1
                if reads == 2:
                    payload += b'\n# alternate project snapshot\n'
            return payload, info

        with patch.object(candidate, '_capture', racing_capture):
            payload, _ = candidate._capture_source(self.repo, path)
            inventory = {'files': {'pyproject.toml': {'sha256': hashlib.sha256(payload).hexdigest()}}}
            with self.assertRaisesRegex(ContractError, 'project configuration differs'):
                candidate._project_document(self.repo, inventory)
        self.assertEqual(reads, 2)

    def test_bundled_test_hook_reaches_capture_boundary(self):
        wheel = self.wheel()
        path = self.repo / 'tests/test_workspace_example.py'
        path.parent.mkdir()
        path.write_bytes(b'# test source')
        capture = candidate._capture
        reads = 0

        def racing_capture(target, **kwargs):
            nonlocal reads
            payload, info = capture(target, **kwargs)
            if target == path:
                reads += 1
                if reads == 2:
                    payload += b'\n# changed after capture\n'
            return payload, info

        with patch.object(candidate, '_capture', racing_capture), self.assertRaisesRegex(ContractError, 'candidate changed'):
            self.build(wheel)
        self.assertEqual(reads, 2)
        self.assertFalse(self.output.exists())

    def test_source_mode_belongs_to_hashed_descriptor(self):
        path = self.repo / 'recorded.py'
        path.write_bytes(b'captured A')
        path.chmod(0o644)
        identity = path.stat()
        replacement = self.repo / 'replacement'
        replacement.write_bytes(b'replacement B')
        replacement.chmod(0o755)
        original_close = os.close

        def replace_after_capture(fd):
            captured = os.fstat(fd)
            original_close(fd)
            if (captured.st_dev, captured.st_ino) == (identity.st_dev, identity.st_ino):
                replacement.replace(path)

        with patch.object(candidate.subprocess, 'check_output', return_value='f' * 40), patch.object(candidate.subprocess, 'Popen', return_value=FakeGitProcess(b'recorded.py\0')), patch.object(candidate.subprocess, 'run'), patch.object(os, 'close', replace_after_capture):
            manifest = candidate.source_manifest(self.repo)
        self.assertEqual(manifest['files']['recorded.py'], {
            'sha256': hashlib.sha256(b'captured A').hexdigest(), 'mode': 0o644})
        self.assertEqual(path.read_bytes(), b'replacement B')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)

    def test_python314_double_star_segment_is_a_supported_glob(self):
        directory = self.repo / 'graphify/foo'
        directory.mkdir()
        (directory / 'mybar').write_bytes(b'valid')
        self.config['package-data']['graphify'] = ['foo/**bar']
        self.assertIn('graphify/foo/mybar', candidate.package_members(self.repo, config=self.config))


if __name__ == '__main__':
    unittest.main()
