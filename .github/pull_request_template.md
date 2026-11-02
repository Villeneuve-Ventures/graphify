<!-- Use a concise title describing the change. Remove optional sections that do not apply. -->

## 📝 Description

<!-- Explain the problem, the resulting behavior, and any material limitations. -->
- <!-- Add details here. -->

## 🔗 Related Issues / Tickets

<!-- Link issues or design documents. Use Closes #123 only when this PR resolves the issue. Write None if there is no related issue. -->
- <!-- Add details here. -->

## 🧪 How Has This Been Tested?

<!-- List the exact commands, results, and relevant environment. Disclose skipped checks and known warnings. Select checks appropriate to the change and follow applicable repository requirements. -->
- [ ] **Validation route:** `make fast-feedback` — selected lane, complete receipt and result:
- [ ] **Focused tests:** `uv run --frozen pytest tests/<affected_test>.py -q --tb=short` — result:
- [ ] **Manual or smoke testing:** steps and result:

<!-- Common Graphify checks, when applicable:
Dependencies: uv sync --all-extras --frozen
Complete-diff routing: make fast-feedback BASE=origin/v8
Full readiness (mixed/uncertain or explicitly requested): make review-ready BASE=origin/v8
README policy: uv run --frozen pytest tests/test_readme_policy.py -q --tb=short
Generated skills: uv run --frozen python -m tools.skillgen --check
After code changes: graphify update .
Record additional required checks and their results above. These examples do not replace repository instructions.
-->

## 📸 Screenshots / Screen Recordings (if applicable)

<!-- Include before/after images for visible UI changes, or remove this section. -->

## 🚀 Checklist

<!-- Check completed items; mark non-applicable items N/A or remove them. -->
- [ ] Followed existing Graphify helpers, code patterns, and repository instructions.
- [ ] Performed a self-review of the complete change.
- [ ] Explained non-obvious behavior in code comments where needed.
- [ ] Updated affected documentation; maintained README documentation remains English-only.
- [ ] Added or updated relevant tests and recorded validation results above.
- [ ] Reviewed warnings and disclosed any new or remaining warnings above.
