# graphify reference: add a URL and watch a folder

Load this when the user ran `/graphify add <url>` or passed `--watch`. Neither is part of the default build.

## For /graphify add

Fetch a URL and add it to the corpus, then update the graph.

```bash
"$(cat graphify-out/.graphify_python)" -E -P -B -c "
import sys
from graphify.ingest import ingest
from pathlib import Path

try:
    out = ingest('URL', Path('./raw'), author='AUTHOR', contributor='CONTRIBUTOR')
    print(f'Saved to {out}')
except ValueError as e:
    print(f'error: {e}', file=sys.stderr)
    sys.exit(1)
except RuntimeError as e:
    print(f'error: {e}', file=sys.stderr)
    sys.exit(1)
"
```

Replace `URL` with the actual URL, `AUTHOR` with the user's name if provided, `CONTRIBUTOR` likewise. If the command exits with an error, tell the user what went wrong - do not silently continue. After a successful save, automatically run the `--update` pipeline on `./raw` to merge the new file into the existing graph.

Supported URL types (auto-detected):
- YouTube / any video URL → audio downloaded via yt-dlp, transcribed to `.txt` on next run (requires `pip install 'graphifyy[video]'`)
- Twitter/X → fetched via oEmbed, saved as `.md` with tweet text and author
- arXiv → abstract + metadata saved as `.md`
- PDF → downloaded as `.pdf`
- Images (.png/.jpg/.webp) → downloaded, Claude vision extracts on next run
- Any webpage → converted to markdown via html2text

---

## For --watch

Start a background watcher that monitors a folder and auto-updates the graph when files change.

```powershell
if (-not (Test-Path -LiteralPath graphify-out\.graphify_python -PathType Leaf)) {
    throw "Missing graphify-out\.graphify_python; rerun Step 1 interpreter bootstrap."
}
$GraphifySaved = (Get-Content -LiteralPath graphify-out\.graphify_python -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($GraphifySaved) -or
    -not [IO.Path]::IsPathFullyQualified($GraphifySaved) -or
    $GraphifySaved -match "[\r\n]") {
    throw "Invalid graphify interpreter pointer."
}
& $GraphifySaved -E -P -B -c "import graphify, sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and sys.version_info.releaselevel == 'final' and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Saved graphify interpreter is unsupported or cannot import graphify."
}
& (Get-Content graphify-out\.graphify_python) -E -P -B -m graphify watch INPUT_PATH --debounce 3
```

Replace INPUT_PATH with the folder to watch. Behavior depends on what changed:

- **Code files only (.py, .ts, .go, etc.):** re-runs AST extraction + rebuild + cluster immediately, no LLM needed. `graph.json` and `GRAPH_REPORT.md` are updated automatically.
- **Docs, papers, or images:** writes a `graphify-out/needs_update` flag and prints a notification to run `/graphify --update` (LLM semantic re-extraction required).

Debounce (default 3s): waits until file activity stops before triggering, so a wave of parallel agent writes doesn't trigger a rebuild per file.

Press Ctrl+C to stop.

For agentic workflows: run `--watch` in a background terminal. Code changes from agent waves are picked up automatically between waves. If agents are also writing docs or notes, you'll need a manual `/graphify --update` after those waves.
