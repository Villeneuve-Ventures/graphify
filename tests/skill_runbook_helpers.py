"""Execute generated Python through the native prepared-token runner."""
from __future__ import annotations

import contextlib
import io
import re
import shlex
from pathlib import Path

from graphify import transaction as tx


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
