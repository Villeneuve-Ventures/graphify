import ast
from pathlib import Path
from types import SimpleNamespace
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def _workflow() -> str:
    return (ROOT / ".github" / "workflows" / "pr-agent.yml").read_text()


def _embedded_python() -> str:
    lines = _workflow().splitlines()
    start = next(index for index, line in enumerate(lines) if line.strip() == "python - <<'PY'") + 1
    end = next(index for index in range(start, len(lines)) if lines[index].strip() == "PY")
    indent = len(lines[start]) - len(lines[start].lstrip())
    return "\n".join(line[indent:] if line.strip() else "" for line in lines[start:end])


def _embedded_policy_namespace() -> dict:
    tree = ast.parse(_embedded_python())
    helper_names = {
        "_marked_publication",
        "_publication_visible",
        "_review_marker_sha",
        "_set_sha_bound_incremental_commits",
        "_workflow_comment",
    }
    body: list[ast.stmt] = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.Import)
            and any(alias.name in {"hashlib", "re"} for alias in node.names)
        )
        or (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "REVIEW_MARKER_RE"
                for target in node.targets
            )
        )
        or (isinstance(node, ast.FunctionDef) and node.name in helper_names)
    ]
    namespace: dict = {}
    module = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    exec(compile(module, "<pr-agent-policy>", "exec"), namespace)
    return namespace


def _config() -> dict:
    with (ROOT / ".pr_agent.toml").open("rb") as config_file:
        return tomllib.load(config_file)


def test_initial_non_draft_event_publishes_summary_and_review() -> None:
    workflow = _workflow()
    config = _config()

    assert "types: [opened, reopened, ready_for_review]" in workflow
    assert "review_requested" not in workflow
    assert "synchronize" not in workflow
    assert "github.event.pull_request.draft == false" in workflow
    assert 'github_action_config.auto_describe: "true"' in workflow
    assert 'github_action_config.auto_review: "true"' in workflow
    assert 'github_action_config.handle_push_trigger: "false"' in workflow
    assert 'pr_description.publish_description_as_comment: "true"' in workflow
    assert 'pr_description.publish_description_as_comment_persistent: "true"' in workflow

    assert config["github_action_config"]["pr_actions"] == [
        "opened",
        "reopened",
        "ready_for_review",
    ]
    assert config["github_action_config"]["handle_push_trigger"] is False
    assert config["pr_description"]["publish_description_as_comment"] is True
    assert config["pr_description"]["publish_description_as_comment_persistent"] is True
    assert config["pr_reviewer"]["persistent_comment"] is True


def test_runtime_and_review_policy_follow_target_python_support() -> None:
    workflow = _workflow()
    config = _config()
    instructions = config["pr_reviewer"]["extra_instructions"]

    assert 'python-version: "3.14"' in workflow
    assert (
        "git+https://github.com/the-pr-agent/pr-agent.git@570f67ed5fc8db5be74c18df070bc20079b64b0d"
    ) in workflow
    assert "qodo-ai/pr-agent" not in workflow
    assert "from pr_agent.servers.github_action_runner import _run_action_and_drain" in workflow
    assert "asyncio.run(_run_action_and_drain())" in workflow
    assert config["config"]["max_model_tokens"] == 65536
    assert "custom_model_max_tokens" not in config["config"]
    assert "target branch's requires-python declaration" in instructions
    assert "PEP 758" in instructions
    assert "Preserve Python 3.10 compatibility" not in instructions


def test_ineligible_pull_request_runs_use_isolated_concurrency_groups() -> None:
    workflow = _workflow()
    concurrency = workflow.split("concurrency:", 1)[1].split("\njobs:", 1)[0]

    eligible_pull_request_group = """
      case(
        github.event_name == 'pull_request' &&
        github.event.sender.type != 'Bot' &&
        github.event.pull_request.draft == false &&
        github.event.pull_request.head.repo.full_name == github.repository,
        github.event.pull_request.number,
"""
    trusted_prreview_group = """
        github.event_name == 'issue_comment' &&
        github.event.sender.type != 'Bot' &&
        github.event.issue.pull_request &&
        github.event.comment.body == '/prreview' &&
        contains(
          fromJSON('[\"OWNER\",\"MEMBER\",\"COLLABORATOR\"]'),
          github.event.comment.author_association
        ),
        github.event.issue.number,
"""
    assert eligible_pull_request_group in concurrency
    assert trusted_prreview_group in concurrency
    assert "\n      github.event.pull_request.number ||\n" not in concurrency
    assert "&&\n        github.event.pull_request.number" not in concurrency
    assert "&&\n        github.event.issue.number" not in concurrency
    assert "format('run-{0}', github.run_id)" in concurrency
    assert "\n      github.run_id\n" not in concurrency
    assert "cancel-in-progress: false" in concurrency


def test_prreview_command_requests_a_published_incremental_review() -> None:
    workflow = _workflow()
    config = _config()

    assert "github.event.comment.body == '/prreview'" in workflow
    assert 'request = "/review -i"' in workflow
    assert '"baseline_full" if requested_incremental else "initial_full"' in workflow
    assert 'verification["review_mode"] = review_mode' in workflow
    assert "github_action_config.push_commands" not in workflow
    assert config["pr_reviewer"]["publish_output_no_suggestions"] is True
    assert config["pr_reviewer"]["final_update_message"] is True


def test_incremental_range_uses_an_authenticated_reviewed_head() -> None:
    helpers = _embedded_policy_namespace()
    marked_publication = helpers["_marked_publication"]
    set_incremental = helpers["_set_sha_bound_incremental_commits"]

    reviewed_sha = "1" * 40
    new_sha = "2" * 40
    marker_provider = SimpleNamespace(last_commit_id=SimpleNamespace(sha=reviewed_sha))
    marked_body = marked_publication(
        ("## PR Reviewer Guide 🔍\nreview",), {}, marker_provider, "review"
    )[0][0]
    trusted_review = SimpleNamespace(
        body=marked_body,
        user=SimpleNamespace(login="github-actions[bot]"),
    )
    forged_review = SimpleNamespace(
        body=marked_body.replace(reviewed_sha, new_sha),
        user=SimpleNamespace(login="untrusted-author"),
    )
    reviewed_commit = SimpleNamespace(sha=reviewed_sha, files=[])
    new_file = SimpleNamespace(filename="new.py")
    old_authored_new_commit = SimpleNamespace(sha=new_sha, files=[new_file])

    class Provider:
        pr_commits = [reviewed_commit, old_authored_new_commit]
        previous_review = None
        pr = SimpleNamespace(
            get_issue_comments=lambda: [trusted_review, forged_review],
            get_commits=lambda: [],
        )
        incremental = SimpleNamespace(
            is_incremental=True,
            commits_range=None,
            first_new_commit=None,
            last_seen_commit=None,
        )
        unreviewed_files_map = {}

    provider = Provider()
    set_incremental(provider)

    assert provider.previous_review is trusted_review
    assert provider.incremental.last_seen_commit is reviewed_commit
    assert provider.incremental.commits_range == [old_authored_new_commit]
    assert provider.unreviewed_files_map == {"new.py": new_file}


def test_incremental_without_a_verified_baseline_falls_back_to_full() -> None:
    set_incremental = _embedded_policy_namespace()["_set_sha_bound_incremental_commits"]

    class Provider:
        pr_commits = [SimpleNamespace(sha="1" * 40, files=[])]
        pr = SimpleNamespace(
            get_issue_comments=lambda: [],
            get_commits=lambda: [],
        )
        incremental = SimpleNamespace(is_incremental=True)
        unreviewed_files_map = {}

    provider = Provider()
    set_incremental(provider)

    assert provider.incremental.is_incremental is False


def test_publication_requires_a_workflow_owned_marker() -> None:
    helpers = _embedded_policy_namespace()
    marked_publication = helpers["_marked_publication"]
    publication_visible = helpers["_publication_visible"]

    provider = SimpleNamespace(last_commit_id=SimpleNamespace(sha="1" * 40))
    args, _, marker = marked_publication(("## Title\nsummary",), {}, provider, "summary")
    marked_body = args[0]
    forged = SimpleNamespace(
        body=marked_body,
        user=SimpleNamespace(login="untrusted-author"),
    )
    published = SimpleNamespace(
        body=marked_body,
        user=SimpleNamespace(login="github-actions[bot]"),
    )

    provider.get_issue_comments = lambda: [forged]
    assert publication_visible(provider, marker) is False
    provider.get_issue_comments = lambda: [forged, published]
    assert publication_visible(provider, marker) is True


def test_embedded_python_compiles() -> None:
    compile(_embedded_python(), ".github/workflows/pr-agent.yml", "exec")
