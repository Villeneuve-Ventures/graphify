"""Enforce GRA-IMPORT-001 without importing extractor or facade code.

Static imports are checked in every AST branch. Dynamic checks prove only string
literal targets of unambiguously bound importlib.import_module / __import__ calls.
Bindings are lexical and deliberately flow-insensitive: reassignment, wildcard
imports and global/nonlocal declarations make a callee unknown. Computed targets,
assignment aliases, runtime namespace mutation and monkeypatching are outside this
proof. Class-private spelling is retained across nested lexical scopes. This is
an architecture regression guard, not a Python execution model.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
from enum import Enum, auto
from importlib.util import resolve_name
import os
from pathlib import Path
import sys
import tokenize


RULE = "GRA-IMPORT-001"
FACADE = "graphify.extract"
SCOPE = Path("graphify/extractors")
REPAIR = (
    "MIGRATION.md invariant 4 requires extract.py -> extractors/; a reverse dependency "
    "can create circular imports and break facade/registry object identity. "
    "Move the needed helper to its shared or private extractor owner and import it there; "
    "keep the facade re-export. Run tests/test_extractor_import_guard.py and "
    "tests/test_extractors_registry.py, including direct-module import regressions."
)


@dataclass(frozen=True)
class Violation:
    """A source location and observed dependency; formatting is stable CLI output."""

    path: Path
    line: int
    dependency: str

    def __str__(self) -> str:
        return f"{RULE} {self.path.as_posix()}:{self.line}: {self.dependency}; {REPAIR}"


@dataclass
class _Scope:
    parent: _Scope | None = None
    kind: str = "module"
    bindings: dict[str, set[str | None]] = field(default_factory=dict)
    wildcard: bool = False

    def bind(self, name: str, target: str | None = None) -> None:
        self.bindings.setdefault(name, set()).add(target)

    def resolve(self, name: str, skip_classes: bool = False) -> str | None:
        if skip_classes and self.kind == "class":
            return self.parent.resolve(name, True) if self.parent is not None else None
        if self.wildcard:
            return None
        if name in self.bindings:
            targets = self.bindings[name]
            return next(iter(targets)) if len(targets) == 1 else None
        if self.parent is not None:
            return self.parent.resolve(
                name, skip_classes or self.kind in {"class", "function", "comprehension"}
            )
        return "builtins.__import__" if name == "__import__" else None


@dataclass(frozen=True)
class _PrivateNames:
    """Apply compiler private spelling independently of lexical scope lookup.

    Names use parsed AST spellings before compiler mangling.
    Functions and comprehensions retain their enclosing class's naming context;
    a nested class replaces it. Generic class headers only mangle their type
    parameter names, while the class body mangles all eligible identifiers.
    Identifiers with dunder suffixes or dots, and identifiers in all-underscore
    classes, retain their spelling. String literal dynamic targets stay unchanged.
    """

    class_name: str = ""
    parameters: frozenset[str] | None = None

    def normalize(self, name: str) -> str:
        class_name = self.class_name.lstrip("_")
        if (
            not class_name or not name.startswith("__") or name.endswith("__") or "." in name
            or (self.parameters is not None and name not in self.parameters)
        ):
            return name
        return f"_{class_name}{name}"


class _Bindings(ast.NodeVisitor):
    """Index lexical bindings before resolving calls, so later stores shadow too."""

    def __init__(self) -> None:
        self.scope = _Scope()
        self.private = _PrivateNames()
        self.calls: list[tuple[ast.Call, _Scope, _PrivateNames]] = []

    def _bind(self, name: str, target: str | None = None) -> None:
        self.scope.bind(self.private.normalize(name), target)

    def _body(self, nodes: list[ast.AST], kind: str, args: ast.arguments | None = None) -> None:
        previous = self.scope
        parent = previous
        # Class namespaces are not enclosing lexical scopes for nested bodies.
        while parent.kind == "class" and parent.parent is not None:
            parent = parent.parent
        self.scope = _Scope(parent, kind)
        if args is not None:
            for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
                self._bind(arg.arg)
            for arg in (args.vararg, args.kwarg):
                if arg is not None:
                    self._bind(arg.arg)
        for node in nodes:
            self.visit(node)
        self.scope = previous

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self._bind(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            root = node.value
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name):
                self._bind(root.id)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            target = self.private.normalize(alias.name)
            if not alias.asname:
                target = target.split(".")[0]
            self._bind(name, target)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == "*":
                self.scope.wildcard = True
            else:
                module = self.private.normalize(node.module or "")
                member = self.private.normalize(alias.name)
                target = f"{module}.{member}" if not node.level else None
                self._bind(alias.asname or alias.name, target)

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append((node, self.scope, self.private))
        self.generic_visit(node)

    def _defaults(self, args: ast.arguments) -> None:
        for value in (*args.defaults, *args.kw_defaults):
            if value is not None:
                self.visit(value)

    def _annotations(self, args: ast.arguments) -> None:
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg):
            if arg is not None and arg.annotation is not None:
                self.visit(arg.annotation)

    def _type_parameters(self, parameters: list[ast.type_param]) -> _Scope:
        previous = self.scope
        if parameters:
            self.scope = _Scope(previous, "type-parameters")
            for parameter in parameters:
                self._bind(parameter.name)
            for parameter in parameters:
                self.generic_visit(parameter)
        return previous

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._bind(node.name)
        for value in node.decorator_list:
            self.visit(value)
        # Defaults use the enclosing scope even when type parameters shadow it.
        self._defaults(node.args)
        previous = self._type_parameters(node.type_params)
        self._annotations(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        self._body(list(node.body), "function", node.args)
        self.scope = previous

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._defaults(node.args)
        self._body([node.body], "function", node.args)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._bind(node.name)
        for value in node.decorator_list:
            self.visit(value)
        previous_private = self.private
        if node.type_params:
            self.private = _PrivateNames(node.name, frozenset(p.name for p in node.type_params))
        previous = self._type_parameters(node.type_params)
        for value in (*node.bases, *node.keywords):
            self.visit(value)
        self.private = _PrivateNames(node.name)
        self._body(list(node.body), "class")
        self.scope = previous
        self.private = previous_private

    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
        self.visit(node.name)
        previous = self._type_parameters(node.type_params)
        self.visit(node.value)
        self.scope = previous

    def visit_Global(self, node: ast.Global | ast.Nonlocal) -> None:
        for name in node.names:
            self._bind(name)

    visit_Nonlocal = visit_Global

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name is not None:
            self._bind(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name is not None:
            self._bind(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name is not None:
            self._bind(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest is not None:
            self._bind(node.rest)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        scope = self.scope
        while scope.kind == "comprehension" and scope.parent is not None:
            scope = scope.parent
        if isinstance(node.target, ast.Name):
            scope.bind(self.private.normalize(node.target.id))

    def visit_ListComp(self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp) -> None:
        # The first iterable is evaluated outside the implicit comprehension scope.
        first, *remaining = node.generators
        self.visit(first.iter)
        tail: list[ast.AST] = [first.target, *first.ifs, *remaining]
        tail.extend([node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt])
        self._body(tail, "comprehension")

    visit_SetComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp
    visit_DictComp = visit_ListComp


def _forbidden(module: str) -> bool:
    return module == FACADE or module.startswith(FACADE + ".")


def _absolute(module: str, level: int, package: str) -> str:
    return resolve_name("." * level + module, package) if level else module


def _callee(node: ast.expr, scope: _Scope, private: _PrivateNames) -> str | None:
    if isinstance(node, ast.Name):
        return scope.resolve(private.normalize(node.id))
    if isinstance(node, ast.Attribute):
        owner = _callee(node.value, scope, private)
        return f"{owner}.{private.normalize(node.attr)}" if owner else None
    return None


class _ArgumentState(Enum):
    ABSENT = auto()
    UNKNOWN = auto()


def _argument(node: ast.Call, position: int, name: str) -> ast.expr | _ArgumentState:
    """Resolve an explicit argument, proven absence, or unpacking uncertainty.

    Positional slots are fixed only before the first ``*args``. An explicit
    keyword fixes its slot even with unpacking: a duplicate would fail argument
    binding rather than override that value. This proof assumes successful call
    binding; it neither evaluates unpacked values nor validates call signatures.
    """
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    for index, argument in enumerate(node.args):
        if isinstance(argument, ast.Starred):
            return _ArgumentState.UNKNOWN
        if index == position:
            return argument
    if any(keyword.arg is None for keyword in node.keywords):
        return _ArgumentState.UNKNOWN
    return _ArgumentState.ABSENT


def _string(node: ast.expr | _ArgumentState) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _dynamic_target(node: ast.Call, scope: _Scope, private: _PrivateNames) -> str | None:
    callee = _callee(node.func, scope, private)
    if callee not in {"importlib.import_module", "builtins.__import__"}:
        return None
    name = _string(_argument(node, 0, "name"))
    if name is None:
        return None
    if callee == "importlib.import_module":
        if name.startswith("."):
            package = _string(_argument(node, 1, "package"))
            if package is None:
                return None
            try:
                return resolve_name(name, package)
            except (ImportError, ValueError):
                return None
        return name
    level = _argument(node, 4, "level")
    if level is not _ArgumentState.ABSENT and not (
        isinstance(level, ast.Constant) and level.value == 0
    ):
        return None
    fromlist = _argument(node, 3, "fromlist")
    if name == "graphify" and isinstance(fromlist, (ast.List, ast.Tuple)):
        if any(_string(item) == "extract" for item in fromlist.elts):
            return FACADE
    return name


def _check_file(path: Path, root: Path) -> list[Violation]:
    relative = path.relative_to(root)
    package = ".".join(relative.parent.parts)
    with tokenize.open(path) as source:
        tree = ast.parse(source.read(), filename=relative.as_posix())
    violations = []
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = _absolute(node.module or "", node.level, package)
            targets = [module] if _forbidden(module) else [
                f"{module}.{alias.name}" for alias in node.names
            ]
        for target in targets:
            if _forbidden(target):
                violations.append(Violation(relative, node.lineno, f"import {target}"))
    bindings = _Bindings()
    bindings.visit(tree)
    for call, scope, private in bindings.calls:
        target = _dynamic_target(call, scope, private)
        if target is not None and _forbidden(target):
            violations.append(Violation(relative, call.lineno, f"dynamic import {target}"))
    return sorted(violations, key=lambda item: (item.line, item.dependency))


def check_repository(root: Path) -> list[Violation]:
    """Read all extractor .py/.pyi files and return deterministic violations.

    ``root`` is the repository directory, never an import/search path. No scanned
    code is executed. Missing/empty scope, traversal/read/decoding/parse errors,
    and invalid relative imports raise an exception rather than report a clean
    scan. The CLI maps those failures to status 2. Symlinked directories are
    refused, so a subtree cannot silently disappear from the scan.
    """
    root = root.absolute()
    directory = root / SCOPE
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError(f"missing or symlinked extractor scope: {directory}")
    paths = []

    def fail(error: OSError) -> None:
        raise error

    for parent, directories, files in os.walk(directory, onerror=fail):
        for name in directories:
            if (Path(parent) / name).is_symlink():
                raise ValueError(f"symlinked extractor directory: {Path(parent) / name}")
        paths.extend(Path(parent) / name for name in files if Path(name).suffix in {".py", ".pyi"})
    if not paths:
        raise ValueError(f"no Python sources in extractor scope: {directory}")
    violations = []
    for path in sorted(paths):
        try:
            violations.extend(_check_file(path, root))
        except (OSError, UnicodeError, SyntaxError, ValueError, ImportError, LookupError) as error:
            raise ValueError(f"cannot check {path.relative_to(root).as_posix()}: {error}") from error
    return violations


def main(argv: list[str] | None = None) -> int:
    """Print diagnostics and return 0 clean, 1 violations, or 2 checker failure.

    ``--root`` selects a repository for fixtures or local use; by default the
    checker scans its own checkout, regardless of the working directory.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        violations = check_repository(args.root)
    except (OSError, ValueError) as error:
        print(f"{RULE} checker error: {error}", file=sys.stderr)
        return 2
    for violation in violations:
        print(violation)
    if not violations:
        print(f"{RULE} passed: {SCOPE.as_posix()} has no proven facade dependencies")
    return int(bool(violations))


if __name__ == "__main__":
    raise SystemExit(main())
