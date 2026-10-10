"""Bounded stdout transport for the callers' explicit Git authority modes."""
from collections.abc import Generator
import os
import selectors
import subprocess
import time


class GitReadError(RuntimeError):
    """Git output could not be read within the selected resource bounds."""


def git_stdout(command: list[str], env: dict[str, str], limit: int) -> Generator[bytes, None, None]:
    """Bound active Git I/O, excluding caller work; kill and reap on refusal."""
    with subprocess.Popen(  # nosec B603 - caller supplies fixed Git arguments, no shell
        command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
    ) as process:
        try:
            if process.stdout is None:
                raise GitReadError("Git inspection pipe is unavailable")
            total = 0
            deadline = time.monotonic() + 30
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(max(0, remaining)):
                        raise GitReadError("Git object inspection timed out")
                    chunk = os.read(process.stdout.fileno(), min(65536, limit + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise GitReadError("Git object inspection exceeds bounds")
                    paused = time.monotonic()
                    try:
                        yield chunk
                    finally:
                        # Streaming consumers may parse a whole graph. Preserve
                        # the accumulated I/O budget without charging their work.
                        deadline += time.monotonic() - paused
            if process.wait(timeout=max(0.1, deadline - time.monotonic())):
                raise GitReadError("Git object inspection failed")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
