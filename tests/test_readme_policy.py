"""Keep Graphify's own README documentation English-only in the existing CI gate."""

from pathlib import Path
from html.parser import HTMLParser
import locale
import re
import subprocess
from urllib.parse import unquote

import pytest
from markdown_it import MarkdownIt  # Already locked through Bandit/Rich in the dev environment.


ROOT = Path(__file__).resolve().parents[1]
DOCUMENTATION_EXTENSIONS = ("md", "mdx", "qmd", "markdown", "rst", "txt", "adoc", "asciidoc", "html", "htm")
# Non-English locale parent names are reserved by policy, regardless of content.
LANGUAGE_CODES = sorted({
    key.split("_", 1)[0] for key in locale.locale_alias
    if re.fullmatch(r"[a-z]{2,3}(?:_[a-z0-9]+)*", key)
    and key.split("_", 1)[0] not in {"en", "eng"}
})
# RFC 5646 sections 2.2.6-2.2.7: extension singleton plus payload,
# followed optionally by private-use subtags (which may be one character).
LOCALE_SUBTAGS = (
    r"(?:[-_][a-z0-9]{2,8})*"
    r"(?:[-_][0-9a-wy-z](?:[-_][a-z0-9]{2,8})+)*"
    r"(?:[-_]x(?:[-_][a-z0-9]{1,8})+)?"
)
TRANSLATED_README = re.compile(
    r"(?:\breadme\.(?:" + "|".join(DOCUMENTATION_EXTENSIONS)
    + r")[._-][a-z]{2,3}" + LOCALE_SUBTAGS
    + r"|\breadme[._-](?!(?:" + "|".join(DOCUMENTATION_EXTENSIONS)
    + r")(?![\w.-]))[a-z]{2,3}" + LOCALE_SUBTAGS
    + r"|(?<![\w.-])(?:translations(?:/[^\s/<>\[\]()\"']+)*|(?:"
    + "|".join(LANGUAGE_CODES) + r")" + LOCALE_SUBTAGS + r")/readme)(?:\.(?:"
    + "|".join(DOCUMENTATION_EXTENSIONS) + r"))?(?![\w.-])",
    re.IGNORECASE,
)
HISTORICAL_DIRECTORY_REFERENCE = re.compile(
    r"(?:\bdocs/translations|(?<![\w/.-])"
    r"(?:(?:https?:)?//[^/\s<>()\"']+/|/|(?:\.\.?/)+)translations)"
    r"(?=$|[/?#\s)>\]\"'])|"
    r"(?:^|(?<=[(\"'<]))translations(?=$|[/?#)>\]\"']|\s+[\"'])",
    re.IGNORECASE,
)
BARE_URL = re.compile(r"(?:https?://|//)[^\s<>\"']+", re.IGNORECASE)


def _translation_reference(text: str) -> bool:
    normalized = unquote(text).replace("\\", "/").strip()
    return bool(TRANSLATED_README.search(normalized)
                or HISTORICAL_DIRECTORY_REFERENCE.search(normalized))


class _HTMLLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[int, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # HTMLParser decodes character references in attributes exactly once.
        for name, value in attrs:
            if name in {"href", "xlink:href"} and value is not None:
                self.links.append((self.getpos()[0], value))


def _documentation_links(text: str, suffix: str) -> list[tuple[int, str]]:
    links: list[tuple[int, str]] = []

    def add_html(fragment: str, line: int) -> None:
        parser = _HTMLLinks()
        parser.feed(fragment)
        parser.close()
        links.extend((line + offset - 1, target) for offset, target in parser.links)

    if suffix in {".html", ".htm"}:
        add_html(text, 1)
        return links

    env = {}
    for block in MarkdownIt("commonmark").parse(text, env):
        line = (block.map or [0])[0] + 1
        if block.type == "html_block":
            add_html(block.content, line)
        for token in block.children or []:
            if token.type == "link_open":
                target = token.attrGet("href")
                if isinstance(target, str):
                    links.append((line, target))
            elif token.type == "html_inline":
                add_html(token.content, line)
            elif token.type == "text":
                links.extend((line, match.group()) for match in BARE_URL.finditer(token.content))
            line += token.content.count("\n") + (token.type in {"softbreak", "hardbreak"})
    for reference in env.get("references", {}).values():
        links.append((reference["map"][0] + 1, reference["href"]))

    # Explicit link syntax used by the other maintained text formats.
    patterns: tuple[str, ...] = ()
    if suffix == ".rst":
        patterns = (
            r"`[^`]*<([^<>\n]+)>`_+",
            r"(?m)^\s*\.\.\s+_[^:\n]+:\s*([^\s]+)",
            r"(?m)^\s*__\s+([^\s]+)",
        )
    elif suffix in {".adoc", ".asciidoc"}:
        patterns = (r"(?:link:|xref:)([^\s\[]+)\[", r"<<([^,\s>]+)(?:,[^>]*)?>>")
    for pattern in patterns:
        links.extend((text.count("\n", 0, match.start()) + 1, match.group(1))
                     for match in re.finditer(pattern, text))
    return links


def _translation_path(path: Path) -> bool:
    normalized = Path(path.as_posix().lower())
    return (
        normalized.is_relative_to("translations")
        or normalized.is_relative_to("docs/translations")
        or TRANSLATED_README.search(normalized.as_posix()) is not None
    )


def _owned_documentation(path: Path) -> bool:
    # Only corpora and fixtures are exempt; maintained nested paths are owned.
    is_corpus = len(path.parts) >= 3 and path.parts[0] == "worked" and path.parts[2] == "raw"
    return not (is_corpus or path.is_relative_to("tests/fixtures"))


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
    # Inspect link destinations, preserving ordinary prose and code examples.
    violations = []
    for path in _repository_files():
        if (
            _owned_documentation(path)
            and (path.suffix.lower().lstrip(".") in DOCUMENTATION_EXTENSIONS
                 or path.name.lower() == "readme")
        ):
            if not (ROOT / path).is_file():
                violations.append(f"{path.as_posix()}: non-regular documentation target")
                continue
            text = (ROOT / path).read_text(encoding="utf-8")
            for number, target in _documentation_links(text, path.suffix.lower()):
                if _translation_reference(target):
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
    ("worked/vendor/raw/README.fr.md", "Third-party documentation", False),
    ("tests/fixtures/translations/README.fr.md", "Multilingual fixture", False),
    ("graphify/README.fr.md", "French documentation", True),
    ("tools/translations/README.md", "French documentation", True),
    ("ARCHITECTURE.md", "[French](README.fr.md)", True),
    ("BENCHMARKS.md", "[French](README.fr.md)", True),
    ("SECURITY.md", "[French](README.fr.md)", True),
    ("graphify/guide.md", "[French](README.fr.md)", True),
    ("CHANGELOG.md", "- Docs: Korean README added (README.ko-KR.md) (#112)", False),
    ("AGENTS.md", "locale-suffixed README files (for example, `README.fr-FR.md`).", False),
    ("worked/vendor/raw/README.md", "[French](README.fr.md)", False),
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
    ("docs/os/README.md", "English operating-system guide", True),
    ("docs/as/README.md", "English guide", True),
    ("docs/operating-systems/README.md", "English operating-system guide", False),
    ("README.md", "[Operating systems](docs/operating-systems/README.md)", False),
    ("docs/en/README.md", "English documentation", False),
    ("docs/en-US/README.md", "English documentation", False),
    ("README.md", "[English](docs/en-US/README.md)", False),
    ("docs/translations/catalog.json", "{}", True),
    ("translations/catalog.json", "{}", True),
    ("worked/vendor/raw/docs/fr/README.md", "French corpus", False),
    ("worked/foo/README.fr.md", "French documentation", True),
    ("worked/foo/README.md", "[French](README.fr.md)", True),
    ("worked/foo/docs/guide.md", "[French](README.fr.md)", True),
    ("worked/foo/README.md", "English benchmark guide", False),
    ("README.fr.adoc", "French documentation", True),
    ("docs/fr/README.adoc", "French documentation", True),
    ("README.fr", "French documentation", True),
    ("docs/fr/README", "French documentation", True),
    ("README", "[French](README.fr)", True),
    ("README", "English guide", False),
    ("README.adoc", "English guide", False),
    ("CHANGELOG.md", "[French](README.fr.md)", True),
    ("CHANGELOG.md", '<a href="docs/translations">Translations</a>', True),
    ("CHANGELOG.md", "[French]: README.fr.md", True),
    ("CHANGELOG.md", "- Docs: Korean README added ([README.ko-KR.md](README.ko-KR.md)) (#112)", True),
    ("README.md", "[Translations](/translations)", True),
    ("docs/guide.md", "[Translations](../translations)", True),
    ("docs/nested/guide.md", "[Translations](../../translations)", True),
    ("README.md", '<a href="/translations?view=all">Translations</a>', True),
    ("docs/guide.md", "[Translations](..%2Ftranslations#languages)", True),
    ("docs/guide.md", "[Assets](../tools/translations)", False),
    ("README.de-DE-u-co-phonebk.md", "German documentation", True),
    ("README.zh-CN-x-private.md", "Chinese documentation", True),
    ("docs/de-DE-u-co-phonebk/README.md", "German documentation", True),
    ("docs/zh-CN-x-private/README.md", "Chinese documentation", True),
    ("README.md", "[German](README.de-DE-u-co-phonebk.md)", True),
    ("README.md", "[Chinese](docs/zh-CN-x-private/README.md)", True),
    ("README.de-x-a.md", "German documentation", True),
    ("docs/en-US-u-ca-gregory/README.md", "English documentation", False),
    ("AGENTS.md", "[French](README.fr.md)", True),
    ("AGENTS.md", '<a href="docs/translations">Translations</a>', True),
    ("AGENTS.md", "[French]: README.fr.md", True),
    ("AGENTS.md", "locale-suffixed README files (for example, [French](README.fr-FR.md)).", True),
    ("README.md", "[Translations](https://example.com/translations)", True),
    ("docs/guide.md", '<a href="https://example.com/translations#languages">Translations</a>', True),
    ("README.md", "[Translations](http://example.com:8080/translations?view=all)", True),
    ("README.md", "[Translations](https://example.com%2Ftranslations)", True),
    ("README.md", "[Translations](//example.com/translations)", True),
    ("README.md", "[Assets](https://example.com/tools/translations)", False),
    ("README.md", "[Guide](https://example.com/translations-guide)", False),
    ("README.md", "Translations remain supported", False),
    ("README.md", '"Translations remain supported"', False),
    ("README.md", "(Translations remain supported)", False),
    ("README.md", "[Translations](translations)", True),
    ("README.md", '[Translations](translations "Languages")', True),
    ("README.md", '<a href="README&#46;fr&#46;md">French</a>', True),
    ("README.md", '<a href="docs&#x2f;translations">Translations</a>', True),
    ("README.md", '[French](README&#37;2Efr&#37;2Emd)', True),
    ("CHANGELOG.md", "Removed the old README.de.md translation", False),
    ("AGENTS.md", "Do not restore README.fr.md", False),
    ("README.md", "`[French](README.fr.md)` is a forbidden link example.", False),
    ("README.md", "```markdown\n[French](README.fr.md)\n```", False),
    ("README.md", '&lt;a href="README.fr.md"&gt;French&lt;/a&gt;', False),
    ("docs/index.html", '<a href="README.fr.md">French</a>', True),
    ("docs/index.htm", '<a href="README&#46;fr&#46;md">French</a>', True),
    ("docs/index.html", '<a\n href="README.fr.md">French</a>', True),
    ("docs/index.html", '<p>Removed README.fr.md</p>', False),
    ("README.md.fr", "French documentation", True),
    ("README.md.zh-CN", "Chinese documentation", True),
    ("README.md", "[French](README.md.fr)", True),
    ("README.md", "[Chinese](README.md.zh-CN)", True),
    ("README.adoc.de-DE-u-co-phonebk", "German documentation", True),
    ("README.md", "[French](\nREADME.fr.md)", True),
    ("README.md", "[French][fr]\n\n[fr]:\n  README.fr.md", True),
    ("docs/guide.rst", "`French <README.fr.rst>`_", True),
    ("docs/guide.rst", "French_\n\n.. _French: README.fr.rst", True),
    ("docs/guide.rst", "French_\n\n.. _French:\n  README.fr.rst", True),
    ("docs/guide.rst", "French__\n\n__ README.fr.rst", True),
    ("docs/guide.adoc", "link:README.fr.adoc[French]", True),
    ("docs/guide.adoc", "<<README.fr.adoc,French>>", True),
    ("README.md", '<a href="translations ">Translations</a>', True),
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


@pytest.mark.parametrize("extension", ["mdx", "markdown", "rst", "qmd", "txt", "adoc", "asciidoc"])
def test_policy_checks_other_documentation_formats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extension: str,
) -> None:
    test_policy_checks_disposable_repository(
        tmp_path, monkeypatch, f"README.fr.{extension}", "French documentation", True,
    )
    (tmp_path / f"README.fr.{extension}").unlink()
    test_policy_checks_disposable_repository(
        tmp_path, monkeypatch, f"guide.{extension}", f"[French](README.fr.{extension})", True,
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
        target.write_text("[French](README.fr.md)" if target_kind == "translated_file" else "English guide")
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
