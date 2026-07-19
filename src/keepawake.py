"""Keep-awake control — single source of truth shared by the web dashboard and the
menu-bar app, so both behave identically.

Two layers:
  1. `caffeinate -i -s`  — a standalone assertion (Amphetamine-style) that stops
     macOS idle/system sleep. The DISPLAY is still allowed to sleep (we omit -d).
     Independent of the trading scheduler: stays on until explicitly turned off.
  2. `sudo pmset -a disablesleep 1` — ALSO blocks lid-closed (clamshell) sleep.
     Needs root, so it runs through a one-time, tightly-scoped sudoers rule
     (scripts/install_lid_rule.sh). First enable pops a native macOS auth dialog;
     a decline is remembered so we never nag again.
"""
from __future__ import annotations

import getpass
import json
import os
import signal
import shlex
import subprocess
import time
from pathlib import Path

from .config import ROOT

CAFFEINATE_PID = ROOT / "logs" / "caffeinate.pid"
LID_STATE_FILE = ROOT / "data" / "lid_setup.json"


# ── pid helpers ────────────────────────────────────────────────────────────────
def _pid_running(pid_file: Path) -> int | None:
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def _stop(pid_file: Path) -> bool:
    """SIGTERM the process group recorded in pid_file, then remove the file."""
    pid = _pid_running(pid_file)
    if pid is None:
        pid_file.unlink(missing_ok=True)
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(15):
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.2)
    pid_file.unlink(missing_ok=True)
    return True


# ── caffeinate (idle/system sleep) ───────────────────────────────────────────────
def is_on() -> bool:
    return _pid_running(CAFFEINATE_PID) is not None


def _start_caffeinate() -> None:
    if is_on():
        return
    proc = subprocess.Popen(
        ["/usr/bin/caffeinate", "-i", "-s"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    CAFFEINATE_PID.write_text(str(proc.pid))


# ── lid-closed sleep (pmset, needs root via the one-time sudoers rule) ───────────
def lid_disabled() -> bool:
    """True if clamshell (lid-closed) sleep is currently disabled. `pmset -g` prints
    a `SleepDisabled 1` line only when it's on (no sudo needed to read)."""
    try:
        out = subprocess.run(["/usr/bin/pmset", "-g"],
                             capture_output=True, text=True, timeout=4).stdout
        for line in out.splitlines():
            if "SleepDisabled" in line:
                return line.split()[-1].strip() == "1"
    except Exception:
        pass
    return False


def _set_lid(disabled: bool) -> bool:
    """Toggle lid-closed sleep via passwordless sudo pmset. Returns True only if the
    command actually ran (sudoers rule installed). `sudo -n` never prompts, so a
    missing rule fails quietly (returncode != 0) with NO side effects."""
    try:
        r = subprocess.run(
            ["/usr/bin/sudo", "-n", "/usr/bin/pmset", "-a",
             "disablesleep", "1" if disabled else "0"],
            capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def lid_declined() -> bool:
    try:
        return bool(json.loads(LID_STATE_FILE.read_text()).get("declined"))
    except Exception:
        return False


def _mark_declined() -> None:
    LID_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    LID_STATE_FILE.write_text(json.dumps({"declined": True}))


def lid_rule_installed() -> bool:
    """Whether the passwordless pmset sudoers rule is present (lid mode available
    without a prompt). Probes harmlessly by reading the *current* SleepDisabled
    value back through `sudo -n pmset`."""
    cur = "1" if lid_disabled() else "0"
    try:
        r = subprocess.run(
            ["/usr/bin/sudo", "-n", "/usr/bin/pmset", "-a", "disablesleep", cur],
            capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _install_lid_rule_gui() -> bool:
    """First-time only: pop the NATIVE macOS admin-auth dialog and install the
    passwordless pmset sudoers rule. True if installed, False if the user cancelled.
    Blocks until the user answers the dialog."""
    user = getpass.getuser()
    installer = ROOT / "scripts" / "install_lid_rule.sh"
    shell_cmd = f"/bin/bash {shlex.quote(str(installer))} {shlex.quote(user)}"

    def _as(s: str) -> str:  # quote a Python string as an AppleScript string literal
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    prompt = ("Moo Trader 想在合盖时也保持后台运行，需要一次管理员授权"
              "（仅授权 pmset 防睡眠，可随时撤销）。")
    osa = (f"do shell script {_as(shell_cmd)} "
           f"with prompt {_as(prompt)} with administrator privileges")
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", osa],
                           capture_output=True, text=True, timeout=180)
        return r.returncode == 0
    except Exception:
        return False


# ── public API ───────────────────────────────────────────────────────────────────
def turn_on(allow_prompt: bool = True) -> dict:
    """Start keep-awake. Always blocks idle/system sleep; additionally tries to block
    lid-closed sleep — silently if the sudoers rule exists, else (first time, if not
    previously declined and allow_prompt) via a one-time macOS auth dialog."""
    _start_caffeinate()
    if not _set_lid(True) and allow_prompt and not lid_declined():
        if _install_lid_rule_gui():
            _set_lid(True)
        else:
            _mark_declined()
    return status()


def turn_off() -> dict:
    _stop(CAFFEINATE_PID)
    _set_lid(False)  # harmless no-op if the rule isn't installed
    return status()


def status() -> dict:
    return {"on": is_on(), "lid": lid_disabled()}
