"""Tests for the tools/skillgen generator and the claude lean-core split.

skillgen renders graphify's committed skill artifacts from human-edited
fragments. These tests lock in the anti-drift guards (``--check``,
``--audit-coverage``), the render idempotency, and the lean-core invariant: the
core runs a default extraction with zero reference reads, on-demand content
lives only in the references, and no reference duplicates core content.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import shutil
import sys
from pathlib import Path

import pytest

# tests/ -> repo root is one parent up; put it on the path so tools.skillgen
# imports regardless of pytest's import mode.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.skillgen import gen  # noqa: E402


_WINDOWS_POWERSHELL = sys.platform == "win32" and (
    shutil.which("pwsh") is not None or shutil.which("powershell") is not None
)


def _windows_powershell_51_available() -> bool:
    if sys.platform != "win32":
        return False
    executable = shutil.which("powershell.exe")
    if executable is None:
        return False
    result = subprocess.run(
        [
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$PSVersionTable.PSVersion.ToString()",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip().startswith("5.1")


def _run_powershell_script(
    executable: str,
    script: str,
    tmp_path: Path,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[bytes]:
    script_path = tmp_path / "test-script.ps1"
    script_path.write_bytes(script.encode("utf-8"))
    return subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-File", str(script_path)],
        cwd=cwd,
        env=env,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def test_audit_coverage_passes():
    """Every v8 heading lands in the lean core or exactly one reference."""
    platforms = gen.load_platforms()
    problems = gen.audit_coverage(platforms["claude"])
    assert problems == [], "\n".join(problems)


def test_check_passes():
    """The committed artifacts and the expected/ snapshot match a fresh render.

    This is the CI / pre-commit drift guard. A failure here means someone
    hand-edited a generated file or forgot to re-run the generator.
    """
    platforms = gen.load_platforms()
    artifacts = gen.render_all(platforms, only="claude")
    problems = gen.check(artifacts)
    assert problems == [], "\n".join(problems)


def test_render_is_idempotent():
    """Rendering twice yields byte-identical output (no timestamps/versions)."""
    platforms = gen.load_platforms()
    first = gen.render_all(platforms, only="claude")
    second = gen.render_all(platforms, only="claude")
    assert [(a.path, a.content) for a in first] == [(a.path, a.content) for a in second]


def test_render_output_is_lf_only():
    """Generated artifacts use LF newlines and end in exactly one newline."""
    platforms = gen.load_platforms()
    for art in gen.render_all(platforms, only="claude"):
        assert "\r" not in art.content, art.path
        assert art.content.endswith("\n"), art.path
        assert not art.content.endswith("\n\n"), art.path


def test_no_version_or_timestamp_in_output():
    """No generated artifact carries the package version string."""
    from graphify.__main__ import __version__

    platforms = gen.load_platforms()
    for art in gen.render_all(platforms, only="claude"):
        assert __version__ not in art.content, f"{art.path} leaked a version string"


def _claude_artifacts():
    platforms = gen.load_platforms()
    arts = gen.render_all(platforms, only="claude")
    core = next(a for a in arts if a.path == "graphify/skill.md")
    refs = {a.path.rsplit("/", 1)[-1]: a.content for a in arts if a.path != "graphify/skill.md"}
    return core.content, refs


def test_lean_core_has_no_reference_only_content():
    """The core must not inline the execution detail of an on-demand reference.

    The ``## Usage`` flag table in the core deliberately lists every command,
    including the on-demand ones (it is the --help payload), so the markers
    below are execution-detail lines that never appear in that table.
    """
    core, _ = _claude_artifacts()
    # The full embedded subagent prompt lives only in extraction-spec.md.
    assert '"file_type":"code|document|paper|image|rationale|concept"' not in core
    # The incremental-update merge machinery lives only in update.md.
    assert "from graphify.build import build_merge" not in core
    assert "graphify cluster-only ." not in core
    # The vocab-expansion query flow lives only in query.md.
    assert "Constrained query expansion" not in core
    assert "save-result --question" not in core
    # The export commands live only in exports.md.
    assert "graphify export wiki" not in core
    assert "graphify export neo4j" not in core
    # The add / watch / hook flows live only in their references.
    assert "from graphify.ingest import ingest" not in core
    assert "graphify hook install" not in core
    assert "python3 -m graphify.watch" not in core


def test_lean_core_runs_default_pipeline_with_zero_references():
    """The default code-corpus run must be fully described inside the core."""
    core, _ = _claude_artifacts()
    # The whole default pipeline (detect -> AST -> build -> label -> HTML ->
    # report) must be present in the core so a plain run reads no reference.
    for needed in (
        "### Step 1 - Ensure graphify is installed",
        "### Step 2 - Detect files",
        "### Step 3 - Extract entities and relationships",
        "#### Part A - Structural extraction for code files",
        "#### Part C - Merge AST + semantic into final extraction",
        "### Step 4 - Build graph, cluster, analyze, generate outputs",
        "### Step 5 - Label communities",
        "### Step 6 - Generate Obsidian vault (opt-in) + HTML",
        "### Step 9 - Save manifest, update cost tracker, clean up, and report",
        "## Honesty Rules",
        "graphify export html",
    ):
        assert needed in core, f"lean core is missing default-pipeline content: {needed!r}"


def test_extraction_states_no_api_key_required_for_every_host():
    """Regression for #1461: every skill body that describes Step 3 extraction must
    state up front that no API key is required, tell the agent never to prompt for or
    block on one, and give a terminal-only (non-subagent) fallback.

    Hermes (and the other AGENTS.md hosts) run the CLI directly and can't dispatch
    subagents; the old text framed the no-key path only as 'dispatch subagents as
    written', so those agents looped for minutes insisting on a missing API key.
    """
    platforms = gen.load_platforms()
    arts = gen.render_all(platforms)
    bodies = [a for a in arts
              if "### Step 3 - Extract entities and relationships" in a.content]
    assert bodies, "no rendered skill body contains the Step 3 extraction section"
    for a in bodies:
        assert "graphify needs no API key" in a.content, a.path
        assert "Never ask the user for one, and never block on one." in a.content, a.path
        # the no-key fallback must not be framed *only* around subagent dispatch
        assert "cannot dispatch subagents" in a.content, a.path
        # where a host prints the GEMINI key tip, the clarity must precede it (be
        # hoisted) rather than sit buried after the key check (aider/devin print no
        # tip — they are the model themselves — so the check only applies if present)
        tip = "Tip: set `GEMINI_API_KEY`"
        if tip in a.content:
            assert a.content.index("graphify needs no API key") < a.content.index(tip), \
                f"{a.path}: no-key clarity is not hoisted above the GEMINI tip"


def test_references_contain_no_core_pipeline_content():
    """No reference fragment may duplicate the core build pipeline."""
    _, refs = _claude_artifacts()
    # Distinctive lines from the core build/label steps must not appear in any
    # reference, or the same content would be double-homed.
    core_only_markers = (
        "from graphify.cluster import cluster, score_all",
        "### Step 4 - Build graph, cluster, analyze, generate outputs",
        "### Step 5 - Label communities",
        "## Honesty Rules",
    )
    for name, body in refs.items():
        for marker in core_only_markers:
            assert marker not in body, f"reference {name} leaked core content: {marker!r}"


def test_reference_pointers_in_core_resolve_to_real_fragments():
    """Every references/<name>.md the core points at is actually rendered."""
    import re

    core, refs = _claude_artifacts()
    pointed = set(re.findall(r"references/([\w-]+)\.md", core))
    rendered = {name[: -len(".md")] for name in refs}
    missing = pointed - rendered
    assert not missing, f"core points at references that were not rendered: {missing}"


def test_query_heading_is_homed_in_core_stub_only():
    """The query section heading is the lean-core stub; query.md re-homes the rest."""
    core, refs = _claude_artifacts()
    core_headings = set(gen.headings(core))
    query_headings = set(gen.headings(refs["query.md"]))
    assert "## For /graphify query" in core_headings
    assert "## For /graphify query" not in query_headings
    # The deeper query content moved into the reference.
    assert "## For /graphify path" in query_headings
    assert "## For /graphify explain" in query_headings
    assert "## For /graphify path" not in core_headings


def test_eight_references_render_for_claude():
    """claude renders exactly the eight on-demand fragments from the design."""
    _, refs = _claude_artifacts()
    assert sorted(refs) == [
        "add-watch.md",
        "exports.md",
        "extraction-spec.md",
        "github-and-merge.md",
        "hooks.md",
        "query.md",
        "transcribe.md",
        "update.md",
    ]


def test_headings_helper_ignores_code_fence_comments():
    """The fence-aware heading scanner must skip '#' lines inside code fences."""
    md = (
        "# Real Heading\n"
        "\n"
        "```bash\n"
        "# not a heading, a shell comment\n"
        "echo hi\n"
        "```\n"
        "\n"
        "## Another Real One\n"
    )
    assert gen.headings(md) == ["# Real Heading", "## Another Real One"]


def test_enum_is_full_six_value_superset_in_extraction_spec():
    """Decision A: the file_type enum is the full six-value superset."""
    _, refs = _claude_artifacts()
    spec = refs["extraction-spec.md"]
    assert "`code`, `document`, `paper`, `image`, `rationale`, `concept`" in spec
    assert '"file_type":"code|document|paper|image|rationale|concept"' in spec


# --- codex + windows (the divergent split hosts) -------------------------------


def _platform_artifacts(key):
    platforms = gen.load_platforms()
    arts = gen.render_all(platforms, only=key)
    skill_dst = platforms[key].skill_dst
    core = next(a for a in arts if a.path == skill_dst)
    refs = {a.path.rsplit("/", 1)[-1]: a.content for a in arts if a.path != skill_dst}
    return core.content, refs


def test_check_passes_for_codex_and_windows():
    """The committed codex/windows artifacts match a fresh render and expected/."""
    platforms = gen.load_platforms()
    for key in ("codex", "windows"):
        artifacts = gen.render_all(platforms, only=key)
        problems = gen.check(artifacts)
        assert problems == [], f"[{key}]\n" + "\n".join(problems)


def test_audit_coverage_passes_for_codex_and_windows():
    """Every v8 heading single-homes for the cli-inline split hosts too."""
    platforms = gen.load_platforms()
    for key in ("codex", "windows"):
        problems = gen.audit_coverage(platforms[key])
        assert problems == [], f"[{key}]\n" + "\n".join(problems)


UNIFIED_DESCRIPTION = (
    "Use for any question about a codebase, its architecture, file relationships, "
    "or project content — especially when graphify-out/ exists, where the question "
    "should be treated as a graphify query first. Turns any input (code, docs, "
    "papers, images, videos) into a persistent knowledge graph with god nodes, "
    "community detection, and query/path/explain tools."
)


def test_descriptions_are_unified():
    """Every platform now carries one unified frontmatter description, byte for byte.

    The two drifted v8 descriptions (claude's short one and the richer 14-host
    line) were collapsed into a single discovery-tuned line that leads with the
    use-condition. Every split host and both monoliths must carry it verbatim,
    and none of the old wording may survive.
    """
    expected_line = f'description: "{UNIFIED_DESCRIPTION}"'
    platforms = gen.load_platforms()
    for key, p in platforms.items():
        body = gen.render(p)[0].content
        assert expected_line in body, f"[{key}] missing the unified description line"
        # None of the drifted v8 wording may survive on any platform.
        assert "Provides persistent graph with god nodes" not in body, f"[{key}] kept old wording"
        assert "treat the question as a /graphify query." not in body, f"[{key}] kept old wording"
        assert "clustered communities" not in body, f"[{key}] kept old wording"


def test_windows_frontmatter_name_and_shell_and_extra():
    """windows: name must be `graphify` (folder-name rule, #1635), powershell
    install, troubleshooting tail."""
    core, _ = _platform_artifacts("windows")
    # Claude Code requires the frontmatter name to equal the install folder
    # (graphify); a `graphify-windows` name broke skill discovery (#1635).
    assert core.startswith("---\nname: graphify\n")
    assert "```powershell" in core
    assert "function Resolve-GraphifyAmbientCommand" in core
    assert "## Troubleshooting" in core
    assert "### PowerShell 5.1: Vertical scrolling stops working" in core
    # The troubleshooting section sits before Honesty Rules, single separator.
    assert "\n4. **Skip graspologic-native**" in core
    assert core.index("## Troubleshooting") < core.index("## Honesty Rules")


def test_every_skill_bootstrap_selects_supported_python314():
    platforms = gen.load_platforms()
    for key, platform in platforms.items():
        body = gen.render(platform)[0].content
        bootstrap = body[body.index("### Step 1"):body.index("### Step 2")]
        assert re.search(
            r"tool install --python [\"']>=3\.14\.2,<3\.15[\"'] --upgrade graphifyy",
            bootstrap,
        ), key
        assert "sys.implementation.name ==" in bootstrap, key
        assert "sys.version_info.releaselevel ==" in bootstrap, key
        assert "(3, 14, 2) <= sys.version_info[:3] < (3, 15, 0)" in bootstrap, key
        assert 'PYTHON="python3"' not in body, key
        if "## Interpreter guard for subcommands" in body:
            assert "freshly discover and bind a trusted interpreter first" in body, key
            assert "before every subcommand" in body, key
            assert "never execute the advisory pointer or persist a" in body, key
        assert "advisory metadata" in body, key
        assert "runtime authority" in body, key
        assert "Pointer symlink and time-of-check/time-of-use hardening" not in body, key
        if platform.shell == "powershell":
            assert "function Resolve-GraphifyAmbientCommand" in bootstrap, key
            assert "function Test-GraphifyWorkspacePath" in bootstrap, key
            assert "function Test-GraphifyPython" in bootstrap, key
            assert 'foreach ($name in @("python3.14", "python3", "py", "python"))' in bootstrap, key
            assert "Resolve-GraphifyAmbientCommand $name" in bootstrap, key
            assert "& $candidate -3.14 -E -P -B -c" in bootstrap, key
            assert "& $installPython -E -P -B -m pip install graphifyy" in bootstrap, key
            assert "& $uv tool install" in bootstrap, key
            assert "\n        pip install graphifyy" not in bootstrap, key
        else:
            assert '"$_gfy_uv" tool install --python \'>=3.14.2,<3.15\'' in bootstrap, key
            assert '_gfy_uv_dir=$("$_gfy_uv" tool dir 2>/dev/null)' in bootstrap, key
            assert "uv tool run" not in bootstrap, key
            assert "for _gfy_name in python3.14 python3 python" in bootstrap, key
            assert '"/usr/bin/env "*)' in bootstrap, key
            assert '_gfy_env_command=${_gfy_shebang#"/usr/bin/env "}' in bootstrap, key
            assert '""|*[!a-zA-Z0-9_.@+-]*) _gfy_shebang="" ;;' in bootstrap, key
            assert '_graphify_ambient_usable "$_gfy_shebang"' in bootstrap, key
            assert '"$PYTHON" -E -P -B -m pip install graphifyy' in bootstrap, key
            assert "head -1" not in bootstrap and "tr -d" not in bootstrap, key


@pytest.mark.parametrize("platform_key", ["aider", "devin"])
def test_monolithic_skills_route_mcp_and_watch_through_fresh_interpreter(platform_key):
    body = gen.render(gen.load_platforms()[platform_key])[0].content

    assert "graphify-mcp graphify-out/graph.json" not in body
    assert not re.search(r"^graphify watch INPUT_PATH --debounce 3$", body, re.MULTILINE)
    assert body.count('"$GRAPHIFY_PYTHON" -E -P -B -m graphify.serve') == 1
    assert body.count('"$GRAPHIFY_PYTHON" -E -P -B -m graphify watch') == 1
    assert "$(cat graphify-out/.graphify_python)" not in body
    assert body.count("successfully rerun Step 1") == 2
    assert body.count("advisory metadata only") >= 2
    assert "is freshly validated and overwritten" not in body
    assert body.count(
        'graph_path = os.path.join(os.path.realpath(os.getcwd()), '
        '"graphify-out", "graph.json")'
    ) == 1
    assert body.count(
        '"args": ["-E", "-P", "-B", "-m", "graphify.serve", graph_path]'
    ) == 1
    assert '"args": ["-E", "-P", "-B", "-m", "graphify.serve", sys.argv[2]]' not in body
    assert "python3 -m graphify.serve" not in body
    assert "python3 -m graphify.watch" not in body
    assert '"command": "python3"' not in body


def test_powershell_troubleshooting_uninstalls_with_fresh_interpreter():
    body = gen.render(gen.load_platforms()["windows"])[0].content
    command = "& $GraphifyPython -E -P -B -m pip uninstall graspologic-native"
    assert command in body
    assert "& $GraphifyPython -E -P -B -m pip install --upgrade graphifyy" in body
    assert "Get-Content graphify-out\\.graphify_python" not in body
    assert "(`pip uninstall graspologic-native`)" not in body


def test_bootstraps_publish_advisory_pointer_through_shared_module():
    """Every Step 1 owner uses the hardened writer, never an inline workspace write."""
    for key, platform in gen.load_platforms().items():
        body = gen.render(platform)[0].content
        bootstrap = body[body.index("### Step 1"):body.index("### Step 2")]
        assert "-m graphify.interpreter_pointer write" in bootstrap, key
        assert "open('graphify-out/.graphify_python'" not in bootstrap, key
        assert "Out-File -FilePath graphify-out\\.graphify_python" not in bootstrap, key
        if platform.shell == "powershell":
            assert "Write-Warning" in bootstrap, key
            assert "Failed to publish the advisory Graphify interpreter pointer" not in bootstrap, key


def test_all_renders_have_no_execution_bearing_pointer_reads():
    """The advisory file must never supply a command, probe, config, or launcher."""
    forbidden = (
        r"\$\(cat\s+graphify-out/\.graphify_python\)",
        r"\bcat\s+(?:--\s+)?[\"']?graphify-out/\.graphify_python",
        r"Get-Content[^\n]*graphify_python",
        r"_GRAPHIFY_SAVED=\$\(cat",
        r"command[^\n]*absolute path from: cat graphify-out/\.graphify_python",
    )
    rendered = gen.render_all(gen.load_platforms())
    assert rendered
    for artifact in rendered:
        for pattern in forbidden:
            assert not re.search(pattern, artifact.content, re.IGNORECASE), (
                artifact.path,
                pattern,
            )


def test_all_renders_describe_pointer_as_advisory_only():
    """Generated prose must not contradict fresh-discovery execution authority."""
    stale = (
        "saved interpreter",
        ".graphify_python` contains a supported Python",
        "Pointer symlink and time-of-check/time-of-use hardening",
        "all subsequent steps read this",
        ".graphify_python` is freshly validated and overwritten",
    )
    for artifact in gen.render_all(gen.load_platforms()):
        for phrase in stale:
            assert phrase.lower() not in artifact.content.lower(), (artifact.path, phrase)


def test_static_mcp_configuration_does_not_delegate_command_selection_to_pointer():
    """Static MCP JSON must be produced from fresh discovery, not user-copied pointer text."""
    found = 0
    for artifact in gen.render_all(gen.load_platforms()):
        for block in re.findall(r"```(?:bash|sh|powershell|json)\n(.*?)\n```", artifact.content, re.DOTALL):
            if "graphify.serve" not in block or '"command"' not in block:
                continue
            found += 1
            assert ".graphify_python" not in block, artifact.path
            assert "<absolute path from:" not in block, artifact.path
            assert '"args": ["-E", "-P", "-B", "-m", "graphify.serve"' in block, artifact.path
    assert found > 0


def test_powershell_operational_blocks_bind_discovery_and_never_read_pointer():
    """PowerShell coverage is structural on non-Windows hosts and runtime when available."""
    body = gen.render(gen.load_platforms()["windows"])[0].content
    blocks = re.findall(r"```powershell\n(.*?)\n```", body, re.DOTALL)
    operational = [block for block in blocks if " -m graphify" in block]
    assert operational
    for block in operational:
        assert "Get-Content" not in block or ".graphify_python" not in block
        assert " -E -P -B -m graphify" in block
        assert "Get-Command" in block or "GraphifyPython" in block


def _posix_bootstrap_script() -> str:
    platform = gen.load_platforms()["claude"]
    body = gen.render(platform)[0].content
    bootstrap = body[body.index("### Step 1"):body.index("### Step 2")]
    return bootstrap.split("```bash\n", 1)[1].split("\n```", 1)[0].replace(
        "GRAPHIFY_INPUT_PATH='INPUT_PATH'", "GRAPHIFY_INPUT_PATH='.'"
    )


def test_posix_bootstrap_helper_replaces_only_standalone_input_placeholder():
    script = _posix_bootstrap_script()

    assert script.startswith("GRAPHIFY_INPUT_PATH='.'\n")
    assert '${GRAPHIFY_INPUT_PATH-}' in script
    assert '_GRAPHIFY_INPUT_ROOT=$(_graphify_canonical_root "${GRAPHIFY_INPUT_PATH-}")' in script


def _powershell_step1_scripts() -> tuple[str, str]:
    platform = gen.load_platforms()["windows"]
    body = gen.render(platform)[0].content
    step1 = body[body.index("### Step 1"):body.index("### Step 2")]
    scripts = re.findall(r"```powershell\n(.*?)\n```", step1, flags=re.DOTALL)
    assert len(scripts) == 2
    bootstrap, root_persistence = scripts
    assert step1.index(bootstrap) < step1.index(root_persistence)
    return bootstrap, root_persistence


def _powershell_bootstrap_script() -> str:
    return _powershell_step1_scripts()[0]


def _powershell_root_persistence_script() -> str:
    return _powershell_step1_scripts()[1].replace("INPUT_PATH", ".")


def _isolated_bootstrap_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for utility in ("cat", "head", "tail", "tr"):
        resolved = shutil.which(utility)
        assert resolved is not None
        (bin_dir / utility).symlink_to(resolved)
    return bin_dir


def _isolated_python(tmp_path: Path, name: str) -> tuple[Path, Path]:
    environment = tmp_path / name
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(environment)],
        check=True,
        capture_output=True,
    )
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    site_packages = Path(
        subprocess.check_output(
            [
                str(python),
                "-E",
                "-P",
                "-B",
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            text=True,
        ).strip()
    )
    return python, site_packages


def _offline_python_with_trusted_fake_pip(tmp_path: Path) -> tuple[Path, Path, Path]:
    python, site_packages = _isolated_python(tmp_path, "offline-python")
    pip_marker = tmp_path / "trusted-pip-ran"
    graphify_marker = tmp_path / "trusted-graphify-ran"
    pip_package = site_packages / "pip"
    pip_package.mkdir()
    (pip_package / "__init__.py").write_text("", encoding="utf-8")
    graphify_main = (
        "from pathlib import Path\n"
        f"Path({str(graphify_marker)!r}).touch()\n"
    )
    (pip_package / "__main__.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(pip_marker)!r}).write_text(' '.join(__import__('sys').argv[1:]))\n"
        f"package = Path({str(site_packages)!r}) / 'graphify'\n"
        f"dist_info = Path({str(site_packages)!r}) / 'graphifyy-0.10.0.dist-info'\n"
        "if 'install' in __import__('sys').argv:\n"
        "    package.mkdir(exist_ok=True)\n"
        "    dist_info.mkdir(exist_ok=True)\n"
        "    (dist_info / 'METADATA').write_text(\n"
        "        'Metadata-Version: 2.1\\nName: graphifyy\\nVersion: 0.10.0\\n'\n"
        "    )\n"
        "    (dist_info / 'RECORD').write_text('graphify/__init__.py,,\\n')\n"
        "    (package / '__init__.py').write_text('TRUSTED = True\\n')\n"
        f"    (package / '__main__.py').write_text({graphify_main!r})\n"
        "    (package / 'interpreter_pointer.py').write_text(\n"
        "        \"from pathlib import Path\\nimport sys\\nPath(sys.argv[2]).parent.mkdir(parents=True, exist_ok=True)\\nPath(sys.argv[2]).write_text(sys.executable)\\n\"\n"
        "    )\n",
        encoding="utf-8",
    )
    return python, pip_marker, graphify_marker


def _disposable_graphify_python(
    tmp_path: Path,
    name: str,
    *,
    package_root: Path | None = None,
    distribution: bool = True,
    metadata_name: str = "graphifyy",
    direct_url: dict[str, object] | str | None = None,
    namespace_package: bool = False,
    owns_graphify_package: bool = True,
) -> tuple[Path, Path]:
    python, site_packages = _isolated_python(tmp_path, name)
    source_root = package_root or site_packages
    package = source_root / "graphify"
    package.mkdir(parents=True)
    marker = tmp_path / f"{name}-graphify-ran"
    if not namespace_package:
        (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).touch()\n",
        encoding="utf-8",
    )
    if source_root != site_packages:
        (site_packages / f"{name}.pth").write_text(f"{source_root}\n", encoding="utf-8")
    if distribution:
        dist_info = site_packages / "graphifyy-0.10.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {metadata_name}\nVersion: 0.10.0\n",
            encoding="utf-8",
        )
        owned_files = "graphify/__init__.py,,\n" if owns_graphify_package else ""
        (dist_info / "RECORD").write_text(owned_files, encoding="utf-8")
        if direct_url is not None:
            payload = direct_url if isinstance(direct_url, str) else json.dumps(direct_url)
            (dist_info / "direct_url.json").write_text(payload, encoding="utf-8")
    return python, marker


def _run_posix_query_with_python(
    tmp_path: Path,
    python: Path,
    *,
    cwd: Path,
    trusted: bool,
    discovery_source: str = "path",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / f"candidate-bin-{python.parent.parent.name}-{discovery_source}"
    bin_dir.mkdir(exist_ok=True)
    delegate = f'#!/bin/sh\nexec "{python}" "$@"\n'
    if discovery_source == "path":
        candidate = bin_dir / "python3.14"
        candidate.write_text(delegate, encoding="utf-8")
        candidate.chmod(0o755)
    elif discovery_source == "launcher":
        launcher = bin_dir / "graphify"
        launcher.write_text(f"#!{python}\n", encoding="utf-8")
        launcher.chmod(0o755)
    elif discovery_source == "uv":
        tool_dir = tmp_path / f"uv-tools-{python.parent.parent.name}"
        candidate = tool_dir / "graphifyy" / "bin" / "python"
        candidate.parent.mkdir(parents=True)
        candidate.write_text(delegate, encoding="utf-8")
        candidate.chmod(0o755)
        uv = bin_dir / "uv"
        uv.write_text(
            "#!/bin/sh\n"
            f"[ \"$1 $2\" = \"tool dir\" ] && printf '%s\\n' '{tool_dir}' && exit 0\n"
            "exit 97\n",
            encoding="utf-8",
        )
        uv.chmod(0o755)
    elif discovery_source == "pipx":
        venvs = tmp_path / f"pipx-venvs-{python.parent.parent.name}"
        candidate = venvs / "graphifyy" / "bin" / "python"
        candidate.parent.mkdir(parents=True)
        candidate.write_text(delegate, encoding="utf-8")
        candidate.chmod(0o755)
        pipx = bin_dir / "pipx"
        pipx.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' '{venvs}'\n",
            encoding="utf-8",
        )
        pipx.chmod(0o755)
    else:
        raise AssertionError(discovery_source)
    env = {**os.environ, "PATH": str(bin_dir), **(extra_env or {})}
    if trusted:
        env["VIRTUAL_ENV"] = str(python.parent.parent)
    else:
        env.pop("VIRTUAL_ENV", None)
    return subprocess.run(
        ["/bin/bash", "-c", _query_block_as_help()],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_python_shadows(tmp_path: Path) -> tuple[Path, list[Path]]:
    markers = [
        tmp_path / "cwd-pip-shadow-ran",
        tmp_path / "cwd-graphify-shadow-ran",
        tmp_path / "cwd-sitecustomize-ran",
        tmp_path / "pythonpath-pip-shadow-ran",
        tmp_path / "pythonpath-graphify-shadow-ran",
        tmp_path / "pythonpath-sitecustomize-ran",
    ]
    roots = (tmp_path, tmp_path / "pythonpath-shadow")
    for root, marker_offset in zip(roots, (0, 3), strict=True):
        root.mkdir(exist_ok=True)
        (root / "pip.py").write_text(
            f"from pathlib import Path\nPath({str(markers[marker_offset])!r}).touch()\n",
            encoding="utf-8",
        )
        package = root / "graphify"
        package.mkdir()
        for module in ("__init__.py", "__main__.py"):
            (package / module).write_text(
                f"from pathlib import Path\nPath({str(markers[marker_offset + 1])!r}).touch()\n",
                encoding="utf-8",
            )
        (root / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(markers[marker_offset + 2])!r}).touch()\n",
            encoding="utf-8",
        )
    return roots[1], markers


def _executable_blocks(body: str) -> list[tuple[str, str]]:
    return re.findall(r"```(bash|sh|powershell)\n(.*?)\n```", body, flags=re.DOTALL)


def _block_containing(body: str, needle: str) -> str:
    matches = [block for _, block in _executable_blocks(body) if needle in block]
    assert len(matches) == 1, (needle, len(matches))
    return matches[0]


def test_step1_bootstrap_targets_only_install_step():
    platforms = gen.load_platforms()
    rendered = {key: gen.render(platform) for key, platform in platforms.items()}
    assert len(rendered) == 16

    install_heading = "### Step 1 - Ensure graphify is installed"
    install_marker = "Installation is the only discovery path allowed to mutate the environment."
    pointer_write = "graphify.interpreter_pointer write"
    split_queries = []
    for key, artifacts in rendered.items():
        platform = platforms[key]
        primary = artifacts[0].content
        assert primary.count(install_heading) == 1, key
        assert primary.count(install_marker) == 1, key
        assert primary.count(pointer_write) == 1, key
        if platform.bucket == "split":
            query = next(
                artifact.content
                for artifact in artifacts
                if artifact.path.endswith("/references/query.md")
            )
            split_queries.append((key, query))

    assert len(split_queries) == 14
    for key, query in split_queries:
        assert install_heading not in query, key
        assert install_marker not in query, key
        assert pointer_write not in query, key
        ordered = (
            "if not Path('graphify-out/graph.json').exists():",
            "### Step 0 — Constrained query expansion",
            "### Step 1 — Traversal",
            '-m graphify query "QUESTION"',
        )
        offsets = [query.index(needle) for needle in ordered]
        assert offsets == sorted(offsets), key

    posix = platforms["claude"]
    target = f"{install_heading}\n\n```bash\necho old\n```\n"
    with pytest.raises(ValueError):
        gen._render_step1_bootstrap("# no install target\n", posix, artifact_role="core")
    with pytest.raises(ValueError):
        gen._render_step1_bootstrap("# no install target\n", posix, artifact_role="monolith")
    untouched = "### Step 1 — Traversal\n\n```bash\necho query\n```\n"
    assert gen._render_step1_bootstrap(
        untouched, posix, artifact_role="reference"
    ) == untouched
    for role in ("core", "monolith", "reference"):
        with pytest.raises(ValueError):
            gen._render_step1_bootstrap(target + target, posix, artifact_role=role)


@pytest.mark.parametrize("candidate_path", ["lexical", "physical"])
def test_posix_operational_input_binding_reaches_privileged_discovery_child(
    tmp_path, candidate_path
):
    _, refs = _platform_artifacts("claude")
    block = _block_containing(refs["update.md"], "-m graphify update INPUT_PATH")
    guard = gen._POSIX_OPERATION_GUARD
    assert guard.startswith("GRAPHIFY_PYTHON=$(")
    assert "/bin/sh -p -c" in guard
    outer_tail = (
        "); GRAPHIFY_PYTHON=${GRAPHIFY_PYTHON%x}; "
        "GRAPHIFY_PYTHON=${GRAPHIFY_PYTHON:?Graphify interpreter discovery failed}"
    )
    assert guard.endswith(outer_tail)
    assert guard[guard.rfind("); ") + 3 :] == outer_tail[3:]

    project = tmp_path / "project"
    project.mkdir()
    physical_input = tmp_path / "physical input root"
    candidate_bin = physical_input / "bin"
    candidate_bin.mkdir(parents=True)
    input_alias = tmp_path / "input alias with spaces"
    input_alias.symlink_to(physical_input, target_is_directory=True)
    marker = tmp_path / f"{candidate_path}-candidate-ran"
    _write_executable_sentinel(
        candidate_bin / "python3.14", marker, delegate=Path(sys.executable)
    )
    selected_bin = input_alias / "bin" if candidate_path == "lexical" else candidate_bin
    script = block.replace(
        "GRAPHIFY_INPUT_PATH='INPUT_PATH'",
        f"GRAPHIFY_INPUT_PATH='{input_alias}'",
        1,
    ).replace(
        '"$GRAPHIFY_PYTHON" -E -P -B -m graphify update INPUT_PATH',
        '"$GRAPHIFY_PYTHON" -E -P -B -m graphify --help',
        1,
    )
    env = {**os.environ, "PATH": str(selected_bin)}
    env.pop("VIRTUAL_ENV", None)

    result = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert not marker.exists()


def test_posix_generated_discovery_uses_privileged_builtin_pwd(tmp_path):
    assert "/bin/pwd" not in gen._POSIX_DISCOVERY
    assert "/bin/pwd" not in gen._POSIX_OPERATION_GUARD
    assert "/bin/pwd" not in gen._MCP_CONFIG["posix"]
    assert gen._POSIX_DISCOVERY.count("command pwd -P") >= 3

    project = tmp_path / "project"
    project.mkdir()
    sentinel_bin = tmp_path / "sentinel-bin"
    sentinel_bin.mkdir()
    path_marker = tmp_path / "path-pwd-ran"
    _write_executable_sentinel(sentinel_bin / "pwd", path_marker)
    env = {
        **os.environ,
        "PATH": str(sentinel_bin) + os.pathsep + os.environ.get("PATH", ""),
        "VIRTUAL_ENV": str(Path(sys.executable).parent.parent),
    }

    builtin = subprocess.run(
        ["/bin/sh", "-p", "-c", "command pwd -P"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert builtin.returncode == 0, builtin.stderr
    assert builtin.stdout.strip() == str(project.resolve())
    assert not path_marker.exists()

    for guard_source, guard in (
        ("owner", gen._POSIX_OPERATION_GUARD),
        ("rendered-query", _query_block_as_help()),
    ):
        control = tmp_path / f"{guard_source}-control"
        script = guard + "\n" + (
            '"$GRAPHIFY_PYTHON" -E -P -B -c '
            "'from pathlib import Path; import sys; Path(sys.argv[1]).touch()' "
            '"$GFY_CONTROL_MARKER"\n'
        )
        result = subprocess.run(
            ["/bin/bash", "-c", script],
            cwd=project,
            env={**env, "GFY_CONTROL_MARKER": str(control)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert control.exists()
        assert not path_marker.exists()

    config = gen._MCP_CONFIG["posix"].replace(
        "@@GRAPHIFY_GUARD@@", gen._POSIX_OPERATION_GUARD
    )
    config_script = config.split("```bash\n", 1)[1].rsplit("\n```", 1)[0]
    child = tmp_path / "mcp-config.sh"
    child.write_text(config_script, encoding="utf-8")
    function_marker = tmp_path / "function-pwd-ran"
    parent = f"""
function pwd {{ : > "{function_marker}"; return 97; }}
export -f pwd
exec /bin/bash "$1"
"""
    result = subprocess.run(
        ["/bin/bash", "-c", parent, "hostile-pwd-parent", str(child)],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["mcpServers"]["graphify"]["args"][-1] == str(
        project.resolve() / "graphify-out" / "graph.json"
    )
    assert not function_marker.exists()
    assert not path_marker.exists()


def test_powershell_discovery_uses_ps51_compatible_fully_qualified_path_check():
    discovery = gen._POWERSHELL_DISCOVERY
    bootstrap, root_persistence = _powershell_step1_scripts()
    windows_artifacts = gen.render(gen.load_platforms()["windows"])

    assert "IsPathFullyQualified" not in discovery
    assert "IsPathFullyQualified" not in gen._POWERSHELL_BOOTSTRAP
    assert all("IsPathFullyQualified" not in artifact.content for artifact in windows_artifacts)
    assert discovery.count("function Test-GraphifyFullyQualifiedPath") == 1
    assert "Test-GraphifyFullyQualifiedPath $Path" in discovery
    assert "Test-GraphifyFullyQualifiedPath $path" in discovery
    assert 'Test-GraphifyFullyQualifiedPath "$env:VIRTUAL_ENV"' in discovery
    assert gen._POWERSHELL_BOOTSTRAP.count(
        'Test-GraphifyFullyQualifiedPath "$env:VIRTUAL_ENV"'
    ) == 2

    from tree_sitter import Language, Parser
    import tree_sitter_powershell

    parser = Parser(Language(tree_sitter_powershell.language()))
    for source in (bootstrap, root_persistence):
        tree = parser.parse(source.encode())
        assert not tree.root_node.has_error, tree.root_node


@pytest.mark.skipif(
    not _windows_powershell_51_available(),
    reason="Windows PowerShell 5.1 runtime unavailable; native compatibility validation gap",
)
def test_powershell_fully_qualified_path_check_matches_windows_semantics(tmp_path):
    executable = shutil.which("powershell.exe")
    assert executable is not None

    def ps_string(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    cases = {
        r"C:\absolute": True,
        "C:/absolute": True,
        r"\\server\share\absolute": True,
        str(tmp_path.resolve()): True,
        "": False,
        r"relative\path": False,
        "C:relative": False,
        r"\current-drive-relative": False,
        "/current-drive-relative": False,
    }
    entries = "\n".join(
        f"$Expected[{ps_string(path)}] = ${str(expected).lower()}"
        for path, expected in cases.items()
    )
    script = (
        "$GraphifyDiscoveryOptional = $true\n"
        + gen._POWERSHELL_DISCOVERY
        + "\n$Expected = [ordered]@{}\n"
        + entries
        + "\n$Actual = [ordered]@{}\n"
        + "foreach ($Entry in $Expected.GetEnumerator()) { "
        + "$Actual[$Entry.Key] = [bool](Test-GraphifyFullyQualifiedPath $Entry.Key) }\n"
        + "$Actual | ConvertTo-Json -Compress\n"
    )
    result = _run_powershell_script(
        executable,
        script,
        tmp_path,
        cwd=tmp_path,
        env=os.environ.copy(),
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    output = [line for line in result.stdout.decode().splitlines() if line.strip()]
    assert json.loads(output[-1]) == cases


def test_generated_query_references_preserve_traversal_step():
    rendered = gen.render_all(gen.load_platforms())
    queries = [
        artifact
        for artifact in rendered
        if artifact.path.endswith("/references/query.md")
    ]
    assert len(queries) == 14

    for artifact in queries:
        committed = (REPO_ROOT / artifact.path).read_text(encoding="utf-8")
        expected = gen._expected_path(artifact.path).read_text(encoding="utf-8")
        assert committed == artifact.content, artifact.path
        assert expected == artifact.content, artifact.path
        assert committed.count("### Step 1 — Traversal") == 1, artifact.path
        assert "### Step 0 — Constrained query expansion" in committed, artifact.path
        assert "if not Path('graphify-out/graph.json').exists():" in committed, artifact.path
        assert '-m graphify query "QUESTION"' in committed, artifact.path
        assert "### Step 1 - Ensure graphify is installed" not in committed, artifact.path
        assert (
            "Installation is the only discovery path allowed to mutate the environment."
            not in committed
        ), artifact.path
        assert "graphify.interpreter_pointer write" not in committed, artifact.path


def test_generated_artifacts_exclude_bin_pwd_and_ps7_only_path_api():
    rendered = gen.render_all(gen.load_platforms())
    assert len(rendered) == 134
    expected_paths = {gen._expected_path(artifact.path) for artifact in rendered}
    assert len(expected_paths) == 134
    assert set(gen.EXPECTED_DIR.iterdir()) == expected_paths

    for artifact in rendered:
        committed = (REPO_ROOT / artifact.path).read_text(encoding="utf-8")
        expected = gen._expected_path(artifact.path).read_text(encoding="utf-8")
        assert committed == artifact.content, artifact.path
        assert expected == artifact.content, artifact.path
        for content in (committed, expected):
            assert "/bin/pwd" not in content, artifact.path
            assert "IsPathFullyQualified" not in content, artifact.path
            if "/bin/sh -p -c" in content:
                assert "command pwd -P" in content, artifact.path
            if "$GraphifyPython = $null" in content:
                assert "function Test-GraphifyFullyQualifiedPath" in content, artifact.path


@pytest.mark.parametrize("command", ["python3.14", "python3"])
def test_posix_bootstrap_accepts_only_validated_supported_candidates(tmp_path, monkeypatch, command):
    bin_dir = _isolated_bootstrap_bin(tmp_path)
    (bin_dir / command).symlink_to(Path(sys.executable))
    monkeypatch.setenv("PATH", str(bin_dir))

    result = subprocess.run(
        ["/bin/bash", "-c", _posix_bootstrap_script()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    saved = (tmp_path / "graphify-out" / ".graphify_python").read_text()
    assert Path(saved).resolve() == Path(sys.executable).resolve()


def test_posix_bootstrap_never_executes_workspace_path_mkdir(tmp_path, monkeypatch):
    bin_dir = _isolated_bootstrap_bin(tmp_path)
    (bin_dir / "python3.14").symlink_to(Path(sys.executable))
    mkdir_marker = tmp_path / "ambient-mkdir-ran"
    mkdir = bin_dir / "mkdir"
    mkdir.write_text(f"#!/bin/sh\n: > '{mkdir_marker}'\nexit 97\n", encoding="utf-8")
    mkdir.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))

    result = subprocess.run(
        ["/bin/bash", "-c", _posix_bootstrap_script()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not mkdir_marker.exists()
    assert (tmp_path / "graphify-out" / ".graphify_python").is_file()


@pytest.mark.parametrize("existing_pointer", [False, True])
def test_posix_bootstrap_pointer_refusal_warns_once_and_continues_under_umask_0002(
    tmp_path, monkeypatch, existing_pointer
):
    bin_dir = _isolated_bootstrap_bin(tmp_path)
    (bin_dir / "python3.14").symlink_to(Path(sys.executable))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("VIRTUAL_ENV", str(Path(sys.executable).parent.parent))
    graphify_out = tmp_path / "graphify-out"
    pointer = graphify_out / ".graphify_python"
    old_pointer = "/preserved/old/python"
    if existing_pointer:
        graphify_out.mkdir(mode=0o775)
        graphify_out.chmod(0o775)
        pointer.write_text(old_pointer, encoding="utf-8")
        pointer.chmod(0o600)
    retained_python = tmp_path / "retained-python"
    script = (
        "umask 0002\n"
        + _posix_bootstrap_script()
        + f'\nprintf "%s" "$GRAPHIFY_PYTHON" > "{retained_python}"\n'
        + _query_block_as_help()
    )

    result = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    warning = "cannot safely publish the advisory interpreter pointer"
    assert result.stderr.lower().count(warning) == 1
    assert retained_python.read_text(encoding="utf-8") == str(Path(sys.executable))
    assert (graphify_out.stat().st_mode & 0o777) == 0o775
    if existing_pointer:
        assert pointer.read_text(encoding="utf-8") == old_pointer
    else:
        assert not pointer.exists()


def test_posix_bootstrap_directory_creation_failure_remains_terminal(tmp_path, monkeypatch):
    bin_dir = _isolated_bootstrap_bin(tmp_path)
    (bin_dir / "python3.14").symlink_to(Path(sys.executable))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("VIRTUAL_ENV", str(Path(sys.executable).parent.parent))
    graphify_out = tmp_path / "graphify-out"
    original = b"not-a-directory\x00\xff"
    graphify_out.write_bytes(original)

    result = subprocess.run(
        ["/bin/bash", "-c", "umask 0002\n" + _posix_bootstrap_script()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert graphify_out.read_bytes() == original
    assert "cannot safely publish the advisory interpreter pointer" not in result.stderr.lower()


def test_posix_bootstrap_rejects_unsupported_python_before_pip(tmp_path, monkeypatch):
    bin_dir = _isolated_bootstrap_bin(tmp_path)
    pip_marker = tmp_path / "pip-was-called"
    python3 = bin_dir / "python3"
    python3.write_text(
        "#!/bin/sh\n"
        f"case \"$*\" in *'-m pip'*) : > '{pip_marker}';; esac\n"
        "exit 1\n"
    )
    python3.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    result = subprocess.run(
        ["/bin/bash", "-c", _posix_bootstrap_script()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Graphify requires CPython 3.14.2" in result.stderr
    assert not pip_marker.exists()


def test_posix_bootstrap_uses_persistent_uv_tool_interpreter(tmp_path, monkeypatch):
    bin_dir = _isolated_bootstrap_bin(tmp_path)
    uv_tool_dir = tmp_path / "uv-tools"
    uv_python = uv_tool_dir / "graphifyy" / "bin" / "python"
    uv_python.parent.mkdir(parents=True)
    uv_python.symlink_to(Path(sys.executable))
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        "if [ \"$1 $2\" = \"tool dir\" ]; then\n"
        f"  printf '%s\\n' '{uv_tool_dir}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 99\n"
    )
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))

    result = subprocess.run(
        ["/bin/bash", "-c", _posix_bootstrap_script()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    saved = (tmp_path / "graphify-out" / ".graphify_python").read_text()
    assert Path(saved).resolve() == Path(sys.executable).resolve()


def test_posix_bootstrap_ignores_unsupported_existing_graphify_shebang(tmp_path, monkeypatch):
    bin_dir = _isolated_bootstrap_bin(tmp_path)
    unsupported = bin_dir / "python-old"
    unsupported.write_text("#!/bin/sh\nexit 1\n")
    unsupported.chmod(0o755)
    graphify = bin_dir / "graphify"
    graphify.write_text(f"#!{unsupported}\nexit 0\n")
    graphify.chmod(0o755)
    (bin_dir / "python3.14").symlink_to(Path(sys.executable))
    monkeypatch.setenv("PATH", str(bin_dir))

    result = subprocess.run(
        ["/bin/bash", "-c", _posix_bootstrap_script()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    saved = (tmp_path / "graphify-out" / ".graphify_python").read_text()
    assert Path(saved).resolve() == Path(sys.executable).resolve()


def test_posix_bootstrap_resolves_supported_env_shebang(tmp_path, monkeypatch):
    bin_dir = _isolated_bootstrap_bin(tmp_path)
    env_python = bin_dir / "python-graphify"
    env_python.symlink_to(Path(sys.executable))
    graphify = bin_dir / "graphify"
    graphify.write_text("#!/usr/bin/env python-graphify\n", encoding="utf-8")
    graphify.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))

    result = subprocess.run(
        ["/bin/bash", "-c", _posix_bootstrap_script()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    saved = (tmp_path / "graphify-out" / ".graphify_python").read_text()
    assert Path(saved).resolve() == Path(sys.executable).resolve()


@pytest.mark.parametrize(
    "env_suffix",
    ["python-graphify -I", "PYTHONPATH=/tmp python-graphify"],
)
def test_posix_bootstrap_rejects_env_shebang_arguments(tmp_path, monkeypatch, env_suffix):
    bin_dir = _isolated_bootstrap_bin(tmp_path)
    (bin_dir / "python-graphify").symlink_to(Path(sys.executable))
    graphify = bin_dir / "graphify"
    graphify.write_text(f"#!/usr/bin/env {env_suffix}\n", encoding="utf-8")
    graphify.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    result = subprocess.run(
        ["/bin/bash", "-c", _posix_bootstrap_script()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Graphify requires CPython 3.14.2" in result.stderr
    assert not (tmp_path / "graphify-out" / ".graphify_python").exists()


def test_posix_missing_package_install_uses_trusted_pip_under_shadows(tmp_path, monkeypatch):
    python, pip_marker, graphify_marker = _offline_python_with_trusted_fake_pip(tmp_path)
    pythonpath_shadow, shadow_markers = _write_python_shadows(tmp_path)
    bin_dir = _isolated_bootstrap_bin(tmp_path)
    candidate = bin_dir / "python3.14"
    candidate.write_text(f'#!/bin/sh\nexec "{python}" "$@"\n', encoding="utf-8")
    candidate.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("PYTHONPATH", str(pythonpath_shadow))
    monkeypatch.setenv("VIRTUAL_ENV", str(python.parent.parent))

    result = subprocess.run(
        ["/bin/bash", "-c", _posix_bootstrap_script()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "install graphifyy" in pip_marker.read_text(encoding="utf-8")
    saved = (tmp_path / "graphify-out" / ".graphify_python").read_text(encoding="utf-8")
    assert Path(saved).resolve() == python.resolve()

    core, _ = _platform_artifacts("claude")
    query = subprocess.run(
        ["/bin/bash", "-c", _block_containing(core, '-m graphify query "<question>"')],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert query.returncode == 0, query.stderr
    assert graphify_marker.exists()
    assert not any(marker.exists() for marker in shadow_markers)
    assert not list(tmp_path.rglob("__pycache__"))


def test_troubleshooting_pip_commands_use_trusted_module_under_shadows(tmp_path):
    python, pip_marker, _ = _offline_python_with_trusted_fake_pip(tmp_path)
    pythonpath_shadow, shadow_markers = _write_python_shadows(tmp_path)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(pythonpath_shadow)
    for arguments in (
        ["install", "--upgrade", "graphifyy"],
        ["uninstall", "graspologic-native"],
    ):
        result = subprocess.run(
            [str(python), "-E", "-P", "-B", "-m", "pip", *arguments],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert " ".join(arguments) in pip_marker.read_text(encoding="utf-8")
    assert not any(marker.exists() for marker in shadow_markers)
    assert not list(tmp_path.rglob("__pycache__"))


def test_startup_policy_preserves_disposable_user_site_and_rejects_shadows(tmp_path):
    base_python = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["APPDATA"] = str(home / "AppData" / "Roaming")
    pythonpath_shadow, shadow_markers = _write_python_shadows(tmp_path)
    env["PYTHONPATH"] = str(pythonpath_shadow)
    user_site = Path(
        subprocess.check_output(
            [str(base_python), "-E", "-P", "-B", "-c", "import site; print(site.getusersitepackages())"],
            cwd=tmp_path,
            env=env,
            text=True,
        ).strip()
    )
    package = user_site / "graphify"
    package.mkdir(parents=True)
    import_marker = tmp_path / "trusted-user-site-imported"
    module_marker = tmp_path / "trusted-user-site-module-ran"
    (package / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(import_marker)!r}).touch()\n",
        encoding="utf-8",
    )
    (package / "__main__.py").write_text(
        f"from pathlib import Path\nPath({str(module_marker)!r}).touch()\n",
        encoding="utf-8",
    )
    for arguments in (
        ["-c", "import graphify"],
        ["-m", "graphify"],
    ):
        result = subprocess.run(
            [str(base_python), "-E", "-P", "-B", *arguments],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    assert import_marker.exists()
    assert module_marker.exists()
    assert not any(marker.exists() for marker in shadow_markers)
    assert not list(tmp_path.rglob("__pycache__"))


def test_fast_path_bootstraps_interpreter_without_overwriting_scan_root(tmp_path, monkeypatch):
    bin_dir = _isolated_bootstrap_bin(tmp_path)
    (bin_dir / "python3.14").symlink_to(Path(sys.executable))
    monkeypatch.setenv("PATH", str(bin_dir))
    graphify_out = tmp_path / "graphify-out"
    graphify_out.mkdir()
    root_marker = graphify_out / ".graphify_root"
    root_marker.write_text("preserve-this-root", encoding="utf-8")

    result = subprocess.run(
        ["/bin/bash", "-c", _posix_bootstrap_script()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert root_marker.read_text(encoding="utf-8") == "preserve-this-root"
    assert "run only the interpreter-bootstrap block" in _platform_artifacts("claude")[0]


def test_rendered_executable_blocks_reject_ambient_graphify_commands():
    platforms = gen.load_platforms()
    bare_graphify = re.compile(r"^\s*(?:[A-Za-z_]\w*=\$\()?graphify(?:\s|$)", re.MULTILINE)
    bare_python_module = re.compile(
        r"^\s*(?:python|python3|python3\.\d+)\s+-m\s+graphify(?:\s|\.)",
        re.MULTILINE,
    )
    unquoted_saved_module = re.compile(
        r"^\s*\$\(cat graphify-out/\.graphify_python\)\s+-m\s+graphify(?:\s|\.)",
        re.MULTILINE,
    )
    checked = 0
    for key, platform in platforms.items():
        for artifact in gen.render(platform):
            for shell_kind, block in _executable_blocks(artifact.content):
                checked += 1
                assert not bare_graphify.search(block), f"{key}:{artifact.path}\n{block}"
                assert not bare_python_module.search(block), f"{key}:{artifact.path}\n{block}"
                assert not unquoted_saved_module.search(block), f"{key}:{artifact.path}\n{block}"
                operational = [
                    line
                    for line in block.splitlines()
                    if not line.lstrip().startswith("#")
                    and (" -m graphify " in line or " -m graphify." in line)
                    and "graphify.interpreter_pointer" not in line
                ]
                if not operational:
                    continue
                assert "No trusted Graphify Python" in block, f"{key}:{artifact.path}\n{block}"
                if shell_kind == "powershell":
                    assert all(
                        line.lstrip().startswith(
                            "& $GraphifyPython -E -P -B -m graphify"
                        )
                        for line in operational
                    ), f"{key}:{artifact.path}\n{block}"
                else:
                    assert all(
                        line.lstrip().startswith(
                            '"$GRAPHIFY_PYTHON" -E -P -B -m graphify'
                        )
                        for line in operational
                    ), f"{key}:{artifact.path}\n{block}"
    assert checked > 100


def test_rendered_python_launches_require_exact_startup_policy():
    checked = 0
    for key, platform in gen.load_platforms().items():
        for artifact in gen.render(platform):
            content = artifact.content
            assert " -I " not in content
            assert '"-I"' not in content
            assert '"args": ["-m", "graphify.serve"' not in content, (
                key,
                artifact.path,
            )
            for _, block in _executable_blocks(content):
                for line in block.splitlines():
                    if line.lstrip().startswith("#"):
                        continue
                    python_launch = any(
                        marker in line
                        for marker in (
                            '"$(cat graphify-out/.graphify_python)"', '"$_GRAPHIFY_SAVED"',
                            '"$1"', '"$PYTHON"', "& $Candidate", "& $GraphifySaved",
                            "& $installPython", "& $py314.Source", "& $python.Source",
                            "& $launcher.Source -3.14", "& (Get-Content graphify-out\\.graphify_python)",
                            '"$GRAPHIFY_PYTHON"', "& $GraphifyPython",
                        )
                    )
                    if python_launch and (" -c " in line or " -m " in line):
                        checked += 1
                        assert " -E -P -B -c " in line or " -E -P -B -m " in line, (
                            f"{key}:{artifact.path}:{line}"
                        )
            if '"graphify.serve"' in content:
                if platform.shell == "powershell":
                    assert 'args = @("-E", "-P", "-B", "-m", "graphify.serve"' in content
                    assert "ConvertTo-Json -Depth 4" in content
                else:
                    assert '"args": ["-E", "-P", "-B", "-m", "graphify.serve"' in content, (
                        key,
                        artifact.path,
                    )
    assert checked > 100


def test_posix_path_shadow_flows_use_fresh_interpreter_and_ignore_pointer(tmp_path, monkeypatch):
    bin_dir = _isolated_bootstrap_bin(tmp_path)
    ambient_marker = tmp_path / "ambient-command-ran"
    for name in ("graphify", "python3"):
        shadow = bin_dir / name
        shadow.write_text(f"#!/bin/sh\n: > '{ambient_marker}'\nexit 97\n", encoding="utf-8")
        shadow.chmod(0o755)
    (bin_dir / "python3.14").symlink_to(Path(sys.executable))
    monkeypatch.setenv("PATH", str(bin_dir))
    cwd_shadow_marker = tmp_path / "cwd-shadow-imported"
    cwd_shadow = tmp_path / "graphify"
    cwd_shadow.mkdir()
    for module in ("__init__.py", "__main__.py"):
        (cwd_shadow / module).write_text(
            f"from pathlib import Path\nPath({str(cwd_shadow_marker)!r}).touch()\n",
            encoding="utf-8",
        )
    pythonpath_shadow_marker = tmp_path / "pythonpath-shadow-imported"
    pythonpath_root = tmp_path / "pythonpath-shadow"
    pythonpath_shadow = pythonpath_root / "graphify"
    pythonpath_shadow.mkdir(parents=True)
    for module in ("__init__.py", "__main__.py"):
        (pythonpath_shadow / module).write_text(
            f"from pathlib import Path\nPath({str(pythonpath_shadow_marker)!r}).touch()\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("PYTHONPATH", str(pythonpath_root))
    monkeypatch.setenv("VIRTUAL_ENV", str(Path(sys.executable).parent.parent))

    bootstrap = subprocess.run(
        ["/bin/bash", "-c", _posix_bootstrap_script()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bootstrap.returncode == 0, bootstrap.stderr

    pointer_marker = tmp_path / "saved-pointer-ran"
    saved = tmp_path / "saved-pointer"
    _write_executable_sentinel(saved, pointer_marker)
    (tmp_path / "graphify-out" / ".graphify_python").write_text(str(saved), encoding="utf-8")

    core, refs = _platform_artifacts("claude")
    flows = [
        _block_containing(core, '-m graphify query "<question>"'),
        _block_containing(refs["query.md"], '-m graphify path "NODE_A"'),
        _block_containing(refs["query.md"], '-m graphify explain "NODE_NAME"'),
        _block_containing(refs["update.md"], "-m graphify update INPUT_PATH"),
        _block_containing(refs["exports.md"], "-m graphify export wiki"),
        _block_containing(refs["add-watch.md"], "-m graphify watch INPUT_PATH"),
        _block_containing(refs["exports.md"], "-m graphify.serve graphify-out/graph.json"),
        _block_containing(refs["hooks.md"], "-m graphify claude install"),
        _block_containing(refs["hooks.md"], "-m graphify hook install"),
    ]
    for block in flows:
        safe_block = re.sub(
            r'"\$GRAPHIFY_PYTHON" -E -P -B -m graphify(?:\.serve)?[^\n]*',
            '"$GRAPHIFY_PYTHON" -E -P -B -m graphify --help',
            block,
        )
        result = subprocess.run(
            ["/bin/bash", "-c", safe_block],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    assert not pointer_marker.exists()
    assert not ambient_marker.exists()
    assert not cwd_shadow_marker.exists()
    assert not pythonpath_shadow_marker.exists()
    assert not list(tmp_path.rglob("__pycache__"))


def _query_block_as_help() -> str:
    core, _ = _platform_artifacts("claude")
    block = _block_containing(core, '-m graphify query "<question>"')
    replaced = block.replace('-m graphify query "<question>"', "-m graphify --help")
    assert replaced != block
    return replaced


@pytest.mark.parametrize("guard_source", ["owner", "rendered-query"])
def test_posix_operation_guard_ignores_exported_hostile_bash_functions(
    tmp_path, guard_source
):
    guard = (
        gen._POSIX_OPERATION_GUARD
        if guard_source == "owner"
        else _query_block_as_help()
    )
    control_marker = tmp_path / f"{guard_source}-control"
    hostile_markers = {
        name: tmp_path / f"{guard_source}-{name}-ran"
        for name in ("printf", "eval", "command", "unset", "pwd", "bracket", "exit")
    }
    child = tmp_path / f"{guard_source}-guard.sh"
    child.write_text(
        guard
        + "\n"
        + '"$GRAPHIFY_PYTHON" -E -P -B -c '
        + "'from pathlib import Path; import sys; "
        + "assert sys.executable == sys.argv[1]; Path(sys.argv[2]).touch()' "
        + '"$GFY_EXPECTED_PYTHON" "$GFY_CONTROL_MARKER"\n',
        encoding="utf-8",
    )
    parent = """
function printf { : > "$GFY_PRINTF_MARKER"; return 97; }
function eval { : > "$GFY_EVAL_MARKER"; return 97; }
function command { : > "$GFY_COMMAND_MARKER"; return 97; }
function unset { : > "$GFY_UNSET_MARKER"; return 97; }
function pwd { : > "$GFY_PWD_MARKER"; return 97; }
function [ { : > "$GFY_BRACKET_MARKER"; return 97; }
function exit { : > "$GFY_EXIT_MARKER"; return 97; }
export -f printf eval command unset pwd [ exit
exec /bin/bash "$1"
"""
    env = {
        **os.environ,
        "VIRTUAL_ENV": str(Path(sys.executable).parent.parent),
        "GFY_EXPECTED_PYTHON": str(Path(sys.executable)),
        "GFY_CONTROL_MARKER": str(control_marker),
        **{
            f"GFY_{name.upper()}_MARKER": str(marker)
            for name, marker in hostile_markers.items()
        },
    }

    def run_child(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    clean = run_child("/bin/bash", str(child))
    assert clean.returncode == 0, clean.stderr
    assert control_marker.exists()
    control_marker.unlink()

    result = run_child(
        "/bin/bash", "-c", parent, "hostile-function-parent", str(child)
    )

    assert not [name for name, marker in hostile_markers.items() if marker.exists()]
    assert result.returncode == 0, result.stderr
    assert control_marker.exists()


def _write_executable_sentinel(path: Path, marker: Path, *, delegate: Path | None = None) -> None:
    tail = f'exec "{delegate}" "$@"\n' if delegate is not None else "exit 97\n"
    path.write_text(
        "#!/bin/sh\n"
        f': > "{marker}"\n'
        + tail,
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_valid_looking_malicious_pointer_is_never_executed(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    graphify_out = project / "graphify-out"
    graphify_out.mkdir(mode=0o700)
    marker = tmp_path / "pointer-target-ran"
    sentinel = tmp_path / "pointer-target"
    _write_executable_sentinel(sentinel, marker)
    pointer = graphify_out / ".graphify_python"
    pointer.write_text(str(sentinel), encoding="utf-8")
    pointer.chmod(0o600)

    result = subprocess.run(
        ["/bin/bash", "-c", _query_block_as_help()],
        cwd=project,
        env={**os.environ, "VIRTUAL_ENV": str(Path(sys.executable).parent.parent)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()


@pytest.mark.parametrize("command", ["uv", "pipx", "graphify", "python3.14", "python3", "python"])
def test_workspace_path_candidates_are_rejected_before_execution(tmp_path, command):
    project = tmp_path / "project"
    bin_dir = project / "bin"
    bin_dir.mkdir(parents=True)
    marker = tmp_path / f"{command}-ran"
    _write_executable_sentinel(bin_dir / command, marker, delegate=Path(sys.executable))

    env = {**os.environ, "PATH": str(bin_dir)}
    env.pop("VIRTUAL_ENV", None)
    result = subprocess.run(
        ["/bin/bash", "-c", _query_block_as_help()],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert not marker.exists(), f"workspace-controlled {command} executed before trust"


@pytest.mark.parametrize("command", ["uv", "pipx", "graphify", "python3.14", "python3", "python"])
@pytest.mark.parametrize("selected_root", ["input", "output"])
def test_sibling_selected_root_candidates_are_rejected_before_execution(
    tmp_path, command, selected_root
):
    project = tmp_path / "project"
    project.mkdir()
    controlled = tmp_path / f"controlled-{selected_root}"
    bin_dir = controlled / "bin"
    bin_dir.mkdir(parents=True)
    marker = tmp_path / f"{selected_root}-{command}-ran"
    _write_executable_sentinel(bin_dir / command, marker, delegate=Path(sys.executable))
    selected_alias = tmp_path / f"selected-{selected_root}"
    selected_alias.symlink_to(controlled, target_is_directory=True)
    other = tmp_path / f"other-{selected_root}"
    other.mkdir()
    env = {
        **os.environ,
        "PATH": str(bin_dir),
        "GRAPHIFY_INPUT_PATH": str(selected_alias if selected_root == "input" else other),
        "GRAPHIFY_OUTPUT_ROOT": str(selected_alias if selected_root == "output" else other),
    }
    env.pop("VIRTUAL_ENV", None)

    result = subprocess.run(
        ["/bin/bash", "-c", _query_block_as_help()],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=3,
    )

    assert result.returncode != 0
    assert not marker.exists(), f"{selected_root}-controlled {command} executed"


@pytest.mark.parametrize("selected_root", ["input", "output"])
def test_posix_root_deny_rejects_every_ambient_absolute_path(tmp_path, selected_root):
    project = tmp_path / "project"
    project.mkdir()
    bin_dir = tmp_path / "ambient-bin"
    bin_dir.mkdir()
    marker = tmp_path / f"root-{selected_root}-ran"
    _write_executable_sentinel(bin_dir / "python3.14", marker, delegate=Path(sys.executable))
    env = {**os.environ, "PATH": str(bin_dir)}
    env["GRAPHIFY_INPUT_PATH" if selected_root == "input" else "GRAPHIFY_OUTPUT_ROOT"] = "/"
    env.pop("VIRTUAL_ENV", None)

    result = subprocess.run(
        ["/bin/bash", "-c", _query_block_as_help()],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert not marker.exists()


def test_posix_external_lexical_venv_symlink_preserves_invocation_semantics(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    environment = tmp_path / "external-venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(environment)],
        check=True,
        capture_output=True,
    )
    candidate = environment / "bin" / "python3.14"
    assert candidate.is_symlink()
    site_packages = Path(
        subprocess.check_output(
            [str(candidate), "-E", "-P", "-B", "-c", "import site; print(site.getsitepackages()[0])"],
            text=True,
        ).strip()
    )
    marker = tmp_path / "lexical-venv-imported"
    package = site_packages / "graphify"
    package.mkdir()
    dist_info = site_packages / "graphifyy-0.10.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: graphifyy\nVersion: 0.10.0\n",
        encoding="utf-8",
    )
    (dist_info / "RECORD").write_text("graphify/__init__.py,,\n", encoding="utf-8")
    for module in ("__init__.py", "__main__.py"):
        (package / module).write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
            encoding="utf-8",
        )
    canonical = candidate.resolve()
    marker.unlink(missing_ok=True)
    subprocess.run(
        [str(canonical), "-E", "-P", "-B", "-c", "import graphify"],
        cwd=project,
        capture_output=True,
        check=False,
    )
    assert not marker.exists(), "canonical target unexpectedly retained venv semantics"

    env = {**os.environ, "PATH": str(candidate.parent)}
    env.pop("VIRTUAL_ENV", None)
    result = subprocess.run(
        ["/bin/bash", "-c", _query_block_as_help()],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert marker.exists(), "discovery executed the canonical target instead of lexical venv"


@pytest.mark.parametrize("local_root", ["workspace", "input"])
def test_posix_local_symlink_to_external_interpreter_is_rejected(tmp_path, local_root):
    project = tmp_path / "project"
    project.mkdir()
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    controlled = project if local_root == "workspace" else corpus
    bin_dir = controlled / "bin"
    bin_dir.mkdir()
    marker = tmp_path / f"local-symlink-{local_root}-ran"
    target = tmp_path / f"external-{local_root}-python"
    _write_executable_sentinel(target, marker, delegate=Path(sys.executable))
    (bin_dir / "python3.14").symlink_to(target)
    env = {**os.environ, "PATH": str(bin_dir), "GRAPHIFY_INPUT_PATH": str(corpus)}
    env.pop("VIRTUAL_ENV", None)

    result = subprocess.run(
        ["/bin/bash", "-c", _query_block_as_help()],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert not marker.exists()


def test_generated_input_path_is_bound_before_discovery():
    _, refs = _platform_artifacts("claude")
    block = _block_containing(refs["update.md"], "-m graphify update INPUT_PATH")

    assert block.startswith("GRAPHIFY_INPUT_PATH='INPUT_PATH'\n")
    assert block.index("GRAPHIFY_INPUT_PATH='INPUT_PATH'") < block.index("_GRAPHIFY_WORKSPACE=")


def test_posix_discovery_never_reads_saved_root_special_file(tmp_path):
    project = tmp_path / "project"
    graphify_out = project / "graphify-out"
    graphify_out.mkdir(parents=True)
    os.mkfifo(graphify_out / ".graphify_root")

    result = subprocess.run(
        ["/bin/bash", "-c", _query_block_as_help()],
        cwd=project,
        env={**os.environ, "VIRTUAL_ENV": str(Path(sys.executable).parent.parent)},
        capture_output=True,
        text=True,
        check=False,
        timeout=3,
    )

    assert result.returncode == 0, result.stderr


def test_posix_discovery_selects_trusted_readlink_without_ambient_path(tmp_path):
    """Symlink resolution uses only the bounded trusted resolver strategy."""
    source = gen._POSIX_DISCOVERY
    fixed_resolvers = (
        "/usr/bin/readlink",
        "/bin/readlink",
        "/run/current-system/sw/bin/readlink",
    )
    positions = [source.index(resolver) for resolver in fixed_resolvers]
    assert positions == sorted(positions)
    assert "command -p readlink" in source
    assert "command -v readlink" not in source
    assert "$(readlink " not in source

    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    target = external / "python"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o755)
    candidate = external / "python-link"
    candidate.symlink_to(target)
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    sentinel = tmp_path / "ambient-readlink-ran"
    _write_executable_sentinel(hostile / "readlink", sentinel)

    definitions = source.split("_graphify_command()", 1)[0]
    for selected, resolver in enumerate(fixed_resolvers):
        stub = tmp_path / f"trusted-readlink-{selected}"
        stub.write_text("#!/bin/sh\nexec /usr/bin/readlink \"$@\"\n", encoding="utf-8")
        stub.chmod(0o755)
        controlled = definitions
        for earlier in fixed_resolvers[:selected]:
            controlled = controlled.replace(earlier, f"{tmp_path}/missing-{selected}")
        controlled = controlled.replace(resolver, str(stub))
        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                controlled
                + "\n_graphify_resolve_ambient \"$GFY_CANDIDATE\""
                + "\nprintf '%s\\n' \"$GRAPHIFY_RESOLVED\"\n",
            ],
            cwd=project,
            env={**os.environ, "PATH": str(hostile), "GFY_CANDIDATE": str(candidate)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(candidate)
        assert not sentinel.exists()

    safe_path = definitions
    for resolver in fixed_resolvers:
        safe_path = safe_path.replace(resolver, f"{tmp_path}/missing-safe-path")
    safe_path_result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            safe_path
            + "\n_graphify_resolve_ambient \"$GFY_CANDIDATE\""
            + "\nprintf '%s\\n' \"$GRAPHIFY_RESOLVED\"\n",
        ],
        cwd=project,
        env={**os.environ, "PATH": str(hostile), "GFY_CANDIDATE": str(candidate)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert safe_path_result.returncode == 0, safe_path_result.stderr
    assert safe_path_result.stdout.strip() == str(candidate)
    assert not sentinel.exists()

    unavailable = safe_path.replace("command -p readlink", "false")
    unavailable_result = subprocess.run(
        ["/bin/bash", "-c", unavailable + "\n_graphify_resolve_ambient \"$GFY_CANDIDATE\""],
        cwd=project,
        env={**os.environ, "PATH": str(hostile), "GFY_CANDIDATE": str(candidate)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert unavailable_result.returncode != 0
    assert not sentinel.exists()


def test_windows_bash_guard_converts_native_discovery_paths(tmp_path):
    """Windows bash discovery explicitly bridges native and MSYS namespaces."""
    source = gen._WINDOWS_POSIX_DISCOVERY
    candidates = (
        "/usr/bin/cygpath.exe",
        "/usr/bin/cygpath",
        "/bin/cygpath.exe",
        "/bin/cygpath",
    )
    positions = [source.index(candidate) for candidate in candidates]
    assert positions == sorted(positions)
    assert "command -v cygpath" not in source
    assert "_graphify_to_posix" in source
    assert "_graphify_to_native" in source
    assert " -u " in source
    assert " -w " in source
    assert "Scripts/python.exe" in source

    # The two-way handoff must cover every native-path ingress and every native
    # identity-probe egress named by the approved compatibility contract.
    ingress_segments = (
        source[source.index("VIRTUAL_ENV"):source.index("# Trusted uv")],
        source[source.index("if [ -z \"$GRAPHIFY_PYTHON\" ] && _graphify_command uv"):
               source.index("if [ -z \"$GRAPHIFY_PYTHON\" ] && _graphify_command pipx")],
        source[source.index("if [ -z \"$GRAPHIFY_PYTHON\" ] && _graphify_command pipx"):
               source.index("# Console-script")],
        source[source.index("# Console-script"):source.index("if [ -z \"$GRAPHIFY_PYTHON\" ]; then")],
    )
    assert all("_graphify_to_posix" in segment for segment in ingress_segments)
    assert "py -3.14" in source
    py_segment = source[source.index("py -3.14"):]
    assert "_graphify_to_posix" in py_segment
    for root in ("_GRAPHIFY_WORKSPACE", "_GRAPHIFY_INPUT_ROOT", "_GRAPHIFY_OUTPUT_ROOT"):
        assert re.search(rf"_graphify_to_native[^\n]*{root}|{root}[^\n]*_graphify_to_native", source)

    core, refs = _platform_artifacts("windows")
    bash_blocks = [
        block
        for body in (core, *refs.values())
        for block in re.findall(r"```(?:bash|sh)\n(.*?)\n```", body, re.DOTALL)
        if "GRAPHIFY_PYTHON=$(" in block
    ]
    assert bash_blocks
    assert all(candidate in block for block in bash_blocks for candidate in candidates)

    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    drive_native = r"C:\Fixture Root"
    unc_native = r"\\server\share\Fixture Root"
    drive_posix = external / "drive root"
    unc_posix = external / "unc root"
    drive_posix.mkdir()
    unc_posix.mkdir()
    (drive_posix / "Input With Spaces").mkdir()
    (unc_posix / "Output With Spaces").mkdir()
    converter_log = tmp_path / "converter.jsonl"
    converter = external / "controlled-cygpath"
    converter.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "mode, value = sys.argv[1:3]\n"
        "dn, un = os.environ['GFY_DRIVE_NATIVE'], os.environ['GFY_UNC_NATIVE']\n"
        "dp, up = os.environ['GFY_DRIVE_POSIX'], os.environ['GFY_UNC_POSIX']\n"
        "if mode == '-u' and (value == dn or value.startswith(dn + '\\\\')):\n"
        "    result = dp + value[len(dn):].replace('\\\\', '/')\n"
        "elif mode == '-u' and (value == un or value.startswith(un + '\\\\')):\n"
        "    result = up + value[len(un):].replace('\\\\', '/')\n"
        "elif mode == '-u' and value.startswith('/'):\n"
        "    result = value\n"
        "elif mode == '-w' and value.startswith(dp):\n"
        "    result = dn + value[len(dp):].replace('/', '\\\\')\n"
        "elif mode == '-w' and value.startswith(up):\n"
        "    result = un + value[len(up):].replace('/', '\\\\')\n"
        "elif mode == '-w' and value.startswith('/'):\n"
        "    result = r'C:\\Host' + value.replace('/', '\\\\')\n"
        "else:\n"
        "    raise SystemExit(2)\n"
        "with open(os.environ['GFY_CONVERTER_LOG'], 'a', encoding='utf-8') as log:\n"
        "    log.write(json.dumps([mode, value, result]) + '\\n')\n"
        "print(result)\n",
        encoding="utf-8",
    )
    converter.chmod(0o755)

    interpreter_log = tmp_path / "interpreter-argv.log"

    def write_python(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "#!/bin/sh\n"
            f'printf \'%s\\n\' "$@" >> "{interpreter_log}"\n'
            f'printf \'%s\\n\' --- >> "{interpreter_log}"\n'
            f'exec "{sys.executable}" "$@"\n',
            encoding="utf-8",
        )
        path.chmod(0o755)

    venv_python = drive_posix / "Venv With Spaces" / "Scripts" / "python.exe"
    write_python(venv_python)

    sentinel = tmp_path / "ambient-cygpath-ran"
    hostile = external / "hostile"
    hostile.mkdir()
    _write_executable_sentinel(hostile / "cygpath", sentinel)
    env = {
        **os.environ,
        "PATH": str(hostile),
        "GFY_DRIVE_NATIVE": drive_native,
        "GFY_UNC_NATIVE": unc_native,
        "GFY_DRIVE_POSIX": str(drive_posix),
        "GFY_UNC_POSIX": str(unc_posix),
        "GFY_CONVERTER_LOG": str(converter_log),
        "VIRTUAL_ENV": drive_native + r"\Venv With Spaces",
        "GRAPHIFY_INPUT_PATH": drive_native + r"\Input With Spaces",
        "GRAPHIFY_OUTPUT_ROOT": unc_native + r"\Output With Spaces",
    }

    # Substitute each fixed candidate independently. The guard must select the
    # first executable spelling, map drive/UNC values with spaces into POSIX,
    # then map deny roots back to native form for the CPython identity probe.
    for selected in range(len(candidates)):
        controlled = source
        for index, candidate in enumerate(candidates):
            replacement = str(converter) if index == selected else str(external / f"missing-{index}")
            controlled = controlled.replace(candidate, replacement)
        controlled += (
            '\nprintf \'SELECTED=%s\\n\' "$GRAPHIFY_PYTHON"\n'
            f'_graphify_to_posix {json.dumps(drive_native + r"\Input With Spaces")}\n'
            f'_graphify_to_posix {json.dumps(unc_native + r"\Output With Spaces")}\n'
            f'_graphify_to_native {json.dumps(str(drive_posix / "Input With Spaces"))}\n'
            f'_graphify_to_native {json.dumps(str(unc_posix / "Output With Spaces"))}\n'
        )
        result = subprocess.run(
            ["/bin/bash", "-c", controlled],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{candidates[selected]}: {result.stderr}"
        assert f"SELECTED={venv_python}" in result.stdout
        assert not sentinel.exists()

    conversions = [json.loads(line) for line in converter_log.read_text().splitlines()]
    assert any(mode == "-u" and value == env["GRAPHIFY_INPUT_PATH"] for mode, value, _ in conversions)
    assert any(mode == "-u" and value == env["GRAPHIFY_OUTPUT_ROOT"] for mode, value, _ in conversions)
    assert any(mode == "-u" and value == env["VIRTUAL_ENV"] for mode, value, _ in conversions)
    assert any(mode == "-w" and value == str(drive_posix / "Input With Spaces") for mode, value, _ in conversions)
    assert any(mode == "-w" and value == str(unc_posix / "Output With Spaces") for mode, value, _ in conversions)

    def fixed_converter_source() -> str:
        controlled = source
        for index, candidate in enumerate(candidates):
            controlled = controlled.replace(
                candidate,
                str(converter if index == 0 else external / f"missing-{index}"),
            )
        return controlled

    def write_command(directory: Path, name: str, output: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        command = directory / name
        command.write_text(
            "#!/bin/sh\n" + f"printf '%s\\n' {json.dumps(output)}\n",
            encoding="utf-8",
        )
        command.chmod(0o755)

    uv_python = drive_posix / "uv tools" / "graphifyy" / "Scripts" / "python.exe"
    pipx_python = unc_posix / "pipx home" / "graphifyy" / "Scripts" / "python.exe"
    py_python = unc_posix / "Python 3.14" / "python.exe"
    for python in (uv_python, pipx_python, py_python):
        write_python(python)

    uv_bin = external / "uv-bin"
    write_command(uv_bin, "uv", drive_native + r"\uv tools")
    pipx_bin = external / "pipx-bin"
    write_command(pipx_bin, "pipx", unc_native + r"\pipx home")
    py_bin = external / "py-bin"
    write_command(py_bin, "py", unc_native + r"\Python 3.14\python.exe")
    launcher_bin = drive_posix / "launcher" / "Scripts"
    launcher_python = launcher_bin / "python.exe"
    write_python(launcher_python)
    write_command(launcher_bin, "graphify", "must-not-execute-launcher")

    for label, path, expected in (
        ("uv", uv_bin, uv_python),
        ("pipx", pipx_bin, pipx_python),
        ("launcher", launcher_bin, launcher_python),
        ("py", py_bin, py_python),
    ):
        result = subprocess.run(
            ["/bin/bash", "-c", fixed_converter_source() + '\nprintf \'SELECTED=%s\\n\' "$GRAPHIFY_PYTHON"\n'],
            cwd=workspace,
            env={**env, "PATH": str(path), "VIRTUAL_ENV": ""},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{label}: {result.stderr}"
        assert f"SELECTED={expected}" in result.stdout, label

    identity_argv = interpreter_log.read_text(encoding="utf-8")
    assert env["GRAPHIFY_INPUT_PATH"] in identity_argv
    assert env["GRAPHIFY_OUTPUT_ROOT"] in identity_argv

    for invalid_root in ("C:", r"C:relative", "\\", r"\current-drive-rooted"):
        invalid = source
        for index, candidate in enumerate(candidates):
            invalid = invalid.replace(candidate, str(converter if index == 0 else external / f"missing-{index}"))
        invalid_result = subprocess.run(
            ["/bin/bash", "-c", invalid],
            cwd=workspace,
            env={**env, "GRAPHIFY_INPUT_PATH": invalid_root},
            capture_output=True,
            text=True,
            check=False,
        )
        assert invalid_result.returncode != 0, invalid_root

    # A lexical input alias and its physical target must both deny an ambient
    # Python, even when the native spelling is converted before containment.
    controlled_origin = drive_posix / "controlled origin"
    controlled_origin.mkdir()
    alias = drive_posix / "input alias"
    alias.symlink_to(controlled_origin, target_is_directory=True)
    denied_python = controlled_origin / "python.exe"
    denied_python.write_text(
        "#!/bin/sh\n"
        f': > "{tmp_path / "denied-python-ran"}"\n'
        f'exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    denied_python.chmod(0o755)
    denied_bin = controlled_origin / "bin"
    denied_bin.mkdir()
    (denied_bin / "python").symlink_to(denied_python)
    denied_result = subprocess.run(
        ["/bin/bash", "-c", fixed_converter_source()],
        cwd=workspace,
        env={
            **env,
            "PATH": str(denied_bin),
            "VIRTUAL_ENV": "",
            "GRAPHIFY_INPUT_PATH": drive_native + r"\input alias",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert denied_result.returncode != 0
    assert not (tmp_path / "denied-python-ran").exists()

    unavailable = source
    for candidate in candidates:
        unavailable = unavailable.replace(candidate, str(external / "missing-cygpath"))
    unavailable_result = subprocess.run(
        ["/bin/bash", "-c", unavailable],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert unavailable_result.returncode != 0
    assert not sentinel.exists()


def test_explicit_project_local_virtualenv_is_accepted_but_same_ambient_path_is_not(tmp_path):
    project = tmp_path / "project"
    venv_bin = project / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    marker = tmp_path / "project-venv-ran"
    candidate = venv_bin / "python"
    _write_executable_sentinel(candidate, marker, delegate=Path(sys.executable))

    active = subprocess.run(
        ["/bin/bash", "-c", _query_block_as_help()],
        cwd=project,
        env={**os.environ, "PATH": str(venv_bin), "VIRTUAL_ENV": str(project / ".venv")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert active.returncode == 0, active.stderr
    assert marker.exists(), "explicit active VIRTUAL_ENV was not honored"

    marker.unlink()
    ambient_env = {**os.environ, "PATH": str(venv_bin)}
    ambient_env.pop("VIRTUAL_ENV", None)
    ambient = subprocess.run(
        ["/bin/bash", "-c", _query_block_as_help()],
        cwd=project,
        env=ambient_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ambient.returncode != 0
    assert not marker.exists(), "ambient project-local interpreter was executed"


def test_posix_static_mcp_configuration_emits_fresh_trusted_command(tmp_path):
    _, refs = _platform_artifacts("claude")
    blocks = re.findall(r"```(?:bash|sh)\n(.*?)\n```", refs["exports.md"], re.DOTALL)
    config_blocks = [block for block in blocks if '"mcpServers"' in block]
    assert len(config_blocks) == 1
    project = tmp_path / 'project "quoted" \\ path'
    project.mkdir()
    graphify_out = project / "graphify-out"
    graphify_out.mkdir(mode=0o700)
    marker = tmp_path / "pointer-ran"
    sentinel = tmp_path / "pointer-python"
    _write_executable_sentinel(sentinel, marker)
    (graphify_out / ".graphify_python").write_text(str(sentinel), encoding="utf-8")

    result = subprocess.run(
        ["/bin/bash", "-c", config_blocks[0]],
        cwd=project,
        env={**os.environ, "VIRTUAL_ENV": str(Path(sys.executable).parent.parent)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    server = config["mcpServers"]["graphify"]
    assert server["command"] == sys.executable
    assert server["args"] == [
        "-E", "-P", "-B", "-m", "graphify.serve",
        str(project / "graphify-out" / "graph.json"),
    ]
    assert str(sentinel) not in result.stdout
    assert not marker.exists()


@pytest.mark.parametrize("command", ["uv", "pipx"])
def test_posix_discovery_rejects_symlink_cycles_within_timeout(tmp_path, command):
    project = tmp_path / "project"
    project.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / command).symlink_to(command)
    env = {**os.environ, "PATH": str(bin_dir)}
    env.pop("VIRTUAL_ENV", None)

    result = subprocess.run(
        ["/bin/bash", "-c", _query_block_as_help()],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=3,
    )

    assert result.returncode != 0


@pytest.mark.parametrize(
    ("identity_case", "accepted"),
    [
        ("wheel", True),
        ("editable", True),
        ("missing-distribution", False),
        ("wrong-distribution-name", False),
        ("missing-spec-origin", False),
        ("installed-origin-mismatch", False),
        ("malformed-direct-url", False),
        ("non-file-direct-url", False),
        ("non-editable-direct-url", False),
        ("editable-origin-mismatch", False),
    ],
)
def test_posix_trusted_candidate_requires_exact_graphifyy_identity_and_origin(
    tmp_path, identity_case, accepted
):
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / f"source-{identity_case}"
    other_source = tmp_path / f"other-source-{identity_case}"
    package_root: Path | None = None
    distribution = True
    metadata_name = "graphifyy"
    direct_url: dict[str, object] | str | None = None
    namespace_package = False
    if identity_case == "editable":
        package_root = source
        direct_url = {"url": source.as_uri(), "dir_info": {"editable": True}}
    elif identity_case == "missing-distribution":
        distribution = False
    elif identity_case == "wrong-distribution-name":
        metadata_name = "graphify-impostor"
    elif identity_case == "missing-spec-origin":
        namespace_package = True
    elif identity_case == "installed-origin-mismatch":
        package_root = source
    elif identity_case == "malformed-direct-url":
        package_root = source
        direct_url = "{"
    elif identity_case == "non-file-direct-url":
        package_root = source
        direct_url = {
            "url": "https://example.invalid/graphify",
            "dir_info": {"editable": True},
        }
    elif identity_case == "non-editable-direct-url":
        package_root = source
        direct_url = {"url": source.as_uri(), "dir_info": {"editable": False}}
    elif identity_case == "editable-origin-mismatch":
        package_root = source
        direct_url = {
            "url": other_source.as_uri(),
            "dir_info": {"editable": True},
        }
    python, marker = _disposable_graphify_python(
        tmp_path,
        f"identity-{identity_case}",
        package_root=package_root,
        distribution=distribution,
        metadata_name=metadata_name,
        direct_url=direct_url,
        namespace_package=namespace_package,
    )

    result = _run_posix_query_with_python(
        tmp_path, python, cwd=project, trusted=True
    )

    assert (result.returncode == 0) is accepted, result.stderr
    assert marker.exists() is accepted


@pytest.mark.parametrize("trusted", [False, True], ids=["ambient", "explicit-trusted"])
def test_posix_candidate_rejects_forged_metadata_without_graphify_ownership(
    tmp_path, trusted
):
    project = tmp_path / "project"
    project.mkdir()
    python, marker = _disposable_graphify_python(
        tmp_path,
        f"forged-metadata-{trusted}",
        owns_graphify_package=False,
    )

    result = _run_posix_query_with_python(
        tmp_path, python, cwd=project, trusted=trusted
    )

    assert not marker.exists(), "unowned graphify package reached generated execution"
    assert result.returncode != 0


@pytest.mark.parametrize("layout", ["wheel", "editable"])
@pytest.mark.parametrize("discovery_source", ["uv", "pipx", "launcher", "path"])
def test_posix_ambient_candidate_accepts_external_identity_valid_origin(
    tmp_path, layout, discovery_source
):
    project = tmp_path / "project"
    project.mkdir()
    package_root = None
    direct_url = None
    if layout == "editable":
        source = tmp_path / "external-source"
        package_root = source
        direct_url = {"url": source.as_uri(), "dir_info": {"editable": True}}
    python, marker = _disposable_graphify_python(
        tmp_path,
        f"external-{layout}",
        package_root=package_root,
        direct_url=direct_url,
    )

    result = _run_posix_query_with_python(
        tmp_path,
        python,
        cwd=project,
        trusted=False,
        discovery_source=discovery_source,
    )

    assert result.returncode == 0, result.stderr
    assert marker.exists()


@pytest.mark.parametrize("deny_root", ["workspace", "input", "output"])
@pytest.mark.parametrize(
    "origin_shape",
    ["inside", "lexical-inside-real-outside", "lexical-outside-real-inside"],
)
@pytest.mark.parametrize("discovery_source", ["uv", "pipx", "launcher", "path"])
def test_posix_ambient_candidate_rejects_lexical_or_resolved_origin_under_deny_root(
    tmp_path, deny_root, origin_shape, discovery_source
):
    project = tmp_path / "project"
    project.mkdir()
    selected_input = tmp_path / "selected-input"
    selected_input.mkdir()
    selected_output = tmp_path / "selected-output"
    selected_output.mkdir()
    controlled = {
        "workspace": project,
        "input": selected_input,
        "output": selected_output,
    }[deny_root]
    if origin_shape == "inside":
        source = controlled / "source"
    elif origin_shape == "lexical-inside-real-outside":
        real_source = tmp_path / f"real-external-{deny_root}"
        real_source.mkdir()
        source = controlled / "source-alias"
        source.symlink_to(real_source, target_is_directory=True)
    else:
        real_source = controlled / "real-source"
        real_source.mkdir()
        source = tmp_path / f"external-alias-{deny_root}"
        source.symlink_to(real_source, target_is_directory=True)
    python, marker = _disposable_graphify_python(
        tmp_path,
        f"denied-{deny_root}-{origin_shape}",
        package_root=source,
        direct_url={"url": source.as_uri(), "dir_info": {"editable": True}},
    )

    result = _run_posix_query_with_python(
        tmp_path,
        python,
        cwd=project,
        trusted=False,
        discovery_source=discovery_source,
        extra_env={
            "GRAPHIFY_INPUT_PATH": str(selected_input),
            "GRAPHIFY_OUTPUT_ROOT": str(selected_output),
        },
    )

    assert result.returncode != 0
    assert not marker.exists(), "denied package origin reached generated operation"


@pytest.mark.parametrize("deny_root", ["workspace", "input", "output"])
@pytest.mark.parametrize("layout", ["wheel", "editable"])
def test_posix_explicit_virtualenv_accepts_identity_valid_origin_under_deny_root(
    tmp_path, deny_root, layout
):
    project = tmp_path / "project"
    project.mkdir()
    selected_input = tmp_path / "selected-input"
    selected_input.mkdir()
    selected_output = tmp_path / "selected-output"
    selected_output.mkdir()
    controlled = {
        "workspace": project,
        "input": selected_input,
        "output": selected_output,
    }[deny_root]
    if layout == "wheel":
        python, marker = _disposable_graphify_python(
            controlled, f"trusted-{deny_root}-wheel"
        )
    else:
        source = controlled / "editable-source"
        python, marker = _disposable_graphify_python(
            tmp_path,
            f"trusted-{deny_root}-editable",
            package_root=source,
            direct_url={"url": source.as_uri(), "dir_info": {"editable": True}},
        )

    result = _run_posix_query_with_python(
        tmp_path,
        python,
        cwd=project,
        trusted=True,
        extra_env={
            "GRAPHIFY_INPUT_PATH": str(selected_input),
            "GRAPHIFY_OUTPUT_ROOT": str(selected_output),
        },
    )

    assert result.returncode == 0, result.stderr
    assert marker.exists()


def test_posix_literal_percent_escape_editable_root_respects_trust_mode(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "editable%2Fsource"
    python, marker = _disposable_graphify_python(
        tmp_path,
        "literal-percent-editable",
        package_root=source,
        direct_url={"url": source.as_uri(), "dir_info": {"editable": True}},
    )

    ambient = _run_posix_query_with_python(
        tmp_path, python, cwd=project, trusted=False
    )
    assert ambient.returncode != 0
    assert not marker.exists(), "ambient denied-root package origin reached execution"

    trusted = _run_posix_query_with_python(
        tmp_path, python, cwd=project, trusted=True
    )
    assert trusted.returncode == 0, trusted.stderr
    assert marker.exists()


def test_powershell_mcp_configuration_uses_native_json_serialization():
    body = "\n".join(
        artifact.content for artifact in gen.render(gen.load_platforms()["windows"])
    )
    blocks = re.findall(r"```powershell\n(.*?)\n```", body, re.DOTALL)
    config_blocks = [block for block in blocks if "mcpServers" in block]

    assert len(config_blocks) == 1
    config = config_blocks[0]
    assert "ConvertTo-Json -Depth 4" in config
    assert "command = $GraphifyPython" in config
    assert 'args = @("-E", "-P", "-B", "-m", "graphify.serve", $graphPath)' in config
    assert '"command": "$GraphifyPython"' not in config


def test_powershell_bootstrap_validates_every_candidate_and_parses():
    from tree_sitter import Language, Parser
    import tree_sitter_powershell

    script, root_persistence = _powershell_step1_scripts()

    assert "function Resolve-GraphifyAmbientCommand" in script
    assert "if (Test-GraphifyWorkspacePath $path) { return $null }" in script
    assert "Resolve-GraphifyAmbientCommand uv" in script
    assert "Resolve-GraphifyAmbientCommand pipx" in script
    assert 'foreach ($name in @("python3.14", "python3", "py", "python"))' in script
    assert "Add-GraphifyDenyRoot $env:GRAPHIFY_INPUT_PATH $true" in script
    assert "Add-GraphifyDenyRoot $GraphifySelectedOutput" in script
    assert "GetPathRoot" in script
    assert "Resolve-GraphifyAmbientCommand graphify" in script
    assert "$path = [IO.Path]::GetFullPath($command.Source)" in script
    assert "return $path" in script
    assert "Resolve-Path -LiteralPath $command.Source" not in script
    assert "function Resolve-GraphifyPolicyPath" in script
    assert 'PSObject.Methods.Name -notcontains "ResolveLinkTarget"' in script
    assert "$script:GraphifyDenyPolicyInvalid = $true" in script
    assert "if ($GraphifyDenyPolicyInvalid) { return $true }" in script
    assert "if ($Required) { $script:GraphifyDenyPolicyInvalid = $true }" in script
    assert "Resolve-Path -LiteralPath $full" not in script
    assert "Resolve-GraphifyAmbientCommand $name" in script
    assert "Test-GraphifySupportedPython" in script
    assert "& $installPython -E -P -B -m pip install graphifyy" in script
    assert "& $uv tool install" in script
    assert "& $Candidate -E -P -B -c $GraphifyIdentityCheck trusted" in script
    assert (
        "& $Candidate -E -P -B -c $GraphifyIdentityCheck ambient @GraphifyDenyRoots"
        in script
    )
    assert "sys.implementation.name == 'cpython'" in script
    assert "sys.version_info.releaselevel == 'final'" in script
    assert "graphify-out\\.graphify_python" in script
    assert ".graphify_root" not in script
    assert ".graphify_python" not in root_persistence
    assert "graphify-out\\.graphify_root" in root_persistence
    assert "Resolve-Path INPUT_PATH" in root_persistence
    for unsafe in (
        "Get-Command uv", "Get-Command pipx", "Get-Command graphify",
        "Get-Command py", "Get-Command python",
    ):
        assert unsafe not in script

    parser = Parser(Language(tree_sitter_powershell.language()))
    for source in (script, root_persistence):
        tree = parser.parse(source.encode())
        assert not tree.root_node.has_error, tree.root_node


def test_powershell_bootstrap_separates_trusted_identity_from_ambient_origin_policy():
    script = _powershell_bootstrap_script()

    assert "function Test-GraphifyPython" in script
    assert "function Test-GraphifyAmbientPython" in script
    assert "importlib.metadata" in script
    assert "importlib.util" in script
    assert "distribution('graphifyy')" in script or 'distribution("graphifyy")' in script
    assert "find_spec('graphify')" in script or 'find_spec("graphify")' in script
    assert "sys.argv[1:]" in script
    assert "@GraphifyDenyRoots" in script
    assert "if (Test-GraphifyPython $activeVenv)" in script
    assert script.count("Test-GraphifyAmbientPython $candidate") >= 5
    assert not re.search(r"\$\w*[Oo]rigin\s*=\s*\(&\s*\$Candidate", script)
    assert "print(origin" not in script and "print(real_origin" not in script
    assert "Resolve-Path -LiteralPath $command.Source" not in script


def test_powershell_bootstrap_directory_creation_failure_is_terminating():
    script = _powershell_bootstrap_script()

    directory_creation = (
        "New-Item -ItemType Directory -Force -Path graphify-out "
        "-ErrorAction Stop | Out-Null"
    )
    assert directory_creation in script
    assert script.index(directory_creation) < script.index("graphify.interpreter_pointer write")


@pytest.mark.skipif(
    not _WINDOWS_POWERSHELL,
    reason="runtime requires a Windows host with PowerShell; this repository has no hosted Windows CI",
)
def test_powershell_bootstrap_rejects_workspace_path_sentinels_before_execution(tmp_path):
    executable = shutil.which("pwsh") or shutil.which("powershell")
    assert executable is not None
    project = tmp_path / "project"
    bin_dir = project / "bin"
    bin_dir.mkdir(parents=True)
    markers: list[Path] = []
    for name in ("uv", "pipx", "graphify", "py", "python3.14", "python3", "python"):
        marker = tmp_path / f"{name}-ran"
        markers.append(marker)
        (bin_dir / f"{name}.cmd").write_text(
            f'@echo off\r\n>>"{marker}" echo ran\r\nexit /b 97\r\n',
            encoding="utf-8",
            newline="",
        )
    env = os.environ.copy()
    env["PATH"] = str(bin_dir)
    env.pop("VIRTUAL_ENV", None)
    result = _run_powershell_script(
        executable,
        _powershell_bootstrap_script(),
        tmp_path,
        cwd=project,
        env=env,
    )
    assert result.returncode != 0
    assert not any(marker.exists() for marker in markers)


@pytest.mark.skipif(
    not _WINDOWS_POWERSHELL,
    reason="runtime requires a Windows host with PowerShell; this repository has no hosted Windows CI",
)
@pytest.mark.parametrize("command", ["uv", "pipx", "graphify", "python3.14", "python3", "python"])
@pytest.mark.parametrize("selected_root", ["input", "output"])
def test_powershell_discovery_rejects_explicit_external_root_sentinels(
    tmp_path, command, selected_root
):
    executable = shutil.which("pwsh") or shutil.which("powershell")
    assert executable is not None
    project = tmp_path / "project"
    project.mkdir()
    controlled = tmp_path / f"controlled-{selected_root}"
    bin_dir = controlled / "bin"
    bin_dir.mkdir(parents=True)
    marker = tmp_path / f"powershell-{selected_root}-{command}-ran"
    (bin_dir / f"{command}.cmd").write_text(
        f'@echo off\r\n>>"{marker}" echo ran\r\nexit /b 97\r\n',
        encoding="utf-8",
        newline="",
    )
    env = os.environ.copy()
    env["PATH"] = str(bin_dir)
    env["GRAPHIFY_INPUT_PATH"] = str(controlled if selected_root == "input" else project)
    env["GRAPHIFY_OUTPUT_ROOT"] = str(controlled if selected_root == "output" else project)
    env.pop("VIRTUAL_ENV", None)

    result = _run_powershell_script(
        executable,
        _powershell_bootstrap_script(),
        tmp_path,
        cwd=project,
        env=env,
    )

    assert result.returncode != 0
    assert not marker.exists()


@pytest.mark.skipif(
    not _WINDOWS_POWERSHELL,
    reason="runtime requires a Windows host with PowerShell; this repository has no hosted Windows CI",
)
@pytest.mark.parametrize("selected_root", ["input", "output"])
def test_powershell_drive_root_deny_rejects_every_ambient_path(tmp_path, selected_root):
    executable = shutil.which("pwsh") or shutil.which("powershell")
    assert executable is not None
    project = tmp_path / "project"
    project.mkdir()
    bin_dir = tmp_path / "ambient-bin"
    bin_dir.mkdir()
    marker = tmp_path / f"powershell-drive-root-{selected_root}-ran"
    (bin_dir / "python3.14.cmd").write_text(
        f'@echo off\r\n>>"{marker}" echo ran\r\nexit /b 97\r\n',
        encoding="utf-8",
        newline="",
    )
    env = os.environ.copy()
    env["PATH"] = str(bin_dir)
    env["GRAPHIFY_INPUT_PATH" if selected_root == "input" else "GRAPHIFY_OUTPUT_ROOT"] = project.anchor
    env.pop("VIRTUAL_ENV", None)

    result = _run_powershell_script(
        executable,
        _powershell_bootstrap_script(),
        tmp_path,
        cwd=project,
        env=env,
    )

    assert result.returncode != 0
    assert not marker.exists()


@pytest.mark.skipif(
    not _WINDOWS_POWERSHELL,
    reason="runtime requires a Windows host with PowerShell; this repository has no hosted Windows CI",
)
@pytest.mark.parametrize("alias_kind", ["candidate", "deny_root"])
def test_powershell_reparse_aliases_cannot_escape_deny_roots(tmp_path, alias_kind):
    executable = shutil.which("pwsh") or shutil.which("powershell")
    assert executable is not None
    project = tmp_path / "project"
    project.mkdir()
    controlled = tmp_path / "controlled"
    bin_dir = controlled / "bin"
    bin_dir.mkdir(parents=True)
    marker = tmp_path / f"powershell-reparse-{alias_kind}-ran"
    (bin_dir / "python3.14.cmd").write_text(
        f'@echo off\r\n>>"{marker}" echo ran\r\nexit /b 97\r\n',
        encoding="utf-8",
        newline="",
    )
    alias = tmp_path / "alias"
    junction = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(alias), str(controlled)],
        capture_output=True,
        check=False,
    )
    if junction.returncode != 0:
        pytest.skip("Windows junction creation is unavailable on this host")
    env = os.environ.copy()
    env["PATH"] = str((alias if alias_kind == "candidate" else controlled) / "bin")
    env["GRAPHIFY_INPUT_PATH"] = str(controlled if alias_kind == "candidate" else alias)
    env.pop("VIRTUAL_ENV", None)

    result = _run_powershell_script(
        executable,
        _powershell_bootstrap_script(),
        tmp_path,
        cwd=project,
        env=env,
    )

    assert result.returncode != 0
    assert not marker.exists()


@pytest.mark.skipif(
    not _WINDOWS_POWERSHELL,
    reason="runtime requires a Windows host with PowerShell; this repository has no hosted Windows CI",
)
def test_powershell_reparse_cycle_is_denied(tmp_path):
    executable = shutil.which("pwsh") or shutil.which("powershell")
    assert executable is not None
    first = tmp_path / "cycle-a"
    second = tmp_path / "cycle-b"
    try:
        first.symlink_to(second, target_is_directory=True)
        second.symlink_to(first, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symbolic-link creation is unavailable on this host")
    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(Path(sys.executable).parent.parent)
    script = (
        "$GraphifyDiscoveryOptional = $true\n"
        + gen._POWERSHELL_DISCOVERY
        + f'\nif (-not (Test-GraphifyWorkspacePath "{first}")) {{ exit 91 }}\n'
    )

    result = _run_powershell_script(
        executable,
        script,
        tmp_path,
        cwd=tmp_path,
        env=env,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")


@pytest.mark.skipif(
    not _WINDOWS_POWERSHELL,
    reason="runtime requires a Windows host with PowerShell; this repository has no hosted Windows CI",
)
def test_powershell_unresolvable_explicit_deny_root_blocks_ambient_execution(tmp_path):
    executable = shutil.which("pwsh") or shutil.which("powershell")
    assert executable is not None
    project = tmp_path / "project"
    project.mkdir()
    first = tmp_path / "deny-cycle-a"
    second = tmp_path / "deny-cycle-b"
    try:
        first.symlink_to(second, target_is_directory=True)
        second.symlink_to(first, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symbolic-link creation is unavailable on this host")
    bin_dir = tmp_path / "ambient-bin"
    bin_dir.mkdir()
    marker = tmp_path / "powershell-invalid-deny-root-ran"
    (bin_dir / "python3.14.cmd").write_text(
        f'@echo off\r\n>>"{marker}" echo ran\r\nexit /b 97\r\n',
        encoding="utf-8",
        newline="",
    )
    env = os.environ.copy()
    env["PATH"] = str(bin_dir)
    env["GRAPHIFY_INPUT_PATH"] = str(first)
    env.pop("VIRTUAL_ENV", None)

    result = _run_powershell_script(
        executable,
        _powershell_bootstrap_script(),
        tmp_path,
        cwd=project,
        env=env,
        timeout=10,
    )

    assert result.returncode != 0
    assert not marker.exists()


@pytest.mark.skipif(
    not _WINDOWS_POWERSHELL,
    reason="runtime requires a Windows host with PowerShell; this repository has no hosted Windows CI",
)
def test_powershell_missing_package_install_uses_trusted_pip_under_shadows(tmp_path):
    executable = shutil.which("pwsh") or shutil.which("powershell")
    assert executable is not None
    python, pip_marker, graphify_marker = _offline_python_with_trusted_fake_pip(tmp_path)
    pythonpath_shadow, shadow_markers = _write_python_shadows(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3.14.cmd").write_text(
        f'@echo off\r\n"{python}" %*\r\n', encoding="utf-8", newline=""
    )
    env = os.environ.copy()
    env["PATH"] = str(bin_dir)
    env["PYTHONPATH"] = str(pythonpath_shadow)
    env.pop("UV_TOOL_DIR", None)
    env.pop("PIPX_HOME", None)

    bootstrap = _run_powershell_script(
        executable,
        _powershell_bootstrap_script(),
        tmp_path,
        cwd=tmp_path,
        env=env,
    )
    assert bootstrap.returncode == 0, bootstrap.stderr.decode(errors="replace")
    assert "install graphifyy" in pip_marker.read_text(encoding="utf-8")
    assert not (tmp_path / "graphify-out" / ".graphify_python").exists()
    assert b"cannot safely publish" in (bootstrap.stdout + bootstrap.stderr).lower()

    core, _ = _platform_artifacts("windows")
    query = _run_powershell_script(
        executable,
        _block_containing(core, '-m graphify query "<question>"'),
        tmp_path,
        cwd=tmp_path,
        env=env,
    )
    assert query.returncode == 0, query.stderr.decode(errors="replace")
    assert graphify_marker.exists()
    assert not any(marker.exists() for marker in shadow_markers)
    assert not list(tmp_path.rglob("__pycache__"))


def test_powershell_troubleshooting_pip_uses_fresh_discovery_not_pointer():
    body = gen.render(gen.load_platforms()["windows"])[0].content

    assert "& $GraphifyPython -E -P -B -m pip install --upgrade graphifyy" in body
    assert "& $GraphifyPython -E -P -B -m pip uninstall graspologic-native" in body
    assert "Get-Content graphify-out\\.graphify_python" not in body


@pytest.mark.skipif(
    not _WINDOWS_POWERSHELL,
    reason="runtime requires a Windows host with PowerShell; this repository has no hosted Windows CI",
)
@pytest.mark.parametrize("reject_first_candidate", [False, True])
def test_powershell_fast_path_bootstrap_preserves_existing_root_without_network(
    tmp_path, reject_first_candidate
):
    executable = shutil.which("pwsh") or shutil.which("powershell")
    assert executable is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rejected = tmp_path / "unsupported-python-was-rejected"
    supported_name = "python.cmd" if reject_first_candidate else "python3.14.cmd"
    supported = bin_dir / supported_name
    supported.write_text(
        f'@echo off\r\n"{sys.executable}" %*\r\n', encoding="utf-8", newline=""
    )
    if reject_first_candidate:
        (bin_dir / "python3.14.cmd").write_text(
            f'@echo off\r\n>>"{rejected}" echo rejected\r\nexit /b 1\r\n',
            encoding="utf-8",
            newline="",
        )

    env = os.environ.copy()
    env["PATH"] = str(bin_dir)
    env.pop("UV_TOOL_DIR", None)
    env.pop("PIPX_HOME", None)
    graphify_out = tmp_path / "graphify-out"
    graphify_out.mkdir()
    root_marker = graphify_out / ".graphify_root"
    original_root_bytes = b"preserve-this-root\x00\xff"
    root_marker.write_bytes(original_root_bytes)
    result = _run_powershell_script(
        executable,
        _powershell_bootstrap_script(),
        tmp_path,
        cwd=tmp_path,
        env=env,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert not (graphify_out / ".graphify_python").exists()
    assert b"cannot safely publish" in (result.stdout + result.stderr).lower()
    assert root_marker.read_bytes() == original_root_bytes
    assert rejected.exists() is reject_first_candidate


@pytest.mark.skipif(
    not _WINDOWS_POWERSHELL,
    reason="runtime requires a Windows host with PowerShell; this repository has no hosted Windows CI",
)
@pytest.mark.parametrize("reject_first_candidate", [False, True])
def test_powershell_full_build_persists_resolved_root_without_network(
    tmp_path, reject_first_candidate
):
    executable = shutil.which("pwsh") or shutil.which("powershell")
    assert executable is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rejected = tmp_path / "unsupported-python-was-rejected"
    supported_name = "python.cmd" if reject_first_candidate else "python3.14.cmd"
    (bin_dir / supported_name).write_text(
        f'@echo off\r\n"{sys.executable}" %*\r\n', encoding="utf-8", newline=""
    )
    if reject_first_candidate:
        (bin_dir / "python3.14.cmd").write_text(
            f'@echo off\r\n>>"{rejected}" echo rejected\r\nexit /b 1\r\n',
            encoding="utf-8",
            newline="",
        )

    env = os.environ.copy()
    env["PATH"] = str(bin_dir)
    env.pop("UV_TOOL_DIR", None)
    env.pop("PIPX_HOME", None)
    bootstrap = _run_powershell_script(
        executable,
        _powershell_bootstrap_script(),
        tmp_path,
        cwd=tmp_path,
        env=env,
    )
    assert bootstrap.returncode == 0, bootstrap.stderr.decode(errors="replace")
    persist_root = _run_powershell_script(
        executable,
        _powershell_root_persistence_script(),
        tmp_path,
        cwd=tmp_path,
        env=env,
    )

    assert persist_root.returncode == 0, persist_root.stderr.decode(errors="replace")
    assert not (tmp_path / "graphify-out" / ".graphify_python").exists()
    assert b"cannot safely publish" in (bootstrap.stdout + bootstrap.stderr).lower()
    saved_root = (tmp_path / "graphify-out" / ".graphify_root").read_text(
        encoding="utf-8-sig"
    )
    assert Path(saved_root).resolve() == tmp_path.resolve()
    assert rejected.exists() is reject_first_candidate


@pytest.mark.skipif(
    not _WINDOWS_POWERSHELL,
    reason="runtime requires a Windows host with PowerShell; this repository has no hosted Windows CI",
)
def test_powershell_path_shadow_flows_use_fresh_interpreter_not_pointer(tmp_path):
    executable = shutil.which("pwsh") or shutil.which("powershell")
    assert executable is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ambient_marker = tmp_path / "ambient-command-ran"
    for name in ("graphify.cmd", "python.cmd"):
        (bin_dir / name).write_text(
            f'@echo off\r\n>>"{ambient_marker}" echo ambient\r\nexit /b 97\r\n',
            encoding="utf-8",
            newline="",
        )

    pointer_marker = tmp_path / "advisory-pointer-ran"
    saved = tmp_path / "advisory-pointer.cmd"
    saved.write_text(
        f'@echo off\r\n>>"{pointer_marker}" echo ran\r\nexit /b 97\r\n',
        encoding="utf-8",
        newline="",
    )
    graphify_out = tmp_path / "graphify-out"
    graphify_out.mkdir()
    (graphify_out / ".graphify_python").write_text(str(saved), encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = str(bin_dir)
    env["VIRTUAL_ENV"] = str(Path(sys.executable).parent.parent)
    cwd_shadow_marker = tmp_path / "cwd-shadow-imported"
    cwd_shadow = tmp_path / "graphify"
    cwd_shadow.mkdir()
    for module in ("__init__.py", "__main__.py"):
        (cwd_shadow / module).write_text(
            f"from pathlib import Path\nPath({str(cwd_shadow_marker)!r}).touch()\n",
            encoding="utf-8",
        )
    pythonpath_shadow_marker = tmp_path / "pythonpath-shadow-imported"
    pythonpath_root = tmp_path / "pythonpath-shadow"
    pythonpath_shadow = pythonpath_root / "graphify"
    pythonpath_shadow.mkdir(parents=True)
    for module in ("__init__.py", "__main__.py"):
        (pythonpath_shadow / module).write_text(
            f"from pathlib import Path\nPath({str(pythonpath_shadow_marker)!r}).touch()\n",
            encoding="utf-8",
        )
    env["PYTHONPATH"] = str(pythonpath_root)
    core, refs = _platform_artifacts("windows")
    flows = [
        _block_containing(core, '-m graphify query "<question>"'),
        _block_containing(refs["query.md"], '-m graphify path "NODE_A"'),
        _block_containing(refs["query.md"], '-m graphify explain "NODE_NAME"'),
        _block_containing(refs["update.md"], "-m graphify update INPUT_PATH"),
        _block_containing(refs["exports.md"], "-m graphify export wiki"),
        _block_containing(refs["add-watch.md"], "-m graphify watch INPUT_PATH"),
        _block_containing(refs["exports.md"], "-m graphify.serve graphify-out/graph.json"),
        _block_containing(refs["hooks.md"], "-m graphify claude install"),
        _block_containing(refs["hooks.md"], "-m graphify hook install"),
    ]
    for block in flows:
        safe_block = re.sub(
            r"& \$GraphifyPython -E -P -B -m graphify(?:\.serve)?[^\r\n]*",
            "& $GraphifyPython -E -P -B -m graphify --help",
            block,
        )
        result = _run_powershell_script(
            executable,
            safe_block,
            tmp_path,
            cwd=tmp_path,
            env=env,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")

    assert not pointer_marker.exists()
    assert not ambient_marker.exists()
    assert not cwd_shadow_marker.exists()
    assert not pythonpath_shadow_marker.exists()
    assert not list(tmp_path.rglob("__pycache__"))


def test_codex_dispatch_is_agenttask_and_collects_in_memory():
    """codex: spawn/wait/close_agent dispatch needing multi_agent = true."""
    core, _ = _platform_artifacts("codex")
    assert "spawn_agent" in core
    assert "wait_agent" in core
    assert "close_agent" in core
    assert "multi_agent = true" in core
    assert "Codex collects in memory" in core
    # The B2 dispatch slot itself (Codex heading -> Step B3) must not carry the
    # claude Agent-tool example. The shared Step B3 prose mentions the agent type
    # in a re-run hint, so scope the check to the dispatch block only.
    b2 = core[core.index("**Step B2"):core.index("**Step B3")]
    assert "Concrete example for 3 chunks" not in b2
    assert "Agent tool call 1" not in b2


def test_codex_and_windows_unify_enum_to_six_values():
    """codex (was 4-value) and windows (was 5-value) now carry the superset."""
    for key in ("codex", "windows"):
        _, refs = _platform_artifacts(key)
        spec = refs["extraction-spec.md"]
        assert "`code`, `document`, `paper`, `image`, `rationale`, `concept`" in spec
        assert '"file_type":"code|document|paper|image|rationale|concept"' in spec
        # No legacy 4-value enum survives anywhere in the rendered bundle.
        for body in refs.values():
            assert '"file_type":"code|document|paper|image"' not in body


def test_codex_uses_compact_extraction_windows_uses_verbose():
    """The extraction variant differs: codex compact, windows verbose."""
    _, codex_refs = _platform_artifacts("codex")
    _, windows_refs = _platform_artifacts("windows")
    assert "(compact)" in codex_refs["extraction-spec.md"]
    assert "(compact)" not in windows_refs["extraction-spec.md"]


def test_every_platform_query_has_expansion_and_fallback():
    """#1325: the unified query reference ships BOTH the vocab-expansion step and
    the inline NetworkX fallback to every platform (previously split so no host
    got both — Claude had expansion but no fallback; the rest the reverse)."""
    for key in ("claude", "codex", "windows", "opencode"):
        core, refs = _platform_artifacts(key)
        # Core stub mentions both the vocab-expansion step and the inline fallback.
        assert "expand the question against the graph's own vocabulary" in core
        assert "NetworkX traversal" in core
        # The query reference carries expansion, fallback, and path/explain.
        q = refs["query.md"]
        assert "Constrained query expansion" in q
        assert "If the CLI is unavailable" in q
        assert "## For /graphify path" in q
        assert "## For /graphify explain" in q


def test_schema_singleton_passes_across_all_platforms():
    """The file_type enum is the six-value superset in every rendered artifact."""
    platforms = gen.load_platforms()
    problems = gen.schema_singleton(platforms)
    assert problems == [], "\n".join(problems)


def test_schema_singleton_catches_legacy_enums():
    """The guard's line scanner flags 4- and 5-value pipe enums, not the superset."""
    four = 'file_type":"code|document|paper|image"'
    five = 'file_type":"code|document|paper|image|rationale"'
    superset = '"file_type":"code|document|paper|image|rationale|concept"'
    assert gen.legacy_enum_lines(four) == [four]
    assert gen.legacy_enum_lines(five) == [five]
    # The full six-value superset is never flagged.
    assert gen.legacy_enum_lines(superset) == []
    assert gen.legacy_enum_lines("no enum here") == []


# --- the remaining progressive hosts -------------------------------------------

_PROGRESSIVE_HOSTS = (
    "opencode",
    "kilo",
    "copilot",
    "claw",
    "droid",
    "amp",
    "trae",
    "kiro",
    "pi",
    "vscode",
)


def test_all_progressive_hosts_check_and_audit_clean():
    """check + audit-coverage pass for every rendered progressive host."""
    platforms = gen.load_platforms()
    for key in _PROGRESSIVE_HOSTS:
        arts = gen.render_all(platforms, only=key)
        assert gen.check(arts) == [], f"[{key}] check\n" + "\n".join(gen.check(arts))
        probs = gen.audit_coverage(platforms[key])
        assert probs == [], f"[{key}] audit\n" + "\n".join(probs)


def test_no_host_has_trigger_in_frontmatter():
    """No split host emits a trigger: field — not part of Agent Skills spec (#1180)."""
    for key in ("claude", "codex", "opencode", "kilo", "copilot", "claw", "droid",
                "amp", "trae", "vscode", "kiro", "pi"):
        core, _ = _platform_artifacts(key)
        head = core.split("---", 2)[1]
        assert "trigger:" not in head, f"[{key}] unexpectedly has a trigger: line"


def test_kilo_renders_its_rules_tail_section():
    """kilo gets the Kilo-specific rules tail before Honesty Rules."""
    core, _ = _platform_artifacts("kilo")
    assert "## Kilo-specific rules" in core
    assert core.index("## Kilo-specific rules") < core.index("## Honesty Rules")


def test_dispatch_variants_are_host_specific():
    """Each dispatch variant lands in the right host's B2 slot."""
    expect = {
        "opencode": "@mention",
        "droid": "Task(description=",
        "amp": "Task(description=",
        "trae": "Task(description=",
        "vscode": "paste each response back",
    }
    for key, marker in expect.items():
        core, _ = _platform_artifacts(key)
        b2 = core[core.index("**Step B2"):core.index("**Step B3")]
        assert marker.lower() in b2.lower(), f"[{key}] dispatch slot missing {marker!r}"


def test_compact_extraction_hosts_use_the_compact_spec():
    """kiro, pi, claw use the compact extraction body; the rest use verbose."""
    for key in ("kiro", "pi", "claw"):
        _, refs = _platform_artifacts(key)
        assert "(compact)" in refs["extraction-spec.md"], f"[{key}] not compact"
    for key in ("opencode", "kilo", "copilot", "droid", "amp", "trae", "vscode"):
        _, refs = _platform_artifacts(key)
        assert "(compact)" not in refs["extraction-spec.md"], f"[{key}] should be verbose"


def test_every_split_host_renders_eight_references():
    """All twelve split hosts render exactly the eight on-demand references."""
    platforms = gen.load_platforms()
    expected = [
        "add-watch.md",
        "exports.md",
        "extraction-spec.md",
        "github-and-merge.md",
        "hooks.md",
        "query.md",
        "transcribe.md",
        "update.md",
    ]
    for key, p in platforms.items():
        if p.bucket != "split":
            continue
        _, refs = _platform_artifacts(key)
        assert sorted(refs) == expected, f"[{key}] reference set drift: {sorted(refs)}"


# --- the aider + devin monoliths -----------------------------------------------


def test_monoliths_render_inline_single_file_no_references():
    """aider and devin render one inline body, no split and no references dir."""
    platforms = gen.load_platforms()
    for key in ("aider", "devin"):
        assert platforms[key].bucket == "monolith"
        arts = gen.render(platforms[key])
        assert len(arts) == 1, f"[{key}] monolith should render exactly one file"
        assert arts[0].path == f"graphify/skill-{key}.md"
        assert "references/" not in arts[0].content or "see `references/" not in arts[0].content.lower()


def test_monolith_roundtrip_passes_for_aider_and_devin():
    """Each monolith is diff-clean vs v8 except sanctioned migrations."""
    platforms = gen.load_platforms()
    for key in ("aider", "devin"):
        problems = gen.monolith_roundtrip(platforms[key])
        assert problems == [], f"[{key}]\n" + "\n".join(problems)


def test_monoliths_change_only_sanctioned_lines():
    """Every line that differs from pristine v8 is a sanctioned change-class.

    The round-trip (multiset diff vs the pinned v8 blob) must come back clean:
    each added/removed line matches one of the documented sanctioned predicates
    in gen — the enum unification, unified description, chunk-cleanup rewrite
    (#1172), the four #1392 runbook fixes, semantic-cache source scoping (#1757),
    and saved-interpreter subcommand routing. Anything else is drift.
    """
    platforms = gen.load_platforms()
    for key in ("aider", "devin"):
        assert gen.monolith_roundtrip(platforms[key]) == []
        # The six-value superset replaced the five-value enum in both files.
        rendered = gen.render(platforms[key])[0].content
        assert gen.ENUM_VALUES in rendered
        assert UNIFIED_DESCRIPTION in rendered


def test_monoliths_carry_the_1392_runbook_fixes():
    """The four #1392 data-loss/correctness fixes are present in both monoliths.

    The round-trip allows these change-classes; this test asserts they are
    actually applied, so a regression that drops a fix fails here even though the
    round-trip (which only forbids *unsanctioned* drift) would still pass.
    """
    platforms = gen.load_platforms()
    for key in ("aider", "devin"):
        body = gen.render(platforms[key])[0].content

        # #6/#7 directed propagation: no bare build_from_json call survives, and
        # the IS_DIRECTED substitution instruction is present.
        assert "directed=IS_DIRECTED" in body
        assert "build_from_json(extraction)" not in body
        assert "Substitute it everywhere it appears" in body

        # #10 content-only semantic scope: code is no longer flattened in.
        assert "for cat in ('document', 'paper', 'image')" in body
        assert "detect['files'].values()" not in body

        # #12 stale-cache unlink on a miss.
        assert ".graphify_cached.json').unlink(missing_ok=True)" in body

        # #18/#20 zero-node guard before any write, report/analysis gated on
        # to_json's return.
        lines = body.splitlines()
        build_i = next(i for i, l in enumerate(lines) if "G = build_from_json(extraction, directed=IS_DIRECTED)" in l)
        guard_i = next(i for i, l in enumerate(lines[build_i:], build_i) if "number_of_nodes() == 0" in l)
        report_i = next(i for i, l in enumerate(lines[build_i:], build_i) if "GRAPH_REPORT.md').write_text(report)" in l)
        wrote_i = next(i for i, l in enumerate(lines[build_i:], build_i) if l.strip().startswith("wrote = to_json("))
        # guard fires right after the build, before the graph/report are written.
        assert build_i < guard_i < wrote_i < report_i, f"[{key}] Step 4 ordering not fixed"
        assert "if not wrote:" in body


def test_monoliths_scope_semantic_cache_writes_to_uncached_files():
    """#1757: generated monoliths pass the dispatched-file allowlist when
    replacing semantic cache entries."""
    platforms = gen.load_platforms()
    for key in ("aider", "devin"):
        body = gen.render(platforms[key])[0].content
        assert ".graphify_uncached.txt').read_text(" in body
        assert "allowed_source_files=uncached" in body


def test_generated_runbooks_pass_root_to_save_manifest():
    """#1417: every save_manifest call in a shipped runbook threads root=.

    Without root=, save_manifest stores absolute path keys, so a clone or move
    breaks --update (every cached file misses and the whole corpus re-extracts).
    The full-build (skill.md / monoliths) and the --update reference all relativize
    the manifest to the scan root via root='INPUT_PATH'. This guards the actual
    shipped artifacts; --check keeps them in sync with the fragments.
    """
    targets = [
        REPO_ROOT / "graphify" / "skill.md",
        REPO_ROOT / "graphify" / "skill-aider.md",
        REPO_ROOT / "graphify" / "skill-devin.md",
    ]
    targets += sorted((REPO_ROOT / "graphify" / "skills").glob("*/references/update.md"))
    checked = 0
    for path in targets:
        for ln in path.read_text(encoding="utf-8").splitlines():
            if "save_manifest(" in ln and "import" not in ln:
                checked += 1
                assert "root=" in ln, (
                    f"{path.relative_to(REPO_ROOT)}: save_manifest without root= (#1417): {ln.strip()!r}"
                )
    assert checked >= 4, f"expected save_manifest calls across the runbooks, found {checked}"


def test_devin_keeps_its_multi_field_frontmatter():
    """devin renders inline, so its 4+-field frontmatter is preserved verbatim."""
    platforms = gen.load_platforms()
    body = gen.render(platforms["devin"])[0].content
    head = body.split("---", 2)[1]
    assert "argument-hint:" in head
    assert "model:" in head
    assert "allowed-tools:" in head


# --- the always-on instruction blocks (D2-a) -----------------------------------


def test_always_on_renders_six_blocks():
    """render_always_on yields exactly the six always-on instruction files."""
    arts = gen.render_always_on()
    paths = sorted(a.path for a in arts)
    assert paths == [
        "graphify/always_on/agents-md.md",
        "graphify/always_on/antigravity-rules.md",
        "graphify/always_on/claude-md.md",
        "graphify/always_on/gemini-md.md",
        "graphify/always_on/kiro-steering.md",
        "graphify/always_on/vscode-instructions.md",
    ]


def test_always_on_included_in_full_render_not_per_platform():
    """A full render carries the always-on files; a --platform render does not."""
    platforms = gen.load_platforms()
    full = {a.path for a in gen.render_all(platforms)}
    claude_only = {a.path for a in gen.render_all(platforms, only="claude")}
    assert "graphify/always_on/claude-md.md" in full
    assert "graphify/always_on/claude-md.md" not in claude_only


def test_always_on_roundtrip_is_byte_faithful():
    """Each always_on/*.md reproduces its former __main__.py constant byte for byte.

    This is the load-bearing fidelity check behind the D2-a extraction: the
    install-string / issue-#580 tests still import the constants from
    graphify.__main__, so the packaged markdown must round-trip exactly or those
    contracts silently change.
    """
    # The guard passes with zero problems: every always-on block reproduces its
    # frozen baseline, with the agents-md block allowed exactly the #1530
    # sanctioned substitution recorded in gen.ALWAYS_ON_SANCTIONED_EDITS.
    problems = gen.always_on_roundtrip()
    assert problems == []

    rendered_agents = next(
        a.content
        for a in gen.render_always_on()
        if a.path == "graphify/always_on/agents-md.md"
    )
    old_instruction = (
        "When the user types `/graphify`, invoke the `skill` tool with "
        '`skill: "graphify"` before doing anything else.'
    )
    new_instruction = (
        "When the user types `/graphify`, use the installed graphify skill or instructions "
        "before doing anything else."
    )
    # The sanctioned-edit registry holds exactly this single old->new substitution.
    assert gen.ALWAYS_ON_SANCTIONED_EDITS["_AGENTS_MD_SECTION"] == (
        (old_instruction, new_instruction),
    )
    baseline_agents = gen._always_on_constants(gen.ALWAYS_ON_BASELINE_REF)["_AGENTS_MD_SECTION"]
    # The ONLY divergence from the frozen baseline is the sanctioned sentence —
    # any other byte drift would have surfaced as a problem above.
    assert old_instruction in baseline_agents
    assert baseline_agents.replace(old_instruction, new_instruction) == rendered_agents
    assert "`skill` tool" not in rendered_agents
    assert 'skill: "graphify"' not in rendered_agents


def test_extracted_constants_equal_the_packaged_always_on_files():
    """The live module constants now equal the packaged files they read at load."""
    from graphify import __main__ as mainmod

    pairs = {
        "_CLAUDE_MD_SECTION": "claude-md",
        "_AGENTS_MD_SECTION": "agents-md",
        "_GEMINI_MD_SECTION": "gemini-md",
        "_VSCODE_INSTRUCTIONS_SECTION": "vscode-instructions",
        "_ANTIGRAVITY_RULES": "antigravity-rules",
        "_KIRO_STEERING": "kiro-steering",
    }
    pkg = Path(mainmod.__file__).parent
    for const_name, basename in pairs.items():
        on_disk = (pkg / "always_on" / f"{basename}.md").read_text(encoding="utf-8")
        assert getattr(mainmod, const_name) == on_disk, const_name


def test_always_on_files_are_guarded_by_check(tmp_path):
    """A hand-edit of an always_on/*.md is caught by --check (the drift guard)."""
    platforms = gen.load_platforms()
    arts = gen.render_all(platforms)
    # The committed + expected/ snapshots match a fresh render.
    assert gen.check(arts) == [], "\n".join(gen.check(arts))
    # A mutated artifact is flagged.
    mutated = [
        gen.RenderedArtifact(a.path, a.content + "drift\n")
        if a.path == "graphify/always_on/claude-md.md"
        else a
        for a in arts
    ]
    problems = gen.check(mutated)
    assert any("always_on/claude-md.md" in p for p in problems)


# --- the per-host coverage audit (the systemic guard) --------------------------


def test_audit_coverage_passes_for_every_split_host():
    """Every split host's render single-homes its own v8 body's headings."""
    platforms = gen.load_platforms()
    for key, p in platforms.items():
        if p.bucket != "split":
            continue
        problems = gen.audit_coverage(p)
        assert problems == [], f"[{key}]\n" + "\n".join(problems)


def test_audit_reads_each_host_against_its_own_v8_body():
    """The audit baseline is the host's OWN v8 skill body, not claude's monolith.

    This is the structural fix: a per-host body, so a drop on one host surfaces.
    """
    assert gen._v8_baseline_ref("claude") == "47042beb05d1f6dd2186c0c499ae2840ce604ead:graphify/skill.md"
    assert gen._v8_baseline_ref("trae") == "47042beb05d1f6dd2186c0c499ae2840ce604ead:graphify/skill-trae.md"
    assert gen._v8_baseline_ref("vscode") == "47042beb05d1f6dd2186c0c499ae2840ce604ead:graphify/skill-vscode.md"


def test_audit_catches_an_induced_per_host_drop():
    """Re-inducing the trae regression (claude-flavored hooks) fails the audit.

    Pointing trae back at the shared CLAUDE.md hooks body drops the
    '## For native AGENTS.md integration (Trae)' heading from its render. The
    per-host audit must catch that against trae's own v8 body. The old audit
    (every host vs claude's monolith) could not see it, because claude's monolith
    never had that heading.
    """
    import dataclasses

    platforms = gen.load_platforms()
    regressed = dataclasses.replace(platforms["trae"], hooks_variant="claude-md")
    problems = gen.audit_coverage(regressed)
    assert any("native AGENTS.md integration (Trae)" in p for p in problems), problems


def test_audit_catches_a_dropped_non_allowlisted_heading():
    """A core fragment that drops a real v8 heading fails the audit.

    Guards that the audit is not a rubber stamp: a host whose v8 has a heading
    that is neither allowlisted nor present anywhere in the render must fail.
    """
    platforms = gen.load_platforms()
    trae = platforms["trae"]
    real_arts = gen.render(trae)
    # Drop the Honesty Rules heading from the rendered core to simulate a real
    # content loss, then re-run the single-home check by hand against trae's v8.
    v8_headings = gen.headings(gen._git_show(gen._v8_baseline_ref("trae")))
    assert "## Honesty Rules" in v8_headings
    by_path = {a.path: a.content for a in real_arts}
    core_no_honesty = by_path[trae.skill_dst].replace("## Honesty Rules", "## Closing notes")
    core_headings = set(gen.headings(core_no_honesty))
    allowlist = gen._audit_allowlist("trae")
    homes = [h for h in v8_headings if h == "## Honesty Rules" and h in core_headings]
    assert "## Honesty Rules" not in allowlist
    assert homes == [], "a dropped, non-allowlisted heading should have no home"


def test_git_show_validators_skip_cleanly_without_origin_v8(monkeypatch, tmp_path, capsys):
    """On a shallow checkout (no origin/v8) the validators skip with exit 0.

    CI sets fetch-depth: 0 so they run for real; this guards the fallback so a
    shallow clone gets a clear message instead of a crash.
    """
    import subprocess as sp

    repo = tmp_path / "shallow"
    repo.mkdir()
    sp.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setattr(gen, "REPO_ROOT", repo)
    assert gen._v8_available() is False
    for flag in ("--audit-coverage", "--monolith-roundtrip", "--always-on-roundtrip"):
        assert gen.main([flag]) == 0
    out = capsys.readouterr()
    assert "SKIPPED" in out.err
    assert "fetch-depth: 0" in out.err


def test_audit_allowlist_documents_only_consolidations():
    """The allowlist holds only the wave-2/3 consolidations, nothing genuine.

    A genuine drop (trae's native AGENTS.md integration) must never be in the
    allowlist, or the guard would rubber-stamp the regression it exists to catch.
    """
    all_allowlisted = set(gen.SHARED_INTRO_ALLOWLIST)
    for hs in gen._CONSOLIDATION_ALLOWLIST.values():
        all_allowlisted |= set(hs)
    assert "## For native AGENTS.md integration (Trae)" not in all_allowlisted
    # Only the two minimal-body hosts carry per-host consolidations.
    assert set(gen._CONSOLIDATION_ALLOWLIST) == {"kilo", "vscode"}


# --- the trae / trae-cn native AGENTS.md integration fix -----------------------


def test_trae_renders_native_agents_md_integration_not_claude():
    """trae wires `graphify trae install` -> AGENTS.md, never `graphify claude install`."""
    core, refs = _platform_artifacts("trae")
    hooks = refs["hooks.md"]
    # The hooks reference carries the v8 native AGENTS.md integration section.
    assert "## For native AGENTS.md integration (Trae)" in hooks
    assert "graphify trae install" in hooks
    assert "graphify trae-cn install" in hooks
    assert "writes a `## graphify` section to the local `AGENTS.md`" in hooks
    # The claude-flavored install command must NOT appear for trae.
    assert "graphify claude install" not in hooks
    assert "native CLAUDE.md integration" not in hooks
    # The lean-core pointer names AGENTS.md, not CLAUDE.md.
    assert "## For the commit hook and native AGENTS.md integration" in core
    assert "wire graphify into a project's AGENTS.md" in core
    assert "native CLAUDE.md integration" not in core


def test_trae_dispatch_carries_the_no_pretooluse_caveat():
    """trae's B2 dispatch block restores the v8 no-PreToolUse-hook caveat."""
    core, _ = _platform_artifacts("trae")
    b2 = core[core.index("**Step B2"):core.index("Pass the extraction prompt")]
    assert "Trae does NOT support PreToolUse hooks" in b2
    assert "AGENTS.md rules are the always-on mechanism instead" in b2


def test_trae_hooks_reference_includes_the_pretooluse_note():
    """The trae hooks reference keeps the v8 PreToolUse note in full."""
    _, refs = _platform_artifacts("trae")
    hooks = refs["hooks.md"]
    assert "Unlike Claude Code, Trae does NOT support PreToolUse hooks" in hooks
    assert "Run `/graphify --update` manually after code changes" in hooks


def test_claude_flavored_hosts_keep_their_hooks_text_unchanged():
    """Hosts whose v8 shipped the claude-flavored hooks keep it (faithful to them).

    droid's v8 dispatch never had the Trae caveat and its hooks section names
    CLAUDE.md; restoring trae must not bleed into droid or any other host.
    """
    for key in ("claude", "droid", "codex", "windows", "kilo", "vscode"):
        core, refs = _platform_artifacts(key)
        hooks = refs["hooks.md"]
        assert "graphify claude install" in hooks, f"[{key}] lost the claude install command"
        assert "native CLAUDE.md integration" in hooks, f"[{key}] lost the CLAUDE.md heading"
        assert "Trae does NOT support PreToolUse hooks" not in core, f"[{key}] leaked the trae caveat"
        assert "Trae does NOT support PreToolUse hooks" not in hooks, f"[{key}] leaked the trae caveat"
        assert "## For the commit hook and native CLAUDE.md integration" in core, f"[{key}] pointer drifted"


# --- the amp native AGENTS.md integration (the 13th split host) ----------------


def test_amp_renders_native_agents_md_integration_v8_faithfully():
    """amp wires `graphify amp install` -> AGENTS.md exactly as its v8 body had it.

    amp shares the agents-md hooks variant with trae but renders its OWN wording:
    a bare "## For native AGENTS.md integration" heading (no "(Trae)" suffix),
    single-line install/uninstall commands (no trae-cn alt), and crucially NO
    PreToolUse caveat (amp's v8 never carried one).
    """
    core, refs = _platform_artifacts("amp")
    hooks = refs["hooks.md"]
    # amp's bare v8 heading and Amp-worded prose.
    assert "## For native AGENTS.md integration" in hooks
    assert "## For native AGENTS.md integration (Trae)" not in hooks
    assert "make graphify always-on in Amp sessions" in hooks
    assert "instructs Amp to check the graph" in hooks
    # amp's single-line install/uninstall, no trae-cn alt comments.
    assert "graphify amp install" in hooks
    assert "graphify amp uninstall  # remove the section" in hooks
    assert "graphify trae install" not in hooks
    assert "graphify trae-cn" not in hooks
    assert "or: graphify" not in hooks
    # No claude flavoring on amp.
    assert "graphify claude install" not in hooks
    assert "native CLAUDE.md integration" not in hooks
    # The lean-core pointer names AGENTS.md, not CLAUDE.md.
    assert "## For the commit hook and native AGENTS.md integration" in core
    assert "wire graphify into a project's AGENTS.md" in core
    assert "native CLAUDE.md integration" not in core


def test_amp_has_no_pretooluse_caveat_anywhere():
    """amp's v8 had no no-PreToolUse-hooks note, so neither its core nor hooks may.

    This is the explicit guard against injecting trae-specific wording into amp.
    The caveat belongs to trae alone; amp uses the plain task-tool-disk dispatch
    and a caveat-free AGENTS.md integration section.
    """
    core, refs = _platform_artifacts("amp")
    hooks = refs["hooks.md"]
    assert "PreToolUse" not in core, "amp leaked a PreToolUse caveat into its core"
    assert "PreToolUse" not in hooks, "amp leaked a PreToolUse caveat into its hooks reference"
    assert "Trae does NOT support" not in core
    assert "Trae does NOT support" not in hooks
    # amp's dispatch is the plain task-tool-disk block (no trae caveat line).
    b2 = core[core.index("**Step B2"):core.index("Pass the extraction prompt")]
    assert "Trae" not in b2


def test_amp_audit_coverage_passes_against_its_own_v8():
    """The per-host audit (the guard amp is the exact case for) passes for amp.

    amp was omitted from wave 3's render list, so its v8 body was never audited
    against a lean split. The audit reads origin/v8:graphify/skill-amp.md and
    confirms every heading single-homes in amp's core + references.
    """
    platforms = gen.load_platforms()
    assert gen._v8_baseline_ref("amp") == "47042beb05d1f6dd2186c0c499ae2840ce604ead:graphify/skill-amp.md"
    problems = gen.audit_coverage(platforms["amp"])
    assert problems == [], "\n".join(problems)


# --- the generic agents platform (#1432) ---------------------------------------


def test_agents_renders_its_own_agents_md_hooks_wording():
    """`agents` re-homes amp's agents-md body but with its OWN install wording.

    It shares amp's bare, caveat-free `## For native AGENTS.md integration`
    section (no `(Trae)` suffix, no PreToolUse note) but points at
    `graphify agents install` and is worded for an unspecified host.
    """
    core, refs = _platform_artifacts("agents")
    hooks = refs["hooks.md"]
    assert "## For native AGENTS.md integration" in hooks
    assert "## For native AGENTS.md integration (Trae)" not in hooks
    assert "make graphify always-on in your agent sessions" in hooks
    assert "graphify agents install" in hooks
    assert "graphify agents uninstall  # remove the section" in hooks
    # No amp/trae/claude wording leaks into the agents render.
    assert "graphify amp install" not in hooks
    assert "graphify trae" not in hooks
    assert "graphify claude install" not in hooks
    assert "PreToolUse" not in hooks and "PreToolUse" not in core
    # The lean-core pointer names AGENTS.md, not CLAUDE.md.
    assert "## For the commit hook and native AGENTS.md integration" in core
    assert "native CLAUDE.md integration" not in core


def test_agents_body_matches_amp_modulo_hooks_wording():
    """The agents skill body is amp's body verbatim (it re-homes amp's bundle).

    The two platforms differ only in the hooks reference's install/uninstall
    command wording — everything else (core, query, extraction spec, the other
    six references) is byte-identical, which is why agents audits cleanly against
    amp's v8 baseline.
    """
    platforms = gen.load_platforms()
    amp = {a.path.rsplit("/", 1)[-1]: a.content for a in gen.render(platforms["amp"])}
    agents = {a.path.rsplit("/", 1)[-1]: a.content for a in gen.render(platforms["agents"])}
    # The lean-core skill body is identical (frontmatter + steps, no hooks ref).
    assert amp["skill-amp.md"] == agents["skill-agents.md"]
    # Every reference except hooks.md is byte-identical.
    for name in amp:
        if name in ("skill-amp.md", "hooks.md"):
            continue
        assert amp[name] == agents[name], f"{name} drifted between amp and agents"
    assert amp["hooks.md"] != agents["hooks.md"]


def test_agents_audit_baseline_is_amps_v8_body():
    """`agents` is a post-v8 platform, so its audit baseline is amp's v8 body."""
    platforms = gen.load_platforms()
    assert gen._v8_baseline_ref("agents") == "47042beb05d1f6dd2186c0c499ae2840ce604ead:graphify/skill-amp.md"
    problems = gen.audit_coverage(platforms["agents"])
    assert problems == [], "\n".join(problems)
