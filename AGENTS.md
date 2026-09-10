# Graphify Agent Guidance

## Current authority and scope

Read the nearest instructions and inspect the current branch, `HEAD`, and working
tree before changing files. Use `README.md`, `ARCHITECTURE.md`, `SECURITY.md`,
`pyproject.toml`, and the relevant source, tests, and CI configuration as needed
for the task. Current files decide which commands and contracts exist; do not
import another branch's workspace layout, milestones, or validation gates.

Keep the requested outcome and delivery boundary explicit. Preserve unrelated
work and stop on unknown ownership or unexpected changes to a frozen candidate.
Do not stash, reset, clean, or overwrite another task's files to proceed.
Follow global progress and delivery stop rules. A plan does not independently
authorize implementation, and completing a milestone does not start its successors.

## Execution and review

Default to direct execution for coherent work with understood contracts. Resolve
discoverable facts through repository inspection and ask only for material
missing decisions. Choose structured planning when unresolved contract or
architecture decisions need it. Use durable orchestration only when explicitly
requested, required by an applicable contract, or justified by a named need for
checkpoint/resume or persistent coordination. File count alone is not a trigger.
Use bounded native helpers when their independent work materially helps.
Adversarial UltraQA requires explicit opt-in.

Classify risk by the behavior changed. Security boundaries, graph integrity,
provenance, compatibility, and publication behavior need tests and independent
review appropriate to their effect. Preserve any stronger protected workflow
required by the active branch or task; a wording change alone does not activate
an unrelated implementation workflow or waive an existing required stage.

Review the complete proposed change before delivery, using an independent review
for substantive behavior or policy changes. Give findings a reachable trigger,
affected contract, and practical impact. Fix supported in-scope defects; reject
unsupported findings with evidence; separate non-blocking follow-ups. Carry those
dispositions into repair reviews and reopen them only for changed inputs or new
evidence. Focus follow-up review on repairs and affected dependencies unless
evidence or the applicable contract requires a wider review. Unresolved blocking
findings and failed required checks remain blockers.

## Validation and tool boundaries

During implementation, run the smallest relevant checks. Before delivery, inspect
the current test and CI configuration and run the applicable final local checks.
The current CI test command is `python -m pytest tests/ -q --tb=short`; derive its
dependencies and supported interpreters from `pyproject.toml` and
`.github/workflows/ci.yml`. Recheck those files when the environment changes.
For guidance-only edits, verify referenced paths, policy consistency, independent
review, and `git diff --check`; full application tests need a behavior-related
reason. Report commands actually run and any validation gaps.

Run installer or generated-guidance tests only in disposable directories with
explicit output locations. Never let a smoke test rewrite the working checkout's
instructions. Keep generated artifacts out of the proposed source change unless
they are requested deliverables. Preserve the security model and explicit
authorization boundaries for external fetching and other side effects.

Use a graph when it materially reduces source inspection. Start with one scoped
query if a usable graph exists; otherwise inspect source directly. A read-only
question does not authorize a graph rebuild. Treat graph output as navigation
evidence and verify material claims against current source. If an actual graph
operation has a certification or integrity gate, follow it before that operation;
direct-source fallback never makes an uncertified graph valid.
