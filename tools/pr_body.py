"""Check PR-body structure without certifying claims, receipts or permission.

Local checks read a completed body file. Hosted checks read current PR metadata
twice through the GitHub CLI; they never modify the PR or execute its contents.
The maintained template remains the only source of starting body text.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / ".github/pull_request_template.md"
CORE_SECTIONS = ("description", "related issues / tickets", "how has this been tested?")
FIELD = re.compile(r"^\s*(?:[-*+]\s+)?(?:\[[ xX]\]\s+)?\*\*([^*]+):\*\*\s*(.*)$")
PENDING = re.compile(
    r"^(?:`?(?:editorial-docs|contract-docs|full)`?\s*[;:—–-]\s*)?(?:pending|todo|tbd)\b",
    re.IGNORECASE,
)


def _heading(text: str) -> str:
    return re.sub(r"^[^a-zA-Z]+", "", text).strip().rstrip("# ").casefold()


def _sections(markdown: str) -> dict[str, list[str]]:
    # Only real level-two headings delimit sections. Later historical copies
    # cannot fill holes in the first current section with the same heading.
    markdown = re.sub(r"<!--.*?(?:-->|\Z)", "", markdown, flags=re.DOTALL)
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    fence = ""
    for line in markdown.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) \
                    and not marker[2].strip():
                fence = ""
            if current is not None:
                current.append(line)
            continue
        if marker:
            fence = marker[1]
            if current is not None:
                current.append(line)
            continue
        heading = re.match(r"^ {0,3}##\s+(.+)$", line)
        if heading:
            key = _heading(heading[1])
            current = None if key in sections else sections.setdefault(key, [])
        elif current is not None:
            current.append(line)
    return sections


def _text(value: str) -> str:
    value = re.sub(r"^\s*(?:>\s*)+", "", value)
    if re.fullmatch(r" {0,3}(?:`{3,}|~{3,})[^\r\n]*", value):
        return ""
    value = re.sub(r"^\s*(?:[-*+]\s+)?(?:\[[ xX]\]\s+)?", "", value)
    return value.strip(" \t\r\n`*_:.—–-")


def _answered(value: str, *, allow_none: bool = False) -> bool:
    text = _text(value)
    empty = {"", "todo", "tbd", "pending", "n/a", "add details here", "result",
             "steps and result", "not applicable", "not run", "skipped", "not tested"}
    if not allow_none:
        empty.add("none")
    return (text.casefold() not in empty and re.search(r"\w", text) is not None
            and not re.match(r"^(?:todo|tbd|add details here)\b", text, re.IGNORECASE))


def _fields(lines: list[str]) -> dict[str, str]:
    fields: dict[str, list[str]] = {}
    current: list[str] | None = None
    fence = ""
    for line in lines:
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker:
            if not fence:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence) \
                    and not marker[2].strip():
                fence = ""
            continue
        match = FIELD.match(line) if not fence else None
        if match:
            key = match[1].casefold()
            current = None if key in fields else fields.setdefault(key, [match[2]])
        elif current is not None:
            current.append(line)
    return {key: "\n".join(value).strip() for key, value in fields.items()}


def validate_body(body: str, template: str, *, draft: bool = False) -> list[str]:
    """Return structural defects in the first current core sections.

    Headings are matched to the canonical template, ignoring leading emoji and
    case. HTML comments and headings inside fenced examples cannot supply core
    sections. Screenshots, checklist items and unrelated appendices are optional.
    Included testing fields need an outcome or explained omission; drafts may
    explicitly record pending work. Pending markers are recognized at the start
    of an outcome, optionally after a canonical lane and separator. Fence syntax
    and quoted placeholders are not answers. This checks text, not receipt authenticity,
    test passage, policy compliance, or the authority for a claimed exception.
    Raise ValueError when the canonical template lacks a core section.
    """
    source = _sections(template)
    if any(key not in source for key in CORE_SECTIONS):
        raise ValueError("Canonical template is missing a required core section")
    sections = _sections(body)
    errors = []
    for key in CORE_SECTIONS:
        if key not in sections:
            errors.append(f"Missing section: {key}")
            continue
        scaffolding = {_text(line) for line in source[key]}
        answers = [line for line in sections[key] if _text(line) not in scaffolding]
        if not any(_answered(line, allow_none=key == CORE_SECTIONS[1]) for line in answers):
            errors.append(f"Fill in section: {key}")

    testing = sections.get(CORE_SECTIONS[2], [])
    fields = _fields(testing)
    if "validation route" not in fields:
        errors.append("Testing needs a populated **Validation route:** field")
    for key in ("validation route", "focused tests", "manual or smoke testing"):
        if key not in fields:
            continue
        answer = fields[key]
        for line in source[CORE_SECTIONS[2]]:
            match = FIELD.match(line)
            if match and match[1].casefold() == key and answer.startswith(match[2]):
                answer = answer[len(match[2]):]
                break
        if key == "validation route":
            answer = re.sub(r"`?make fast-feedback(?: BASE=\S+)?`?", "", answer)
        if not _answered(answer) or "<affected_test>" in answer:
            errors.append(f"Record a result or explained omission for: {key}")
        elif not draft and PENDING.match(_text(answer)):
            errors.append(f"Resolve pending evidence before readiness: {key}")
    return errors


def fetch_pull_request(repository: str, number: int) -> dict:
    """Read GitHub.com PR metadata using the existing authenticated CLI.

    No GitHub mutation is made. Repository and number must have passed the
    hosted input checks. CLI failures, timeouts and invalid JSON propagate;
    the caller must not turn unavailable metadata into a successful check.
    """
    result = subprocess.run(
        ["gh", "api", "--hostname", "github.com", "--method", "GET",
         f"repos/{repository}/pulls/{number}"],
        capture_output=True, text=True, check=True, timeout=20,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError("GitHub returned invalid PR metadata")
    return value


def _snapshot(pr: dict) -> tuple:
    return (pr["number"], pr["state"], pr["body"], pr["draft"],
            pr["head"]["sha"], pr["head"]["ref"], (pr["head"]["repo"] or {}).get("full_name"),
            pr["base"]["ref"], pr["base"]["repo"]["full_name"],
            pr["user"]["login"], pr["user"]["id"], pr["user"]["type"])


def check_pull_request(
    event: dict, repository: str, template: str, fetch: Callable[[str, int], dict],
) -> str:
    """Return a live PR diagnostic, rejecting invalid structure or changed inputs.

    The event identifies the repository, PR and expected head; it cannot supply
    the body being judged. Both API reads must agree on body, head, base, draft
    and author. A stale head or changed snapshot fails without retries. Closed
    PRs and canonical same-repository Dependabot branches get explicit outcomes.
    The Dependabot exception changes no CI approval or validation requirement.
    Reads are not an atomic edit/merge lock; a later edit still needs a new run.
    Contract violations raise ValueError; malformed metadata and API read errors
    propagate to the CLI's failure handler rather than admitting the PR.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository) \
            or repository.split("/")[1] in {".", ".."}:
        raise ValueError("Invalid repository identity")
    if event["repository"]["full_name"].casefold() != repository.casefold():
        raise ValueError("Event does not belong to this repository")
    number = event["number"]
    if type(number) is not int or number <= 0:
        raise ValueError("Invalid pull request number")
    expected_head = event["pull_request"]["head"]["sha"]
    if not isinstance(expected_head, str) or not re.fullmatch(r"[0-9a-f]{40}", expected_head):
        raise ValueError("Invalid event head SHA")
    first = fetch(repository, number)
    before = _snapshot(first)
    if first["number"] != number or first["base"]["repo"]["full_name"].casefold() \
            != repository.casefold():
        raise ValueError("Live PR does not belong to the requested repository")
    if first["head"]["sha"] != expected_head:
        raise ValueError("PR head changed; use its current event or eligible rerun")
    if type(first["draft"]) is not bool or not isinstance(first["body"], (str, type(None))):
        raise ValueError("Invalid body or draft metadata")
    user = first["user"]
    dependabot = (
        user["login"] == "dependabot[bot]" and user["id"] == 49699333 and user["type"] == "Bot"
        and (first["head"]["repo"] or {}).get("full_name", "").casefold() == repository.casefold()
        and first["head"]["ref"].startswith("dependabot/")
    )
    body = first["body"] or ""
    errors: list[str] = []
    if first["state"] == "closed":
        message = "Not applicable: closed pull request"
    elif first["state"] != "open":
        raise ValueError("Invalid pull request state")
    elif dependabot:
        message = "Exception: Dependabot-generated body; CI approval and validation remain separate"
    else:
        errors = validate_body(body, template, draft=first["draft"])
        message = "; ".join(errors) if errors else "PR body structure passed"
    if _snapshot(fetch(repository, number)) != before:
        raise ValueError("PR changed during validation; check current metadata again")
    if first["state"] == "open" and not dependabot and errors:
        raise ValueError(message)
    digest = hashlib.sha256(body.encode()).hexdigest()
    return f"{message} (PR #{number}, head {expected_head}, body SHA-256 {digest})"


def main() -> int:
    """Return 0 for valid structure or an exception, 1 for data/API failure.

    Argument parsing retains argparse's normal help and usage exit behavior.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--body-file", help="Completed UTF-8 Markdown file, or - for stdin")
    inputs.add_argument("--event-file", type=Path, help="GitHub pull_request_target event JSON")
    parser.add_argument("--draft", action="store_true", help="Allow explicitly pending local evidence")
    args = parser.parse_args()
    try:
        template = TEMPLATE.read_text(encoding="utf-8")
        if args.body_file is not None:
            body = sys.stdin.read() if args.body_file == "-" \
                else Path(args.body_file).read_text(encoding="utf-8")
            errors = validate_body(body, template, draft=args.draft)
            if errors:
                raise ValueError("; ".join(errors))
            print("PR body structure passed; claims and required validation still need review")
        else:
            if args.draft:
                raise ValueError("Hosted draft state must come from live PR metadata")
            event = json.loads(args.event_file.read_text(encoding="utf-8"))
            print(check_pull_request(event, os.environ["GITHUB_REPOSITORY"], template,
                                     fetch_pull_request))
    except (AttributeError, KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"PR body: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
