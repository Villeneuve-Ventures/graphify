# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.3.x   | Yes       |
| < 0.3   | No        |

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Report security issues via GitHub's private vulnerability reporting, or email the maintainer directly. Please include:

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

We will acknowledge receipt within 48 hours and aim to release a fix within 7 days for critical issues.

## Security Model

graphify is a **local development tool**. It runs as a Claude Code skill and optionally as a local MCP stdio server. It makes no network calls during graph analysis - only during `ingest` (explicit URL fetch by the user).

### Threat Surface

| Vector | Mitigation |
|--------|-----------|
| SSRF via URL fetch | `security.validate_url()` allows only `http` and `https` schemes, blocks private/loopback/link-local IPs, and blocks cloud metadata endpoints. Redirect targets are re-validated. All fetch paths including tweet oEmbed go through `safe_fetch()`. |
| Oversized downloads | `safe_fetch()` streams responses and aborts at 50 MB. `safe_fetch_text()` aborts at 10 MB. |
| Non-2xx HTTP responses | `safe_fetch()` raises `HTTPError` on non-2xx status codes - error pages are not silently treated as content. |
| Path traversal in MCP server | `security.validate_graph_path()` resolves paths and requires them to be inside `graphify-out/`. Also requires the `graphify-out/` directory to exist. |
| XSS in graph HTML output | `security.sanitize_label()` strips control characters, caps at 256 chars, and HTML-escapes all node labels and edge titles before pyvis embeds them. |
| Prompt injection via node labels | `sanitize_label()` also applied to MCP text output - node labels from user-controlled source files cannot break the text format returned to agents. |
| Prompt injection via source file content | During the semantic pass, source files are attacker-controlled text mixed into the LLM context. `_read_files()` in `llm.py` wraps every file in a hash-stamped `<untrusted_source path=... sha256=...>` delimiter block, the extraction system prompt instructs the model to treat that block as inert data and never as instructions, and `_neutralise_injection_sentinels()` defangs known chat-template/jailbreak tokens (`<\|im_start\|>`, `[INST]`, `<<SYS>>`, forged `</untrusted_source>`, etc.) before insertion. This is the table-stakes defense (issue #1210): it does not make injection impossible, but changes it from "works on first try" to "requires evasion." |
| YAML frontmatter injection | `_yaml_str()` escapes backslashes, double quotes, and newlines before embedding user-controlled strings (webpage titles, query questions) in YAML frontmatter. |
| Encoding crashes on source files | All tree-sitter byte slices decoded with `errors="replace"` - non-UTF-8 source files degrade gracefully instead of crashing extraction. |
| Symlink traversal | `os.walk(..., followlinks=False)` is explicit throughout `detect.py`. |
| Saved Python interpreter pointer | `graphify-out/.graphify_python` is advisory compatibility state, not execution authority. Generated commands rediscover and validate a final CPython 3.14.2+ runtime containing the installed `graphifyy` distribution; installed Git hooks try the interpreter already running `graphify hook install` first, then validated, containment-checked lower-authority launcher, shebang, and PATH fallbacks, with isolated `-E -P -B` probes and launches. |
| Corrupted graph.json | `_load_graph()` in `serve.py` wraps `json.JSONDecodeError` and prints a clear recovery message instead of crashing. |

### Interpreter selection trust boundary

The atomic interpreter-pointer writer prevents partial updates and rejects
unsafe pointer parents, destinations, symlinks, and non-regular files. The
pointer remains advisory: readers do not execute the pathname stored there.
Fresh generated commands discover a candidate from the active installed
Graphify environment and validate final CPython `>=3.14.2,<3.15` plus the
installed `graphifyy` distribution before use. `graphify hook install` has a
separate authority class: the Python process already running the install is a
trusted user-selected input, so its lexical invocation path is embedded in the
hook, including project-local virtual environments. Launcher, shebang, and
`PATH` fallbacks are lower-authority and reject candidates contained in the
current workspace, explicitly selected corpus (`GRAPHIFY_INPUT_PATH`), or
selected output root (`GRAPHIFY_OUTPUT_ROOT`, with `GRAPHIFY_OUT` retained for
hook compatibility) before their first execution. Generated blocks containing
`INPUT_PATH` bind that corpus before discovery. These roots only widen denial
and never select an interpreter.

On POSIX, pointer creation enforces regular-file, symlink, ownership, and
writable-permission assumptions with directory-handle-relative atomic
replacement. Publication is atomic to filesystem observers, but Graphify does
not fsync the parent directory, so advisory rename durability across abrupt
power loss is not guaranteed. The Python standard library does not provide an equivalent
handle-relative, reparse-point-safe Windows namespace write. Graphify therefore
fails closed and does not publish `.graphify_python` on Windows. Windows
bootstrap remains operational through its freshly discovered and validated
`$GraphifyPython`; it emits an explicit warning because the pointer is optional
advisory state and never execution authority. Atomic replacement also cannot
prevent the same user, an administrator, or a process with equivalent filesystem
rights from replacing an explicitly trusted virtual environment or installed
interpreter after installation. Protect those environments with normal OS
ownership and ACL controls, and rerun `graphify hook install` after reinstalling
or moving Graphify.

### What graphify does NOT do

- Does not run a network listener by default (stdio transport); `--transport http` is opt-in, documented in the README, and binds to `127.0.0.1` unless `--host 0.0.0.0` is passed
- Does not execute code from source files (tree-sitter parses ASTs - no eval/exec)
- Does not use `shell=True` in any subprocess call
- Does not store credentials or API keys

### Optional network calls

- `ingest` subcommand: fetches URLs explicitly provided by the user
- PDF extraction: reads local files only (pypdf does not make network calls)
- watch mode: local filesystem events only (watchdog does not make network calls)
