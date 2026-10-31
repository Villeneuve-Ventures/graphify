"""Keep draft transitions and CI cancellation in the same PR workflow."""

from pathlib import Path

import pytest
import yaml


def test_draft_transition_cancels_only_the_same_pull_requests_ci():
    workflow = yaml.load(
        (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert "converted_to_draft" in workflow["on"]["pull_request"]["types"]
    assert "ready_for_review" in workflow["on"]["pull_request"]["types"]
    assert workflow["concurrency"] == {
        "group": (
            "ci-${{ github.workflow }}-${{ github.event_name }}-"
            "${{ github.event.pull_request.number || github.run_id }}"
        ),
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }
    for name, job in workflow["jobs"].items():
        draft_condition = (
            "${{ github.event_name != 'pull_request' || "
            "(github.event.pull_request.user.login != 'dependabot[bot]' && "
            "github.event.pull_request.draft == false) }}"
        )
        if name == "quality":
            assert job["if"] == (
                "${{ always() && (github.event_name != 'pull_request' || "
                "(github.event.pull_request.user.login != 'dependabot[bot]' && "
                "github.event.pull_request.draft == false)) }}"
            )
        else:
            assert job["if"] == draft_condition


@pytest.mark.parametrize("event,author,draft,expected", [
    ("pull_request", "human", False, True),
    ("pull_request", "human", True, False),
    ("pull_request", "dependabot[bot]", False, False),
    ("pull_request", "dependabot[bot]", True, False),
    ("push", "dependabot[bot]", True, True),
    ("workflow_dispatch", "dependabot[bot]", True, True),
])
def test_normal_ci_routes_humans_and_keeps_dependabot_in_opt_in_lane(event, author, draft, expected):
    workflow = yaml.load(
        (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    for name, job in workflow["jobs"].items():
        condition = job["if"].removeprefix("${{").removesuffix("}}").strip()
        for key, value in {
            "github.event_name": event,
            "github.event.pull_request.user.login": author,
            "github.event.pull_request.draft": draft,
            "always()": True,
            "false": False,
        }.items():
            condition = condition.replace(key, repr(value))
        # These guards use only comparisons and boolean operators shared with Python.
        condition = condition.replace("&&", " and ").replace("||", " or ")
        assert eval(condition, {"__builtins__": {}}) is expected, name
