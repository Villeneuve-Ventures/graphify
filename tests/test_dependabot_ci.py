"""Execute Dependabot approval checks with stale events and current API responses."""

import copy
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/dependabot-ci.yml"
HEAD = "a" * 40
VALIDATORS = {"skillgen-check", "test", "quality", "security-scan", "leiden-binary-smoke"}


def workflow():
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


def live_step(job):
    return next(step for step in workflow()["jobs"][job]["steps"]
                if step["name"] == "Check current Dependabot approval")


def run_live_check(step, response, *, api_exit=0, output=os.devnull):
    # Intercept the API, but execute the workflow's actual shell and Python.
    harness = '''gh() {
        [ "$#" -eq 2 ] && [ "$1" = api ] &&
            [ "$2" = repos/example/graphify/pulls/253 ] || return 99
        printf '%s' "$API_RESPONSE"
        return "$API_EXIT"
    }
    '''
    env = {**os.environ, "GH_REPO": "example/graphify", "PR_NUMBER": "253",
           "PR_HEAD_SHA": HEAD, "API_RESPONSE": response, "API_EXIT": str(api_exit),
           # A rerun retains this approved event even after live approval is removed.
           "GITHUB_EVENT_NAME": "pull_request", "GITHUB_RUN_ATTEMPT": "2",
           "HAS_RUN_CI": "true", "GITHUB_OUTPUT": str(output)}
    return subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail",
                           "-c", harness + step["run"]], env=env,
                          capture_output=True, text=True, timeout=10)


@pytest.fixture
def approved_pr():
    return {"user": {"login": "dependabot[bot]"}, "state": "open", "draft": False,
            "head": {"sha": HEAD}, "labels": [{"name": "run-ci"}]}


@pytest.mark.parametrize("job", ["admission", "dependabot-ci-gate"])
def test_rerun_rejects_revoked_live_approval(job, approved_pr):
    step = live_step(job)
    assert run_live_check(step, json.dumps(approved_pr)).returncode == 0
    approved_pr["labels"] = []
    result = run_live_check(step, json.dumps(approved_pr))
    assert result.returncode != 0
    assert "does not authorize CI" in result.stderr


@pytest.mark.parametrize("job", ["admission", "dependabot-ci-gate"])
@pytest.mark.parametrize("change", ["head", "draft", "author", "closed"])
def test_live_pr_identity_and_state_are_required(job, change, approved_pr):
    if change == "head":
        approved_pr["head"]["sha"] = "b" * 40
    elif change == "draft":
        approved_pr["draft"] = True
    elif change == "author":
        approved_pr["user"]["login"] = "human"
    else:
        approved_pr["state"] = "closed"
    assert run_live_check(live_step(job), json.dumps(approved_pr)).returncode != 0


@pytest.mark.parametrize("job", ["admission", "dependabot-ci-gate"])
@pytest.mark.parametrize("response,api_exit", [("", 1), ("not json", 0), ("{}", 0)])
def test_live_lookup_fails_closed(job, response, api_exit):
    assert run_live_check(live_step(job), response, api_exit=api_exit).returncode != 0


@pytest.mark.parametrize("job", ["admission", "dependabot-ci-gate"])
def test_api_failure_cannot_pass_with_an_approved_response(job, approved_pr):
    assert run_live_check(live_step(job), json.dumps(approved_pr), api_exit=1).returncode != 0


def test_validation_and_gate_require_live_admission():
    jobs = workflow()["jobs"]
    for name in VALIDATORS:
        assert "admission" in jobs[name]["needs"]
        assert "needs.admission.result == 'success'" in jobs[name]["if"]
        assert "needs.admission.outputs.attempt == github.run_attempt" in jobs[name]["if"]
        assert "labels" not in jobs[name]["if"]
    gate = jobs["dependabot-ci-gate"]
    assert set(gate["needs"]) == VALIDATORS | {"admission"}
    assert gate["steps"][0] == live_step("dependabot-ci-gate")
    assert "always()" in gate["steps"][0]["if"]
    assert "github.event.pull_request.user.login == 'dependabot[bot]'" in gate["steps"][0]["if"]
    for name in ("admission", "dependabot-ci-gate"):
        assert jobs[name]["permissions"] == {"pull-requests": "read"}
        step = live_step(name)
        assert step["env"] == {
            "GH_TOKEN": "${{ github.token }}", "GH_REPO": "${{ github.repository }}",
            "PR_NUMBER": "${{ github.event.pull_request.number }}",
            "PR_HEAD_SHA": "${{ github.event.pull_request.head.sha }}"}
        assert not any("uses" in step for step in jobs[name]["steps"])


@pytest.mark.parametrize("failed_job", [None, *sorted(VALIDATORS | {"admission"})])
@pytest.mark.parametrize("state", ["failure", "skipped", "cancelled"])
def test_final_gate_requires_all_successful_results(failed_job, state):
    gate = workflow()["jobs"]["dependabot-ci-gate"]
    step = next(step for step in gate["steps"]
                if step["name"] == "Require approval and successful CI for Dependabot")
    results = {name: {"result": "success"} for name in gate["needs"]}
    if failed_job:
        results[failed_job]["result"] = state
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step["run"]],
                            env={**os.environ, "PR_AUTHOR": "dependabot[bot]",
                                 "CI_RESULTS": json.dumps(results)},
                            capture_output=True, text=True, timeout=10)
    assert (result.returncode == 0) == (failed_job is None)


def test_validation_job_bodies_remain_equal_to_original_ci():
    original = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    for name in VALIDATORS:
        old = copy.deepcopy(original["jobs"][name])
        new = copy.deepcopy(workflow()["jobs"][name])
        for job in (old, new):
            job.pop("if", None)
            job.pop("needs", None)
        assert old == new


def test_admission_emits_current_attempt_only_after_live_approval(tmp_path, approved_pr):
    jobs = workflow()["jobs"]
    step = live_step("admission")
    assert step["id"] == "approval"
    assert jobs["admission"]["outputs"] == {
        "attempt": "${{ steps.approval.outputs.attempt }}"}
    output = tmp_path / "output"
    assert run_live_check(step, json.dumps(approved_pr), output=output).returncode == 0
    assert output.read_text() == "attempt=2\n"
    output.unlink()
    approved_pr["labels"] = []
    assert run_live_check(step, json.dumps(approved_pr), output=output).returncode != 0
    assert not output.exists()
