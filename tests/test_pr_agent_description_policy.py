"""Exercise the actual PR-Agent entrypoint without calling a provider or container."""

import json
import os
from pathlib import Path
import shlex
import tomllib

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
COMMENT_COMMAND = "/describe --pr_description.publish_description_as_comment=true"


class _ExecIntercepted(Exception):
    pass


def _run_entrypoint(tmp_path, monkeypatch, body):
    """Run the workflow's Python with a private event and intercept its final exec."""
    workflow = yaml.load(
        (ROOT / ".github/workflows/pr-agent.yml").read_text(), Loader=yaml.BaseLoader,
    )
    step = next(
        step for step in workflow["jobs"]["command"]["steps"]
        if step.get("name") == "Run upstream PR-Agent command"
    )
    arguments = shlex.split(step["with"]["args"])
    assert arguments[0] == "-c" and len(arguments) == 2
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({
        "comment": {"body": body, "id": 123},
        "issue": {"number": 42, "pull_request": {}},
    }) + "\n")
    original = event_path.read_bytes()
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    calls = []

    def intercept(path, argv):
        calls.append((path, argv))
        raise _ExecIntercepted

    monkeypatch.setattr(os, "execv", intercept)
    exit_code = None
    try:
        exec(compile(arguments[1], str(ROOT / ".github/workflows/pr-agent.yml"), "exec"), {})
    except _ExecIntercepted:
        pass
    except SystemExit as error:
        exit_code = error.code
    return event_path, original, calls, exit_code


@pytest.mark.parametrize("command", [
    "/describe", "/describe_pr", "/DESCRIBE", "/Describe_PR",
    "/describe ", "/describe_pr\t", "/describe\n", "/describe_pr \n\t",
])
def test_description_aliases_force_comment_only_output(tmp_path, monkeypatch, command):
    event_path, _, calls, exit_code = _run_entrypoint(tmp_path, monkeypatch, command)
    assert exit_code is None
    assert calls == [("/entrypoint.sh", ["/entrypoint.sh"])]
    assert json.loads(event_path.read_text()) == {
        "comment": {"body": COMMENT_COMMAND, "id": 123},
        "issue": {"number": 42, "pull_request": {}},
    }


@pytest.mark.parametrize("command", [
    "//describe", "///DESCRIBE_PR", '/"describe"', '/"describe_pr"',
    '/des"cribe"', '/describe_"pr"',
])
@pytest.mark.parametrize("argument", ["", " --pr_description.publish_description_as_comment=false"])
def test_normalized_description_aliases_cannot_bypass_comment_policy(
    tmp_path, monkeypatch, command, argument,
):
    event_path, original, calls, exit_code = _run_entrypoint(
        tmp_path, monkeypatch, command + argument,
    )
    if argument:
        assert exit_code not in (None, 0)
        assert "no arguments" in str(exit_code)
        assert calls == []
        assert event_path.read_bytes() == original
    else:
        assert exit_code is None
        assert calls == [("/entrypoint.sh", ["/entrypoint.sh"])]
        assert json.loads(event_path.read_text())["comment"]["body"] == COMMENT_COMMAND


@pytest.mark.parametrize("argument", [
    "--pr_description.publish_description_as_comment=false",
    "--pr_description.publish_description_as_comment=true",
    "--pr_description__publish_description_as_comment=false",
    "--pr_description={publish_description_as_comment: false}",
    "'--pr_description.publish_description_as_comment=false'",
    '"--pr_description.publish_description_as_comment=false"',
    "\n--pr_description.publish_description_as_comment=false",
    "--pr_description.use_description_markers=true",
    "-i", "please explain", "''",
])
@pytest.mark.parametrize("command", ["/describe", "/DESCRIBE_PR"])
def test_description_arguments_fail_before_exec_or_event_mutation(
    tmp_path, monkeypatch, command, argument,
):
    event_path, original, calls, exit_code = _run_entrypoint(
        tmp_path, monkeypatch, f"{command} {argument}",
    )
    assert exit_code not in (None, 0)
    assert "no arguments" in str(exit_code)
    assert calls == []
    assert event_path.read_bytes() == original


@pytest.mark.parametrize("command", ['/describe "', '/de"scribe'])
def test_invalid_description_quoting_fails_before_exec_or_mutation(tmp_path, monkeypatch, command):
    event_path, original, calls, exit_code = _run_entrypoint(tmp_path, monkeypatch, command)
    assert exit_code not in (None, 0)
    assert "Invalid command quoting" in str(exit_code)
    assert calls == []
    assert event_path.read_bytes() == original


@pytest.mark.parametrize("command", ["/review", "/REVIEW -i", "/review\n--anything", '/review "'])
def test_direct_review_remains_a_noop(tmp_path, monkeypatch, command):
    event_path, original, calls, exit_code = _run_entrypoint(tmp_path, monkeypatch, command)
    assert exit_code == 0
    assert calls == []
    assert event_path.read_bytes() == original


@pytest.mark.parametrize("command,expected", [
    ("/prreview", "/review"),
    ("/PRREVIEW -i", "/review -i"),
    ("/prreview\n--config.reasoning_effort=high", "/review\n--config.reasoning_effort=high"),
])
def test_prreview_options_are_preserved(tmp_path, monkeypatch, command, expected):
    event_path, _, calls, exit_code = _run_entrypoint(tmp_path, monkeypatch, command)
    assert exit_code is None
    assert calls == [("/entrypoint.sh", ["/entrypoint.sh"])]
    assert json.loads(event_path.read_text())["comment"]["body"] == expected


@pytest.mark.parametrize("command", [
    "/describe_more", "/describe_pr_more", "/describe-more", "/describefix",
    "/ask What changed?", "/improve", "/help", "/prreview_more",
    '/"improve"', '/ask an unmatched " quote',
])
def test_unrelated_commands_are_forwarded_without_mutation(tmp_path, monkeypatch, command):
    event_path, original, calls, exit_code = _run_entrypoint(tmp_path, monkeypatch, command)
    assert exit_code is None
    assert calls == [("/entrypoint.sh", ["/entrypoint.sh"])]
    assert event_path.read_bytes() == original


def test_repository_default_keeps_generated_descriptions_in_comments():
    settings = tomllib.loads((ROOT / ".pr_agent.toml").read_text())
    assert settings["pr_description"]["publish_description_as_comment"] is True
