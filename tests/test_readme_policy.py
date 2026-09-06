"""Enforce README naming rules; English text remains a contribution requirement."""

from pathlib import Path
import locale
import re
import subprocess

import pytest


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
# RFC 5646 section 2.1 also permits private-use-only and fixed grandfathered tags.
# This extends README suffixes only; reserved parent-directory codes stay separate.
README_LOCALE = (
    r"(?:[a-z]{2,3}" + LOCALE_SUBTAGS
    + r"|x(?:[-_][a-z0-9]{1,8})+"
    + r"|i[-_](?:ami|bnn|default|enochian|hak|klingon|lux|mingo|navajo|pwn|tao|tay|tsu))"
)
TRANSLATED_README = re.compile(
    r"(?:\breadme\.(?:" + "|".join(DOCUMENTATION_EXTENSIONS)
    + r")[._-]" + README_LOCALE
    + r"|\breadme[._-](?!(?:" + "|".join(DOCUMENTATION_EXTENSIONS)
    + r")(?![\w.-]))" + README_LOCALE
    + r"|(?<![\w.-])(?:translations(?:/[^\s/<>\[\]()\"']+)*|(?:"
    + "|".join(LANGUAGE_CODES) + r")" + LOCALE_SUBTAGS + r")/readme)(?:\.(?:"
    + "|".join(DOCUMENTATION_EXTENSIONS) + r"))?(?![\w.-])",
    re.IGNORECASE,
)


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
    # Keep gitlinks, symlinks, and absent tracked entries for the naming policy.
    # No file content or symlink target needs to be read.
    # Git index names may contain arbitrary bytes, including on Windows hosts.
    return [Path(name.decode("utf-8", "surrogateescape"))
            for name in result.stdout.split(b"\0") if name]


def test_no_readme_translations_in_repository() -> None:
    violations = [
        path.as_posix() for path in _repository_files()
        if _owned_documentation(path) and _translation_path(path)
    ]
    assert not violations, (
        "README documentation is English-only. Remove restored translation files: "
        + ", ".join(sorted(violations))
    )


@pytest.mark.parametrize("relative, rejected", [
    ("README.md", False),
    ("docs/README.md", False),
    ("docs/en/README.md", False),
    ("docs/en-US/README.md", False),
    ("docs/en-US-u-ca-gregory/README.md", False),
    ("docs/src/README.md", False),
    ("docs/api/README.md", False),
    ("docs/operating-systems/README.md", False),
    ("README.fr.md", True),
    ("README.md.fr", True),
    ("README.md.zh-CN", True),
    ("README-fil-PH.md", True),
    ("README_zh_Hant.md", True),
    ("README.de-DE-u-co-phonebk.md", True),
    ("README.zh-CN-x-private.md", True),
    ("README.de-x-a.md", True),
    ("README.adoc.de-DE-u-co-phonebk", True),
    ("docs/fr/README.md", True),
    ("docs/os/README.md", True),
    ("docs/as/README.md", True),
    ("docs/de-DE-u-co-phonebk/README.md", True),
    ("docs/zh-CN-x-private/README.md", True),
    ("graphify/README.fr.md", True),
    ("tools/translations/README.md", True),
    ("docs/translations/catalog.json", True),
    ("translations/catalog.json", True),
    ("DOCS/TRANSLATIONS/README.FR-FR.MD", True),
    ("graphify/translations/catalog.json", False),
    ("tools/translations/parser.py", False),
    ("worked/example/README.md", False),
    ("worked/example/README.fr.md", True),
    ("worked/example/raw/README.fr.md", False),
    ("worked/example/raw/translations/fr.json", False),
    ("tests/fixtures/translations/README.fr.md", False),
    ("README.x-private.md", True),
    ("README.x-a.md", True),
    ("README.md.x-private", True),
    ("README.i-klingon.md", True),
    ("README.md.i-klingon", True),
    ("README.en-GB-oed.md", True),
    ("README.x-.md", False),
    ("README.x-abcdefghi.md", False),
    ("README.i-other.md", False),
    ("README.i-klingon-extra.md", False),
    ("README.I-AMI.md", True),
    ("README.i-enochian.md", True),
    ("README.x-abcdefgh-a.md", True),
    ("README.x--a.md", False),
    ("docs/x-private/README.md", False),
    ("docs/i-klingon/README.md", False),
])
def test_readme_path_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str, rejected: bool,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    monkeypatch.setattr(f"{__name__}.ROOT", tmp_path)
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Example content", encoding="utf-8")
    if rejected:
        with pytest.raises(AssertionError, match="translation"):
            test_no_readme_translations_in_repository()
    else:
        test_no_readme_translations_in_repository()


@pytest.mark.parametrize("extension", ["", *DOCUMENTATION_EXTENSIONS])
def test_readme_path_policy_supports_documentation_formats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extension: str,
) -> None:
    suffix = f".{extension}" if extension else ""
    translated = f"README.fr{suffix}"
    test_readme_path_policy(tmp_path, monkeypatch, translated, True)
    (tmp_path / translated).unlink()
    test_readme_path_policy(tmp_path, monkeypatch, f"README{suffix}", False)


def test_path_checks_ignore_document_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    monkeypatch.setattr(f"{__name__}.ROOT", tmp_path)
    (tmp_path / "guide.md").write_bytes(b"[Historical translation](README.fr.md)\n\xff")
    test_no_readme_translations_in_repository()


@pytest.mark.parametrize("target_kind", ["missing", "directory", "file"])
def test_readme_path_policy_checks_symlink_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_kind: str,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    monkeypatch.setattr(f"{__name__}.ROOT", tmp_path)
    target = tmp_path / "target"
    if target_kind == "directory":
        target.mkdir()
    elif target_kind == "file":
        target.write_text("English guide", encoding="utf-8")
    try:
        (tmp_path / "README.md").symlink_to(target, target_is_directory=target_kind == "directory")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    test_no_readme_translations_in_repository()
    (tmp_path / "README.fr.md").symlink_to(target, target_is_directory=target_kind == "directory")
    with pytest.raises(AssertionError, match="README.fr.md"):
        test_no_readme_translations_in_repository()


def test_readme_path_policy_checks_absent_tracked_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    monkeypatch.setattr(f"{__name__}.ROOT", tmp_path)
    path = tmp_path / "README.fr.md"
    path.write_text("Example", encoding="utf-8")
    subprocess.run(["git", "add", "README.fr.md"], cwd=tmp_path, check=True, capture_output=True)
    path.unlink()
    with pytest.raises(AssertionError, match="README.fr.md"):
        test_no_readme_translations_in_repository()


@pytest.mark.parametrize("relative, rejected", [
    (b"worked/example/raw/caf\xe9.txt", False),
    (b"docs/caf\xe9/README.fr.md", True),
])
def test_readme_path_policy_preserves_non_utf8_git_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: bytes, rejected: bool,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    monkeypatch.setattr(f"{__name__}.ROOT", tmp_path)
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=tmp_path,
        input=b"Example", check=True, capture_output=True,
    ).stdout.strip()
    # Index bytes exercise Git's path protocol even on UTF-8-only filesystems.
    subprocess.run(
        ["git", "update-index", "--index-info"], cwd=tmp_path,
        input=b"100644 " + blob + b"\t" + relative + b"\n", check=True, capture_output=True,
    )
    if rejected:
        with pytest.raises(AssertionError, match="translation"):
            test_no_readme_translations_in_repository()
    else:
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
