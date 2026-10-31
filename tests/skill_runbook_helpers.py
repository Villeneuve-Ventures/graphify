"""Execute generated Python through the native prepared-token runner."""
from __future__ import annotations

import contextlib
import io
import re
import shlex
from pathlib import Path

from graphify import transaction as tx


def resolve_parent_recipes(core: str, reference: str) -> str:
    """Follow the core's parent-recipe links in the shipped extraction sidecar."""
    for match in list(re.finditer(
        r"\[([^\n]+?)\]\(references/extraction-spec\.md#(parent-[a-z0-9-]+)\)", core
    )):
        title, anchor = match.groups()
        assert re.sub(r"[^a-z0-9 -]", "", title.lower()).replace(" ", "-") == anchor
        heading = "## " + title + "\n"
        assert reference.count(heading) == 1
        recipe = reference.split(heading, 1)[1].split("\n## ", 1)[0]
        core = core.replace(match.group(0), recipe)
    return core


def python_bodies(text: str) -> list[str]:
    bodies = []
    for block in re.findall(r"```bash\n(.*?)\n```", text, re.S):
        if '-c "\n' not in block:
            continue
        source = block.rsplit('-c "\n', 1)[1].split('\n"', 1)[0]
        bodies.append(shlex.split('python -c "' + source + '"')[-1])
    return bodies


def prepared_path(token: Path) -> Path:
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        tx.run_prepared_token(token, ["-c", "from pathlib import Path; print(Path.cwd())"])
    return Path(captured.getvalue().strip())


def run_body(source: str, *, token: Path, root: Path) -> None:
    source = source.replace("'INPUT_PATH'", repr(str(root))).replace("IS_DIRECTED", "False")
    tx.run_prepared_token(token, ["-c", source])
