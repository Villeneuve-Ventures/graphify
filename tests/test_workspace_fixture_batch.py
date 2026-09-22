"""Source-to-wheel metadata, parsed requirements and early selection budgets."""
import base64
import csv
import hashlib
import io
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

from packaging.requirements import Requirement

from graphify.workspace.contracts import ContractError
from tests import test_workspace_fixture_inputs as fixtures
from tools.workspace_artifacts import candidate


class FixtureBatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FixtureInputReviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.wheel = self.fixture.wheel()
        self.project = self.fixture.repo / 'pyproject.toml'

    def add_project(self, declarations):
        self.project.write_text(self.project.read_text().replace('[project]\n', '[project]\n' + declarations))

    def rewrite_wheel(self, name, payload):
        prefix = 'graphifyy-0.10.0.dist-info/'
        with zipfile.ZipFile(self.wheel) as archive:
            members = {n: archive.read(n) for n in archive.namelist()}
        members[prefix + name] = payload
        members.pop(prefix + 'RECORD')
        rows = [[n, 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(b).digest()).rstrip(b'=').decode(), str(len(b))]
                for n, b in members.items()]
        rows.append([prefix + 'RECORD', '', ''])
        stream = io.StringIO()
        csv.writer(stream).writerows(rows)
        members[prefix + 'RECORD'] = stream.getvalue().encode()
        with zipfile.ZipFile(self.wheel, 'w') as archive:
            for n, b in members.items():
                archive.writestr(n, b)

    def metadata(self, headers='', body=''):
        return ('Metadata-Version: 2.4\nName: graphifyy\nVersion: 0.10.0\n'
                'Requires-Python: ==3.14.*,>=3.14.2\n' + headers + '\n' + body).encode()

    def refuse(self):
        with self.assertRaises(ContractError):
            self.fixture.build(self.wheel)
        self.assertFalse(self.fixture.output.exists())

    def test_stale_descriptive_fields_refuse_before_publication(self):
        original = self.project.read_text()
        declarations = ('description="changed"\n', 'keywords=["changed"]\n',
                        'classifiers=["Topic :: Software Development"]\n',
                        'authors=[{name="Changed", email="new@example.com"}]\n',
                        'maintainers=[{name="Changed"}]\n', 'license={text="changed"}\n',
                        '[project.urls]\nHomepage="https://example.com/changed"\n[project]\n')
        for declaration in declarations:
            with self.subTest(declaration=declaration):
                self.project.write_text(original)
                if declaration.startswith('[project.urls]'):
                    self.project.write_text(original + '\n[project.urls]\nHomepage="https://example.com/changed"\n')
                else:
                    self.add_project(declaration)
                self.refuse()

    def test_stale_readme_refuses(self):
        self.add_project('readme="README.md"\n')
        (self.fixture.repo / 'README.md').write_text('Changed README\n')
        self.refuse()

    def test_fresh_descriptive_metadata_and_readme_pass(self):
        self.add_project('description="  A summary  "\nkeywords=["a", " b,c"]\n'
                         'classifiers=["Topic :: Software Development"]\nreadme="README.MD"\n'
                         'authors=[{name="Name, Jr", email="a@example.com"}, {name="Other"}]\n'
                         'maintainers=[{email="m@example.com"}]\nlicense={file="LICENSE"}\n')
        self.project.write_text(self.project.read_text() + '\n[project.urls]\n" Home "=" https://example.com "\n')
        (self.fixture.repo / 'README.MD').write_bytes(b'New README\r\nSecond line')
        self.rewrite_wheel('METADATA', self.metadata(
            'Summary: A summary  \nKeywords: a, b,c\nClassifier: Topic :: Software Development\n'
            'Project-URL: Home, https://example.com\nAuthor: Other\n'
            'Author-email: "Name, Jr" <a@example.com>\nMaintainer-email: m@example.com\n'
            'License: fixture license\nDescription-Content-Type: text/markdown\n',
            'New README\nSecond line\n'))
        self.fixture.build(self.wheel)
        self.assertTrue(self.fixture.output.is_dir())

    def test_inline_readme_and_spdx_license_pass(self):
        self.add_project('readme={text="Inline text", content-type="text/plain"}\nlicense="MIT"\n')
        self.rewrite_wheel('METADATA', self.metadata('License-Expression: MIT\nDescription-Content-Type: text/plain\n', 'Inline text\n'))
        self.fixture.build(self.wheel)
        self.assertTrue(self.fixture.output.is_dir())

    def test_readme_bytes_must_match_captured_inventory(self):
        self.add_project('readme="README.md"\n')
        readme = self.fixture.repo / 'README.md'
        readme.write_text('Captured text\n')
        self.rewrite_wheel('METADATA', self.metadata('Description-Content-Type: text/markdown\n', 'Different text\n'))
        read = candidate._read_source

        def substitute(repo, path, **kwargs):
            return b'Different text\n' if path == readme else read(repo, path, **kwargs)

        with patch.object(candidate, '_read_source', substitute):
            self.refuse()

    def test_unexpected_metadata_is_not_silently_accepted(self):
        self.rewrite_wheel('METADATA', self.metadata('Summary: not declared\n'))
        self.refuse()

    def test_modern_fixture_profile_accepts_matching_generator(self):
        self.rewrite_wheel('WHEEL', b'Wheel-Version: 1.0\nGenerator: setuptools (77.0.1)\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        self.fixture.build(self.wheel)
        self.assertTrue(self.fixture.output.is_dir())

    def test_generator_must_satisfy_declared_build_requirement(self):
        self.project.write_text(self.project.read_text().replace('setuptools>=68', 'setuptools==68.0.0'))
        self.refuse()

    def test_legacy_unknown_or_ambiguous_generator_refuses(self):
        for generator in ('setuptools (68.0.0)', 'setuptools (76.1.0)', 'setuptools (77.0rc1)',
                          'bdist_wheel (0.45.1)', 'setuptools (unknown)', '',
                          'setuptools (82.0.1)\nGenerator: setuptools (82.0.1)'):
            with self.subTest(generator=generator):
                self.rewrite_wheel('WHEEL', ('Wheel-Version: 1.0\nGenerator: ' + generator + '\nRoot-Is-Purelib: true\nTag: py3-none-any\n').encode())
                self.refuse()


class RequirementParsingTests(unittest.TestCase):
    def test_direct_url_semicolons_survive_extra_marker_composition(self):
        for raw in ('demo @ https://example.com/a;b',
                    'demo @ https://example.com/a;b ; python_version >= "3.14"',
                    'demo>=1; python_version >= "3.14" or sys_platform == "win32"'):
            with self.subTest(raw=raw):
                result, extras = candidate._project_requirements({'dependencies': [], 'optional-dependencies': {'extra_name': [raw]}})
                parsed = Requirement(result.pop())
                self.assertEqual(parsed.url, Requirement(raw).url)
                self.assertEqual(extras, {'extra-name'})
                assert parsed.marker is not None
                self.assertFalse(parsed.marker.evaluate({'extra': 'other', 'python_version': '3.14', 'sys_platform': 'win32'}))
                self.assertTrue(parsed.marker.evaluate({'extra': 'extra-name', 'python_version': '3.14', 'sys_platform': 'linux'}))


class PackageBudgetTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FixtureInputReviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo = self.fixture.repo
        self.config = {'packages': ['graphify'], 'include-package-data': False, 'package-data': {}}

    def test_glob_count_stops_before_materialization_or_hashing(self):
        original_glob = Path.glob
        yielded = []

        def huge_glob(path, pattern, *args, **kwargs):
            if pattern != '*.py':
                return original_glob(path, pattern, *args, **kwargs)
            def matches():
                for i in range(20):
                    yielded.append(i)
                    yield path / f'{i}.py'
            return matches()

        # Fake stat only for selected names: no files need to be created to
        # prove that expansion stops before consuming the large iterator.
        lstat = Path.lstat
        reference = (self.repo / 'graphify/__init__.py').lstat()
        with patch.object(candidate, 'MAX_PACKAGE_MEMBERS', 3, create=True), patch.object(Path, 'glob', huge_glob), patch.object(Path, 'lstat', lambda p: reference if p.suffix == '.py' else lstat(p)), patch.object(candidate, '_read_source', return_value=b'') as read:
            with self.assertRaisesRegex(ContractError, 'count limit'):
                candidate.package_members(self.repo, config=self.config)
            self.assertEqual(yielded, [0, 1, 2, 3])
            read.assert_not_called()

    def test_ignored_data_bytes_refuse_before_any_member_read(self):
        data = self.repo / 'graphify/ignored'
        data.mkdir()
        (self.repo / '.gitignore').write_text('graphify/ignored/\n')
        (data / 'one.bin').write_bytes(b'x' * 32)
        (data / 'two.bin').write_bytes(b'x' * 32)
        self.config['package-data'] = {'graphify': ['ignored/*.bin']}
        with patch.object(candidate, 'MAX_PACKAGE_BYTES', 60, create=True), patch.object(candidate, '_read_source', return_value=b'') as read:
            with self.assertRaisesRegex(ContractError, 'aggregate byte limit'):
                candidate.package_members(self.repo, config=self.config)
            read.assert_not_called()

    def test_overlapping_patterns_do_not_double_charge_bytes(self):
        self.config['package-data'] = {'graphify': ['*.py', '__init__.py']}
        expected = candidate.package_members(self.repo, config=self.config)
        total = sum((self.repo / name).stat().st_size for name in expected)
        with patch.object(candidate, 'MAX_PACKAGE_BYTES', total, create=True):
            self.assertEqual(candidate.package_members(self.repo, config=self.config), expected)

    def test_growth_after_preflight_refuses(self):
        read = candidate._read_source
        changed = False

        def grow(repo, path, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                path.write_bytes(path.read_bytes() + b'growth')
            return read(repo, path, **kwargs)

        with patch.object(candidate, '_read_source', grow):
            with self.assertRaises(ContractError):
                candidate.package_members(self.repo, config=self.config)


if __name__ == '__main__':
    unittest.main()
