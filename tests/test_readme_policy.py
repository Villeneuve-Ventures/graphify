"""Keep Graphify's own README documentation English-only in the existing CI gate."""

from pathlib import Path
import locale
import re
import subprocess
from urllib.parse import unquote

import pytest


ROOT = Path(__file__).resolve().parents[1]
DOCUMENTATION_EXTENSIONS = ("md", "mdx", "qmd", "markdown", "rst", "txt")
# Use non-English language codes so English and ordinary src/api/go paths remain allowed.
LANGUAGE_CODES = sorted({
    key.split("_", 1)[0] for key in locale.locale_alias
    if re.fullmatch(r"[a-z]{2,3}(?:_[a-z0-9]+)*", key)
    and key.split("_", 1)[0] not in {"en", "eng"}
})
TRANSLATED_README = re.compile(
    r"(?:\breadme[._-][a-z]{2,3}(?:[-_][a-z0-9]{2,8})*|"
    r"(?<![\w.-])(?:translations(?:/[^\s/<>\[\]()\"']+)*|(?:"
    + "|".join(LANGUAGE_CODES) + r")(?:[-_][a-z0-9]{2,8})*)/readme)\.(?:"
    + "|".join(DOCUMENTATION_EXTENSIONS) + r")\b",
    re.IGNORECASE,
)
HISTORICAL_DIRECTORY_REFERENCE = re.compile(
    r"(?:\bdocs/translations|(?:^|(?<=[(\"']))(?:\./)?translations)"
    r"(?=$|[/?#\s)>\]\"'])", re.IGNORECASE,
)


def _translation_reference(text: str) -> bool:
    normalized = unquote(text).replace("\\", "/")
    return bool(TRANSLATED_README.search(normalized)
                or HISTORICAL_DIRECTORY_REFERENCE.search(normalized))


def _translation_path(path: Path) -> bool:
    normalized = Path(path.as_posix().lower())
    return (
        normalized.is_relative_to("translations")
        or normalized.is_relative_to("docs/translations")
        or TRANSLATED_README.search(normalized.as_posix()) is not None
    )


def _owned_documentation(path: Path) -> bool:
    # Only corpora and fixtures are exempt; maintained nested paths are owned.
    return not (path.is_relative_to("worked") or path.is_relative_to("tests/fixtures"))


def _repository_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    # Keep gitlinks and absent tracked entries for the path policy. File-type
    # checks belong only to the documentation content scan below.
    return [Path(name.decode("utf-8")) for name in result.stdout.split(b"\0") if name]


def test_no_readme_translations_in_repository() -> None:
    violations = [
        path.as_posix() for path in _repository_files()
        if _owned_documentation(path) and _translation_path(path)
    ]
    assert not violations, (
        "README documentation is English-only. Remove restored translation files: "
        + ", ".join(sorted(violations))
    )


def test_public_documentation_does_not_reference_readme_translations() -> None:
    # Preserve historical CHANGELOG entries and the agent policy's examples
    # of forbidden paths, as well as the corpus/fixture ownership exceptions.
    violations = []
    for path in _repository_files():
        if (
            _owned_documentation(path)
            and path.suffix.lower().lstrip(".") in DOCUMENTATION_EXTENSIONS
            and path not in {Path("AGENTS.md"), Path("CHANGELOG.md")}
        ):
            if not (ROOT / path).is_file():
                violations.append(f"{path.as_posix()}: non-regular documentation target")
                continue
            for number, line in enumerate((ROOT / path).read_text(encoding="utf-8").splitlines(), 1):
                if _translation_reference(line):
                    violations.append(f"{path.as_posix()}:{number}")
    assert not violations, (
        "README translation policy violations in maintained documentation: "
        + ", ".join(violations)
    )


@pytest.mark.parametrize("reference", [
    "docs/translations/README.fr-FR.md",
    "translations/README.md",
    "README.zh.md",
    "README.pt-BR.md",
    "docs/README_zh_Hant.md",
    "README-fil-PH.md",
    "[French](docs/translations/README.fr-FR.md)",
    '[fr]: https://github.com/Graphify-Labs/graphify/blob/v8/docs/translations/README.fr-FR.md',
    '<a href="docs/translations/README.fr-FR.md">French</a>',
    "<DOCS/TRANSLATIONS/README.FR-FR.MD>",
    "docs%2Ftranslations%2FREADME.fr-FR.md",
    "docs/translations",
    "[Translations](docs/translations)",
    '<a href="docs/translations#languages">Translations</a>',
    "[Translations](docs%2Ftranslations?view=all)",
])
def test_translation_guard_rejects_restored_paths_and_links(reference: str) -> None:
    assert _translation_reference(reference)


@pytest.mark.parametrize("reference", [
    "README.md",
    "docs/README.md",
    "worked/example/README.md",
    "[Install](README.md#install)",
    "Graphify supports multilingual input corpora.",
    "Graphify supports translations",
    "docs/translations-guide.md",
])
def test_translation_guard_preserves_english_readmes_and_corpus_support(reference: str) -> None:
    assert not _translation_reference(reference)


@pytest.mark.parametrize("relative, content, rejected", [
    ("docs/translations/README.fr-FR.md", "French documentation", True),
    ("README.fr.md", "French documentation", True),
    ("README.md", "[French](docs/translations/README.fr-FR.md)", True),
    ("docs/guide.md", '<a href="README.fr-FR.md">French</a>', True),
    ("worked/example/raw/translations/fr.json", '{"hello": "bonjour"}', False),
    ("worked/vendor/README.fr.md", "Third-party documentation", False),
    ("tests/fixtures/translations/README.fr.md", "Multilingual fixture", False),
    ("graphify/README.fr.md", "French documentation", True),
    ("tools/translations/README.md", "French documentation", True),
    ("ARCHITECTURE.md", "[French](README.fr.md)", True),
    ("BENCHMARKS.md", "[French](README.fr.md)", True),
    ("SECURITY.md", "[French](README.fr.md)", True),
    ("graphify/guide.md", "[French](README.fr.md)", True),
    ("CHANGELOG.md", "Previously shipped README.fr.md", False),
    ("AGENTS.md", "Do not restore README.fr.md", False),
    ("worked/vendor/README.md", "[French](README.fr.md)", False),
    ("tests/fixtures/guide.md", "[French](README.fr.md)", False),
    ("README.md", "[Translations](docs/translations)", True),
    ("docs/fr/README.md", "French documentation", True),
    ("docs/zh-CN/README.md", "Chinese documentation", True),
    ("README.md", "[French](docs/fr/README.md)", True),
    ("docs/guide.md", "[Chinese](docs/zh-CN/README.md)", True),
    ("graphify/translations/catalog.json", "{}", False),
    ("tools/translations/parser.py", "pass", False),
    ("README.md", "[Parser](tools/translations/parser.py)", False),
    ("docs/src/README.md", "Source guide", False),
    ("docs/api/README.md", "API guide", False),
    ("docs/en/README.md", "English documentation", False),
    ("docs/en-US/README.md", "English documentation", False),
    ("README.md", "[English](docs/en-US/README.md)", False),
    ("docs/translations/catalog.json", "{}", True),
    ("translations/catalog.json", "{}", True),
    ("worked/vendor/docs/fr/README.md", "French corpus", False),
])
def test_policy_checks_disposable_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str, content: str, rejected: bool,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    monkeypatch.setattr(f"{__name__}.ROOT", tmp_path)
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    def check_policy() -> None:
        test_no_readme_translations_in_repository()
        test_public_documentation_does_not_reference_readme_translations()

    if rejected:
        with pytest.raises(AssertionError, match="translation"):
            check_policy()
    else:
        check_policy()


@pytest.mark.parametrize("extension", ["mdx", "markdown", "rst", "qmd", "txt"])
def test_policy_checks_other_documentation_formats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extension: str,
) -> None:
    test_policy_checks_disposable_repository(
        tmp_path, monkeypatch, f"README.fr.{extension}", "French documentation", True,
    )
    (tmp_path / f"README.fr.{extension}").unlink()
    test_policy_checks_disposable_repository(
        tmp_path, monkeypatch, f"guide.{extension}", f"README.fr.{extension}", True,
    )
    assert not _translation_reference(f"README.{extension}")


@pytest.mark.parametrize("target_kind", ["missing", "directory", "file", "translated_file"])
def test_policy_checks_documentation_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_kind: str,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    monkeypatch.setattr(f"{__name__}.ROOT", tmp_path)
    target = tmp_path / "target"
    if target_kind == "directory":
        target.mkdir()
    elif target_kind in {"file", "translated_file"}:
        target.write_text("README.fr.md" if target_kind == "translated_file" else "English guide")
    try:
        (tmp_path / "guide.md").symlink_to(target, target_is_directory=target_kind == "directory")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    if target_kind in {"missing", "directory"}:
        with pytest.raises(AssertionError, match="guide.md: non-regular documentation target"):
            test_public_documentation_does_not_reference_readme_translations()
    elif target_kind == "translated_file":
        with pytest.raises(AssertionError, match="guide.md:1"):
            test_public_documentation_does_not_reference_readme_translations()
    else:
        test_public_documentation_does_not_reference_readme_translations()
    (tmp_path / "README.fr.md").symlink_to(target, target_is_directory=target_kind == "directory")
    with pytest.raises(AssertionError, match="README.fr.md"):
        test_no_readme_translations_in_repository()


@pytest.mark.parametrize("relative", ["translations", "docs/translations"])
def test_policy_rejects_historical_translation_gitlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    monkeypatch.setattr(f"{__name__}.ROOT", tmp_path)
    # A gitlink needs no network or real submodule clone to exercise ls-files.
    subprocess.run(
        ["git", "update-index", "--add", "--cacheinfo", "160000", "1" * 40, relative],
        cwd=tmp_path, check=True, capture_output=True,
    )
    (tmp_path / relative).mkdir(parents=True)
    with pytest.raises(AssertionError, match="translations"):
        test_no_readme_translations_in_repository()
