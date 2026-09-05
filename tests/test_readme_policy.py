"""Keep Graphify's own README documentation English-only in the existing CI gate."""

from pathlib import Path
import re
import subprocess
from urllib.parse import unquote

import pytest


ROOT = Path(__file__).resolve().parents[1]
DOCUMENTATION_EXTENSIONS = ("md", "mdx", "qmd", "markdown", "rst", "txt")
# Cover the historical directories and locale-suffixed README naming schemes.
TRANSLATION_REFERENCE = re.compile(
    r"(?:\btranslations/|\breadme[._-][a-z]{2,3}(?:[-_][a-z0-9]{2,8})*\.(?:"
    + "|".join(DOCUMENTATION_EXTENSIONS) + r")\b)",
    re.IGNORECASE,
)


def _translation_reference(text: str) -> bool:
    return TRANSLATION_REFERENCE.search(unquote(text).replace("\\", "/")) is not None


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
    paths = [Path(name.decode("utf-8")) for name in result.stdout.split(b"\0") if name]
    return [path for path in paths if (ROOT / path).is_file() or (ROOT / path).is_symlink()]


def test_no_readme_translations_in_repository() -> None:
    violations = [
        path.as_posix() for path in _repository_files()
        if _owned_documentation(path) and _translation_reference(path.as_posix())
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
            for number, line in enumerate((ROOT / path).read_text(encoding="utf-8").splitlines(), 1):
                if _translation_reference(line):
                    violations.append(f"{path.as_posix()}:{number}")
    assert not violations, (
        "Remove references to README translations from maintained documentation: "
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
])
def test_translation_guard_rejects_restored_paths_and_links(reference: str) -> None:
    assert _translation_reference(reference)


@pytest.mark.parametrize("reference", [
    "README.md",
    "docs/README.md",
    "worked/example/README.md",
    "[Install](README.md#install)",
    "Graphify supports multilingual input corpora.",
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
