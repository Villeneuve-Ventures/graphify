"""Keep draft transitions and CI cancellation scoped to one pull request."""

from pathlib import Path

import yaml


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
WORKSPACE_BRANCHES = [
    "v1",
    "v2",
    "v3",
    "v4",
    "v5",
    "v6",
    "v7",
    "v8",
    "main",
    "baseline/**",
    "workspace/**",
]
DRAFT_GUARD = (
    "${{ github.event_name != 'pull_request' || github.event.pull_request.draft == false }}"
)


def _workflow() -> dict:
    return yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)


def test_pull_request_transitions_preserve_workspace_triggers():
    triggers = _workflow()["on"]

    assert triggers["push"]["branches"] == WORKSPACE_BRANCHES
    assert triggers["pull_request"] == {
        "types": [
            "opened",
            "synchronize",
            "reopened",
            "ready_for_review",
            "converted_to_draft",
        ],
        "branches": WORKSPACE_BRANCHES,
    }
    assert triggers["workflow_dispatch"] == ""


def test_pull_request_runs_supersede_only_the_same_pull_request():
    assert _workflow()["concurrency"] == {
        "group": (
            "ci-${{ github.workflow }}-${{ github.event_name }}-"
            "${{ github.event.pull_request.number || github.run_id }}"
        ),
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }


def test_every_workspace_job_skips_draft_pull_requests():
    jobs = _workflow()["jobs"]

    assert set(jobs) == {"skillgen-check", "test", "security-scan"}
    assert all(job.get("if") == DRAFT_GUARD for job in jobs.values())
