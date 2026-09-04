"""Starting and stopping process TREES, on whichever platform this is running on.

The bot does not run as one process. The worker spawns scanners and model runs,
and the web server spawns the worker — so stopping "the bot" means stopping a
tree, not a pid. Signal the pid alone and the children are orphaned, still
holding an OpenD connection and still able to place an order.

POSIX expresses that with process groups: start the child with
start_new_session=True so it leads its own group, then signal the group.
Windows has no equivalent, and `taskkill /T` walking the child tree is the way
that platform says the same thing. Both live here so a call site does not have
to know which one it is on, and so the next one added cannot forget.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"

# Windows has no SIGKILL. taskkill /F is the same intent, and _signal() maps it.
SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def spawn_kwargs() -> dict:
    """Popen kwargs that make a child lead its own process tree.

    POSIX: its own session, so killpg reaches it and its children. Windows:
    its own process group, so it survives the parent's console closing and
    taskkill /T can find the tree.
    """
    if IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def signal_tree(pid: int, sig=signal.SIGTERM) -> None:
    """Signal a pid AND everything it spawned. Raises if the process is gone.

    ProcessLookupError means "already stopped", which every caller here treats
    as success rather than as an error — the point is that it is not running.
    """
    if not IS_WINDOWS:
        os.killpg(os.getpgid(pid), sig)
        return
    args = ["taskkill", "/PID", str(pid), "/T"]
    if sig == SIGKILL:
        args.append("/F")
    r = subprocess.run(args, capture_output=True)
    if r.returncode != 0:
        # taskkill reports a missing process with a non-zero exit, and callers
        # already handle "already gone" — so say it the way they expect.
        raise ProcessLookupError(
            f"taskkill could not stop pid {pid}: "
            f"{r.stderr.decode(errors='replace').strip()}")


def alive(pid: int) -> bool:
    """Is this pid still running? False also when it is not ours to ask about."""
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                       capture_output=True)
    return str(pid) in r.stdout.decode(errors="replace")
