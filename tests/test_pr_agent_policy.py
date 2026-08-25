import ast
import hashlib
import json
from pathlib import Path
import sys
import types
from types import SimpleNamespace

import pytest
import yaml

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
VALID_DESCRIPTION = ("title: Test\ndescription: Body\ntype:\n- Enhancement\npr_files:\n"
                     "- filename: src/a.py\n  changes_title: Improve behavior\n"
                     "  changes_summary: Improve behavior safely\n  label: enhancement")


def _workflow() -> str:
    return (ROOT / ".github/workflows/pr-agent.yml").read_text()


def _embedded_python() -> str:
    lines = _workflow().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "python - <<'PY'") + 1
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "PY")
    indent = len(lines[start]) - len(lines[start].lstrip())
    return "\n".join(line[indent:] if line.strip() else "" for line in lines[start:end])


def _helpers(converter=lambda _patch, _file: "@@ -1 +1 @@\n__new hunk__\n1 +new\n__old hunk__\n-old",
             verified_renames=()) -> dict:
    tree = ast.parse(_embedded_python())
    wanted = {"_body_digest", "_canonical_body", "_manifest", "_marker", "_patch_counts", "_raw_diff", "_same",
              "_same_files", "_substantive", "_valid_int", "_valid_path", "_visible"}
    body = [node for node in tree.body if (
        isinstance(node, ast.Import) and any(alias.name in {"hashlib", "json", "re"} for alias in node.names)
    ) or (isinstance(node, ast.FunctionDef) and node.name in wanted)]
    namespace: dict = {"get_max_tokens": lambda _model: 262144,
                       "decouple_and_convert_to_hunks_with_lines_numbers": converter,
                       "verified_renames": set(verified_renames)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])),
                 "<pr-agent-policy>", "exec"), namespace)
    return namespace


def _config() -> dict:
    with (ROOT / ".pr_agent.toml").open("rb") as config_file:
        return tomllib.load(config_file)


def _file(name="src/a.py", status="modified", additions=1, deletions=1,
          patch="@@ -1 +1 @@\n-old\n+new", previous_filename=None, sha=None):
    return SimpleNamespace(filename=name, status=status, additions=additions,
                           deletions=deletions, patch=patch,
                           previous_filename=previous_filename, sha=sha)


def test_common_path_and_trusted_runtime_settings_are_preserved() -> None:
    workflow = _workflow()
    config = _config()
    assert "types: [opened, reopened, ready_for_review]" in workflow
    assert "synchronize" not in workflow
    assert "github.event.pull_request.draft == false" in workflow
    assert "timeout-minutes: 45" in workflow
    assert 'python-version: "3.14"' in workflow
    assert "570f67ed5fc8db5be74c18df070bc20079b64b0d" in workflow
    assert 'config.model: "gemini/gemini-3.7-flash"' in workflow
    assert 'config.fallback_models: \'["gemini/gemini-3.5-flash-lite"]\'' in workflow
    assert 'config.max_model_tokens: "262144"' in workflow
    assert config["config"]["model"] == "gemini/gemini-3.7-flash"
    assert config["config"]["fallback_models"] == ["gemini/gemini-3.5-flash-lite"]
    assert config["config"]["reasoning_effort"] == "high"
    assert config["config"]["max_model_tokens"] == 262144
    assert config["config"]["large_patch_policy"] == "clip"
    assert 'config.propagate_tool_errors: "true"' in workflow
    assert 'config.extra_config_url: ""' in workflow
    assert 'config.use_global_settings_file: "false"' in workflow
    assert 'config.use_repo_settings_file: "true"' in workflow
    assert 'github_action_config.handle_push_trigger: "false"' in workflow
    assert config["pr_reviewer"]["num_max_findings"] == 5
    assert config["config"]["restricted_mode"] is True
    assert config["config"]["repo_context_from_default_branch"] is False
    assert config["config"]["repo_context_max_lines"] == 500
    assert hashlib.sha256((ROOT / ".pr_agent.toml").read_bytes()).hexdigest() == (
        "d82aa7f6deb76ada6fa18f141212d5181ddd0f32da18e64e61f1f744d3c9129f"
    )


def test_prreview_is_an_exact_full_review_without_incremental_state() -> None:
    code = _embedded_python()
    config_text = (ROOT / ".pr_agent.toml").read_text()
    assert "github.event.comment.body == '/prreview'" in _workflow()
    assert 'request = "/review"' in code
    assert "regenerates a frozen full review" in config_text
    assert "incremental diff" not in config_text
    for forbidden in ("/review -i", "REVIEW_MARKER_RE", "pr_commits", "commits_range",
                      "unreviewed_files_map", ".incremental", "is_incremental"):
        assert forbidden not in code


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 1.0, "1"])
def test_pr_number_rejects_non_positive_exact_integers(value) -> None:
    assert _helpers()["_valid_int"](value, positive=True) is False


@pytest.mark.parametrize("value", [None, True, False, -1, 1.0, "1"])
def test_counts_reject_non_nonnegative_exact_integers(value) -> None:
    assert _helpers()["_valid_int"](value) is False


def test_manifest_requires_complete_supported_unique_files() -> None:
    manifest = _helpers()["_manifest"]
    valid = [_file(), _file("new.py", "added", 1, 0, "@@ -0,0 +1 @@\n+x"),
             _file("old.py", "removed", 0, 1, "@@ -1 +0,0 @@\n-x"),
             _file("new/name.py", "renamed", 0, 0, None, "old/name.py")]
    assert manifest(SimpleNamespace(changed_files=4), valid) == valid
    for bad in ("copied", "changed", "unchanged", "unknown"):
        with pytest.raises(RuntimeError):
            manifest(SimpleNamespace(changed_files=1), [_file(status=bad)])
    for pull, files in ((SimpleNamespace(changed_files=3001), valid),
                        (SimpleNamespace(changed_files=2), [_file()]),
                        (SimpleNamespace(changed_files=2), [_file(), _file()]),
                        (SimpleNamespace(changed_files=1), [_file("../escape.py")]),
                        (SimpleNamespace(changed_files=1), [_file("new.py", "renamed", 0, 0)])):
        with pytest.raises(RuntimeError):
            manifest(pull, files)


def test_raw_diff_proves_patch_counts_deletions_renames_and_token_bound() -> None:
    raw_diff = _helpers(verified_renames={"new.py"})["_raw_diff"]
    files = [_file("gone.py", "removed", 0, 1, "@@ -1 +0,0 @@\n-secret"),
             _file("new.py", "renamed", 0, 0, None, "old.py")]
    token_handler = SimpleNamespace(prompt_tokens=10, count_tokens=len)
    result = raw_diff(files, token_handler, "model", max_tokens=lambda _: 10000)
    assert "secret" in result
    assert "rename from old.py\nrename to new.py" in result
    with pytest.raises(RuntimeError):
        raw_diff(files, token_handler, "model", max_tokens=lambda _: 20)
    with pytest.raises(RuntimeError):
        raw_diff([_file(patch=None)], token_handler, "model", max_tokens=lambda _: 10000)
    with pytest.raises(RuntimeError):
        raw_diff([_file(additions=2)], token_handler, "model", max_tokens=lambda _: 10000)
    changing_rename = _file("new.py", "renamed", 1, 1, "@@ -1 +1 @@\n-old\n+new", "old.py")
    assert "rename from old.py" in raw_diff(
        [changing_rename], token_handler, "model", max_tokens=lambda _: 10000
    )
    with pytest.raises(RuntimeError):
        raw_diff([_file("new.py", "renamed", 1, 1, None, "old.py")], token_handler,
                 "model", max_tokens=lambda _: 10000)
    with pytest.raises(RuntimeError, match="Eligible patch unavailable"):
        _helpers()["_raw_diff"](
            [_file("new.py", "renamed", 0, 0, None, "old.py", sha="a" * 40)],
            token_handler, "model", max_tokens=lambda _: 10000)
    signs = _file(patch="@@ -1,2 +1,2 @@\n context\n---value\n+++value\n\\ No newline at end of file")
    assert "+++value" in raw_diff([signs], token_handler, "model", max_tokens=lambda _: 10000)
    sign_converter = _helpers(lambda _patch, _file: "__new hunk__\n1 +++value\n__old hunk__\n---value")["_raw_diff"]
    assert "+++value" in sign_converter([signs], token_handler, "model", numbered=True,
                                         max_tokens=lambda _: 10000)
    with pytest.raises(RuntimeError, match="Malformed unified patch"):
        raw_diff([_file(patch="-old\n+new")], token_handler, "model",
                 max_tokens=lambda _: 10000)


@pytest.mark.parametrize("total_tokens", [65537, 262143])
def test_raw_diff_accepts_complete_comparisons_above_old_and_below_new_boundary(
        total_tokens) -> None:
    raw_diff = _helpers()["_raw_diff"]
    token_handler = SimpleNamespace(prompt_tokens=17, count_tokens=len)
    empty = _file(status="added", additions=1, deletions=0, patch="@@ -0,0 +1 @@\n+")
    fixed_tokens = token_handler.prompt_tokens + len(raw_diff(
        [empty], token_handler, "model", max_tokens=lambda _: 300000)) + 1500
    payload = "x" * (total_tokens - fixed_tokens)
    file = _file(status="added", additions=1, deletions=0,
                 patch=f"@@ -0,0 +1 @@\n+{payload}")
    result = raw_diff([file], token_handler, "model", max_tokens=lambda _: 262144)
    assert token_handler.prompt_tokens + len(result) + 1500 == total_tokens
    assert result.endswith(payload)


@pytest.mark.parametrize("total_tokens", [262144, 262145])
def test_raw_diff_rejects_complete_comparisons_at_or_above_new_boundary(
        total_tokens) -> None:
    raw_diff = _helpers()["_raw_diff"]
    token_handler = SimpleNamespace(prompt_tokens=17, count_tokens=len)
    empty = _file(status="added", additions=1, deletions=0, patch="@@ -0,0 +1 @@\n+")
    fixed_tokens = token_handler.prompt_tokens + len(raw_diff(
        [empty], token_handler, "model", max_tokens=lambda _: 300000)) + 1500
    payload = "x" * (total_tokens - fixed_tokens)
    file = _file(status="added", additions=1, deletions=0,
                 patch=f"@@ -0,0 +1 @@\n+{payload}")
    with pytest.raises(
            RuntimeError, match="Complete raw comparison exceeds the one-call token budget"):
        raw_diff([file], token_handler, "model", max_tokens=lambda _: 262144)


@pytest.mark.parametrize("status", ["added", "removed"])
def test_raw_diff_preserves_patchless_empty_source_file_metadata(status) -> None:
    empty_blob = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    result = _helpers()["_raw_diff"](
        [_file("empty.py", status, 0, 0, None, sha=empty_blob)],
        SimpleNamespace(prompt_tokens=1, count_tokens=len), "model",
        max_tokens=lambda _: 10000)
    assert result == f"## File: 'empty.py'\nstatus: {status}"
    with pytest.raises(RuntimeError, match="Eligible patch unavailable"):
        _helpers()["_raw_diff"](
            [_file("binary.py", status, 0, 0, None, sha="a" * 40)],
            SimpleNamespace(prompt_tokens=1, count_tokens=len), "model",
            max_tokens=lambda _: 10000)


@pytest.mark.parametrize("file", [
    _file(additions=2, deletions=2,
          patch="@@ -1 +1 @@\n-old\n+new\n@@ malformed @@\n-old2\n+new2"),
    _file(patch="@@ -1,2 +1,2 @@\n-old\n+new"),
    _file(additions=0, deletions=0, patch="@@ -1,0 +1,0 @@\n"),
    _file(patch="@@ -1 +1 @@\n-old\n+new\n\\ unexpected control"),
    _file(additions=2, deletions=2,
          patch="@@ -3 +3 @@\n-old\n+new\n@@ -1 +1 @@\n-old2\n+new2"),
    _file(additions=2, deletions=2,
          patch="@@ -1,2 +1,2 @@\n context\n-old\n+new\n@@ -2 +2 @@\n-old2\n+new2"),
    _file(additions=2, deletions=2,
          patch="@@ -1 +1 @@\n-old\n+new\n@@ -1 +1 @@\n-old2\n+new2"),
    _file(patch="@@ -0 +1 @@\n-old\n+new"),
    _file(patch="@@ -1 +1 @@\n\\ No newline at end of file\n-old\n+new"),
    _file(patch="@@ -1 +1 @@\n-old\n\\ No newline at end of file\n\\ No newline at end of file\n+new"),
    _file(additions=2, deletions=1,
          patch="@@ -0,0 +1 @@\n+first\n@@ -3 +3 @@\n-old\n+new"),
    _file(patch="@@ -1,2 +1,2 @@\n context\n\\ No newline at end of file\n-old\n+new"),
    _file(additions=1, deletions=0,
          patch="@@ -1,2 +1,3 @@\n context\n+new\n\\ No newline at end of file\n context2"),
    _file(additions=2, deletions=0,
          patch="@@ -1 +1,3 @@\n context\n+new\n\\ No newline at end of file\n+later"),
    _file(additions=0, deletions=2,
          patch="@@ -1,3 +1 @@\n context\n-old\n\\ No newline at end of file\n-old2"),
    _file(additions=2, deletions=2,
          patch="@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n@@ -3 +3 @@\n-old2\n+new2"),
    _file(additions=2, deletions=2,
          patch="@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file\n@@ -3 +3 @@\n-old2\n+new2"),
])
def test_raw_diff_rejects_malformed_sequential_hunks(file) -> None:
    with pytest.raises(RuntimeError, match="Malformed unified patch"):
        _helpers()["_raw_diff"](
            [file], SimpleNamespace(prompt_tokens=1, count_tokens=len), "model",
            max_tokens=lambda _: 10000)


def test_raw_diff_accepts_multiple_cardinality_valid_hunks() -> None:
    patch = "@@ -1 +1 @@\n-old\n+new\n@@ -3 +3 @@\n-old2\n+new2"
    result = _helpers()["_raw_diff"](
        [_file(additions=2, deletions=2, patch=patch)],
        SimpleNamespace(prompt_tokens=1, count_tokens=len), "model",
        max_tokens=lambda _: 10000)
    assert patch in result


@pytest.mark.parametrize("file", [
    _file("new.py", "added", 1, 0, "@@ -0,0 +1 @@\n+new"),
    _file("old.py", "removed", 0, 1, "@@ -1 +0,0 @@\n-old"),
    _file(additions=1, deletions=0, patch="@@ -1,0 +2 @@\n+new"),
    _file(additions=0, deletions=1, patch="@@ -2 +1,0 @@\n-old"),
])
def test_raw_diff_accepts_valid_zero_count_range_boundaries(file) -> None:
    assert file.filename in _helpers()["_raw_diff"](
        [file], SimpleNamespace(prompt_tokens=1, count_tokens=len), "model",
        max_tokens=lambda _: 10000)


@pytest.mark.parametrize("patch,additions,deletions", [
    ("@@ -0,0 +1 @@\n+first\n@@ -3 +4 @@\n-old\n+new", 2, 1),
    ("@@ -1 +0,0 @@\n-old\n@@ -4 +3 @@\n-old2\n+new", 1, 2),
])
def test_raw_diff_accepts_cumulative_anchor_delta(
        patch, additions, deletions) -> None:
    assert patch in _helpers()["_raw_diff"](
        [_file(additions=additions, deletions=deletions, patch=patch)],
        SimpleNamespace(prompt_tokens=1, count_tokens=len), "model",
        max_tokens=lambda _: 10000)


@pytest.mark.parametrize("patch", [
    "@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new",
    "@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file",
    "@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file",
])
def test_raw_diff_accepts_valid_single_and_both_side_eof_markers(patch) -> None:
    assert patch in _helpers()["_raw_diff"](
        [_file(patch=patch)], SimpleNamespace(prompt_tokens=1, count_tokens=len),
        "model", max_tokens=lambda _: 10000)


def test_numbered_diff_preserves_deletions_renames_counts_and_final_token_bound() -> None:
    files = [_file(), _file("gone.py", "removed", 0, 1, "@@ -1 +0,0 @@\n-secret"),
             _file("new.py", "renamed", 0, 0, None, "old.py")]
    token_handler = SimpleNamespace(prompt_tokens=10, count_tokens=len)
    numbered = _helpers(verified_renames={"new.py"})["_raw_diff"](
        files, token_handler, "model", numbered=True, max_tokens=lambda _: 10000)
    assert numbered.count("## File:") == 3
    assert "__new hunk__\n1 +new" in numbered
    assert "secret" in numbered
    assert "rename from old.py\nrename to new.py" in numbered
    lossy = _helpers(lambda _patch, _file: "__new hunk__\n__old hunk__\n-old")["_raw_diff"]
    with pytest.raises(RuntimeError, match="Numbered patch is incomplete"):
        lossy([_file()], token_handler, "model", numbered=True, max_tokens=lambda _: 10000)
    expanded = _helpers(lambda _patch, _file: (
        "__new hunk__\n1 +new\n__old hunk__\n-old\n" + "x" * 300))["_raw_diff"]
    assert _helpers()["_raw_diff"](
        [_file()], token_handler, "model", max_tokens=lambda _: 1700)
    with pytest.raises(RuntimeError, match="token budget"):
        expanded([_file()], token_handler, "model", numbered=True, max_tokens=lambda _: 1700)


def test_numbered_diff_preserves_live_source_mentions_of_eof_marker() -> None:
    def pinned_shape(patch, _file):
        assert "\0" in patch  # Shielding happens before the pinned converter sees source text.
        lines = [line for line in patch.splitlines()
                 if "no newline at end of file" not in line.lower()]
        added = [f"{index} {line}" for index, line in enumerate(lines, 1)
                 if line.startswith("+")]
        removed = [line for line in lines if line.startswith("-")]
        return "__new hunk__\n" + "\n".join(added) + "\n__old hunk__\n" + "\n".join(removed)

    patch = ('@@ -1 +1,3 @@\n-old\n+pattern = r"No newline at end of file"\n'
             '+if line == r"\\ No newline at end of file": pass\n'
             '+lines = [line for line in body if line != r"\\ No newline at end of file"]')
    result = _helpers(pinned_shape)["_raw_diff"](
        [_file(additions=3, deletions=1, patch=patch)],
        SimpleNamespace(prompt_tokens=1, count_tokens=len), "model", numbered=True,
        max_tokens=lambda _: 10000)
    assert result.count("No newline at end of file") == 3
    assert "\0" not in result


def test_numbered_diff_flushes_pinned_final_deletion_only_hunk() -> None:
    patch = "@@ -1 +0,0 @@\n-old\n\\ No newline at end of file"
    calls = []

    def pinned_final_omission(candidate, _file):
        calls.append(candidate)
        if not candidate.endswith("\n@@ -0,0 +0,0 @@"):
            return ""
        return "@@ -1 +0,0 @@\n__new hunk__\n__old hunk__\n-old"

    assert pinned_final_omission(patch, None) == ""
    calls.clear()
    result = _helpers(pinned_final_omission)["_raw_diff"](
        [_file(additions=0, deletions=1, patch=patch)],
        SimpleNamespace(prompt_tokens=1, count_tokens=len), "model", numbered=True,
        max_tokens=lambda _: 10000)
    assert calls == [patch + "\n@@ -0,0 +0,0 @@"]
    assert "@@ -1 +0,0 @@\n__new hunk__\n__old hunk__\n-old" in result
    assert r"\ No newline at end of file" not in result
    with pytest.raises(RuntimeError, match="Numbered patch is incomplete"):
        _helpers(lambda _patch, _file: "")["_raw_diff"](
            [_file(additions=0, deletions=1, patch=patch)],
            SimpleNamespace(prompt_tokens=1, count_tokens=len), "model", numbered=True)


def test_list_policy_comparison_rejects_noniterable_and_mapping_actuals() -> None:
    same = _helpers()["_same"]
    assert not same(1, ["a"])
    assert not same({"a": 1}, ["a"])


def test_eligible_canonical_files_and_ineligible_missing_patch() -> None:
    h = _helpers()
    expected = [_file("a.py"), _file("b.py")]
    assert h["_same_files"](expected, [_file("b.py"), _file("a.py")])
    assert not h["_same_files"](expected, [_file("a.py")])
    assert not h["_same_files"](expected, [*expected, _file("c.py")])
    assert not h["_same_files"](expected, [_file("a.py"), _file("a.py")])
    ignored_binary = _file("asset.png", "modified", 0, 0, None)
    assert h["_manifest"](SimpleNamespace(changed_files=2), [expected[0], ignored_binary])
    token_handler = SimpleNamespace(prompt_tokens=1, count_tokens=len)
    assert "a.py" in h["_raw_diff"]([expected[0]], token_handler, "model",
                                     max_tokens=lambda _: 10000)


def test_publication_is_structural_run_bound_and_digest_verified() -> None:
    h = _helpers()
    summary = "## Title\n\nUseful title\n\n___\n\n### Walkthrough\nDetails"
    review = "## PR Reviewer Guide 🔍\n\n### No major issues detected\n\n| Check | Result |\n|---|---|"
    assert h["_substantive"]("summary", summary)
    assert h["_substantive"]("review", review)
    for body in ("", "Preparing review...", "No changes", "Incremental Review Skipped"):
        assert not h["_substantive"]("review", body)
    frozen = ("owner/repo", 7, "a" * 40, "b" * 40)
    marker = h["_marker"]("review", frozen, "123", "2", review)
    visible = review.split("\n", 1)[0] + "\n" + marker + "\n" + review.split("\n", 1)[1]
    bot = SimpleNamespace(body=visible, user=SimpleNamespace(login="github-actions[bot]"))
    assert h["_visible"]([bot], "review", frozen, "123", "2", review)
    bot.body = bot.body.replace("No major", "Some major")
    assert not h["_visible"]([bot], "review", frozen, "123", "2", review)


def test_publication_rejects_wrong_binding_and_canonicalizes_persistent_update() -> None:
    h = _helpers()
    frozen = ("owner/repo", 7, "a" * 40, "b" * 40)
    review = "## PR Reviewer Guide 🔍\n\n<table><tr><td>Clean</td></tr></table>"
    marker = h["_marker"]("review", frozen, "123", "2", review, "p" * 64)
    body = review.split("\n", 1)[0] + "\n" + marker + "\n" + review.split("\n", 1)[1]
    comment = lambda text=body, author="github-actions[bot]": SimpleNamespace(
        body=text, user=SimpleNamespace(login=author)
    )
    assert h["_visible"]([comment()], "review", frozen, "123", "2", review, "p" * 64)
    assert not h["_visible"]([comment(author="someone")], "review", frozen, "123", "2", review, "p" * 64)
    variants = [
        ("summary", frozen, "123", "2", "p" * 64),
        ("review", frozen, "122", "2", "p" * 64),
        ("review", frozen, "123", "1", "p" * 64),
        ("review", (frozen[0], frozen[1], "c" * 40, frozen[3]), "123", "2", "p" * 64),
        ("review", (frozen[0], frozen[1], frozen[2], "c" * 40), "123", "2", "p" * 64),
        ("review", frozen, "123", "2", "q" * 64),
    ]
    for kind, coordinate, run, attempt, policy in variants:
        assert not h["_visible"]([comment()], kind, coordinate, run, attempt, review, policy)
    expected_marker = h["_marker"]("review", frozen, "123", "2", review, "p" * 64)
    decorated = ("## PR Reviewer Guide 🔍\n\n#### (Review updated until commit "
                 f"https://github.com/owner/repo/commit/{'b' * 40})\n\n{expected_marker}"
                 "\n\n<table><tr><td>Clean</td></tr></table>")
    assert h["_canonical_body"](decorated, expected_marker) == h["_canonical_body"](review)
    assert h["_canonical_body"](decorated.replace("Clean", "Altered"), expected_marker) != h["_canonical_body"](review)
    assert h["_canonical_body"](decorated + "\n<!-- graphify-pr-agent:review:extra -->",
                                expected_marker) != h["_canonical_body"](review)
    assert h["_canonical_body"](decorated + "\n#### (Review updated until commit x)",
                                expected_marker) != h["_canonical_body"](review)


def test_wiring_attests_policy_before_constructors_and_uses_one_raw_builder() -> None:
    code = _embedded_python()
    shim = code.index('MAX_TOKENS["gemini/gemini-3.7-flash"] = 1048576')
    assert shim < code.index("from pr_agent.agent.pr_agent import PRAgent")
    assert code.index("policy_attested = True") < code.index("asyncio.run(_run_action_and_drain())")
    assert "pr_description.get_pr_diff = _complete_diff" in code
    assert "pr_reviewer.get_pr_diff = _complete_diff" in code
    assert "if not policy_attested:" in code
    assert "if handled is False:" in code
    assert code.count("_fresh()") >= 4


def _run_stubbed_entry(monkeypatch, tmp_path, event_name="pull_request", handled=True,
                       drift=None, policy_available=True, setting_updates=None,
                       publish_current=True, visible_transform=None, seed_old=False,
                       body_overrides=None, omit=None,
                       run_id="123", attempt="2", apply_error=False,
                       tool_error=False, policy_error=False, event_mutator=None,
                       description_predictions=None, record=None,
                       conversion_loss=False, review_numbered=True,
                       summary_include_changes=True, changed_file=None,
                       previous_sha=None, context_overrides=None,
                       context_error=None):
    base, head = "a" * 40, "b" * 40
    policy = (ROOT / ".pr_agent.toml").read_bytes()
    context_files = ["AGENTS.md", "SECURITY.md", "pyproject.toml", ".github/workflows/ci.yml"]
    context = {path: f"frozen {path}\n".encode() for path in context_files}
    context.update(context_overrides or {})
    comments = []
    reads = {"pull": 0, "requests": [], "aliases": [], "eager": 0,
             "description_models": [], "description_diffs": [], "tool_diffs": [],
             "conversions": [], "published": [], "errors": [],
             "context_fetches": [], "context_served": []}
    if record is not None:
        record["reads"] = reads
    prediction_queue = list(description_predictions or [VALID_DESCRIPTION])
    changed = changed_file or _file()
    pull = SimpleNamespace(number=7, base=SimpleNamespace(sha=base), head=SimpleNamespace(sha=head),
                           additions=1, deletions=1, changed_files=1,
                           get_files=lambda: [changed], get_issue_comments=lambda: comments,
                           get_commits=lambda: (_ for _ in ()).throw(AssertionError("commit aggregation used")))
    moved = SimpleNamespace(**vars(pull))
    moved.base = SimpleNamespace(sha="c" * 40 if drift == "base" else base)
    moved.head = SimpleNamespace(sha="c" * 40 if drift == "head" else head)
    def get_pull(number):
        reads["pull"] += 1
        return moved if drift and reads["pull"] >= 3 else pull
    def get_contents(path, ref):
        if policy_error:
            raise RuntimeError("policy API failed")
        if path == ".pr_agent.toml":
            return SimpleNamespace(decoded_content=policy if policy_available else None)
        if path in context_files:
            reads["context_fetches"].append((path, ref))
            if path == context_error:
                raise RuntimeError("context API failed")
            return SimpleNamespace(decoded_content=context[path])
        return SimpleNamespace(sha=previous_sha)
    head_commit = SimpleNamespace(sha=head)
    repo = SimpleNamespace(get_pull=get_pull, get_contents=get_contents,
                           get_commit=lambda sha: head_commit)

    class Settings:
        def __init__(self):
            self.values = {
            "CONFIG.GIT_PROVIDER": "github", "CONFIG.PUBLISH_OUTPUT": True,
            "CONFIG.MODEL": "gemini/gemini-3.7-flash",
            "CONFIG.FALLBACK_MODELS": ["gemini/gemini-3.5-flash-lite"],
            "CONFIG.REASONING_EFFORT": "high", "CONFIG.MAX_MODEL_TOKENS": 262144,
            "CONFIG.PROPAGATE_TOOL_ERRORS": True, "CONFIG.EXTRA_CONFIG_URL": "",
            "CONFIG.USE_GLOBAL_SETTINGS_FILE": False, "CONFIG.USE_REPO_SETTINGS_FILE": True,
            "CONFIG.RESTRICTED_MODE": True, "CONFIG.REPO_CONTEXT_FROM_DEFAULT_BRANCH": False,
            "CONFIG.REPO_CONTEXT_MAX_LINES": 500,
            "CONFIG.REPO_CONTEXT_FILES": ["AGENTS.md", "SECURITY.md", "pyproject.toml", ".github/workflows/ci.yml"],
            "GITHUB_ACTION_CONFIG.HANDLE_PUSH_TRIGGER": False,
            "GITHUB_ACTION_CONFIG.AUTO_REVIEW": True, "GITHUB_ACTION_CONFIG.AUTO_DESCRIBE": True,
            "GITHUB_ACTION_CONFIG.AUTO_IMPROVE": False,
            "GITHUB_ACTION_CONFIG.PR_ACTIONS": ["opened", "reopened", "ready_for_review"],
            "PR_DESCRIPTION.PUBLISH_DESCRIPTION_AS_COMMENT": True,
            "PR_DESCRIPTION.PUBLISH_DESCRIPTION_AS_COMMENT_PERSISTENT": True,
            "PR_REVIEWER.PUBLISH_OUTPUT_NO_SUGGESTIONS": True,
            "PR_REVIEWER.PERSISTENT_COMMENT": True, "PR_REVIEWER.FINAL_UPDATE_MESSAGE": True,
            "PR_REVIEWER.NUM_MAX_FINDINGS": 5, "PR_REVIEWER.REQUIRE_TESTS_REVIEW": True,
            "PR_REVIEWER.REQUIRE_SECURITY_REVIEW": True,
                "PR_REVIEWER.EXTRA_INSTRUCTIONS": "target branch's requires-python declaration SECURITY.md",
                "IGNORE.REGEX": [], "IGNORE.GLOB": [],
                "CONFIG.IGNORE_LANGUAGE_FRAMEWORK": [],
            }
            self.values.update(setting_updates or {})
        def get(self, key, default=None):
            return self.values.get(key, default)
        def set(self, key, value):
            self.values[key] = value

    settings = Settings()
    class Provider:
        repo = "owner/repo"
        pr = pull
        def publish_persistent_comment(self, body, *args, **kwargs):
            if publish_current:
                visible = visible_transform(body) if visible_transform else body
                comments.append(SimpleNamespace(body=visible, user=SimpleNamespace(login="github-actions[bot]")))
                reads["published"].append(kwargs.get("name"))

    class Tool:
        kind = "review"
        def __init__(self, *args, **kwargs):
            self.git_provider = GithubProvider("https://example/pull/7")
            self.prediction = None
            repo_context = []
            for path in settings.values["CONFIG.REPO_CONTEXT_FILES"]:
                value = self.git_provider.get_repo_file_content(
                    path, from_default_branch=False)
                reads["context_served"].append((path, value))
                repo_context.append(value)
            self.vars = {"include_file_summary_changes": summary_include_changes,
                         "repo_context": "\n".join(repo_context)}
        async def run(self):
            if tool_error:
                raise RuntimeError("tool failed")
            self.prediction = "prediction"
            alias = description_module.get_pr_diff if self.kind == "summary" else reviewer_module.get_pr_diff
            diff = alias(self.git_provider, SimpleNamespace(prompt_tokens=1, count_tokens=len),
                         settings.values["CONFIG.MODEL"],
                         add_line_numbers_to_hunks=review_numbered)
            reads["aliases"].append(self.kind)
            reads["tool_diffs"].append(diff)
            defaults = {"summary": "## Title\n\nTitle\n\n___\n\n### Walkthrough\nBody",
                        "review": "## PR Reviewer Guide 🔍\n\n### No major issues detected\n\n| A | B |\n|---|---|"}
            body = (body_overrides or {}).get(self.kind, defaults[self.kind])
            self.git_provider.publish_persistent_comment(body, name=self.kind)

    class Description(Tool):
        kind = "summary"
        keys_fix = ["filename:"]
        async def _prepare_prediction(self, model):
            if tool_error:
                raise RuntimeError("tool failed")
            if prediction_queue:
                self.prediction = prediction_queue.pop(0)
            diff = description_module.get_pr_diff(
                self.git_provider, SimpleNamespace(prompt_tokens=1, count_tokens=len), model)
            reads["aliases"].append(self.kind)
            reads["description_models"].append(model)
            reads["description_diffs"].append(diff)
        async def run(self):
            await description_module.retry_with_fallback_models(self._prepare_prediction, "weak")
            if not isinstance(description_module.load_yaml(self.prediction), dict):
                raise TypeError("description data is not a mapping")
            body = (body_overrides or {}).get(
                self.kind, "## Title\n\nTitle\n\n___\n\n### Walkthrough\nBody")
            self.git_provider.publish_persistent_comment(body, name=self.kind)
    class Reviewer(Tool):
        pass
    class Agent:
        async def handle_request(self, url, request, *args, **kwargs):
            reads["requests"].append(request)
            if not handled:
                return False
            try:
                agent_module.apply_repo_settings(url)
                if omit != "review":
                    await Reviewer(url).run()
                return True
            except Exception as error:
                reads["errors"].append(repr(error))
                return False

    class GithubProvider(Provider):
        def __init__(self, pr_url=None):
            self.git_files = None
            self.set_pr(pr_url)
            if pr_url:
                self.pr_commits = list(self.pr.get_commits())
                reads["eager"] += 1
                self.last_commit_id = self.pr_commits[-1]
        def set_pr(self, pr_url):
            self.repo, self.pr_num, self.pr = "owner/repo", 7, pull

    def module(name, **attrs):
        value = types.ModuleType(name)
        vars(value).update(attrs)
        monkeypatch.setitem(sys.modules, name, value)
        return value

    module("github", Github=lambda token: SimpleNamespace(get_repo=lambda name: repo))
    pr_agent = module("pr_agent")
    max_tokens = {"gemini/gemini-3.5-flash-lite": 1048576}
    no_temperature_models = []
    algo = module("pr_agent.algo", MAX_TOKENS=max_tokens,
                  NO_SUPPORT_TEMPERATURE_MODELS=no_temperature_models,
                  SUPPORT_REASONING_EFFORT_MODELS=[])
    module("pr_agent.algo.file_filter", filter_ignored=lambda files: files)
    def convert_patch(_patch, file):
        reads["conversions"].append(file)
        return ("__new hunk__\n__old hunk__\n-old" if conversion_loss else
                "@@ -1 +1 @@\n__new hunk__\n1 +new\n__old hunk__\n-old")
    module("pr_agent.algo.git_patch_processing",
           decouple_and_convert_to_hunks_with_lines_numbers=convert_patch)
    module("pr_agent.algo.language_handler", is_valid_file=lambda name: True)
    def stub_get_max_tokens(model):
        return min(max_tokens[model], settings.values["CONFIG.MAX_MODEL_TOKENS"])
    module("pr_agent.algo.utils", get_max_tokens=stub_get_max_tokens)
    module("pr_agent.config_loader", get_settings=lambda: settings)
    module("pr_agent.git_providers")
    github_provider_module = module("pr_agent.git_providers.github_provider", GithubProvider=GithubProvider)
    def apply(url):
        if apply_error:
            raise RuntimeError("settings apply failed")
        GithubProvider(url).get_repo_settings()
    utils_module = module("pr_agent.git_providers.utils", apply_repo_settings=apply)
    module("pr_agent.agent")
    agent_module = module("pr_agent.agent.pr_agent", PRAgent=Agent, apply_repo_settings=apply)
    def load_yaml(text, **_kwargs):
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError:
            return None
    async def retry_with_fallback_models(function, _model_type):
        models = ["gemini/gemini-3.7-flash", "gemini/gemini-3.5-flash-lite"]
        for index, model in enumerate(models):
            try:
                return await function(model)
            except Exception as error:
                if index == len(models) - 1:
                    raise RuntimeError("all description models failed") from error
        raise AssertionError("fallback model list must not be empty")
    description_module = module("pr_agent.tools.pr_description", PRDescription=Description,
                                get_pr_diff=None, load_yaml=load_yaml,
                                retry_with_fallback_models=retry_with_fallback_models)
    reviewer_module = module("pr_agent.tools.pr_reviewer", PRReviewer=Reviewer, get_pr_diff=None)
    tools_module = module("pr_agent.tools", pr_description=description_module, pr_reviewer=reviewer_module)
    async def drain():
        if event_name == "pull_request":
            try:
                runner_module.apply_repo_settings("https://example/pr/7")
            except Exception as error:
                reads["errors"].append(repr(error))
            if omit != "summary":
                await Description().run()
            if omit != "review":
                await Reviewer().run()
        else:
            await Agent().handle_request("https://example/pr/7", "/prreview")
    runner_module = module("pr_agent.servers.github_action_runner",
                           apply_repo_settings=utils_module.apply_repo_settings,
                           _run_action_and_drain=drain)
    module("pr_agent.servers", github_action_runner=runner_module)
    pr_agent.algo, pr_agent.tools = algo, tools_module
    payload = ({"pull_request": {"number": 7, "html_url": "https://example/pr/7",
                                  "base": {"sha": base, "repo": {"full_name": "owner/repo"}},
                                  "head": {"sha": head}}} if event_name == "pull_request" else
               {"issue": {"number": 7, "pull_request": {"html_url": "https://example/pr/7"}},
                "comment": {"body": "/prreview"}})
    if event_mutator:
        event_mutator(payload)
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(payload))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_RUN_ID", run_id)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", attempt)
    if seed_old:
        review = "## PR Reviewer Guide 🔍\n\n### No major issues detected\n\n| A | B |\n|---|---|"
        frozen = ("owner/repo", 7, base, head)
        old = _helpers()["_marker"]("review", frozen, run_id, "1", review,
                                    hashlib.sha256(policy).hexdigest())
        comments.append(SimpleNamespace(body=review.split("\n", 1)[0] + "\n" + old + "\n" +
                                        review.split("\n", 1)[1],
                                        user=SimpleNamespace(login="github-actions[bot]")))
    exec(compile(_embedded_python(), "<embedded-entry>", "exec"), {})
    reads["effective_tokens"] = {
        model: stub_get_max_tokens(model) for model in (
            "gemini/gemini-3.7-flash", "gemini/gemini-3.5-flash-lite")
    }
    reads["no_temperature_models"] = list(no_temperature_models)
    reads["reasoning_models"] = list(algo.SUPPORT_REASONING_EFFORT_MODELS)
    reads["reasoning_effort"] = settings.values["CONFIG.REASONING_EFFORT"]
    return reads, description_module, reviewer_module


def test_stubbed_embedded_entry_runs_initial_and_full_prreview(monkeypatch, tmp_path) -> None:
    reads, description, reviewer = _run_stubbed_entry(monkeypatch, tmp_path)
    context_files = ["AGENTS.md", "SECURITY.md", "pyproject.toml", ".github/workflows/ci.yml"]
    assert reads["pull"] >= 6
    assert description.get_pr_diff is reviewer.get_pr_diff
    assert reads["aliases"] == ["summary", "review"]
    assert "__new hunk__" not in reads["description_diffs"][0]
    assert "## File: 'src/a.py'" in reads["tool_diffs"][0]
    assert "__new hunk__\n1 +new" in reads["tool_diffs"][0]
    assert reads["conversions"] == [None]
    assert reads["eager"] >= 3
    assert reads["context_fetches"] == [(path, "a" * 40) for path in context_files]
    assert {path for path, _content in reads["context_served"]} == set(context_files)
    assert reads["effective_tokens"] == {
        "gemini/gemini-3.7-flash": 262144,
        "gemini/gemini-3.5-flash-lite": 262144,
    }
    assert reads["no_temperature_models"] == ["gemini/gemini-3.7-flash"]
    assert reads["reasoning_models"] == ["gemini-3.7-flash", "gemini-3.5-flash-lite"]
    assert reads["reasoning_effort"] == "high"
    reads, _, _ = _run_stubbed_entry(monkeypatch, tmp_path, event_name="issue_comment")
    assert reads["requests"] == ["/review"]
    assert reads["aliases"] == ["review"]


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"context_error": "SECURITY.md"}, "context is unavailable"),
    ({"context_overrides": {"SECURITY.md": None}}, "context is unavailable"),
    ({"context_overrides": {"SECURITY.md": b" \n"}}, "context is empty"),
    ({"context_overrides": {"SECURITY.md": b"\xff"}}, "context is not UTF-8"),
    ({"context_overrides": {"AGENTS.md": b"line\n" * 480}}, "exceeds the complete-context budget"),
])
def test_stubbed_entry_requires_complete_frozen_repo_context(
        monkeypatch, tmp_path, kwargs, message) -> None:
    with pytest.raises(RuntimeError, match=message):
        _run_stubbed_entry(monkeypatch, tmp_path, **kwargs)


def test_stubbed_entry_requires_blob_identity_for_patchless_rename(monkeypatch, tmp_path) -> None:
    rename = _file("new.py", "renamed", 0, 0, None, "old.py", sha="c" * 40)
    reads, _, _ = _run_stubbed_entry(
        monkeypatch, tmp_path, changed_file=rename, previous_sha="c" * 40)
    assert "rename from old.py\nrename to new.py" in reads["tool_diffs"][0]
    with pytest.raises(RuntimeError, match="Patchless rename blob identity mismatch"):
        _run_stubbed_entry(
            monkeypatch, tmp_path, changed_file=rename, previous_sha="d" * 40)


def test_description_malformed_primary_uses_valid_fallback(monkeypatch, tmp_path) -> None:
    malformed = "pr_files:\n- filename: |.github/workflows/pr-agent.yml"
    reads, _, _ = _run_stubbed_entry(
        monkeypatch, tmp_path, description_predictions=[malformed, VALID_DESCRIPTION])
    assert reads["description_models"] == [
        "gemini/gemini-3.7-flash", "gemini/gemini-3.5-flash-lite"]
    assert reads["aliases"] == ["summary", "summary", "review"]
    assert reads["description_diffs"][0] == reads["description_diffs"][1]
    assert reads["published"] == ["summary", "review"]


@pytest.mark.parametrize("predictions", [
    ["filename: |.github/a.py"],
    ["{}", "[]"],
    ["title: ''\ndescription: Body\ntype: [feature]\npr_files: [{filename: a.py}]", "{title: Test}"],
    ["title: Test\ndescription: Body\ntype: feature\npr_files: [{filename: a.py}]", "title: Test\ndescription: Body\ntype: []\npr_files: []"],
    ["title: Test\ndescription: Body\ntype: [feature]\npr_files: [a.py]", "title: Test\ndescription: Body\ntype: ['']\npr_files: [{}]"],
    ["title: Test\ndescription: Body\ntype: [Feature]\npr_files: [{filename: a.py, changes_title: T, changes_summary: S, label: L}]", "title: Test\ndescription: Body\ntype: [Other]\npr_files: [{filename: a.py}]"],
    ["title: Test\ndescription: Body\ntype: [{}]\npr_files: [{filename: a.py, changes_title: T, changes_summary: S, label: L}]"],
])
def test_description_all_invalid_attempts_publish_nothing(
        monkeypatch, tmp_path, predictions) -> None:
    record = {}
    with pytest.raises(RuntimeError, match="all description models failed"):
        _run_stubbed_entry(
            monkeypatch, tmp_path, description_predictions=predictions, record=record)
    assert record["reads"]["description_models"] == [
        "gemini/gemini-3.7-flash", "gemini/gemini-3.5-flash-lite"]
    assert record["reads"]["published"] == []


def test_description_valid_primary_is_single_attempt(monkeypatch, tmp_path) -> None:
    reads, _, _ = _run_stubbed_entry(
        monkeypatch, tmp_path, description_predictions=[VALID_DESCRIPTION])
    assert reads["description_models"] == ["gemini/gemini-3.7-flash"]
    assert reads["aliases"] == ["summary", "review"]
    assert reads["published"] == ["summary", "review"]


def test_description_changes_summary_is_required_only_when_prompt_includes_it(
        monkeypatch, tmp_path) -> None:
    prediction = ("title: Test\ndescription: Body\ntype: [Other]\npr_files:\n"
                  "- filename: src/a.py\n  changes_title: T\n  label: other")
    reads, _, _ = _run_stubbed_entry(
        monkeypatch, tmp_path, description_predictions=[prediction],
        summary_include_changes=False)
    assert reads["description_models"] == ["gemini/gemini-3.7-flash"]


def test_stubbed_entry_rejects_numbered_conversion_loss_and_invalid_flag(
        monkeypatch, tmp_path) -> None:
    with pytest.raises(RuntimeError, match="Numbered patch is incomplete"):
        _run_stubbed_entry(monkeypatch, tmp_path, conversion_loss=True)
    with pytest.raises(RuntimeError, match="Invalid numbered-diff request"):
        _run_stubbed_entry(monkeypatch, tmp_path, review_numbered="true")


def test_stubbed_embedded_entry_propagates_false_handle(monkeypatch, tmp_path) -> None:
    with pytest.raises(RuntimeError, match="swallowed a review failure"):
        _run_stubbed_entry(monkeypatch, tmp_path, event_name="issue_comment", handled=False)


@pytest.mark.parametrize("drift", ["base", "head"])
def test_stubbed_entry_rejects_fresh_identity_drift(monkeypatch, tmp_path, drift) -> None:
    with pytest.raises(RuntimeError, match="base or head moved"):
        _run_stubbed_entry(monkeypatch, tmp_path, drift=drift)


def test_stubbed_entry_rejects_missing_or_altered_policy(monkeypatch, tmp_path) -> None:
    with pytest.raises(RuntimeError, match="policy is unavailable"):
        _run_stubbed_entry(monkeypatch, tmp_path, policy_available=False)
    with pytest.raises(RuntimeError, match="before policy attestation"):
        _run_stubbed_entry(monkeypatch, tmp_path,
                           setting_updates={"CONFIG.MAX_MODEL_TOKENS": 32000})


@pytest.mark.parametrize("setting_updates", [
    {"IGNORE.REGEX": ["["]}, {"IGNORE.REGEX": [1]}, {"IGNORE.GLOB": [1]},
    {"CONFIG.IGNORE_LANGUAGE_FRAMEWORK": "python"}, {"CONFIG.IGNORE_LANGUAGE_FRAMEWORK": [1]},
])
def test_stubbed_entry_fails_closed_on_invalid_ignore_policy(monkeypatch, tmp_path,
                                                             setting_updates) -> None:
    with pytest.raises(RuntimeError, match="before policy attestation"):
        _run_stubbed_entry(monkeypatch, tmp_path, setting_updates=setting_updates)


@pytest.mark.parametrize("setting_updates", [
    {"CONFIG.GIT_PROVIDER": "gitlab"}, {"CONFIG.MODEL": "other"},
    {"CONFIG.USE_GLOBAL_SETTINGS_FILE": True}, {"CONFIG.EXTRA_CONFIG_URL": "https://x"},
    {"CONFIG.REPO_CONTEXT_FILES": ["AGENTS.md"]}, {"CONFIG.REPO_CONTEXT_FILES": 1},
    {"GITHUB_ACTION_CONFIG.HANDLE_PUSH_TRIGGER": True},
    {"PR_DESCRIPTION.PUBLISH_DESCRIPTION_AS_COMMENT": False},
    {"PR_REVIEWER.NUM_MAX_FINDINGS": 3},
])
def test_stubbed_entry_rejects_each_critical_policy_class(monkeypatch, tmp_path,
                                                          setting_updates) -> None:
    with pytest.raises(RuntimeError, match="before policy attestation"):
        _run_stubbed_entry(monkeypatch, tmp_path, setting_updates=setting_updates)


@pytest.mark.parametrize("setting_updates,expected_error", [
    ({"IGNORE.REGEX": ["["]}, "Invalid PR-Agent ignore regex"),
    ({"CONFIG.MODEL": "other"}, "Effective PR-Agent policy attestation failed"),
    ({"CONFIG.REPO_CONTEXT_FILES": 1}, "Repository context policy mismatch"),
])
def test_stubbed_entry_records_swallowed_policy_cause(monkeypatch, tmp_path,
                                                      setting_updates,
                                                      expected_error) -> None:
    record = {}
    with pytest.raises(RuntimeError, match="before policy attestation"):
        _run_stubbed_entry(
            monkeypatch, tmp_path, setting_updates=setting_updates, record=record)
    assert record["reads"]["errors"] == [f"RuntimeError('{expected_error}')"]


def test_stubbed_entry_propagates_settings_tool_and_policy_api_failures(monkeypatch, tmp_path) -> None:
    def recorded_failure(match, **kwargs):
        record = {}
        with pytest.raises(RuntimeError, match=match):
            _run_stubbed_entry(monkeypatch, tmp_path, record=record, **kwargs)
        return record["reads"]["errors"]

    assert recorded_failure("before policy attestation", apply_error=True) == [
        "RuntimeError('settings apply failed')"]
    assert recorded_failure(
        "swallowed a review failure", event_name="issue_comment", apply_error=True
    ) == ["RuntimeError('settings apply failed')"]
    with pytest.raises(RuntimeError, match="all description models failed"):
        _run_stubbed_entry(monkeypatch, tmp_path, tool_error=True)
    assert recorded_failure(
        "swallowed a review failure", event_name="issue_comment", tool_error=True
    ) == ["RuntimeError('tool failed')"]
    with pytest.raises(RuntimeError, match="policy API failed"):
        _run_stubbed_entry(monkeypatch, tmp_path, policy_error=True)


@pytest.mark.parametrize("event_mutator,match", [
    (lambda event: event["pull_request"]["base"]["repo"].update(full_name="other/repo"),
     "Event repository"),
    (lambda event: event["pull_request"].update(number=8), "identity differ"),
    (lambda event: event["pull_request"]["base"].update(sha="c" * 40), "identity differ"),
    (lambda event: event["pull_request"]["head"].update(sha="c" * 40), "identity differ"),
])
def test_stubbed_entry_rejects_event_identity_mismatch(monkeypatch, tmp_path,
                                                       event_mutator, match) -> None:
    with pytest.raises(RuntimeError, match=match):
        _run_stubbed_entry(monkeypatch, tmp_path, event_mutator=event_mutator)


@pytest.mark.parametrize("run_id,attempt", [
    ("", "1"), ("abc", "1"), ("123", ""), ("123", "0"), ("123", "-1"), ("123", "x")
])
def test_stubbed_entry_rejects_invalid_run_coordinates(monkeypatch, tmp_path,
                                                       run_id, attempt) -> None:
    with pytest.raises(RuntimeError, match="run coordinates"):
        _run_stubbed_entry(monkeypatch, tmp_path, run_id=run_id, attempt=attempt)


@pytest.mark.parametrize("body", [None, "", "Preparing review...", "No changes",
                                          "Incremental Review Skipped"])
def test_stubbed_entry_rejects_non_substantive_review(monkeypatch, tmp_path, body) -> None:
    with pytest.raises(RuntimeError, match="swallowed a review failure"):
        _run_stubbed_entry(monkeypatch, tmp_path, event_name="issue_comment",
                           body_overrides={"review": body})


def test_stubbed_entry_rejects_stale_or_altered_visible_publication(monkeypatch, tmp_path) -> None:
    with pytest.raises(RuntimeError, match="swallowed a review failure"):
        _run_stubbed_entry(monkeypatch, tmp_path, event_name="issue_comment",
                           publish_current=False, seed_old=True)
    with pytest.raises(RuntimeError, match="not visible intact"):
        _run_stubbed_entry(monkeypatch, tmp_path,
                           visible_transform=lambda body: body.replace("Body", "Altered"))
    with pytest.raises(RuntimeError, match="not visible intact"):
        _run_stubbed_entry(monkeypatch, tmp_path,
                           visible_transform=lambda body: body.rsplit("\n", 1)[0])


@pytest.mark.parametrize("event_name,omit", [
    ("pull_request", "summary"), ("pull_request", "review"), ("issue_comment", "review")
])
def test_stubbed_entry_requires_event_specific_outputs(monkeypatch, tmp_path,
                                                       event_name, omit) -> None:
    with pytest.raises(RuntimeError, match="outputs did not complete"):
        _run_stubbed_entry(monkeypatch, tmp_path, event_name=event_name, omit=omit)


def test_embedded_python_compiles_and_stays_lean() -> None:
    compile(_embedded_python(), ".github/workflows/pr-agent.yml", "exec")
    assert len(_workflow().splitlines()) <= 500
    assert len((ROOT / ".pr_agent.toml").read_text().splitlines()) <= 125
