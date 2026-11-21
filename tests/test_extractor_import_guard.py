"""Exercise the source-only extractor dependency guard and its entry point."""

from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from tools.extractor_import_guard import check_repository


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "tools/extractor_import_guard.py"


def _repository(tmp_path: Path, source: str, name: str = "sample.py") -> Path:
    path = tmp_path / "graphify/extractors" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("source,name", [
    ("import graphify.extract", "sample.py"),
    ("import graphify.extract as facade", "sample.pyi"),
    ("from graphify.extract import helper as renamed", "sample.py"),
    ("from graphify import extract as facade", "sample.py"),
    ("from .. import extract as facade", "sample.py"),
    ("from ..extract import helper", "sample.py"),
    ("from ... import extract", "nested/sample.py"),
    ("from ...extract import helper", "nested/__init__.py"),
    ("def extract():\n    import graphify.extract", "sample.py"),
    ("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from ..extract import X", "sample.py"),
    ('import importlib\nimportlib.import_module("graphify.extract")', "sample.py"),
    ('import importlib as il\nil.import_module(name="graphify.extract")', "sample.py"),
    ('from importlib import import_module as load\nload("graphify.extract")', "sample.py"),
    ('from importlib import import_module\nimport_module("..extract", "graphify.extractors")', "sample.py"),
    ('import importlib\nimportlib.import_module("...extract", package="graphify.extractors.nested")', "sample.py"),
    ('__import__("graphify.extract")', "sample.py"),
    ('import builtins as bi\nbi.__import__(name="graphify.extract")', "sample.py"),
    ('from builtins import __import__ as load\nload("graphify.extract")', "sample.py"),
    ('__import__("graphify", fromlist=["extract"])', "sample.py"),
    ('import importlib\ndef run():\n    importlib.import_module("graphify.extract")', "sample.py"),
    ('def run():\n    from importlib import import_module as load\n    load("graphify.extract")', "sample.py"),
    ('import importlib\n(lambda: importlib.import_module("graphify.extract"))()', "sample.py"),
    ('import importlib\n[importlib.import_module("graphify.extract") for x in values]', "sample.py"),
    ('import importlib\nclass C:\n    importlib = custom\n    def run(self):\n        importlib.import_module("graphify.extract")', "sample.py"),
    ('import importlib\nclass C:\n    importlib = custom\n    def run[T](self):\n        importlib.import_module("graphify.extract")', "sample.py"),
])
def test_forbidden_dependencies(tmp_path, source, name):
    diagnostics = check_repository(_repository(tmp_path, source, name))
    assert len(diagnostics) == 1
    diagnostic = str(diagnostics[0])
    assert "GRA-IMPORT-001" in diagnostic
    assert f"graphify/extractors/{name}:" in diagnostic
    assert "graphify.extract" in diagnostic
    assert "MIGRATION.md" in diagnostic
    assert "shared" in diagnostic and "facade" in diagnostic and "registry" in diagnostic


@pytest.mark.parametrize("source,name", [
    ("import graphify.extractors.models", "sample.py"),
    ("from graphify.extractors import engine", "sample.py"),
    ("from . import extract", "sample.py"),
    ("from ..extractors.models import LanguageConfig", "sample.py"),
    ("from .. import extract", "nested/sample.py"),
    ("from .extract import helper", "__init__.py"),
    ('import importlib\nimportlib.import_module(config.ts_module)', "sample.py"),
    ('import importlib\nimportlib.import_module("graphify.extractors.models")', "sample.py"),
    ('import importlib\nimportlib.import_module(".extract", "graphify.extractors")', "sample.py"),
    ('import importlib\nimportlib.import_module("..extract", "graphify.extractors.nested")', "sample.py"),
    ('def run(importlib):\n    importlib.import_module("graphify.extract")', "sample.py"),
    ('import importlib\ndef run(importlib):\n    importlib.import_module("graphify.extract")', "sample.py"),
    ('import importlib\ndef run():\n    importlib.import_module("graphify.extract")\n    importlib = custom', "sample.py"),
    ('from importlib import import_module as load\nload = custom\nload("graphify.extract")', "sample.py"),
    ('def run(__import__):\n    __import__("graphify.extract")', "sample.py"),
    ('__import__ = custom\n__import__("graphify.extract")', "sample.py"),
    ('import importlib\n(lambda importlib: importlib.import_module("graphify.extract"))(custom)', "sample.py"),
    ('import importlib\n[importlib.import_module("graphify.extract") for importlib in values]', "sample.py"),
    ('import importlib\nclass C:\n    importlib = custom\n    importlib.import_module("graphify.extract")', "sample.py"),
    ('import importlib\nimportlib.import_module = custom\nimportlib.import_module("graphify.extract")', "sample.py"),
    ('import importlib\ntry:\n    pass\nexcept Exception as importlib:\n    importlib.import_module("graphify.extract")', "sample.py"),
    ('import importlib\nmatch value:\n    case {"loader": importlib}:\n        importlib.import_module("graphify.extract")', "sample.py"),
    ('import importlib\n[(importlib := custom) for x in values]\nimportlib.import_module("graphify.extract")', "sample.py"),
    ('from custom import *\n__import__("graphify.extract")', "sample.py"),
    ('import importlib\ndef run():\n    global importlib\n    importlib.import_module("graphify.extract")', "sample.py"),
    ('import importlib\ndef run[importlib]():\n    importlib.import_module("graphify.extract")', "sample.py"),
    ('import importlib\nclass C[importlib]:\n    importlib.import_module("graphify.extract")', "sample.py"),
    ('import importlib\ntype Alias[importlib] = importlib.import_module("graphify.extract")', "sample.py"),
    ('class C:\n    import importlib\n    def run[T](self):\n        importlib.import_module("graphify.extract")', "sample.py"),
])
def test_permitted_or_unresolved_dependencies(tmp_path, source, name):
    assert check_repository(_repository(tmp_path, source, name)) == []


@pytest.mark.parametrize(("body", "expected"), [
    ("load: object\nload('graphify.extract')", 1),
    ("class C:\n    load: object\n    load('graphify.extract')", 1),
    ("def f():\n    load: object\n    load('graphify.extract')", 0),
    ("def f():\n    from importlib import import_module as load\n    load: object\n    load('graphify.extract')", 1),
    ("load: object = custom\nload('graphify.extract')", 0),
    ("load.attribute: object\nload('graphify.extract')", 1),
    ("table[load('graphify.extract')]: object", 1),
])
def test_annotated_loader_binding(tmp_path, body, expected):
    source = "from importlib import import_module as load\n" + body
    assert len(check_repository(_repository(tmp_path, source))) == expected


@pytest.mark.parametrize("name", ["sample.py", "sample.pyi"])
@pytest.mark.parametrize("source,expected", [
    ("__import__('graphify.extract')", 1),
    ("__import__('graphify.extract', level=0)", 1),
    ("__import__('graphify.extract', level=1)", 0),
    ("__import__('graphify.extract', **{'globals': {'__package__': 'vendor'}, 'level': 1})", 0),
    ("__import__('graphify.extract', *({'__package__': 'vendor'}, None, (), 1))", 0),
    ("__import__('graphify.extract', **options)", 0),
    ("__import__('graphify.extract', *arguments)", 0),
    ("__import__('graphify.extract', level=0, **options)", 1),
    ("__import__('graphify.extract', *arguments, level=0)", 1),
    ("__import__('graphify.extract', None, None, (), 0, **options)", 1),
    ("__import__(*arguments, name='graphify.extract', level=0)", 1),
    ("__import__('graphify', fromlist=['extract'], **options)", 0),
    ("__import__('graphify', *arguments, fromlist=['extract'], level=0)", 1),
    ("__import__('graphify', *arguments, ['extract'], 0)", 0),
    ("__import__('graphify', *(), None, ['extract'], level=0)", 0),
    ("__import__('graphify', *arguments, fromlist=members, level=0)", 0),
    ("__import__('graphify', None, None, ['extract'], 0, *arguments)", 1),
    ("__import__('graphify', None, None, ['extract'], *arguments)", 0),
    ("import importlib\nimportlib.import_module('graphify.extract', **options)", 1),
    ("import importlib\nimportlib.import_module(*arguments, name='graphify.extract')", 1),
    ("import importlib\nimportlib.import_module('..extract', **options)", 0),
    ("import importlib\nimportlib.import_module('..extract', *arguments)", 0),
    ("import importlib\nimportlib.import_module('..extract', *arguments, package='graphify.extractors')", 1),
    ("import importlib\nimportlib.import_module('..extract', 'graphify.extractors', **options)", 1),
    ("import importlib\nimportlib.import_module(*arguments, '..extract', 'graphify.extractors')", 0),
])
def test_dynamic_argument_proof_with_unpacking(tmp_path, name, source, expected):
    compile(source, name, "exec")
    violations = check_repository(_repository(tmp_path, source, name))
    assert len(violations) == expected
    if expected:
        assert violations[0].dependency == "dynamic import graphify.extract"


@pytest.mark.parametrize("definition", ["def", "async def"])
@pytest.mark.parametrize("keyword_only", [False, True])
@pytest.mark.parametrize("loader", ["importlib", "load"])
def test_generic_function_defaults_use_enclosing_scope(tmp_path, definition, keyword_only, loader):
    binding = "import importlib" if loader == "importlib" else "from importlib import import_module as load"
    call = f'{loader}.import_module("graphify.extract")' if loader == "importlib" else 'load("graphify.extract")'
    arguments = f"{'*, ' if keyword_only else ''}value={call}"
    source = f"{binding}\n{definition} run[{loader}]({arguments}):\n    pass\n"
    violations = check_repository(_repository(tmp_path, source))
    assert len(violations) == 1
    assert violations[0].dependency == "dynamic import graphify.extract"
    assert violations[0].line == 2


@pytest.mark.parametrize("definition", ["def", "async def"])
@pytest.mark.parametrize("annotation", ["argument", "return"])
def test_generic_annotations_keep_type_parameter_scope(tmp_path, definition, annotation):
    call = 'importlib.import_module("graphify.extract")'
    arguments = f"value: {call}" if annotation == "argument" else ""
    returns = f" -> {call}" if annotation == "return" else ""
    source = f"import importlib\n{definition} run[importlib]({arguments}){returns}:\n    pass\n"
    assert check_repository(_repository(tmp_path, source)) == []


@pytest.mark.parametrize("name", ["sample.py", "sample.pyi"])
@pytest.mark.parametrize("binding,expected", [
    ("from importlib import import_module as _Extractor__load", 1),
    ("from importlib import import_module as __load\n"
     "from pathlib import Path as _Extractor__load", 0),
])
def test_class_private_lookup_uses_compiled_name(tmp_path, name, binding, expected):
    source = f"{binding}\nclass Extractor:\n    def extract(self):\n        return __load('graphify.extract')\n"
    assert len(check_repository(_repository(tmp_path, source, name))) == expected


@pytest.mark.parametrize("name", ["sample.py", "sample.pyi"])
@pytest.mark.parametrize("class_name,loader,binding", [
    ("Extractor", "__load", "_Extractor__load"),
    ("_Extractor", "__load", "_Extractor__load"),
    ("___Extractor", "__load", "_Extractor__load"),
    ("___", "__load", "__load"),
    ("Extractor", "__load__", "__load__"),
])
def test_private_name_spelling_rules(tmp_path, name, class_name, loader, binding):
    source = (
        f"from importlib import import_module as {binding}\n"
        f"class {class_name}:\n    def extract(self):\n        return {loader}('graphify.extract')\n"
    )
    assert len(check_repository(_repository(tmp_path, source, name))) == 1


@pytest.mark.parametrize("name", ["sample.py", "sample.pyi"])
@pytest.mark.parametrize("body", [
    "def extract(self, __load):\n    return _Extractor__load('graphify.extract')",
    "def extract(self, *__load):\n    return _Extractor__load('graphify.extract')",
    "def extract(self, **__load):\n    return _Extractor__load('graphify.extract')",
    "def extract(self):\n    from pathlib import Path as __load\n    return _Extractor__load('graphify.extract')",
    "def extract(self):\n    import pathlib as __load\n    return _Extractor__load('graphify.extract')",
    "def extract(self):\n    __load = custom\n    return _Extractor__load('graphify.extract')",
    "def extract(self):\n    del __load\n    return _Extractor__load('graphify.extract')",
    "def extract(self):\n    try:\n        pass\n    except Exception as __load:\n        return _Extractor__load('graphify.extract')",
    "def extract(self):\n    match value:\n        case {'loader': __load}:\n            return _Extractor__load('graphify.extract')",
    "def extract(self):\n    match value:\n        case [*__load]:\n            return _Extractor__load('graphify.extract')",
    "def extract(self):\n    match value:\n        case {**__load}:\n            return _Extractor__load('graphify.extract')",
    "def extract(self):\n    [(__load := custom) for value in values]\n    return _Extractor__load('graphify.extract')",
    "def extract(self):\n    global __load\n    return _Extractor__load('graphify.extract')",
    "def extract(self):\n    return [_Extractor__load('graphify.extract') for __load in values]",
    "def extract(self):\n    return (lambda __load: _Extractor__load('graphify.extract'))(custom)",
    "def __load():\n    pass\n_Extractor__load('graphify.extract')",
    "class __load:\n    pass\n_Extractor__load('graphify.extract')",
    "type __load = object\n_Extractor__load('graphify.extract')",
    "def extract[__load](self):\n    return _Extractor__load('graphify.extract')",
    "def extract(self):\n    __load.import_module = custom\n    return _Extractor__load('graphify.extract')",
])
def test_private_bindings_shadow_the_same_compiled_name(tmp_path, name, body):
    source = (
        "from importlib import import_module as _Extractor__load\nclass Extractor:\n"
        + textwrap.indent(body, "    ") + "\n"
    )
    compile(source, name, "exec")
    assert check_repository(_repository(tmp_path, source, name)) == []


@pytest.mark.parametrize("name", ["sample.py", "sample.pyi"])
@pytest.mark.parametrize("body", [
    "def extract(self):\n    def nested():\n        return __load('graphify.extract')",
    "def extract(self):\n    return (lambda: __load('graphify.extract'))()",
    "def extract(self):\n    return [__load('graphify.extract') for value in values]",
    "def extract(self):\n    from importlib import import_module as __load\n    return __load('graphify.extract')",
    "__load = custom\ndef extract(self):\n    return __load('graphify.extract')",
    "def extract(self, value=__load('graphify.extract')):\n    pass",
    "@__load('graphify.extract')\ndef extract(self):\n    pass",
    "def extract[__T: __load('graphify.extract')](self):\n    pass",
    "def extract[__T = __load('graphify.extract')](self):\n    pass",
    "def extract[__T](self, value: __load('graphify.extract')):\n    pass",
    "def extract[__load](self, value=__load('graphify.extract')):\n    pass",
    "class Nested(__load('graphify.extract')):\n    pass",
    "type Alias = __load('graphify.extract')",
])
def test_private_context_survives_nested_function_scopes(tmp_path, name, body):
    source = (
        "from importlib import import_module as _Extractor__load\nclass Extractor:\n"
        + textwrap.indent(body, "    ") + "\n"
    )
    compile(source, name, "exec")
    assert len(check_repository(_repository(tmp_path, source, name))) == 1


@pytest.mark.parametrize("name", ["sample.py", "sample.pyi"])
@pytest.mark.parametrize("source,expected", [
    ("""
        from importlib import import_module as _Nested__load
        class Extractor:
            class Nested:
                def extract(self): return __load('graphify.extract')
    """, 1),
    ("""
        from importlib import import_module as _Extractor__load
        class Extractor:
            class Nested:
                def extract(self): return __load('graphify.extract')
    """, 0),
    ("""
        from importlib import import_module as __load
        class Extractor:
            class Nested[__T: __load('graphify.extract')](__load('graphify.extract')):
                pass
    """, 2),
    ("""
        from importlib import import_module as _Extractor__load
        class Extractor:
            class Nested[__T: __load('graphify.extract')](__load('graphify.extract')):
                pass
    """, 0),
    ("""
        from importlib import import_module as _Nested__load
        class Extractor:
            class Nested[__load](__load('graphify.extract')):
                def extract(self): return __load('graphify.extract')
    """, 0),
    ("""
        class Extractor:
            def extract(self):
                from importlib import import_module as _Extractor__load
                def nested():
                    nonlocal __load
                    return _Extractor__load('graphify.extract')
    """, 0),
    ("""
        class Extractor:
            import importlib as __loader
            __loader.import_module('graphify.extract')
    """, 1),
    ("""
        class Extractor:
            from importlib import import_module as __load
            __load('graphify.extract')
    """, 1),
    ("""
        from importlib import import_module as _Extractor__load
        class Extractor:
            class Nested:
                pass
            __load('graphify.extract')
    """, 1),
    ("""
        from importlib import import_module as __load
        class Extractor:
            pass
        def extract(): return __load('graphify.extract')
    """, 1),
    ("""
        from importlib import import_module as _Extractor__load
        class Extractor:
            @__load('graphify.extract')
            class Nested[__T]:
                pass
    """, 1),
    ("""
        import importlib as _Extractor__loader
        class Extractor:
            def extract(self): return __loader.import_module('graphify.extract')
    """, 1),
    ("""
        import builtins
        class Extractor:
            def extract(self): return builtins.__import__('graphify.extract')
    """, 1),
    ("""
        from importlib import __import_module as __load
        class Extractor:
            def extract(self): return __load('graphify.extract')
    """, 0),
    ("""
        import importlib
        class Extractor:
            def extract(self): return importlib.__import_module('graphify.extract')
    """, 0),
])
def test_private_class_and_import_boundaries(tmp_path, name, source, expected):
    source = textwrap.dedent(source)
    compile(source, name, "exec")
    assert len(check_repository(_repository(tmp_path, source, name))) == expected


def test_facade_reverse_exports_are_outside_scope(tmp_path):
    _repository(tmp_path, "from .models import LanguageConfig")
    (tmp_path / "graphify/extract.py").write_text(
        "from graphify.extractors.models import LanguageConfig\n", encoding="utf-8"
    )
    assert check_repository(tmp_path) == []


def test_repository_extractor_dependencies():
    """Make the complete local/CI pytest suite enforce the real source contract."""
    assert check_repository(ROOT) == []


def test_direct_module_imports_before_facade_preserve_identity():
    """Import the implementation package first in a fresh process, then the facade."""
    code = """
import importlib
import sys
from graphify.extractors import LANGUAGE_EXTRACTORS
assert "graphify.extract" not in sys.modules
from graphify.extractors.models import LanguageConfig
from graphify.extractors.engine import _extract_generic
from graphify.extractors.resolution import _resolve_cross_file_imports
facade = importlib.import_module("graphify.extract")
assert facade.LanguageConfig is LanguageConfig
assert facade._extract_generic is _extract_generic
assert facade._resolve_cross_file_imports is _resolve_cross_file_imports
for function in LANGUAGE_EXTRACTORS.values():
    implementation = importlib.import_module(function.__module__)
    assert getattr(implementation, function.__name__) is function
    assert getattr(facade, function.__name__) is function
"""
    subprocess.run([sys.executable, "-I", "-B", "-c", code], check=True, cwd=ROOT)


def _run_checker(root: Path):
    return subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(CHECKER), "--root", str(root)],
        text=True, capture_output=True, check=False,
    )


def test_cli_failure_and_success(tmp_path):
    _repository(tmp_path, "import graphify.extract\nraise RuntimeError('never execute')")
    result = _run_checker(tmp_path)
    assert result.returncode == 1
    assert "GRA-IMPORT-001 graphify/extractors/sample.py:1:" in result.stdout
    assert "graphify.extract" in result.stdout
    assert not result.stderr
    (tmp_path / "graphify/extractors/sample.py").write_text("# fixed\n", encoding="utf-8")
    result = _run_checker(tmp_path)
    assert result.returncode == 0
    assert "passed" in result.stdout


def test_make_entry_point_preserves_failure(tmp_path):
    _repository(tmp_path, "import graphify.extract")
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / CHECKER.name).write_bytes(CHECKER.read_bytes())
    result = subprocess.run(
        ["make", "-s", "-f", str(ROOT / "Makefile"), "extractor-import-check",
         f"PYTHON={sys.executable}"],
        cwd=tmp_path, text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert "GRA-IMPORT-001 graphify/extractors/sample.py:1:" in result.stdout


@pytest.mark.parametrize("source", ["def broken(", "\x00", "# coding: unknown-encoding\n"])
def test_cli_parse_error_is_not_a_clean_scan(tmp_path, source):
    result = _run_checker(_repository(tmp_path, source))
    assert result.returncode == 2
    assert "GRA-IMPORT-001" in result.stderr and "sample.py" in result.stderr
    assert "passed" not in result.stdout


def test_cli_missing_scope_is_an_error(tmp_path):
    result = _run_checker(tmp_path)
    assert result.returncode == 2
    assert "graphify/extractors" in result.stderr


def test_cli_empty_scope_is_an_error(tmp_path):
    (tmp_path / "graphify/extractors").mkdir(parents=True)
    assert _run_checker(tmp_path).returncode == 2


def test_cli_does_not_skip_symlinked_directories(tmp_path):
    _repository(tmp_path, "# safe")
    (tmp_path / "graphify/extractors/nested").symlink_to(tmp_path, target_is_directory=True)
    assert _run_checker(tmp_path).returncode == 2


def test_cli_read_error_is_not_a_clean_scan(tmp_path):
    _repository(tmp_path, "# safe")
    (tmp_path / "graphify/extractors/broken.py").symlink_to(tmp_path / "absent.py")
    result = _run_checker(tmp_path)
    assert result.returncode == 2
    assert "broken.py" in result.stderr
    assert "passed" not in result.stdout
