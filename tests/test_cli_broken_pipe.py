"""CLI must not crash when a downstream reader closes the pipe early (#1807).

Truncating a command's output (`head`, PowerShell `Select-Object -First N`,
`sed q`) is routine. graphify used to keep writing after the reader disconnected,
hit an unhandled BrokenPipeError, and exit 255 — so CI wrappers and agent
harnesses that both trim output and check the exit code read a successful query
as a failure. An early-closing reader is now treated as success (exit 0).
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

PYTHON = sys.executable


def test_help_survives_reader_closing_pipe_early():
    """`graphify --help | head -n1` must leave graphify exiting 0, not 255."""
    producer = subprocess.Popen(
        [PYTHON, "-m", "graphify", "--help"], stdout=subprocess.PIPE
    )
    reader = subprocess.Popen(
        [PYTHON, "-c", "import sys; sys.stdin.readline()"],
        stdin=producer.stdout,
        stdout=subprocess.DEVNULL,
    )
    producer.stdout.close()  # let the producer see EPIPE when the reader exits
    reader.wait()
    rc = producer.wait()
    # 0 (our handled-and-succeed convention). Never the 255 unhandled-exception code.
    assert rc == 0, f"expected clean exit after early pipe close, got {rc}"


def test_small_buffered_output_survives_reader_that_reads_nothing():
    """A short, fully-buffered output (piped stdout is block-buffered) only flushes
    at exit. If the reader closed the pipe without reading, that flush must be
    handled inside the CLI's guard and exit 0, not escape as a shutdown error."""
    producer = subprocess.Popen(
        [PYTHON, "-m", "graphify", "--version"], stdout=subprocess.PIPE
    )
    reader = subprocess.Popen(
        [PYTHON, "-c", "pass"],  # exits immediately, reads nothing
        stdin=producer.stdout,
        stdout=subprocess.DEVNULL,
    )
    producer.stdout.close()
    reader.wait()
    rc = producer.wait()
    assert rc == 0, f"expected clean exit when reader reads nothing, got {rc}"


def test_workspace_nonzero_exit_flushes_inside_broken_pipe_guard(tmp_path: Path):
    """A workspace result must flush before its nonzero SystemExit escapes main()."""
    home = tmp_path / "home"
    checkout = tmp_path / "checkout"
    state_home = tmp_path / "state-home"
    home.mkdir()
    checkout.mkdir()
    environment = dict(os.environ)
    environment.update(
        {
            "HOME": str(home),
            "XDG_STATE_HOME": str(state_home),
            "PYTHONPATH": str(Path(__file__).parents[1]),
        }
    )
    producer = subprocess.Popen(
        [PYTHON, "-m", "graphify", "workspace", "status", "--json"],
        cwd=checkout,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    reader = subprocess.Popen(
        [PYTHON, "-c", "pass"],
        stdin=producer.stdout,
        stdout=subprocess.DEVNULL,
    )
    assert producer.stdout is not None
    producer.stdout.close()
    reader.wait()
    assert producer.stderr is not None
    stderr = producer.stderr.read().decode("utf-8", errors="replace")
    rc = producer.wait()

    assert rc == 0, f"expected guarded broken-pipe exit, got {rc}: {stderr}"
    assert "Exception ignored while flushing sys.stdout" not in stderr


@pytest.mark.parametrize(
    ("arguments", "expected_exit"),
    [
        (["workspace", "register"], 64),
        (
            [
                "workspace",
                "register",
                "enroll",
                "--repo-uuid",
                "11111111-1111-4111-8111-111111111111",
                "--expected-registry-revision",
                "0",
                "--authorization-stdin",
            ],
            20,
        ),
    ],
)
@pytest.mark.parametrize("combined_output", [False, True])
def test_workspace_registration_preserves_failure_exit_when_pipe_is_closed(
    tmp_path: Path,
    arguments: list[str],
    expected_exit: int,
    combined_output: bool,
) -> None:
    home = tmp_path / "home"
    state_home = tmp_path / "state-home"
    checkout = tmp_path / "checkout"
    home.mkdir()
    checkout.mkdir()
    environment = dict(os.environ)
    environment.update(
        {
            "HOME": str(home),
            "XDG_STATE_HOME": str(state_home),
            "PYTHONPATH": str(Path(__file__).parents[1]),
        }
    )
    read_descriptor, write_descriptor = os.pipe()
    os.close(read_descriptor)
    try:
        producer = subprocess.Popen(
            [PYTHON, "-m", "graphify", *arguments],
            cwd=checkout,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=write_descriptor if combined_output else subprocess.DEVNULL,
            stderr=write_descriptor,
        )
    finally:
        os.close(write_descriptor)

    assert producer.wait() == expected_exit
