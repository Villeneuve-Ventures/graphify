"""Offline compatibility checks with the workflow's pinned PR-Agent installed.

Run with the pinned runtime: python -m pytest --noconftest tests/test_pr_agent_runtime.py -q.
The ordinary Graphify environment intentionally does not depend on PR-Agent.
"""
import ast
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pr_agent", reason="requires the workflow's pinned PR-Agent runtime")
import importlib

add_pr_review_identity = importlib.import_module("pr_agent.algo.utils").add_pr_review_identity
get_settings = importlib.import_module("pr_agent.config_loader").get_settings
GithubProvider = importlib.import_module("pr_agent.git_providers.github_provider").GithubProvider
pr_reviewer = importlib.import_module("pr_agent.tools.pr_reviewer")
PRReviewer = pr_reviewer.PRReviewer

from tests.test_pr_agent_policy import _embedded_python, _helpers


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", [None, "empty", "head", "base", "tamper", "api"])
def test_real_reviewer_run_and_persistent_provider(monkeypatch, existing, failure):
    """Exercise actual run/publication dispatch and receipt with prepared output."""
    settings = get_settings()
    monkeypatch.setattr(settings, "config", settings.config.copy())
    for key, value in {"config.publish_output": True, "config.is_auto_command": True,
                       "config.propagate_tool_errors": True,
                       "pr_reviewer.persistent_comment": True,
                       "pr_reviewer.persistent_finding_state": False,
                       "pr_reviewer.enable_large_pr_chunking": False,
                       "pr_reviewer.final_update_message": True,
                       "github.publish_as_check_run": False}.items():
        settings.set(key, value)
    body = "## PR Reviewer Guide 🔍\n\n### No major issues detected\n\n| A | B |\n|---|---|"
    comments = []
    def comment(text):
        return SimpleNamespace(body=text, user=SimpleNamespace(login="github-actions[bot]"),
                               html_url="https://example/comment/1")
    if existing:
        comments.append(comment(body))
    provider = object.__new__(GithubProvider)
    provider.get_files = lambda: [SimpleNamespace(filename="a.py")]
    provider.get_issue_comments = lambda: comments
    provider.get_issue_comments_newest_first = lambda: list(reversed(comments))
    provider.get_latest_commit_url = lambda: "https://github.com/owner/repo/commit/" + "b" * 40
    provider.get_comment_url = lambda item: item.html_url
    writes = []
    def publish(text, **kwargs):
        if failure == "api":
            raise RuntimeError("API unavailable")
        result = comment(text + ("tampered" if failure == "tamper" else ""))
        comments.append(result)
        writes.append("create")
        return result
    def edit(item, text):
        if failure == "api":
            raise RuntimeError("API unavailable")
        item.body = text + ("tampered" if failure == "tamper" else "")
        writes.append("edit")
    provider.publish_comment = publish
    provider.edit_comment = edit
    provider.remove_comment = lambda item: None
    reviewer = object.__new__(PRReviewer)
    reviewer.git_provider = provider
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.pr_url = "https://github.com/owner/repo/pull/7"
    reviewer.vars = {}
    reviewer.prediction = None
    async def prepare(*args, **kwargs):
        reviewer.prediction = "" if failure == "empty" else "validated prediction"
    reviewer._prepare_prediction = prepare
    reviewer._prepare_pr_review = lambda: body
    monkeypatch.setattr(pr_reviewer, "extract_and_cache_pr_tickets", AsyncMock())
    async def retry(callback, **kwargs):
        await callback("gemini/gemini-3.8-flash")
    monkeypatch.setattr(pr_reviewer, "retry_with_fallback_models", retry)
    frozen = ("owner/repo", 7, "a" * 40, "b" * 40)
    def fresh():
        if failure in {"head", "base"}:
            raise RuntimeError("Pull request base or head moved during review")
    namespace = _helpers()
    namespace.update(policy_attested=True, frozen=frozen, run_id="123", attempt="2",
                     policy_digest="policy", add_pr_review_identity=add_pr_review_identity,
                     _fresh=fresh, number=7, verification={"review": False},
                     repo_obj=SimpleNamespace(get_pull=lambda number:
                                              SimpleNamespace(get_issue_comments=lambda: comments)))
    node = next(node for node in ast.parse(_embedded_python()).body
                if isinstance(node, ast.FunctionDef) and node.name == "_verified_run")
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<workflow>", "exec"), namespace)
    run = namespace["_verified_run"]("review", PRReviewer.run)
    if failure:
        with pytest.raises(RuntimeError):
            asyncio.run(run(reviewer))
        assert namespace["verification"]["review"] is False
    else:
        asyncio.run(run(reviewer))
        assert namespace["verification"]["review"] is True
        assert ("edit" in writes) is existing
        verified = [item for item in comments if "graphify-pr-agent:review:" in item.body]
        assert len(verified) == 1
        assert "<!-- pr-agent:review:full -->" in verified[0].body


def test_actual_repo_settings_loader_preserves_gemini_and_safe_dispatch(monkeypatch):
    from pathlib import Path
    utils = importlib.import_module("pr_agent.git_providers.utils")
    policy = (Path(__file__).parents[1] / ".pr_agent.toml").read_bytes()
    monkeypatch.setattr(utils, "get_git_provider_with_context", lambda url:
                        SimpleNamespace(get_repo_settings=lambda: policy))
    settings = get_settings()
    settings.set("config.extra_config_url", "")
    settings.set("config.use_repo_settings_file", True)
    settings.set("config.fallback_models", ["old/model"], merge=False)
    utils.apply_repo_settings("https://github.com/owner/repo/pull/7")
    assert settings.config.model == "gemini/gemini-3.8-flash"
    assert settings.config.fallback_models == ["gemini/gemini-3.5-flash-lite"]
    assert settings.config.max_model_tokens == 262144
    assert settings.pr_reviewer.persistent_finding_state is False
    assert settings.pr_reviewer.enable_large_pr_chunking is False


@pytest.mark.parametrize("kind", ["review", "summary"])
def test_real_constructor_prediction_render_and_publication(monkeypatch, kind):
    """Keep constructor, prompts, prediction parsing and rendering real; mock IO."""
    from tests.test_pr_agent_policy import ROOT, VALID_DESCRIPTION, _file
    from unittest.mock import MagicMock
    module = importlib.import_module("pr_agent.tools.pr_reviewer" if kind == "review"
                                     else "pr_agent.tools.pr_description")
    tool_class = module.PRReviewer if kind == "review" else module.PRDescription
    settings = get_settings()
    utils = importlib.import_module("pr_agent.git_providers.utils")
    policy = (ROOT / ".pr_agent.toml").read_bytes()
    provider = object.__new__(GithubProvider)
    provider.pr = SimpleNamespace(title="Update a.py", url="https://api.github.com/repos/owner/repo/pulls/7")
    provider.repo = "owner/repo"
    provider.pr_num = 7
    provider.last_commit_id = SimpleNamespace(sha="b" * 40)
    provider.get_repo_settings = lambda: policy
    provider.get_languages = lambda: {"Python": 100}
    provider.get_files = lambda: [_file()]
    provider.get_diff_files = lambda: []
    provider.get_pr_branch = lambda: "upgrade"
    provider.get_pr_description = lambda **kwargs: ("Fix a.py", []) if kwargs.get("split_changes_walkthrough") else "Fix a.py"
    provider.get_user_description = lambda: "Fix a.py"
    provider.get_commit_messages = lambda: "Update a.py"
    provider.get_num_of_files = lambda: 1
    provider.get_repo_file_content = lambda path, **kwargs: (ROOT / path).read_text()
    provider.get_latest_commit_url = lambda: "https://github.com/owner/repo/commit/" + "b" * 40
    provider.get_pr_url = lambda: "https://github.com/owner/repo/pull/7"
    provider.get_pr_labels = lambda **kwargs: []
    provider.publish_labels = MagicMock()
    provider.get_recent_inline_comment_bodies = lambda: []
    provider.publish_code_suggestions = MagicMock()
    provider.is_supported = lambda capability: capability in {"gfm_markdown", "get_labels"}
    comments = []
    def publish(text, **kwargs):
        item = SimpleNamespace(body=text, user=SimpleNamespace(login="github-actions[bot]"))
        comments.append(item)
        return item
    provider.publish_comment = publish
    provider.get_issue_comments = lambda: comments
    provider.get_issue_comments_newest_first = lambda: list(reversed(comments))
    provider.remove_comment = lambda item: comments.remove(item)
    monkeypatch.setattr(utils, "get_git_provider_with_context", lambda url: provider)
    utils.apply_repo_settings(provider.get_pr_url())
    settings.set("config.is_auto_command", True)
    settings.set("config.publish_output", True)
    settings.set("config.propagate_tool_errors", True)
    settings.set("github.publish_as_check_run", False)
    monkeypatch.setattr(module, "get_git_provider_with_context", lambda url: provider)
    monkeypatch.setattr(module, "extract_and_cache_pr_tickets", AsyncMock())
    namespace = _helpers(converter=importlib.import_module(
        "pr_agent.algo.git_patch_processing").decouple_and_convert_to_hunks_with_lines_numbers)
    namespace.update(policy_attested=True, frozen=("owner/repo", 7, "a" * 40, "b" * 40),
                     run_id="123", attempt="2", policy_digest="policy",
                     add_pr_review_identity=add_pr_review_identity, _fresh=lambda: None,
                     number=7, verification={kind: False},
                     repo_obj=SimpleNamespace(get_pull=lambda number:
                                              SimpleNamespace(get_issue_comments=lambda: comments)))
    nodes: list[ast.stmt] = [node for node in ast.parse(_embedded_python()).body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name in {"_verified_run", "prepare_valid_description"}]
    namespace.update(original_prepare_description=getattr(tool_class, "_prepare_prediction"),
                     pr_description=module)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<workflow>", "exec"), namespace)
    def complete_diff(provider, handler, model, **kwargs):
        return namespace["_raw_diff"]([_file()], handler, model,
                                     numbered=kwargs.get("add_line_numbers_to_hunks", False))
    monkeypatch.setattr(module, "get_pr_diff", complete_diff)
    if kind == "summary":
        monkeypatch.setattr(tool_class, "_prepare_prediction", namespace["prepare_valid_description"])
    requests = []
    class AI:
        async def chat_completion(self, **kwargs):
            requests.append(kwargs)
            return (VALID_DESCRIPTION if kind == "summary" else
                    "review:\n  estimated_effort_to_review_[1-5]: 1\n  relevant_tests: Yes\n  security_concerns: No\n  key_issues_to_review: []\n"), "stop"
    tool = tool_class(provider.get_pr_url(), ai_handler=AI)
    asyncio.run(namespace["_verified_run"](kind, tool_class.run)(tool))
    assert namespace["verification"][kind] is True
    assert len(requests) == 1
    assert requests[0]["model"] == "gemini/gemini-3.8-flash"
    assert "old" in requests[0]["user"] and "new" in requests[0]["user"]
    assert "SECURITY.md" in tool.vars["repo_context"]


@pytest.mark.parametrize("action", ["opened", "reopened", "ready_for_review"])
def test_actual_action_runner_dispatches_review_without_summary(monkeypatch, tmp_path, action):
    import json
    from unittest.mock import MagicMock
    runner = importlib.import_module("pr_agent.servers.github_action_runner")
    test_actual_repo_settings_loader_preserves_gemini_and_safe_dispatch(monkeypatch)
    settings = get_settings()
    assert settings.github_action_config.auto_describe is False
    settings.set("github_action_config.auto_review", True)
    settings.set("github_action_config.auto_improve", False)
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"action": action, "pull_request": {
        "url": "https://api.github.com/repos/owner/repo/pulls/7"}}))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_TOKEN", "offline-test-token")
    reviewer = MagicMock(return_value=SimpleNamespace(run=AsyncMock()))
    description = MagicMock(side_effect=AssertionError("automatic description invoked"))
    suggestions = MagicMock(side_effect=AssertionError("automatic suggestions invoked"))
    monkeypatch.setattr(runner, "PRReviewer", reviewer)
    monkeypatch.setattr(runner, "PRDescription", description)
    monkeypatch.setattr(runner, "PRCodeSuggestions", suggestions)
    asyncio.run(runner.run_action())
    reviewer.assert_called_once()
    reviewer.return_value.run.assert_awaited_once()
    description.assert_not_called()
    suggestions.assert_not_called()


@pytest.mark.parametrize("model", ["gemini/gemini-3.8-flash", "gemini/gemini-3.5-flash-lite"])
def test_actual_litellm_handler_sends_high_reasoning_without_temperature(monkeypatch, model):
    algo = importlib.import_module("pr_agent.algo")
    handler_module = importlib.import_module("pr_agent.algo.ai_handlers.litellm_ai_handler")
    litellm = importlib.import_module("litellm")
    # Run exactly the workflow's model metadata adapter, before handler construction.
    prefix = _embedded_python().split("def _valid_int", 1)[0]
    exec(compile(prefix, "<workflow-model-adapter>", "exec"), {})
    settings = get_settings()
    settings.set("config.reasoning_effort", "high")
    settings.set("config.max_model_tokens", 262144)
    captured = []
    async def completion(**kwargs):
        mapped = litellm.utils.get_optional_params(
            model=kwargs["model"].split("/", 1)[1], custom_llm_provider="gemini",
            reasoning_effort=kwargs["reasoning_effort"])
        assert mapped["thinkingConfig"]["thinkingLevel"] == "high"
        captured.append(kwargs)
        return litellm.ModelResponse(choices=[{"message": {"role": "assistant", "content": "ok"},
                                               "finish_reason": "stop"}])
    monkeypatch.setattr(handler_module, "acompletion", completion)
    handler = handler_module.LiteLLMAIHandler()
    result = asyncio.run(handler.chat_completion(model, "system", "user"))
    assert result == ("ok", "stop")
    assert captured[0]["model"] == model
    assert captured[0]["reasoning_effort"] == "high"
    assert "temperature" not in captured[0]
    assert algo.MAX_TOKENS["gemini/gemini-3.8-flash"] == 1048576
    assert importlib.import_module("pr_agent.algo.utils").get_max_tokens(model) == 262144


@pytest.mark.parametrize("year,factor", [(2026, 1), (2027, 2)])
def test_missing_model_registration_uses_official_price_boundary(monkeypatch, year, factor):
    import datetime
    litellm = importlib.import_module("litellm")
    captured = []
    class Clock(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(year, 1, 1, tzinfo=tz)
    monkeypatch.setattr(datetime, "datetime", Clock)
    monkeypatch.setattr(litellm, "model_cost", {})
    monkeypatch.setattr(litellm, "register_model", lambda entry: captured.append(entry))
    prefix = _embedded_python().split("def _valid_int", 1)[0]
    exec(compile(prefix, "<workflow-model-adapter>", "exec"), {})
    metadata = captured[0]["gemini/gemini-3.8-flash"]
    assert metadata["input_cost_per_token"] == 0.75e-6 * factor
    assert metadata["output_cost_per_token"] == 3.75e-6 * factor
    assert metadata["cache_read_input_token_cost"] == 0.075e-6 * factor
    monkeypatch.setattr(litellm, "model_cost", {"gemini/gemini-3.8-flash": {"future": "metadata"}})
    exec(compile(prefix, "<workflow-model-adapter>", "exec"), {})
    assert len(captured) == 1  # Never replace upstream's known metadata.
