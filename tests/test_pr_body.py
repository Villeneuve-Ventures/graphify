"""Exercise author input, live metadata and the trusted metadata-only workflow."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import NoReturn

import pytest
import yaml

from tools import pr_body


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / ".github/pull_request_template.md").read_text(encoding="utf-8")
REPOSITORY = "Villeneuve-Ventures/graphify"
HEAD = "a" * 40


def _body():
    return """## 📝 Description
Explain why the PR-body check runs separately from product validation.

## 🔗 Related Issues / Tickets
None

## 🧪 How Has This Been Tested?
- [x] **Validation route:** `make fast-feedback` — contract-docs; complete receipt attached.
- [x] **Focused tests:** N/A — the selected docs route covers this wording change.
- [x] **Manual or smoke testing:** Not run — there is no user interface change.
"""


def _pr(body=None):
    return {
        "number": 123, "state": "open", "body": _body() if body is None else body,
        "draft": False, "user": {"login": "author", "id": 1234, "type": "User"},
        "head": {"sha": HEAD, "ref": "codex/body-check", "repo": {"full_name": REPOSITORY}},
        "base": {"ref": "v8", "repo": {"full_name": REPOSITORY}},
    }


def _event():
    return {"number": 123, "repository": {"full_name": REPOSITORY},
            "pull_request": {"head": {"sha": HEAD}, "body": "Stale event body"}}


def _check(first, second=None, event=None):
    snapshots = iter([first, deepcopy(first) if second is None else second])
    calls = []

    def fetch(repository, number):
        calls.append((repository, number))
        return next(snapshots)

    result = pr_body.check_pull_request(event or _event(), REPOSITORY, TEMPLATE, fetch)
    assert calls == [(REPOSITORY, 123)] * 2
    return result


def test_complete_docs_body_with_honest_skips_and_optional_sections_removed():
    assert pr_body.validate_body(_body(), TEMPLATE) == []
    assert pr_body.validate_body(_body() + "\n## Appendix\nHistorical context.\n", TEMPLATE) == []


def test_filling_the_original_template_preserves_its_route_field_contract():
    body = TEMPLATE.replace("<!-- Add details here. -->", "Explain the behavior.", 1)
    body = body.replace("<!-- Add details here. -->", "None", 1)
    body = body.replace("selected lane, complete receipt and result:",
                        "selected lane, complete receipt and result: contract-docs; passed; receipt attached")
    body = body.replace("`uv run --frozen pytest tests/<affected_test>.py -q --tb=short` — result:",
                        "N/A — selected docs checks cover the change")
    body = body.replace("steps and result:", "Not run — no runtime change")
    assert pr_body.validate_body(body, TEMPLATE) == []


@pytest.mark.parametrize("body", ["", " ", TEMPLATE, TEMPLATE.replace("[ ]", "[x]"),
                                  "<!--\n" + _body() + "\n-->",
                                  "```markdown\n" + _body() + "```",
                                  "~~~markdown\n" + _body() + "~~~",
                                  "\n".join("> " + line for line in _body().splitlines())])
def test_empty_template_and_quoted_examples_cannot_supply_a_completed_body(body):
    assert pr_body.validate_body(body, TEMPLATE)


@pytest.mark.parametrize("placeholder", ["TODO", "TBD: fill this", "Add details here.", "N/A"])
def test_description_placeholders_are_not_answers(placeholder):
    body = _body().replace("Explain why the PR-body check runs separately from product validation.",
                           placeholder)
    assert "Fill in section: description" in pr_body.validate_body(body, TEMPLATE)


@pytest.mark.parametrize("example", [
    "```markdown\n```", "~~~python\n~~~",
    "```markdown\nAdd details here.\n```", "> Add details here.",
])
def test_description_examples_cannot_supply_missing_answers(example):
    body = _body().replace("Explain why the PR-body check runs separately from product validation.",
                           example)
    assert "Fill in section: description" in pr_body.validate_body(body, TEMPLATE)


def test_description_examples_can_follow_a_real_answer():
    body = _body().replace("Explain why the PR-body check runs separately from product validation.",
                           "Run the body check separately.\n```markdown\nAdd details here.\n```")
    assert pr_body.validate_body(body, TEMPLATE) == []


@pytest.mark.parametrize("field,unfinished", [
    ("Validation route", "`make fast-feedback`"),
    ("Focused tests", "`uv run --frozen pytest tests/<affected_test>.py -q --tb=short` — result:"),
    ("Manual or smoke testing", "N/A"),
])
def test_included_fields_need_a_result_or_an_explained_omission(field, unfinished):
    lines = _body().splitlines()
    body = "\n".join(f"- [x] **{field}:** {unfinished}" if f"**{field}:**" in line else line
                     for line in lines)
    assert any(field.casefold() in error for error in pr_body.validate_body(body, TEMPLATE))


def test_comment_instructions_and_fenced_headings_do_not_reset_real_sections():
    body = _body().replace("None", "<!-- author instructions -->\nNone", 1)
    body = body.replace("None", "```markdown\n## Description\nExample only\n```\nNone", 1)
    assert pr_body.validate_body(body, TEMPLATE) == []
    assert pr_body.validate_body(_body() + TEMPLATE, TEMPLATE) == []
    assert pr_body.validate_body(TEMPLATE + _body(), TEMPLATE)


def test_core_headings_follow_the_template_with_optional_emoji_and_case():
    body = _body().replace("📝 Description", "DESCRIPTION").replace("🔗 ", "").replace("🧪 ", "")
    assert pr_body.validate_body(body, TEMPLATE) == []
    with pytest.raises(ValueError, match="Canonical template"):
        pr_body.validate_body(body, "## Different template\n")


def test_missing_testing_route_is_reported():
    body = "\n".join(line for line in _body().splitlines() if "**Validation route:**" not in line)
    assert any("populated" in error for error in pr_body.validate_body(body, TEMPLATE))


def test_multiline_route_receipt_counts_but_fenced_field_examples_do_not():
    body = _body().replace("contract-docs; complete receipt attached.",
                           '\n  ```json\n  {"lane": "contract-docs", "status": "complete"}\n  ```')
    assert pr_body.validate_body(body, TEMPLATE) == []
    route = next(line for line in _body().splitlines() if "**Validation route:**" in line)
    body = _body().replace(route, f"```markdown\n{route}\n```")
    assert any("populated" in error for error in pr_body.validate_body(body, TEMPLATE))


def test_later_testing_field_cannot_fill_an_empty_first_field():
    route = next(line for line in _body().splitlines() if "**Validation route:**" in line)
    body = _body().replace(route, "- [ ] **Validation route:**\n" + route)
    assert any("validation route" in error for error in pr_body.validate_body(body, TEMPLATE))


def test_multiline_pending_outcome_still_requires_draft_state():
    body = _body().replace("contract-docs; complete receipt attached.", "\n  Pending — run after editing")
    assert pr_body.validate_body(body, TEMPLATE, draft=True) == []
    assert any("Resolve pending" in error for error in pr_body.validate_body(body, TEMPLATE))


@pytest.mark.parametrize("omission", ["Not run", "Skipped", "Not tested", "N/A —"])
def test_bare_omissions_need_explanations(omission):
    body = _body().replace("Not run — there is no user interface change.", omission)
    assert any("manual or smoke testing" in error for error in pr_body.validate_body(body, TEMPLATE))


def test_pending_evidence_is_explicitly_limited_to_drafts():
    body = _body().replace("contract-docs; complete receipt attached.",
                           "Pending — run the complete route after the final edit.")
    assert pr_body.validate_body(body, TEMPLATE, draft=True) == []
    assert any("Resolve pending" in error for error in pr_body.validate_body(body, TEMPLATE))
    assert "passed" in _check({**_pr(body), "draft": True})
    with pytest.raises(ValueError, match="Resolve pending"):
        _check(_pr(body))


@pytest.mark.parametrize("answer", [
    "full; Pending — run after the final edit.",
    "contract-docs: Pending — run after the final edit.",
    "editorial-docs — Pending — run after the final edit.",
    "`full`; Pending — run after the final edit.",
])
def test_lane_prefixed_pending_evidence_requires_draft_state(answer):
    body = _body().replace("contract-docs; complete receipt attached.", answer)
    assert pr_body.validate_body(body, TEMPLATE, draft=True) == []
    assert any("Resolve pending" in error for error in pr_body.validate_body(body, TEMPLATE))


def test_pending_history_does_not_replace_a_current_completed_outcome():
    body = _body().replace("contract-docs; complete receipt attached.",
                           "full; passed; complete receipt attached. Earlier status: Pending.")
    assert pr_body.validate_body(body, TEMPLATE) == []


def test_live_body_is_used_instead_of_event_body():
    assert "structure passed" in _check(_pr())
    event = _event()
    event["pull_request"]["body"] = _body()
    with pytest.raises(ValueError, match="Missing section"):
        _check(_pr(""), event=event)


def test_an_edit_on_the_same_head_can_fail_then_pass():
    with pytest.raises(ValueError, match="Missing section"):
        _check(_pr(""))
    assert "structure passed" in _check(_pr())


@pytest.mark.parametrize("mutation", ["body", "draft", "head", "base", "author", "head_ref"])
def test_metadata_changed_during_validation_fails(mutation):
    first = _pr()
    second = deepcopy(first)
    if mutation == "body":
        second["body"] = ""
    elif mutation == "draft":
        second["draft"] = True
    elif mutation == "head":
        second["head"]["sha"] = "b" * 40
    elif mutation == "base":
        second["base"]["ref"] = "codex/stack-parent"
    elif mutation == "author":
        second["user"]["id"] = 5678
    else:
        second["head"]["ref"] = "different-branch"
    with pytest.raises(ValueError, match="changed during validation"):
        _check(first, second)


def test_fork_and_stacked_base_get_the_normal_body_policy():
    pr = _pr()
    pr["head"]["repo"]["full_name"] = "contributor/graphify"
    pr["base"]["ref"] = "codex/stack-parent"
    assert "structure passed" in _check(pr)


def test_only_canonical_dependabot_authorship_and_branch_get_the_exception():
    bot = _pr("")
    bot["user"] = {"login": "dependabot[bot]", "id": 49699333, "type": "Bot"}
    bot["head"]["ref"] = "dependabot/uv/group-123"
    assert "Exception: Dependabot-generated body" in _check(bot)
    for field, value in [("id", 1), ("type", "User"), ("login", "another[bot]")]:
        wrong = deepcopy(bot)
        wrong["user"][field] = value
        with pytest.raises(ValueError, match="Missing section"):
            _check(wrong)
    for key, value in [("ref", "human-branch"), ("repo", {"full_name": "fork/graphify"})]:
        wrong = deepcopy(bot)
        wrong["head"][key] = value
        with pytest.raises(ValueError, match="Missing section"):
            _check(wrong)


def test_stale_event_head_fails_before_second_fetch():
    calls = []

    def fetch(repository, number):
        calls.append(number)
        return {**_pr(), "head": {**_pr()["head"], "sha": "b" * 40}}

    with pytest.raises(ValueError, match="head changed"):
        pr_body.check_pull_request(_event(), REPOSITORY, TEMPLATE, fetch)
    assert calls == [123]


def test_invalid_identity_is_rejected_without_network():
    def forbidden(*args) -> NoReturn:
        pytest.fail("Invalid identity must not reach the API")

    with pytest.raises(ValueError, match="repository identity"):
        pr_body.check_pull_request(_event(), "owner/../repo", TEMPLATE, forbidden)
    event = _event()
    event["repository"]["full_name"] = "other/repository"
    with pytest.raises(ValueError, match="Event does not belong"):
        pr_body.check_pull_request(event, REPOSITORY, TEMPLATE, forbidden)


def test_closed_pr_has_an_explicit_stable_not_applicable_outcome():
    assert "Not applicable: closed" in _check({**_pr(""), "state": "closed"})


def test_deleted_fork_does_not_gain_an_automation_exception():
    pr = _pr()
    pr["head"]["repo"] = None
    assert "structure passed" in _check(pr)


def test_workflow_runs_a_trusted_standalone_check_for_every_pr_target():
    workflow = yaml.load((ROOT / ".github/workflows/pr-body.yml").read_text(), Loader=yaml.BaseLoader)
    assert workflow["on"] == {"pull_request_target": {"types": [
        "opened", "edited", "reopened", "synchronize", "ready_for_review", "converted_to_draft",
    ]}}
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert workflow["concurrency"] == {
        "group": "pr-body-${{ github.repository }}-${{ github.event.pull_request.number }}",
        "cancel-in-progress": "true",
    }
    job = workflow["jobs"]["body"]
    assert job["name"] == "Graphify — PR body"
    assert "if" not in job and "needs" not in job
    checkout, check = job["steps"]
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    assert checkout["with"]["persist-credentials"] == "false"
    assert set(checkout["with"]["sparse-checkout"].splitlines()) == {
        "tools/pr_body.py", ".github/pull_request_template.md",
    }
    assert check["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert check["run"] == 'python3 -I -S -B tools/pr_body.py --event-file "$GITHUB_EVENT_PATH"'


def test_local_cli_reads_body_file_and_stdin_without_mutation(tmp_path):
    path = tmp_path / "body.md"
    path.write_text(_body(), encoding="utf-8")
    before = path.read_bytes()
    for source in [str(path), "-"]:
        result = subprocess.run([sys.executable, "-I", "-S", "-B", str(ROOT / "tools/pr_body.py"),
                                 "--body-file", source], input=_body(), text=True,
                                capture_output=True, check=False)
        assert result.returncode == 0, result.stderr
        assert "claims and required validation still need review" in result.stdout
    assert path.read_bytes() == before


def test_hosted_cli_uses_get_only_and_never_executes_body_text(tmp_path):
    sentinel = tmp_path / "must-not-exist"
    pr = _pr(_body() + f"\n$(touch {sentinel})\n`touch {sentinel}`\n")
    response = tmp_path / "response.json"
    response.write_text(json.dumps(pr), encoding="utf-8")
    event = tmp_path / "event.json"
    event.write_text(json.dumps(_event()), encoding="utf-8")
    calls = tmp_path / "calls"
    gh = tmp_path / "gh"
    gh.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$PR_TEST_CALLS"\ncat "$PR_TEST_RESPONSE"\n')
    gh.chmod(0o700)
    result = subprocess.run([sys.executable, "-I", "-S", "-B", str(ROOT / "tools/pr_body.py"),
                             "--event-file", str(event)], capture_output=True, text=True, check=False,
                            env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
                                 "GITHUB_REPOSITORY": REPOSITORY,
                                 "PR_TEST_RESPONSE": str(response), "PR_TEST_CALLS": str(calls)})
    assert result.returncode == 0, result.stderr
    assert "structure passed" in result.stdout
    assert calls.read_text().splitlines() == [
        f"api --hostname github.com --method GET repos/{REPOSITORY}/pulls/123",
    ] * 2
    assert not sentinel.exists()


def test_api_failure_cannot_produce_success(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(23, ["gh", "api"])

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        pr_body.check_pull_request(_event(), REPOSITORY, TEMPLATE, pr_body.fetch_pull_request)


@pytest.mark.parametrize("arguments,body,error", [
    (["--body-file", "-"], "", "Missing section"),
    (["--event-file", "event.json", "--draft"], "", "live PR metadata"),
])
def test_cli_failures_return_nonzero_without_a_success_message(arguments, body, error):
    result = subprocess.run([sys.executable, "-I", "-S", "-B", str(ROOT / "tools/pr_body.py"),
                             *arguments], input=body, capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert error in result.stderr
    assert "passed" not in result.stdout
