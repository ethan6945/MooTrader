"""One worker, whatever else is happening.

A pid file is not mutual exclusion. Twenty concurrent Start requests all read
"no pid", all decide to spawn, and the last one to write the file owns a record
of one process while five others trade. The 2026-08-11 incident was a milder
version: a stale parent relaunched a worker while the operator believed none was
running.

So the lease is an O_EXCL create — the filesystem decides the winner, once — and
it carries a fencing token that increments on every acquisition. A worker that
was slow to start and finds the token has moved on knows it lost the race and
must not trade, even if its own process is perfectly healthy. That is the case a
pid check cannot see: the pid is alive, the pid file names it, and it is still
the wrong worker.

STALE HOLDERS
  A crashed holder leaves the file behind. It is only breakable when the recorded
  pid is genuinely gone — and "gone" means no such process, not merely one that
  stopped responding. A pid can also be REUSED, so the record carries the
  process start time; a pid that matches but started later is a different
  process wearing the same number.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)


class LeaseUnavailable(Exception):
    """Someone else holds it, and they are alive."""


@dataclass(frozen=True)
class Lease:
    path: Path
    holder_pid: int
    holder_host: str
    fence: int
    acquired_at: float

    def release(self) -> None:
        """Give it up — only if we still hold it."""
        try:
            cur = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if cur.get("pid") == self.holder_pid and cur.get("fence") == self.fence:
            try:
                self.path.unlink()
            except OSError:
                pass

    def still_held(self) -> bool:
        """Do we still hold it, with the same token?

        The worker checks this before it trades. Between acquiring the lease and
        being ready, another process may have found us stale and taken over.
        """
        try:
            cur = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        return cur.get("pid") == self.holder_pid and cur.get("fence") == self.fence


def _lease_path() -> Path:
    return settings.root / "logs" / "worker.lease"


def _proc_start_time(pid: int) -> str | None:
    """When this pid started, so a reused number is not mistaken for the same
    process. Returns None if the pid does not exist."""
    try:
        r = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out or None


def _holder_alive(rec: dict) -> bool:
    pid = rec.get("pid")
    if not isinstance(pid, int):
        return False
    started = _proc_start_time(pid)
    if started is None:
        return False                      # no such process
    recorded = rec.get("started_at_str")
    if recorded and recorded != started:
        # Same number, different process — the original died and the OS handed
        # the pid to something unrelated. Treating that as a live holder would
        # block every future start until someone deleted the file by hand.
        log.warning("lease pid %s was reused (started %r, recorded %r) — stale",
                    pid, started, recorded)
        return False
    return True


def read() -> dict | None:
    try:
        return json.loads(_lease_path().read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _high_water_path(lease_path: Path) -> Path:
    return lease_path.with_suffix(".fence")


def _remembered_high(lease_path: Path) -> int:
    """The highest token ever issued, surviving the lease file's deletion.

    The lease file goes away on every clean release, taking the counter with it.
    This keeps the count monotonic across the whole life of the installation.
    """
    try:
        return int(_high_water_path(lease_path).read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def _remember_high(lease_path: Path, fence: int) -> None:
    p = _high_water_path(lease_path)
    try:
        if fence > _remembered_high(lease_path):
            fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, str(fence).encode())
                os.fsync(fd)
            finally:
                os.close(fd)
    except OSError as e:
        log.warning("could not persist the fence high-water mark: %s", e)


def take_over(fence: int, *, purpose: str = "worker") -> Lease:
    """Move an existing lease to THIS process, keeping its token.

    The parent takes the lease so that twenty concurrent Start requests produce
    one winner. But the parent is a web server, and the lease has to outlive it:
    if it stays in the parent's name, a parent that dies leaves a record whose
    pid is gone, the next Start finds it stale, breaks it, and spawns a second
    worker while the first is still holding positions and placing orders. The
    lease has to name the process that is actually trading.

    The token does not change. It identifies the START, and both sides have
    already agreed on it; re-issuing here would invalidate the fence the parent
    is about to check in the READY payload.
    """
    p = _lease_path()
    cur = read()
    if not cur:
        raise LeaseUnavailable("no lease to take over — it was released or "
                               "broken between the parent taking it and this "
                               "process starting")
    if int(cur.get("fence", -1)) != int(fence):
        raise LeaseUnavailable(
            f"lease now carries fence {cur.get('fence')}, this worker was "
            f"started under {fence} — another start overtook this one")

    rec = dict(cur)
    rec.update({
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "purpose": purpose,
        "started_at_str": _proc_start_time(os.getpid()),
        "taken_over_at": time.time(),
        "handed_over_from": cur.get("pid"),
    })
    # Replace in place rather than unlink-then-create: for the moment between
    # the two there would be no lease at all, and a concurrent starter would
    # find nothing and win one.
    tmp = p.with_suffix(".handover")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(rec).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(p))
    log.info("worker %s took over the lease from %s (fence %s)",
             rec["pid"], rec["handed_over_from"], fence)
    return Lease(path=p, holder_pid=rec["pid"], holder_host=rec["host"],
                 fence=int(fence), acquired_at=rec.get("acquired_at", time.time()))


def assert_ours(fence: int, *, what: str = "this operation") -> None:
    """Raise unless this process still holds the lease with `fence`.

    Called before every order, modification and cancellation. A pid check
    cannot answer this: our own process is alive and well in exactly the case
    that matters — we were slow, someone found us stale, and a second worker
    took the lease. We are healthy, we are wrong, and the only evidence is the
    token.
    """
    cur = read() or {}
    if cur.get("pid") != os.getpid() or int(cur.get("fence", -1)) != int(fence):
        raise LeaseUnavailable(
            f"{what} refused: this process no longer holds the worker lease "
            f"(lease is pid {cur.get('pid')} fence {cur.get('fence')}, we are "
            f"pid {os.getpid()} fence {fence}). Another worker has taken over.")


def acquire(*, purpose: str = "worker", break_stale: bool = True) -> Lease:
    """Take the lease, or raise LeaseUnavailable.

    Atomic by O_EXCL: whichever process the filesystem lets create the file has
    it, and the rest fail. No read-then-write window for concurrent starters to
    fall through.
    """
    p = _lease_path()
    p.parent.mkdir(parents=True, exist_ok=True)

    # Highest token seen across attempts. Reading it fresh after breaking a
    # stale lease would restart the count at 1 — and a token that can go
    # backwards is not a fencing token. A worker still holding the old 1 would
    # then match a newly issued 1 and believe it was current, which is the
    # exact confusion the token exists to prevent.
    high = int((read() or {}).get("fence", 0))
    high = max(high, _remembered_high(p))

    for attempt in (1, 2):
        prev = read()
        high = max(high, int((prev or {}).get("fence", 0)))
        fence = high + 1
        rec = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "fence": fence,
            "purpose": purpose,
            "acquired_at": time.time(),
            "started_at_str": _proc_start_time(os.getpid()),
        }
        try:
            fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            cur = read()
            if cur and _holder_alive(cur):
                raise LeaseUnavailable(
                    f"worker lease held by pid {cur.get('pid')} on "
                    f"{cur.get('host')} (fence {cur.get('fence')})")
            if not break_stale or attempt == 2:
                raise LeaseUnavailable("lease file present and could not be cleared")
            # Holder is gone. Remove and retry once; if another process wins the
            # retry, the O_EXCL on that attempt fails and we report it honestly.
            log.warning("breaking stale worker lease from pid %s",
                        (cur or {}).get("pid"))
            try:
                p.unlink()
            except OSError:
                pass
            continue
        try:
            os.write(fd, json.dumps(rec).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        _remember_high(p, fence)
        return Lease(path=p, holder_pid=rec["pid"], holder_host=rec["host"],
                     fence=fence, acquired_at=rec["acquired_at"])
    raise LeaseUnavailable("could not acquire the worker lease")
