from __future__ import annotations

from pathlib import Path


CI = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"


def _security_job() -> str:
    workflow = CI.read_text(encoding="utf-8")
    marker = "  security-scan:\n"
    assert workflow.count(marker) == 1
    return workflow.split(marker, 1)[1]


def test_security_job_pins_uv_and_cannot_mask_required_failures() -> None:
    job = _security_job()
    assert "fetch-depth: 0" in job
    assert 'version: "0.11.29"' in job
    assert "uv --version | grep -Eq '^uv 0\\.11\\.29( |$)'" in job
    assert "continue-on-error" not in job


def test_security_job_gates_high_bandit_and_candidate_artifact_audits() -> None:
    job = _security_job()
    assert "bandit -r graphify tools/workspace_artifacts -lll" in job
    assert "python -m tools.workspace_artifacts build" in job
    assert "python -m tools.workspace_artifacts audit" in job
    assert "pip-audit --strict" not in job
