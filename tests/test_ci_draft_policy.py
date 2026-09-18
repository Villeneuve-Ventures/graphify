"""Keep draft transitions and CI cancellation in the same PR workflow."""

from pathlib import Path

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
    for job in workflow["jobs"].values():
        assert job["if"] == (
            "${{ github.event_name != 'pull_request' || "
            "github.event.pull_request.draft == false }}"
        )
