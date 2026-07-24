from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


def _workflow() -> str:
    return (ROOT / ".github" / "workflows" / "pr-agent.yml").read_text()


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
    assert (
        'pr_description.publish_description_as_comment_persistent: "true"'
        in workflow
    )

    assert config["github_action_config"]["pr_actions"] == [
        "opened",
        "reopened",
        "ready_for_review",
    ]
    assert config["github_action_config"]["handle_push_trigger"] is False
    assert config["pr_description"]["publish_description_as_comment"] is True
    assert config["pr_description"]["publish_description_as_comment_persistent"] is True
    assert config["pr_reviewer"]["persistent_comment"] is True


def test_prreview_command_requests_a_published_incremental_review() -> None:
    workflow = _workflow()
    config = _config()

    assert "github.event.comment.body == '/prreview'" in workflow
    assert 'request = "/review -i"' in workflow
    assert 'verification["incremental_review"] = requested_incremental' in workflow
    assert "github_action_config.push_commands" not in workflow
    assert config["pr_reviewer"]["publish_output_no_suggestions"] is True
    assert config["pr_reviewer"]["final_update_message"] is True
