"""Moo Trader menu-bar status app (rumps).

Its very presence in the menu bar means the trading scheduler is running in the
background — so you can close the web dashboard and still tell at a glance whether
the bot is alive. It quits itself the instant the scheduler stops, so the icon
disappearing == 'scheduler is no longer running'.

It also shows the keep-awake (caffeinate) state and offers one-click actions:
open the dashboard, toggle keep-awake, stop the scheduler.

Launched automatically whenever the scheduler starts (web ▶ Start button, the web
server boot if the scheduler is already up, or start-scheduler.command). The launch
is guarded by logs/menubar.pid so there's never more than one icon.
"""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

import rumps
from AppKit import NSApplication

from src import keepawake
from src.config import ROOT
SCHED_PID = ROOT / "logs" / "scheduler.pid"
MENUBAR_PID = ROOT / "logs" / "menubar.pid"
WEB_PID = ROOT / "logs" / "web.pid"
PORT = int(os.getenv("WEB_PORT", "8770"))


def _pid_alive(pid_file: Path) -> int | None:
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


class MenuBar(rumps.App):
    def __init__(self):
        # Plain text title "MAT" (Moo Auto Trading) — no logo/emoji. macOS renders
        # menu-bar titles in the system colour automatically (black in light mode,
        # white in dark mode). State lives inside the menu, not in the title.
        # quit_button=None: we replace the default Quit with clearly-labelled items
        # so the user never accidentally thinks closing the icon stops trading.
        super().__init__("Moo", title="MAT", quit_button=None)
        self.m_status = rumps.MenuItem("Scheduler …")
        self.m_awake = rumps.MenuItem("☕ 防睡眠 …", callback=self.toggle_awake)
        self.m_open = rumps.MenuItem("🌐 打开 Web 仪表盘", callback=self.open_web)
        self.m_stop = rumps.MenuItem("■ 停止 Scheduler（图标随之消失）", callback=self.stop_sched)
        self.m_hide = rumps.MenuItem("✕ 仅隐藏此图标（后台继续运行）", callback=self.hide_icon)
        self.menu = [self.m_status, None, self.m_awake, self.m_open,
                     None, self.m_stop, self.m_hide]
        self._refresh()

    # ── periodic refresh: track scheduler liveness + keep-awake state ──
    @rumps.timer(3)
    def _tick(self, _sender):
        self._refresh()

    def _refresh(self):
        sp = _pid_alive(SCHED_PID)
        if sp is None:
            # The scheduler is gone — the icon's whole reason to exist with it.
            MENUBAR_PID.unlink(missing_ok=True)
            rumps.quit_application()
            return
        st = keepawake.status()
        awake, lid = st["on"], st["lid"]
        # Title stays a constant "MAT"; all live state shows inside the menu.
        self.m_status.title = f"● Scheduler 运行中 (PID {sp})"
        self.m_awake.title = (
            "☕ 防睡眠: 开" + ("（含合盖）" if lid else "（屏幕可熄灭）")
            if awake else "☕ 防睡眠: 关 — 点击开启")

    # ── actions ──
    def toggle_awake(self, _sender):
        keepawake.turn_off() if keepawake.is_on() else keepawake.turn_on(allow_prompt=True)
        self._refresh()

    def open_web(self, _sender):
        if _pid_alive(WEB_PID) is None:
            # Web server not running — relaunch it (the script opens the browser).
            subprocess.Popen(["/bin/bash", str(ROOT / "start-web.command")],
                             cwd=str(ROOT), start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["/usr/bin/open", f"http://127.0.0.1:{PORT}"])

    def stop_sched(self, _sender):
        if not rumps.alert(
                title="停止 Scheduler？",
                message="将停止后台交易调度（持仓不动，只是不再下新单/管理）。\n"
                        "此菜单栏图标也会随之消失。",
                ok="停止", cancel="取消"):
            return
        pid = _pid_alive(SCHED_PID)
        if pid is not None:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except Exception:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        self._refresh()  # likely quits the icon right away; the timer is the backstop

    def hide_icon(self, _sender):
        MENUBAR_PID.unlink(missing_ok=True)
        rumps.quit_application()


def main():
    app = MenuBar()
    # Accessory mode: live in the menu bar only — no Dock icon, no app-switcher entry.
    try:
        NSApplication.sharedApplication().setActivationPolicy_(1)
    except Exception:
        pass
    app.run()


if __name__ == "__main__":
    main()
