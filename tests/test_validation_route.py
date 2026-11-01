"""Exercise complete-diff docs eligibility and dispatch without expensive gates."""

from __future__ import annotations

from pathlib import Path
import os
import subprocess

import pytest
import yaml

from tools import validation_route as route


ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Fixture")
    (root / "docs").mkdir()
    (root / "docs/notes.md").write_text("Plain explanation.\n")
    (root / "README.md").write_text("Project overview.\n")
    (root / ".gitignore").write_text("ignored/\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "baseline")
    base = git(root, "rev-parse", "HEAD")
    monkeypatch.setattr(route, "ROOT", root)
    return root, base


@pytest.mark.parametrize("surface", ["committed", "staged", "unstaged", "untracked"])
def test_editorial_inventory_covers_every_surface(repository, surface):
    root, base = repository
    path = root / ("docs/new.md" if surface == "untracked" else "docs/notes.md")
    path.write_text("Revised explanation.\n")
    if surface in {"committed", "staged"}:
        git(root, "add", ".")
    if surface == "committed":
        git(root, "commit", "-qm", "docs")
    result = route.classify_repository(base)
    assert result.lane == route.EDITORIAL_DOCS
    assert not result.fallback_reasons
    assert any(change.source == {"committed": "base-to-head"}.get(surface, surface)
               for change in result.changes)


@pytest.mark.parametrize("path, content, lane", [
    ("README.md", "Revised overview.\n", route.CONTRACT_DOCS),
    ("AGENTS.md", "Review rules.\n", route.CONTRACT_DOCS),
    ("docs/runtime-policy.md", "Rules.\n", route.CONTRACT_DOCS),
    ("docs/notes.md", "Run `uv sync`.\n", route.CONTRACT_DOCS),
    ("docs/notes.md", "```python\nprint(1)\n```\n", route.CONTRACT_DOCS),
    ("graphify/skill-codex.md", "Instructions.\n", route.FULL),
    ("tools/skillgen/fragments/core.md", "Generated skill source.\n", route.FULL),
    ("tests/fixtures/README.md", "A fixture.\n", route.FULL),
    ("worked/corpus/raw/data.md", "Corpus.\n", route.FULL),
    ("unknown.md", "Unknown ownership.\n", route.FULL),
    (".github/workflows/example.yml", "name: CI\n", route.FULL),
])
def test_path_and_content_contracts(repository, path, content, lane):
    root, base = repository
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    assert route.classify_repository(base).lane == lane


@pytest.mark.parametrize("name", ["AGENTS.md", "AGENTS.override.md"])
@pytest.mark.parametrize("directory", [
    "", "docs", "docs/operators", ".github/ISSUE_TEMPLATE",
    ".github/PULL_REQUEST_TEMPLATE",
])
def test_maintained_instruction_documents_keep_contract_lane(repository, name, directory):
    root, base = repository
    path = root / directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Review rules.\n")
    result = route.classify_repository(base)
    assert result.lane == route.CONTRACT_DOCS
    assert not result.fallback_reasons


@pytest.mark.parametrize("name", ["AGENTS.md", "AGENTS.override.md"])
@pytest.mark.parametrize("directory", [
    "tests/fixtures", "worked/example/raw", "tools/skillgen/fragments",
    "graphify", "src", "configs", "scripts", "unknown",
])
@pytest.mark.parametrize("surface", ["committed", "staged", "unstaged", "untracked"])
def test_excluded_instruction_documents_delegate_full_ci(
    repository, monkeypatch, tmp_path, name, directory, surface,
):
    root, base = repository
    path = root / directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Original instructions.\n")
    if surface != "untracked":
        git(root, "add", ".")
        git(root, "commit", "-qm", "original instructions")
        base = git(root, "rev-parse", "HEAD")
    path.write_text("Revised instructions.\n")
    if surface in {"committed", "staged"}:
        git(root, "add", ".")
    if surface == "committed":
        git(root, "commit", "-qm", "revise instructions")
    calls = []
    monkeypatch.setattr(route, "run_commands", lambda commands, admission=None: calls.extend(commands) or 0)
    output = tmp_path / "github-output"
    assert route.main(["--base", base, "--ci-plan", "--github-output", str(output)]) == 0
    assert not calls
    assert output.read_text() == "lane=full\nstatus=deferred\n"


@pytest.mark.parametrize("surface", ["committed", "staged", "unstaged", "untracked"])
@pytest.mark.parametrize("ci_plan", [False, True])
def test_protected_policy_uses_full_lane_with_pinned_git(
    repository, monkeypatch, tmp_path, surface, ci_plan,
):
    root, base = repository
    path = root / "docs/protected-change-review.md"
    path.write_text("Original review policy.\n")
    if surface != "untracked":
        git(root, "add", ".")
        git(root, "commit", "-qm", "original policy")
        base = git(root, "rev-parse", "HEAD")
    path.write_text("Revised review policy.\n")
    if surface in {"committed", "staged"}:
        git(root, "add", ".")
    if surface == "committed":
        git(root, "commit", "-qm", "revise policy")
    calls = []
    monkeypatch.setattr(route, "run_commands", lambda commands, admission=None: calls.extend(commands) or 0)
    output = tmp_path / "github-output"
    argv = ["--base", base]
    if ci_plan:
        argv.extend(["--ci-plan", "--github-output", str(output)])
    assert route.main(argv) == 0
    if ci_plan:
        assert not calls
        assert output.read_text() == "lane=full\nstatus=deferred\n"
    else:
        assert [command.argv[-1] for command in calls] == ["review-ready"]
    # Full CI installs the oracle's exact Git before either normal or optimized tests.
    jobs = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)["jobs"]
    for job_name, test_name in (
        ("test", "Run tests"),
        ("quality", "Run protected verifier conformance with optimized Python"),
    ):
        steps = jobs[job_name]["steps"]
        names = [step.get("name") for step in steps]
        assert names.index("Install acceptance-pinned Git 2.55.0") < names.index(test_name)
        if job_name == "test":
            assert "needs.validation-route.outputs.lane == 'full'" in jobs[job_name]["if"]
        else:
            assert steps[names.index("Install acceptance-pinned Git 2.55.0")]["if"] == (
                "${{ needs.validation-route.outputs.lane == 'full' }}"
            )


def test_mixed_change_is_full_even_when_code_is_untracked(repository):
    root, base = repository
    (root / "docs/notes.md").write_text("New prose.\n")
    (root / "surprise.py").write_text("print('code')\n")
    assert route.classify_repository(base).lane == route.FULL


@pytest.mark.parametrize("kind", ["executable", "binary", "nonutf8", "symlink"])
def test_unsafe_doc_types_never_take_docs_lane(repository, kind):
    root, base = repository
    path = root / "docs/new.md"
    if kind == "symlink":
        path.symlink_to("notes.md")
    else:
        path.write_bytes({"binary": b"text\0data", "nonutf8": b"\xff"}.get(kind, b"text\n"))
        if kind == "executable":
            path.chmod(0o755)
    assert route.classify_repository(base).lane == route.FULL


def test_removed_command_content_still_selects_contract_docs(repository):
    root, _ = repository
    path = root / "docs/notes.md"
    path.write_text("Run `uv sync`.\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "command")
    base = git(root, "rev-parse", "HEAD")
    path.write_text("Revised explanation.\n")
    assert route.classify_repository(base).lane == route.CONTRACT_DOCS


def test_deleted_executable_doc_and_category_crossing_rename_are_full(repository):
    root, _ = repository
    path = root / "old.py"
    path.write_text("Plain explanation.\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "code")
    base = git(root, "rev-parse", "HEAD")
    git(root, "mv", "old.py", "docs/moved.md")
    assert route.classify_repository(base).lane == route.FULL


@pytest.mark.parametrize("base", [None, "missing-ref"])
def test_missing_base_is_full(repository, base):
    root, _ = repository
    (root / "docs/notes.md").write_text("New prose.\n")
    result = route.classify_repository(base)
    assert result.lane == route.FULL
    assert result.fallback_reasons


def test_git_inspection_failure_is_full(repository, monkeypatch):
    root, base = repository
    (root / "docs/notes.md").write_text("New prose.\n")
    original = route.run_git

    def fail(args):
        if "--name-status" in args:
            return subprocess.CompletedProcess(args, 128, "", "inspection denied")
        return original(args)

    monkeypatch.setattr(route, "run_git", fail)
    result = route.classify_repository(base)
    assert result.lane == route.FULL
    assert any("inspection denied" in reason for reason in result.fallback_reasons)


def test_docs_dispatch_and_snapshot_invalidation(repository, monkeypatch, capsys):
    root, base = repository
    path = root / "docs/notes.md"
    path.write_text("New prose.\n")
    calls = []

    def run(commands, admission=None):
        calls.extend(command.argv for command in commands)
        return 0

    monkeypatch.setattr(route, "run_commands", run)
    assert route.main(["--base", base]) == 0
    assert calls[0][-1] == "docs-check"
    assert calls[1] == ("uv", "sync", "--frozen", "--inexact")
    assert "tests/test_readme_policy.py" in calls[2]
    assert not any("review-ready" in command for command in calls)
    assert "validation_route_status=complete; selected_lane=editorial-docs" in capsys.readouterr().out

    def drift(commands, admission=None):
        path.write_text("Different prose.\n")
        return 0

    monkeypatch.setattr(route, "run_commands", drift)
    assert route.main(["--base", base]) == 3
    assert "validation_route_status=invalidated" in capsys.readouterr().err


@pytest.mark.parametrize("options", ["--collect-only", "--lf"])
@pytest.mark.parametrize("ci_plan", [False, True])
def test_final_route_rejects_ambient_pytest_options(repository, monkeypatch, capsys,
                                                  options, ci_plan):
    root, base = repository
    (root / "docs/notes.md").write_text("Revised explanation.\n")
    monkeypatch.setenv("PYTEST_ADDOPTS", options)
    calls = []
    monkeypatch.setattr(route, "run_commands", lambda commands, admission=None: calls.extend(commands) or 0)
    output = root / "ci-output"
    argv = ["--base", base]
    if ci_plan:
        argv.extend(["--ci-plan", "--github-output", str(output)])
    assert route.main(argv) != 0
    assert not calls
    assert not output.exists()
    captured = capsys.readouterr()
    assert "validation_route_status=complete" not in captured.out
    assert "ambient-pytest-addopts" in captured.err


def test_ambient_options_preserve_nonfinal_helpers(repository, monkeypatch, capsys):
    root, base = repository
    (root / "docs/notes.md").write_text("Revised explanation.\n")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only")
    monkeypatch.setattr(route, "run_commands", lambda commands, admission=None: 0)
    assert route.main(["--base", base, "--dry-run"]) == 0
    assert route.main(["--base", base, "--docs-check"]) == 0
    assert route.main(["--base", base, "--check-change-set-whitespace"]) == 0
    assert "validation_route_status=complete" not in capsys.readouterr().out



def test_contract_tests_are_path_bound_not_pytest_cache(repository, monkeypatch):
    root, base = repository
    (root / ".github").mkdir()
    (root / ".github/pull_request_template.md").write_text("Review template.\n")
    cache = root / ".pytest_cache/v/cache"
    cache.mkdir(parents=True)
    (cache / "lastfailed").write_text('{"tests/test_unrelated.py::test_old": true}')
    # Cache is ignored by normal Git inventory, just as in the real repository.
    with (root / ".gitignore").open("a") as file:
        file.write(".pytest_cache/\n")
    git(root, "add", ".gitignore")
    git(root, "commit", "-qm", "ignore cache")
    base = git(root, "rev-parse", "HEAD")
    result = route.classify_repository(base)
    commands = route.build_plan(result, base, False, None, 1)
    assert result.lane == route.CONTRACT_DOCS
    argv = [value for command in commands for value in command.argv]
    assert "tests/test_validation_route.py" in argv
    assert "tests/test_unrelated.py" not in argv


def test_full_dispatch_and_ci_delegation(repository, monkeypatch, tmp_path, capsys):
    root, base = repository
    (root / "code.py").write_text("print(1)\n")
    calls = []
    monkeypatch.setattr(route, "run_commands", lambda commands, admission=None: calls.extend(commands) or 0)
    assert route.main(["--base", base]) == 0
    assert [command.argv[-1] for command in calls] == ["review-ready"]
    output = tmp_path / "github-output"
    calls.clear()
    assert route.main(["--base", base, "--ci-plan", "--github-output", str(output)]) == 0
    assert not calls
    assert output.read_text() == "lane=full\nstatus=deferred\n"
    text = capsys.readouterr().out
    assert "validation_route_status=deferred" in text


@pytest.mark.parametrize("checks_fail", [False, True])
def test_full_checks_preserve_ci_commands_and_whitespace_order(repository, monkeypatch, checks_fail):
    _, base = repository
    events = []
    monkeypatch.setattr(route, "check_change_set_whitespace", lambda base: events.append("whitespace") or 0)
    def execute(commands, admission):
        events.extend(command.argv for command in commands)
        return 17 if checks_fail else 0
    monkeypatch.setattr(route, "run_commands", execute)
    assert route.main(["--full-checks", "--base", base]) == (17 if checks_fail else 0)
    assert events[:3] == ["whitespace", ("uv", "sync", "--all-extras", "--frozen"),
        ("uv", "run", "--frozen", "python", "-m", "pytest", "tests/", "-q", "--tb=short")]
    assert events[3:] == ([] if checks_fail else ["whitespace"])


def test_ci_docs_output_requires_success_and_never_claims_dry_run_completion(repository, monkeypatch, tmp_path, capsys):
    root, base = repository
    (root / "docs/notes.md").write_text("New prose.\n")
    output = tmp_path / "github-output"
    monkeypatch.setattr(route, "run_commands", lambda commands, admission=None: 1)
    assert route.main(["--base", base, "--ci-plan", "--github-output", str(output)]) == 1
    assert not output.exists()
    assert route.main(["--base", base, "--dry-run"]) == 0
    assert "validation_route_status=complete" not in capsys.readouterr().out


def test_whitespace_conflicts_and_deleted_docs(repository):
    root, base = repository
    (root / "docs/notes.md").unlink()
    assert route.classify_repository(base).lane == route.EDITORIAL_DOCS
    assert route.check_change_set_whitespace(base) == 0
    (root / "docs/new.md").write_text("<<<<<<< ours\ntext\n=======\nother\n>>>>>>> theirs\n")
    assert route.check_change_set_whitespace(base) == 1


def test_ci_keeps_all_full_jobs_and_stable_quality_gate():
    workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    jobs = workflow["jobs"]
    classifier = jobs["validation-route"]
    checkout = classifier["steps"][0]
    assert checkout["with"]["fetch-depth"] == "0"
    classify = classifier["steps"][-1]
    assert "--ci-plan --github-output" in classify["run"]
    assert 'if [ "$EVENT_NAME" != pull_request ]; then args+=(--full); fi' in classify["run"]
    assert classify["env"]["BASE_SHA"] == "${{ github.event.pull_request.base.sha || github.sha }}"
    for name in ("test", "skillgen-check", "security-scan", "leiden-binary-smoke"):
        assert jobs[name]["needs"] == ["validation-route"]
        assert "needs.validation-route.outputs.lane == 'full'" in jobs[name]["if"]
    quality = jobs["quality"]
    assert quality["needs"] == ["validation-route", "test"]
    assert "always()" in quality["if"]
    assert 'test "$ROUTE_RESULT" = success' in quality["steps"][0]["run"]
    assert "full:deferred" in quality["steps"][0]["run"]
    for step in quality["steps"][1:]:
        if step.get("name") == "Reject whole-run cancellation":
            continue
        assert step["if"] == "${{ needs.validation-route.outputs.lane == 'full' }}"
    assert any("tools.ci_pytest_gate check" in step.get("run", "") for step in quality["steps"])
    assert any("python -O -m pytest" in step.get("run", "") for step in quality["steps"])
    assert any("tools.ci_pytest_gate smoke" in step.get("run", "") for step in quality["steps"])


@pytest.mark.parametrize("result,lane,status,accepted", [
    ("success", "editorial-docs", "complete", True),
    ("success", "contract-docs", "complete", True),
    ("success", "full", "deferred", True),
    ("success", "editorial-docs", "printed", False),
    ("success", "full", "complete", False),
    ("failure", "full", "deferred", False),
    ("skipped", "", "", False),
    ("success", "", "", False),
])
def test_ci_quality_executes_route_receipt_check(result, lane, status, accepted):
    workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    step = workflow["jobs"]["quality"]["steps"][0]
    environment = {**os.environ, "ROUTE_RESULT": result, "ROUTE_LANE": lane,
                   "ROUTE_STATUS": status}
    completed = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        env=environment, capture_output=True, text=True, timeout=10,
    )
    assert (completed.returncode == 0) is accepted
