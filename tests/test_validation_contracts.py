"""Check maintained documentation contracts selected by the validation router."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_pr_template_requires_validation_route_receipt():
    template = (ROOT / ".github/pull_request_template.md").read_text()
    assert "- [ ] **Validation route:** `make fast-feedback`" in template
    assert "selected lane, complete receipt and result:" in template
