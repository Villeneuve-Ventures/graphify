import ast
import hashlib
import json
from pathlib import Path
import sys
import types
from types import SimpleNamespace

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]


def _workflow() -> str:
    return (ROOT / ".github/workflows/pr-agent.yml").read_text()


def _embedded_python() -> str:
    lines = _workflow().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "python - <<'PY'") + 1
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "PY")
    indent = len(lines[start]) - len(lines[start].lstrip())
    return "\n".join(line[indent:] if line.strip() else "" for line in lines[start:end])


def _helpers() -> dict:
    tree = ast.parse(_embedded_python())
    wanted = {"_body_digest", "_canonical_body", "_manifest", "_marker", "_raw_diff",
              "_same_files", "_substantive", "_valid_int", "_valid_path", "_visible"}
    body = [node for node in tree.body if (
        isinstance(node, ast.Import) and any(alias.name in {"hashlib", "re"} for alias in node.names)
    ) or (isinstance(node, ast.FunctionDef) and node.name in wanted)]
    namespace: dict = {"get_max_tokens": lambda _model: 65536}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])),
                 "<pr-agent-policy>", "exec"), namespace)
    return namespace


def _config() -> dict:
    with (ROOT / ".pr_agent.toml").open("rb") as config_file:
        return tomllib.load(config_file)


def _file(name="src/a.py", status="modified", additions=1, deletions=1,
          patch="@@ -1 +1 @@\n-old\n+new", previous_filename=None):
    return SimpleNamespace(filename=name, status=status, additions=additions,
                           deletions=deletions, patch=patch,
                           previous_filename=previous_filename)


def test_common_path_and_trusted_runtime_settings_are_preserved() -> None:
    workflow = _workflow()
    config = _config()
    assert "types: [opened, reopened, ready_for_review]" in workflow
    assert "synchronize" not in workflow
    assert "github.event.pull_request.draft == false" in workflow
    assert 'python-version: "3.14"' in workflow
    assert "570f67ed5fc8db5be74c18df070bc20079b64b0d" in workflow
    assert 'config.model: "gemini/gemini-3.6-flash"' in workflow
    assert 'config.fallback_models: \'["gemini/gemini-3.5-flash-lite"]\'' in workflow
    assert 'config.max_model_tokens: "65536"' in workflow
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
        "eeb22d812ec5e7d2432c442bcad18849fb5ae10b8bef13503dc541325c0fc34d"
    )


def test_prreview_is_an_exact_full_review_without_incremental_state() -> None:
    code = _embedded_python()
    assert "github.event.comment.body == '/prreview'" in _workflow()
    assert 'request = "/review"' in code
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
    raw_diff = _helpers()["_raw_diff"]
    files = [_file("gone.py", "removed", 0, 1, "@@ -1 +0,0 @@\n-secret"),
             _file("new.py", "renamed", 0, 0, None, "old.py")]
    token_handler = SimpleNamespace(prompt_tokens=10, count_tokens=lambda text: len(text))
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


def test_eligible_canonical_files_and_ineligible_missing_patch() -> None:
    h = _helpers()
    expected = [_file("a.py"), _file("b.py")]
    assert h["_same_files"](expected, [_file("b.py"), _file("a.py")])
    assert not h["_same_files"](expected, [_file("a.py")])
    assert not h["_same_files"](expected, [*expected, _file("c.py")])
    assert not h["_same_files"](expected, [_file("a.py"), _file("a.py")])
    ignored_binary = _file("asset.png", "modified", 0, 0, None)
    assert h["_manifest"](SimpleNamespace(changed_files=2), [expected[0], ignored_binary])
    token_handler = SimpleNamespace(prompt_tokens=1, count_tokens=lambda text: len(text))
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
                       tool_error=False, policy_error=False, event_mutator=None):
    base, head = "a" * 40, "b" * 40
    policy = (ROOT / ".pr_agent.toml").read_bytes()
    comments = []
    reads = {"pull": 0, "requests": [], "aliases": [], "eager": 0}
    changed = _file()
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
        return SimpleNamespace(decoded_content=policy if policy_available else None)
    head_commit = SimpleNamespace(sha=head)
    repo = SimpleNamespace(get_pull=get_pull, get_contents=get_contents,
                           get_commit=lambda sha: head_commit)

    class Settings:
        def __init__(self):
            self.values = {
            "CONFIG.GIT_PROVIDER": "github", "CONFIG.PUBLISH_OUTPUT": True,
            "CONFIG.MODEL": "gemini/gemini-3.6-flash",
            "CONFIG.FALLBACK_MODELS": ["gemini/gemini-3.5-flash-lite"],
            "CONFIG.REASONING_EFFORT": "high", "CONFIG.MAX_MODEL_TOKENS": 65536,
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

    class Tool:
        kind = "review"
        def __init__(self, *args, **kwargs):
            self.git_provider = GithubProvider("https://example/pull/7")
            self.prediction = None
        async def run(self):
            if tool_error:
                raise RuntimeError("tool failed")
            self.prediction = "prediction"
            alias = description_module.get_pr_diff if self.kind == "summary" else reviewer_module.get_pr_diff
            alias(self.git_provider, SimpleNamespace(prompt_tokens=1, count_tokens=lambda text: len(text)), "model")
            reads["aliases"].append(self.kind)
            defaults = {"summary": "## Title\n\nTitle\n\n___\n\n### Walkthrough\nBody",
                        "review": "## PR Reviewer Guide 🔍\n\n### No major issues detected\n\n| A | B |\n|---|---|"}
            body = (body_overrides or {}).get(self.kind, defaults[self.kind])
            self.git_provider.publish_persistent_comment(body, name=self.kind)

    class Description(Tool):
        kind = "summary"
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
            except Exception:
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
    algo = module("pr_agent.algo", SUPPORT_REASONING_EFFORT_MODELS=[])
    module("pr_agent.algo.file_filter", filter_ignored=lambda files: files)
    module("pr_agent.algo.language_handler", is_valid_file=lambda name: True)
    module("pr_agent.algo.utils", get_max_tokens=lambda model: 65536)
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
    description_module = module("pr_agent.tools.pr_description", PRDescription=Description, get_pr_diff=None)
    reviewer_module = module("pr_agent.tools.pr_reviewer", PRReviewer=Reviewer, get_pr_diff=None)
    tools_module = module("pr_agent.tools", pr_description=description_module, pr_reviewer=reviewer_module)
    async def drain():
        if event_name == "pull_request":
            try:
                runner_module.apply_repo_settings("https://example/pr/7")
            except Exception:
                pass
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
    return reads, description_module, reviewer_module


def test_stubbed_embedded_entry_runs_initial_and_full_prreview(monkeypatch, tmp_path) -> None:
    reads, description, reviewer = _run_stubbed_entry(monkeypatch, tmp_path)
    assert reads["pull"] >= 6
    assert description.get_pr_diff is reviewer.get_pr_diff
    assert reads["aliases"] == ["summary", "review"]
    assert reads["eager"] >= 3
    reads, _, _ = _run_stubbed_entry(monkeypatch, tmp_path, event_name="issue_comment")
    assert reads["requests"] == ["/review"]
    assert reads["aliases"] == ["review"]


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
    {"CONFIG.REPO_CONTEXT_FILES": ["AGENTS.md"]},
    {"GITHUB_ACTION_CONFIG.HANDLE_PUSH_TRIGGER": True},
    {"PR_DESCRIPTION.PUBLISH_DESCRIPTION_AS_COMMENT": False},
    {"PR_REVIEWER.NUM_MAX_FINDINGS": 3},
])
def test_stubbed_entry_rejects_each_critical_policy_class(monkeypatch, tmp_path,
                                                          setting_updates) -> None:
    with pytest.raises(RuntimeError, match="before policy attestation"):
        _run_stubbed_entry(monkeypatch, tmp_path, setting_updates=setting_updates)


def test_stubbed_entry_propagates_settings_tool_and_policy_api_failures(monkeypatch, tmp_path) -> None:
    with pytest.raises(RuntimeError, match="before policy attestation"):
        _run_stubbed_entry(monkeypatch, tmp_path, apply_error=True)
    with pytest.raises(RuntimeError, match="swallowed a review failure"):
        _run_stubbed_entry(monkeypatch, tmp_path, event_name="issue_comment", apply_error=True)
    with pytest.raises(RuntimeError, match="tool failed"):
        _run_stubbed_entry(monkeypatch, tmp_path, tool_error=True)
    with pytest.raises(RuntimeError, match="swallowed a review failure"):
        _run_stubbed_entry(monkeypatch, tmp_path, event_name="issue_comment", tool_error=True)
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
    assert len(_workflow().splitlines()) <= 450
    assert len((ROOT / ".pr_agent.toml").read_text().splitlines()) <= 125
