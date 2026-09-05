"""Web dashboard backend (Flask) — monitor + control the bot from a browser.

Runs as its OWN background process (macos-start-web.command / windows-start-web.bat). The trading scheduler
(`python -m src.main run`) is a SEPARATE background process — closing the browser
does NOT stop trading; this server just reads the bot's state (data/account.json
+ SQLite) and sends control actions (start/stop, approvals, budget).

Endpoints (JSON):
  GET  /                     → dashboard page
  GET  /api/status           → account snapshot + scheduler running flag
  GET  /api/approvals        → approval queue (pending first)
  POST /api/approvals/<id>/<approve|reject>
  GET  /api/closed?n=        → recent closed trades (History + Equity)
  GET  /api/sectors          → live US sector ETF overview (60s cache)
  GET  /api/log?n=           → tail of logs/trader.log (compact activity)
  POST /api/budget           → {value} set runtime budget (no restart)
  POST /api/scheduler/<start|stop>
  POST /api/param-tune/run   → run the backtest tuning chain (background thread)
  POST /api/param-tune/stop  → stop it at the next checkpoint, keeping partials
  GET  /api/param-tune       → its progress, and the survivors awaiting confirm
  POST /api/param-tune/apply → write the changes the owner ticked
"""
from __future__ import annotations

import gc
import hashlib
import hmac
import json
import logging
import os
import secrets as _secrets
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, make_response, redirect, request, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import ai, approvals, clock, db, keepawake, risk_manager  # noqa: E402
from src import proc
from src.config import BUNDLE_DIR, IS_FROZEN, ROOT, settings  # noqa: E402

# This module had no logger, while api_exit's error path already called
# log.warning — a NameError waiting for the one case it was written for. Note
# that three handlers bind a LOCAL `log` to an open file; those are unaffected.
log = logging.getLogger(__name__)

# In the frozen .app the static assets ship inside the bundle; in dev
# BUNDLE_DIR == repo root, so this resolves to web/static either way.
STATIC = BUNDLE_DIR / "web" / "static"
# Override only for running an isolated/secondary instance (e.g. tests). Default = repo .env.
ENV_FILE = Path(os.getenv("WEB_ENV_FILE") or (ROOT / ".env"))
ACCOUNT_FILE = ROOT / "data" / "account.json"
TRADER_LOG = ROOT / "logs" / "trader.log"
SIGNAL_WL_FILE = ROOT / "config" / "signal_watchlist.json"
SELF_REVIEW_FILE = ROOT / "data" / "self_review_last.json"
OPEND_PID = ROOT / "logs" / "opend.pid"
VENV_PY = ROOT / ".venv" / "bin" / "python"


def _worker_cmd(module: str, *args: str) -> list[str]:
    """Command that runs `<module>.main()` as its own detached process.

    Dev: `.venv/bin/python -m <module> <args…>` — the repo venv sits next to
    the code.

    Frozen: there IS no venv. ROOT is ~/Library/Application Support/MooMooTrader
    and the interpreter only exists inside the .app, so the app re-execs its own
    binary with a `--worker` switch; packaging/entry.py routes that through
    runpy, giving the module the same argv it would see under `python -m`.
    sys.executable is the bundled backend binary under PyInstaller.
    """
    from src.config import worker_cmd as _shared
    return _shared(module, *args)


app = Flask(__name__, static_folder=None)


@app.after_request
def _no_cache(resp):
    # Always serve the freshest dashboard (no stale cached HTML/JS in the browser).
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


# ── auth: optional password gate (needed when exposed beyond localhost) ────────
# Zero new deps. A signed cookie = HMAC(secret, password). If WEB_PASSWORD is not
# set the whole gate is OFF (localhost-only dev convenience). Changing the password
# instantly invalidates every old cookie. The server REFUSES to bind to a non-local
# host without a password (see main()).
AUTH_COOKIE = "wt_auth"
_PUBLIC_PATHS = {
    "/login", "/api/login", "/api/logout", "/favicon.ico",
    "/static/favicon.svg", "/static/favicon-32.png", "/static/apple-touch-icon.png",
}
_pw_cache = {"mtime": -1.0, "v": ""}
_secret_cache = {"v": ""}

# Login throttle — lock an IP out after repeated wrong passwords, so the control
# panel (which can stop the scheduler / change budget / write .env keys) can't be
# brute-forced online. In-memory: a restart resets it, which is fine since brute
# force needs sustained attempts. Concurrency: dict ops are atomic under CPython's
# GIL; an occasional miscount can't weaken the lock.
_login_fails: dict = {}          # ip → [fail_count, locked_until_epoch, last_seen_epoch]
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SEC = 300


def _login_locked(ip: str) -> bool:
    rec = _login_fails.get(ip)
    return bool(rec and rec[1] > time.time())


def _login_record(ip: str, ok: bool) -> None:
    if ok:
        _login_fails.pop(ip, None)
        return
    now = time.time()
    # Bound memory: an attacker rotating source IPs (only ever failing, never
    # authenticating) would otherwise grow this dict without limit. Prune every
    # OTHER entry that is not currently locked and has been idle for a full lock
    # window — never the current ip (pruning it here would reset its own streak
    # before the count below reads it, so the lockout could never trigger).
    for k, v in list(_login_fails.items()):
        if k != ip and v[1] < now and (now - v[2]) > LOGIN_LOCK_SEC:
            _login_fails.pop(k, None)
    rec = _login_fails.get(ip)
    # Rolling window: if this ip is unlocked and has been quiet for a full lock
    # window, start its fail streak fresh rather than accumulating forever.
    if rec and rec[1] < now and (now - rec[2]) > LOGIN_LOCK_SEC:
        rec = None
    count = (rec[0] if rec else 0) + 1
    locked = now + LOGIN_LOCK_SEC if count >= LOGIN_MAX_FAILS else 0.0
    _login_fails[ip] = [count, locked, now]


def _web_password() -> str:
    """Configured access password (re-read from .env when the file changes, so
    setting it in the panel takes effect without a restart). Empty = no auth."""
    try:
        m = ENV_FILE.stat().st_mtime
    except Exception:
        m = 0.0
    if m != _pw_cache["mtime"]:
        _pw_cache["mtime"] = m
        _pw_cache["v"] = (_read_env().get("WEB_PASSWORD", "") or os.getenv("WEB_PASSWORD", "")).strip()
    return _pw_cache["v"]


def _web_secret() -> str:
    """Stable per-install signing secret. Generated once and persisted to .env so
    cookies survive restarts."""
    if _secret_cache["v"]:
        return _secret_cache["v"]
    s = (_read_env().get("WEB_SECRET", "") or os.getenv("WEB_SECRET", "")).strip()
    if not s:
        s = _secrets.token_hex(32)
        try:
            _write_env_key("WEB_SECRET", s)
        except Exception:
            pass
    _secret_cache["v"] = s
    os.environ["WEB_SECRET"] = s
    return s


def _auth_token(pw: str) -> str:
    return hmac.new(_web_secret().encode(), pw.encode(), hashlib.sha256).hexdigest()


def _authed() -> bool:
    pw = _web_password()
    if not pw:
        return True   # auth disabled
    return hmac.compare_digest(request.cookies.get(AUTH_COOKIE, ""), _auth_token(pw))


@app.before_request
def _require_auth():
    if not _web_password():
        return None                       # gate off
    if request.path in _PUBLIC_PATHS or _authed():
        return None
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "auth required"}), 401
    return redirect("/login")


@app.route("/login")
def login_page():
    return send_from_directory(STATIC, "login.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    pw_cfg = _web_password()
    if not pw_cfg:
        return jsonify({"ok": True})      # no auth configured
    ip = request.remote_addr or "?"
    if _login_locked(ip):
        return jsonify({"ok": False, "error": "尝试过多，请稍后再试"}), 429
    pw = (request.json or {}).get("password", "")
    if isinstance(pw, str) and hmac.compare_digest(pw, pw_cfg):
        _login_record(ip, True)
        resp = make_response(jsonify({"ok": True}))
        # secure follows the actual transport: on plain HTTP (LAN dev) we must NOT
        # set Secure or the browser would drop the cookie; behind HTTPS it engages
        # automatically for defence-in-depth.
        resp.set_cookie(AUTH_COOKIE, _auth_token(pw_cfg),
                        max_age=60 * 60 * 24 * 30, httponly=True,
                        samesite="Lax", secure=request.is_secure)
        return resp
    _login_record(ip, False)
    return jsonify({"ok": False, "error": "wrong password"}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie(AUTH_COOKIE)
    return resp



# ── helpers ──────────────────────────────────────────────────────────────────

def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _tail(p: Path, n: int) -> list[str]:
    try:
        return p.read_text(errors="replace").splitlines()[-n:]
    except Exception:
        return []


def _pid_running(pid_file: Path) -> int | None:
    """Return the live PID recorded in pid_file, or None if not running.

    2026-07-07: also reject ZOMBIES — os.kill(pid, 0) succeeds on a defunct
    child this server spawned but never reaped, so a dead scheduler showed
    as "running" on the dashboard until the web server restarted."""
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)   # signal 0 = liveness check
        out = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=3)
        if out.stdout.strip().startswith("Z"):
            return None   # zombie — process is dead, entry not yet reaped
        return pid
    except Exception:
        return None


def _scheduler_pid() -> int | None:
    """The worker's pid, from the lease that grants it the right to trade."""
    try:
        from src import start_lease
        rec = start_lease.read() or {}
        pid = rec.get("pid")
        return pid if isinstance(pid, int) else None
    except Exception:
        return None


def _scheduler_running() -> bool:
    """Is a worker running? Answered by the lease, not by a pid file.

    logs/scheduler.pid was written by the spawn path that no longer exists, so
    reading it now would report "not running" while a worker was trading — the
    exact wrong direction for a dashboard, and the same class of mistake as the
    incident: a control surface confidently describing a process it had lost
    track of. The lease is the record of which process holds the right to
    trade, so it is also the honest answer to whether one does.
    """
    try:
        from src import start_lease
        rec = start_lease.read()
        if not rec:
            return False
        pid = rec.get("pid")
        if not isinstance(pid, int):
            return False
        os.kill(pid, 0)
        out = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=3)
        return not out.stdout.strip().startswith("Z")
    except Exception:
        return False


def _opend_running() -> bool:
    """True if OpenD is reachable on the configured host:port."""
    try:
        with socket.create_connection(
            (settings.moo_host, settings.moo_port), timeout=0.6
        ):
            return True
    except Exception:
        return False


def _start_opend() -> str | None:
    """Ensure OpenD is reachable, launching the OFFICIAL moomoo_OpenD.app if not.
    Returns error message or None on success.

    2026-07-09: reverted from the headless OpenD-rs gateway — its v1.4.122
    silently dropped every order (need_op_confirm stub with order_id=0, purged
    after ~30s with no broker push), which turned each buy into a fake
    MANUAL_SELL ghost + re-buy loop. The official app keeps its own login
    session; we just launch it and wait for port 11111."""
    if _opend_running():
        return None  # already up

    try:
        subprocess.run(["/usr/bin/open", "-a", "moomoo_OpenD"],
                       capture_output=True, text=True, timeout=10, check=True)
    except Exception as e:
        return f"启动 moomoo_OpenD.app 失败: {e}"

    # GUI login can take a while (saved session usually auto-logs-in).
    for _ in range(60):
        if _opend_running():
            return None  # success
        time.sleep(1)
    return ("moomoo_OpenD.app 已启动但端口 11111 未就绪 — 请到 OpenD 窗口完成"
            "登录（账号/密码/验证码），登录成功后再点 ▶ 启动")


def _stop_opend() -> bool:
    """Stop the OpenD process we started. Returns True if there was one to stop."""
    pid = _pid_running(OPEND_PID)
    if pid is None:
        # Also try killing any futu-opend by name (belt-and-suspenders)
        try:
            subprocess.run(
                ["taskkill", "/IM", "futu-opend.exe", "/F"] if proc.IS_WINDOWS
                else ["pkill", "-x", "futu-opend"], timeout=3,
                capture_output=True)
        except Exception:
            pass
        try:
            OPEND_PID.unlink()
        except FileNotFoundError:
            pass
        return False
    # killpg first (OpenD may have children); fall back to a direct kill —
    # the old version's `except Exception` fallback was unreachable because
    # every kill failure is an OSError subclass caught by the first clause.
    try:
        proc.signal_tree(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    for _ in range(10):
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.2)
    try:
        OPEND_PID.unlink()
    except FileNotFoundError:
        pass
    return True


def _stop_pid(pid_file: Path) -> bool:
    """Gracefully stop the process group recorded in pid_file (mirrors the GUI):
    SIGTERM the whole group, wait up to ~3s, then remove the pid file. Returns
    True if there was a live process to signal."""
    pid = _pid_running(pid_file)
    if pid is None:
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass
        return False
    try:
        proc.signal_tree(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
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
    try:
        pid_file.unlink()
    except FileNotFoundError:
        pass
    return True


def _opend_status(acct: dict, sched_running: bool,
                  lang: str = "zh") -> tuple[str, str]:
    """OpenD light, inferred WITHOUT opening a second broker connection (a fresh
    connection has its own unlock state and wouldn't reflect the scheduler's).

    2026-07-09 honesty rewrite: the old badge claimed 已解锁 whenever an accinfo
    query had succeeded — WRONG premise: queries never require unlock (the broker's
    trade unlock only gates order ops on REAL accounts) and SIMULATE has no
    unlock concept at all, so the badge lit green with the trade lock untouched.
    Now the unlock claim is env-aware:
      · SIMULATE — never mentions 解锁. A fresh snapshot proves the pipeline
        (OpenD + scheduler + account query) works, and simulate orders need no
        unlock → "模拟盘 · 可交易".
      · REAL — 已解锁 only after a gated order op actually succeeded this
        scheduler session (`real_unlock_confirmed` in the snapshot, written by
        src/main.py from moo_client). Until then the badge warns 解锁未确认
        — a fresh REAL session that hasn't traded yet shows the warning even if
        the user did unlock; erring on the safe side is the point.

      red    — OpenD socket unreachable (not started)
      green  — pipeline OK + market open + snapshot fresh (+ REAL: unlock proven)
      blue   — pipeline OK but market closed → snapshot ages out BY DESIGN
      yellow — reachable but worth a look (label says why)

    The label is the only pill in the top bar whose text is the server's, so it
    takes `lang` like everything else the panel renders. It used to be Chinese
    unconditionally, which put one Chinese pill in the middle of an otherwise
    English top bar.
    """
    en = lang == "en"

    def L(zh: str, en_: str) -> str:
        return en_ if en else zh

    try:
        with socket.create_connection((settings.moo_host, settings.moo_port), timeout=0.6):
            pass
    except Exception:
        return "red", L("OpenD 未启动", "OpenD not running")

    # A written snapshot proves a SUCCESSFUL accinfo query (the writer bails
    # before writing if the query raises) — i.e. the data pipeline works. It
    # says NOTHING about the trade unlock (queries don't need it).
    snapshot_ok = acct.get("cash") is not None
    is_real = ((acct.get("trade_env") or settings.moo_trade_env or "SIMULATE")
               .upper() == "REAL")
    unlock_ok = bool(acct.get("real_unlock_confirmed"))
    interval = (acct.get("scan_interval_min") or 30) * 60
    try:
        age = time.time() - ACCOUNT_FILE.stat().st_mtime
    except Exception:
        age = float("inf")
    fresh = age < (interval * 2 + 300)

    session = clock.market_session()   # plain system NY time — no network/drift cost
    market_open = session == "open"

    # Label prefix: what we can honestly claim about trading capability.
    #   SIMULATE        → 模拟盘 (no unlock exists; orders always work)
    #   REAL, proven    → 已解锁
    #   REAL, unproven  → handled separately below (warning state)
    prefix = (L("模拟盘", "paper") if not is_real
              else L("已解锁", "unlocked"))

    if sched_running and snapshot_ok and market_open and fresh:
        if is_real and not unlock_ok:
            return "yellow", L("已连接 · 解锁未确认 — 请在 OpenD 窗口点击解锁",
                               "connected · unlock unconfirmed — click unlock in "
                               "the OpenD window")
        return "green", prefix + L(" · 可交易", " · can trade")

    # Off-hours: scanning intentionally pauses, so a stale snapshot is expected.
    # Tolerate a Fri-close→Mon-open weekend plus an adjacent holiday.
    OFF_HOURS_GRACE = 4 * 24 * 3600   # 4 days
    if sched_running and snapshot_ok and not market_open and age < OFF_HOURS_GRACE:
        rest = ({
            "premarket":  "pre-market",
            "afterhours": "closed",
            "weekend":    "weekend",
            "holiday":    "holiday",
        } if en else {
            "premarket":  "待开盘",
            "afterhours": "已收盘",
            "weekend":    "周末休市",
            "holiday":    "假期休市",
        }).get(session, L("休市", "market closed"))
        if is_real and not unlock_ok:
            # Nothing trades off-hours, so blue (not alarming) — but flag that
            # the unlock still needs doing before the next open.
            return "blue", rest + L(" · 解锁未确认(开盘前请解锁)",
                                    " · unlock unconfirmed (unlock before the open)")
        return "blue", f"{prefix} · {rest}"

    # 走到这里 = 既非绿(可交易)也非蓝(休市)。真正的问题在调度器/快照管道,
    # 不在锁本身 —— 前缀只报能证实的事。
    if snapshot_ok:
        head = (prefix if unlock_ok or not is_real
                else L("已连接", "connected"))
        if not sched_running:
            return "yellow", head + L(" · 调度器已停", " · scheduler stopped")
        return "yellow", head + L(" · 连接中…", " · connecting…")
    return "yellow", L("已连接 · 等待首次账户查询",
                       "connected · waiting for the first account query")


def _trade_summary() -> dict:
    """Gross won / gross lost / net / count + current consecutive streak.
    Respects a 'stats_reset_at' baseline (counts only trades closed after it)."""
    rows = db.closed_trades(limit=10_000)
    reset_at = db.get_state().get("stats_reset_at")
    if reset_at:
        rows = [r for r in rows if (r.get("ts") or "") >= reset_at]
    wins = sum(1 for r in rows if (r.get("pnl") or 0) > 0)
    won = sum(r["pnl"] for r in rows if (r.get("pnl") or 0) > 0)
    lost = sum(r["pnl"] for r in rows if (r.get("pnl") or 0) < 0)
    n = len(rows)
    streak, kind = 0, None
    for r in reversed(rows):
        s = 1 if (r.get("pnl") or 0) > 0 else (-1 if (r.get("pnl") or 0) < 0 else 0)
        if s == 0:
            continue
        if kind is None:
            kind, streak = s, 1
        elif s == kind:
            streak += 1
        else:
            break
    return {"count": n, "wins": wins,
            "win_rate": round(wins / n * 100, 1) if n else 0.0,
            "gross_won": round(won, 2), "gross_lost": round(lost, 2),
            "net": round(won + lost, 2),
            "streak": streak,
            "streak_kind": "win" if kind == 1 else ("loss" if kind == -1 else "none")}


# ── pages ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    # First-run gate (2026-07-28): a freshly installed .app has no broker
    # gateway, no API key and no .env, and the dashboard is meaningless (and
    # alarming — empty positions, failing health) until those exist. Send the
    # user to the wizard instead. setup_state() is a cheap file read + one
    # 0.4s TCP probe, and once complete this is a dict lookup.
    if not setup_state()["complete"]:
        return redirect("/setup")
    return send_from_directory(STATIC, "index.html")


@app.route("/setup")
def setup_page():
    return send_from_directory(STATIC, "setup.html")


@app.route("/static/<path:fn>")
def static_files(fn):
    return send_from_directory(STATIC, fn)


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(STATIC, "favicon-32.png")


# ── read API ─────────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    acct = _read_json(ACCOUNT_FILE, {})
    sched = _scheduler_running()
    acct["scheduler_running"] = sched
    # The pid too, from the same record. The native panel used to read it out
    # of logs/scheduler.pid — a file the spawn path that wrote it took with it
    # when it was replaced. Nothing has written it since, so alivePID() found
    # nothing and the menu bar said "scheduler stopped" while a worker was
    # holding the lease and trading. _scheduler_running fixed exactly this on
    # the Python side and the Swift side never got the fix.
    acct["scheduler_pid"] = _scheduler_pid() if sched else None
    acct["opend_status"], acct["opend_label"] = _opend_status(acct, sched, _lang())
    # Build identity. `version` is what the settings panels show; `running_from`
    # and `home` are here because a version alone cannot tell you WHICH copy is
    # executing — the 2026-08-05 incident was an app running out of a build
    # directory while everyone read the version and assumed /Applications.
    try:
        from src.config import app_version, running_from
        acct["version"] = app_version()
        acct["running_from"] = running_from()
        acct["home"] = str(ROOT)
    except Exception:
        pass
    try:
        acct["budget"] = risk_manager.budget_usd()   # live override beats stale snapshot
    except Exception:
        pass
    # AI health (2026-08-07). Read from the runtime ledger + the watchdog's last
    # verdict — NOT by probing here, because /api/status is polled every 4s and
    # a probe per poll would bill the provider ~900 times an hour. Surfaced so
    # the dashboard can show a banner: a fail-safe AI layer degrades silently by
    # design, and "silently" is the part that cost four blind trading days.
    try:
        from src import ai as _ai
        from src import db as _db
        _ch = _ai.call_health()
        _st = _db.get_state()
        _watchdog_ok = _st.get("health_ai_ok")
        _calls_ok = _st.get("health_ai_calls_ok")
        _has_key = _ai.has_key()
        _down = _has_key and (_watchdog_ok is False or _calls_ok is False)
        try:
            from src import news_driven as _nd
            _nd_on = _nd.enabled()
        except Exception:
            _nd_on = False
        acct["ai_health"] = {
            "ok": not _down,
            "has_key": _has_key,
            "provider": _ai.PROVIDER_LABELS.get(_ai.active_provider(), "AI"),
            "model": _ai.active_model(),
            "fail_streak": _ch.get("fail_streak", 0),
            "last_error": _ch.get("last_error", ""),
            "last_ok": _ch.get("last_ok"),
            # Whether an outage stops trading outright or only blinds the
            # advisory layers — the banner says different things for each.
            "blocks_trading": bool(_nd_on),
        }
    except Exception:
        pass
    # Enrich per_position with the GUI's open-trade fields (entry/stop/tp/atr) so the
    # web positions table mirrors the desktop GUI exactly. account.per_position only
    # carries live price/PnL; the static trade params live in open_trades.json.
    try:
        # From the database, not the JSON mirror. The mirror is written after
        # the fact and goes stale whenever anything corrects the database
        # without refreshing it — on 2026-08-11 it showed a phantom HPE 64 for
        # hours after that position had been removed as never-filled. A panel
        # that displays a holding the account does not have is worse than one
        # that displays nothing.
        trades = db.load_open_trades()
        pp = acct.get("per_position") or {}
        for sym, tr in trades.items():
            cell = pp.setdefault(sym, {})
            cell.setdefault("qty", tr.get("qty"))
            cell["entry_price"] = tr.get("entry_price")
            cell["stop_loss"]   = tr.get("stop_loss")
            cell["take_profit"] = tr.get("take_profit")
            cell["atr"]         = tr.get("atr")
            cell["strategy"]    = tr.get("strategy")
            cell["pattern"]     = tr.get("pattern")   # chart pattern (pattern strategy only)
            # Manual-adoption status so the web can badge YOUR own broker-app buys
            # and show whether the bot has taken over or you're self-managing.
            cell["manual_adopted"] = bool(tr.get("manual_adopted"))
            cell["user_managed"]   = bool(tr.get("user_managed"))
            cell["adopt_risk"]     = tr.get("adopt_risk")
        acct["per_position"] = pp
    except Exception:
        pass
    try:
        acct["summary"] = _trade_summary()
    except Exception:
        acct["summary"] = {}
    # Live realized today/total from db state (reflects a stats reset instantly,
    # not waiting for the next snapshot write).
    try:
        st = db.get_state()
        acct["realized_pnl_today"] = float(st.get("realized_pnl_today") or 0)
        acct["realized_pnl_total"] = float(st.get("realized_pnl_total") or 0)
    except Exception:
        pass
    # Auto-compounding budget snapshot (enabled/armed/seed/target) for the panel.
    try:
        from src import auto_budget
        acct["auto_budget"] = auto_budget.status()
    except Exception:
        pass
    # Which file to tell the user to double-click. The panel is served to
    # whatever device is looking at it — a phone on the LAN, say — so this is
    # the SERVER's platform, not the browser's, and hardcoding the .command
    # name told every Windows user to open a file they do not have.
    acct["launcher"] = ("windows-start-web.bat" if proc.IS_WINDOWS
                        else "macos-start-web.command")
    return jsonify(acct)


@app.route("/api/approvals")
def api_approvals():
    items = approvals.list_all()
    items.sort(key=lambda a: (a.get("status") != "pending", a.get("created_at", "")))
    return jsonify(items)


@app.route("/api/closed")
def api_closed():
    """The History tab — the RAW ledger, including quality-marked rows.

    This view is the record of what was written, so it must show everything;
    hiding a duplicated close here is how nobody notices there was one. Rows
    carry their ledger_quality marking in `extra` so the UI can label them.
    Anything that reasons about performance takes the default instead.
    """
    n = int(request.args.get("n", 100))
    rows = db.closed_trades(limit=10_000, include_excluded=True)
    return jsonify(rows[-n:])


@app.route("/api/log")
def api_log():
    n = int(request.args.get("n", 40))
    return jsonify(_tail(TRADER_LOG, n))


# ── live US sector overview (dashboard panel) ─────────────────────────────────
# 11 SPDR sector ETFs + SMH (semis — this bot's home turf) + broad indices.
# yfinance daily bars: today's Close row tracks the live price intraday, so
# last-vs-prev close = today's % change during RTH and yesterday's after close.
_SECTOR_ETFS = [
    ("XLK", "科技", "Tech"),
    ("SMH", "半导体", "Semis"),
    ("XLC", "通讯服务", "Comm Svcs"),
    ("XLY", "可选消费", "Cons Disc"),
    ("XLP", "必需消费", "Staples"),
    ("XLV", "医疗保健", "Health Care"),
    ("XLF", "金融", "Financials"),
    ("XLI", "工业", "Industrials"),
    ("XLE", "能源", "Energy"),
    ("XLB", "材料", "Materials"),
    ("XLRE", "房地产", "Real Estate"),
    ("XLU", "公用事业", "Utilities"),
]
_INDEX_ETFS = [
    ("SPY", "标普500", "S&P 500"),
    ("QQQ", "纳指100", "Nasdaq 100"),
    ("IWM", "罗素2000", "Russell 2000"),
]
_sector_cache: dict = {"ts": 0.0, "data": None}
_sector_lock = threading.Lock()
_SECTOR_TTL = 60.0  # one upstream fetch per minute, shared by every open browser


def _fetch_sectors() -> dict:
    import yfinance as yf

    syms = [r[0] for r in _SECTOR_ETFS + _INDEX_ETFS]
    # threads=False on purpose. Each yfinance worker thread opens its OWN
    # SQLite connection to the tz cache (peewee parks connection state in a
    # threading.local), and those objects land in reference cycles that only a
    # generational GC pass breaks. A mostly-idle Flask process allocates far
    # too little to trigger a gen-2 collection, so at ~28 leaked fds per call
    # and one call per minute this panel walked straight into EMFILE — see the
    # 22 "Too many open files" tracebacks in logs/web.log. 15 symbols sitting
    # behind a 60s cache do not need the parallelism.
    df = yf.download(syms, period="5d", interval="1d",
                     progress=False, auto_adjust=False, threads=False)
    # Belt and braces: reclaim those cycles now instead of whenever a gen-2
    # pass happens to fire. df stays referenced, so this only frees garbage.
    gc.collect()
    closes = df["Close"]

    def pack(spec):
        out = []
        for sym, zh, en in spec:
            try:
                s = closes[sym].dropna()
                last, prev = float(s.iloc[-1]), float(s.iloc[-2])
                out.append({"sym": sym, "zh": zh, "en": en, "price": round(last, 2),
                            "pct": round((last / prev - 1) * 100, 2)})
            except Exception:
                continue  # one bad ticker shouldn't blank the whole panel
        return out

    sectors, indices = pack(_SECTOR_ETFS), pack(_INDEX_ETFS)
    if not sectors:
        raise RuntimeError("yfinance returned no sector data")
    session = clock.market_session()
    try:
        asof = str(closes.dropna(how="all").index[-1].date())
    except Exception:
        asof = None
    return {"sectors": sectors, "indices": indices, "session": session,
            "live": session == "open", "asof": asof}


@app.route("/api/licence")
def api_licence():
    """Trial / licence state for the panel. Never returns the key itself.

    The key is a bearer credential — anyone holding it can activate on their
    own machine up to the licence's limit — so it is write-only over this API.
    The panel shows what state the copy is in and the machine id to quote when
    buying, and nothing that helps someone re-use a licence they can already see.
    """
    from src import licence
    st = licence.status()
    return jsonify({
        "state": st.state,
        "may_trade": st.may_trade,
        "days_left": st.days_left,
        "detail": st.detail,
        "licence_id": st.licence_id,
        "ends_on": st.ends_on,
        "machine": licence.machine_id(),
        "trial_days": licence.TRIAL_DAYS,
    })


@app.route("/api/licence/activate", methods=["POST"])
def api_licence_activate():
    """Take a licence key from the panel and try to activate this installation."""
    from src import licence
    key = ((request.json or {}).get("key") or "").strip()
    # One response shape for every outcome. The empty-key case used to answer
    # with "error" while a rejected key answered with "message", so the panel
    # had to know which failure it was looking at to find the sentence to show.
    if not key:
        message, ok = "Paste your licence key first.", False
    else:
        ok, message = licence.activate(key)
    st = licence.status()
    return (jsonify({"ok": ok, "message": message, "error": None if ok else message,
                     "state": st.state, "detail": st.detail,
                     "licence_id": st.licence_id}),
            200 if ok else 400)


@app.route("/api/sectors")
def api_sectors():
    cached = _sector_cache["data"]
    if cached is not None and time.time() - _sector_cache["ts"] < _SECTOR_TTL:
        return jsonify(cached)
    with _sector_lock:
        # another request may have refreshed while we waited on the lock
        cached = _sector_cache["data"]
        if cached is not None and time.time() - _sector_cache["ts"] < _SECTOR_TTL:
            return jsonify(cached)
        try:
            data = _fetch_sectors()
        except Exception as e:
            if cached is not None:  # serve stale rather than a broken panel
                return jsonify({**cached, "stale": True})
            return jsonify({"error": str(e)}), 502
        _sector_cache["ts"] = time.time()
        _sector_cache["data"] = data
        return jsonify(data)


# ── action API ───────────────────────────────────────────────────────────────

@app.route("/api/approvals/<item_id>/<action>", methods=["POST"])
def api_resolve(item_id, action):
    if action not in ("approve", "reject"):
        return jsonify({"ok": False, "error": "bad action"}), 400
    ok = approvals.resolve(item_id, approved=(action == "approve"))
    if ok and action == "approve":
        try:
            approvals.apply_approved()
        except Exception:
            pass
    return jsonify({"ok": ok})


@app.route("/api/reset-stats", methods=["POST"])
def api_reset_stats():
    """Reset the cumulative trade stats: set a baseline so the Trade Record
    counts only trades from now on, and zero the bot's realized PnL totals +
    DD peak. Closed-trade HISTORY is preserved (History tab still shows it)."""
    from src import clock
    now = clock.ny_now().isoformat()
    db.update_state({
        "stats_reset_at": now,
        "realized_pnl_total": 0.0,
        "realized_pnl_today": 0.0,
        "peak_equity": risk_manager.budget_usd(),
    })
    return jsonify({"ok": True, "reset_at": now})


# ── settings: .env key management ─────────────────────────────────────────────
# (中文, English) — same rule as PARAM_SECTIONS: the row carries both and the
# panel asks for one.
SETTING_KEYS = {
    "WEB_PASSWORD": (
        "网页访问密码 — 设了之后，从手机/局域网打开面板要先登录(本机也是)。空=不需要密码(仅本机可用)。这是开放手机访问的前提。",
        "Panel access password — with one set, opening the panel from a phone or "
        "the LAN (and from this Mac) requires signing in. Empty = no password, "
        "this machine only. Phone access cannot be turned on without it."),
    "DEEPSEEK_API_KEY": (
        "DeepSeek API Key(可逗号分隔多个)— 所有 AI 分析(信号/情绪/退出/优化/入场验证)都用它。",
        "DeepSeek API key (comma-separate several) — every AI step runs on it: "
        "signals, sentiment, exits, tuning, entry validation."),
    "TAVILY_API_KEY": (
        "Tavily 新闻搜索 Key — 给 AI 提供实时新闻上下文。",
        "Tavily news-search key — gives the AI live news context."),
    "FINNHUB_API_KEY": (
        "Finnhub Key(免费 60次/分)— 按股票代码标注的新闻，"
        "且能查历史某一天(Tavily 只能查\"现在\")。需 FINNHUB_ENABLED=true。",
        "Finnhub key (free tier: 60/min) — news tagged by ticker, and queryable "
        "for a past date, which Tavily cannot do. Needs FINNHUB_ENABLED=true."),
    "TELEGRAM_TOKEN": (
        "Telegram Bot Token — 推送交易通知 + 审批卡片。",
        "Telegram bot token — pushes trade notifications and approval cards."),
    "TELEGRAM_CHAT_ID": (
        "Telegram Chat ID — 接收通知的聊天 ID。",
        "Telegram chat ID — the chat that receives them."),
}

# Not every .env value is a secret, and a password field for one that isn't is
# theatre that hides typos. Empty right now — SEC_EDGAR_USER_AGENT was the only
# member and that source is gone — but the distinction is load-bearing, so the
# mechanism stays rather than being rebuilt the next time a plain key appears.
_PLAIN_KEYS: set[str] = set()

# Booleans the panel can flip. Kept apart from SETTING_KEYS because these are
# switches, not secrets: they render as toggles, they are never masked, and the
# write path only ever puts "true"/"false" in .env.
SETTING_TOGGLES = {
    "FINNHUB_ENABLED": "Finnhub 新闻源 — 按代码标注、可查历史某一天，是回测新闻策略的前提。需要先填 FINNHUB_API_KEY。",
    "MOO_NOTICES_ENABLED": "申报 + 分析师动作 — 由 OpenD 转发的 SEC 申报和评级变动，不直连任何监管机构，也不需要注册。注意：只知道「发了 8-K」，不知道内容；日期精确到天，没有时分。",
    "FINBERT_ENABLED": "FinBERT 本地情绪打分 — 训练语料早于任何测试窗口，所以没有后见之明。要先在下面下载模型(约 120 MB)，否则开了也不会被调用。",
}


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _read_env() -> dict:
    out = {}
    try:
        for line in ENV_FILE.read_text().splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                out[k.strip()] = v.split("  #")[0].strip()
    except Exception:
        pass
    return out


def _mask(v: str) -> str:
    """Enough to recognise the key, not enough to use it.

    Keeps the head as well as the tail, because the head is what tells two
    keys apart at a glance (sk-ant… vs sk-proj…) and the tail is what you
    check against the provider's dashboard. Short values get no head — with
    fewer than twelve characters, showing four each end leaves almost nothing
    hidden.
    """
    if not v:
        return ""
    if len(v) <= 8:
        return "•" * len(v)
    head = v[:4] if len(v) >= 12 else ""
    return f"{head}{'•' * max(4, min(len(v) - len(head) - 4, 16))}{v[-4:]}"


def _write_env_key(key: str, value: str) -> None:
    """Persist a setting. The NAME is historical — this no longer picks .env.

    It wrote .env unconditionally. Parameters moved to config/parameters.json,
    which config._load_parameters overlays onto the environment AFTER
    load_dotenv and overwrites, so five of the panel's controls — the strategy
    mode selector and every news/FinBERT toggle — wrote to the losing file,
    reported success, told the user to restart, and changed nothing on
    restart. runtime_config.write_setting routes by which file owns the key.
    """
    from src import runtime_config
    runtime_config.write_setting(key, value, source="web-panel")


# ── first-run setup ──────────────────────────────────────────────────────────
# A packaged install starts with nothing: no .env, no OpenD, no AI key. Rather
# than drop the user on a dashboard full of red, / redirects to /setup until
# every REQUIRED step passes. Steps are checked live (not a "done" flag), so a
# later breakage — OpenD quit, key revoked — surfaces the wizard again instead
# of failing silently mid-session.

def _opend_reachable(host: str, port: int, timeout: float = 0.4) -> bool:
    """TCP connect probe. Deliberately not a moomoo API call: we only need to
    know the gateway is listening, and a full SDK handshake here would add
    seconds to every page load."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def setup_state() -> dict:
    """Per-step readiness. `complete` is True only when every required step is."""
    env = _read_env()

    def val(k: str, default: str = "") -> str:
        # .env wins; fall back to the process env so an externally-configured
        # deployment (docker/launchd with real env vars) isn't forced through
        # the wizard just because it keeps no .env file.
        return (env.get(k) or os.getenv(k, "") or default).strip()

    host = val("MOO_HOST", "127.0.0.1")
    try:
        port = int(val("MOO_PORT", "11111") or 11111)
    except ValueError:
        port = 11111

    # One provider (ai.PROVIDERS). Named from there rather than from
    # AI_PROVIDER, so the wizard cannot ask for a key belonging to something
    # the system would not use — which is what the branch removed here did.
    provider = ai.PROVIDERS[0]
    ai_key = val("DEEPSEEK_API_KEYS") or val("DEEPSEEK_API_KEY")

    opend_ok = _opend_reachable(host, port)
    steps = [
        {"id": "opend", "required": True, "ok": opend_ok,
         "title": "安装并登录 OpenD", "title_en": "Install and sign in to OpenD",
         "detail": f"{host}:{port} " + ("已连接" if opend_ok else "未监听"),
         "detail_en": f"{host}:{port} " + ("reachable" if opend_ok else "not listening")},
        {"id": "ai", "required": True, "ok": bool(ai_key),
         "title": f"填写 {provider.upper()} API Key",
         "title_en": f"Add your {provider.upper()} API key",
         "detail": "AI 负责信号验证/情绪/智能退出" if not ai_key else "已设置",
         "detail_en": "Powers signal validation, sentiment and smart exits"
                      if not ai_key else "set"},
        # Optional steps still appear in the wizard (so the user can do them
        # now rather than discover them later) but never block entry.
        {"id": "telegram", "required": False,
         "ok": bool(val("TELEGRAM_TOKEN") and val("TELEGRAM_CHAT_ID")),
         "title": "Telegram 通知（可选）", "title_en": "Telegram alerts (optional)",
         "detail": "交易通知 + 审批卡片", "detail_en": "Trade alerts + approval cards"},
        {"id": "tavily", "required": False, "ok": bool(val("TAVILY_API_KEY")),
         "title": "Tavily 新闻搜索（可选）", "title_en": "Tavily news search (optional)",
         "detail": "给 AI 提供实时新闻上下文",
         "detail_en": "Gives the AI live news context"},
    ]
    return {"complete": all(s["ok"] for s in steps if s["required"]),
            "steps": steps,
            "trade_env": val("MOO_TRADE_ENV", "SIMULATE").upper()}


@app.route("/api/setup/status")
def api_setup_status():
    return jsonify(setup_state())


@app.route("/api/settings")
def api_settings():
    env = _read_env()
    en = _lang() == "en"
    keys = [{"key": k, "desc": d[1] if en else d[0],
             "masked": (env.get(k, "") if k in _PLAIN_KEYS else _mask(env.get(k, ""))),
             "secret": k not in _PLAIN_KEYS,
             "set": bool(env.get(k))}
            for k, d in SETTING_KEYS.items()]
    return jsonify({"keys": keys})


# ── strategy mode: which layer selects the trades ────────────────────────────
# Deliberately NOT one of the toggles above. A toggle says "add this feature";
# this is a choice between two mutually exclusive strategies, and presenting it
# as a switch labelled with one of its options is what made the old
# NEWS_DRIVEN_ENABLED read as an addition rather than a replacement.
STRATEGY_MODE_INFO = {
    "technical": {
        "label": "技术指标模式", "label_en": "Technical",
        "desc": "指标评分选股和定仓位，新闻/AI 只做注释和否决。"
                "回测描述的就是这个模式 —— 回测引擎故意不跑 LLM。",
        "desc_en": "Indicators score the names and size the positions; news and "
                   "the AI only annotate and veto. This is the mode the backtest "
                   "describes — the engine deliberately runs no LLM.",
    },
    "news": {
        "label": "新闻主导模式", "label_en": "News-driven",
        "desc": "AI 的新闻读数选股和定仓位，指标评分降级为预筛(硬地板 50)，"
                "收盘前平掉全部持仓。没有任何回测描述这个模式 —— 实盘结果本身就是实验。",
        "desc_en": "The AI's read of the news picks the names and sizes the "
                   "positions; the indicator score drops to a pre-filter (hard "
                   "floor 50), and everything is flattened before the close. NO "
                   "backtest describes this mode — the live results are the "
                   "experiment.",
    },
}


@app.route("/api/strategy-mode")
def api_strategy_mode():
    """What the trading worker is running, and what a restart would change it to.

    `mode` used to be settings.strategy_mode — THIS process's frozen snapshot,
    and this process is the web server, not the worker. `pending` used to be
    the .env line, and STRATEGY_MODE moved to config/parameters.json. So the
    banner compared a file that no longer decided anything against a snapshot
    of the wrong process: restarting the trading loop could not clear it, and
    restarting the web server cleared it whether or not the worker had changed.

    Both sides now come from the thing they claim to describe. The worker
    publishes its effective mode at startup (src/main.py); `pending` is the
    effective configured value, read through the same overlay the worker will
    read at its next start.
    """
    from src import db as _db
    st = _db.get_state()
    running = (st.get("worker_strategy_mode") or "").strip().lower() or None
    configured = (os.environ.get("STRATEGY_MODE") or "").strip().lower() \
        or settings.strategy_mode

    return jsonify({
        # None when no worker has ever published one — the panel must say
        # "not running", not silently show the configured value as if it were.
        "mode": running,
        "pending": configured,
        "worker_started_at": st.get("worker_started_at"),
        "trade_env": (settings.moo_trade_env or "").upper(),
        "options": [{"id": k, **v} for k, v in STRATEGY_MODE_INFO.items()],
    })


@app.route("/api/strategy-mode", methods=["POST"])
def api_set_strategy_mode():
    mode = str((request.json or {}).get("mode", "")).strip().lower()
    if mode not in STRATEGY_MODE_INFO:
        return jsonify({"ok": False, "error": "unknown mode"}), 400
    try:
        _write_env_key("STRATEGY_MODE", mode)
        # NEWS_DRIVEN_ENABLED is NOT written alongside. config reads it only
        # when STRATEGY_MODE is absent, which it never is — so the pair could
        # only ever be a second copy that disagrees, and settings.
        # news_driven_enabled is derived from the mode anyway.
        try:
            from src import preflight
            preflight.invalidate()
        except Exception:
            pass
        if mode == "news":
            live = (settings.moo_trade_env or "").upper() != "SIMULATE"
            note = ("⚠️ 已切到新闻主导模式，而当前是**实盘**。这个模式没有因子研究背书，"
                    "实盘结果本身就是实验 —— 建议先切模拟盘。重启 bot 后生效"
                    if live else
                    "已切到新闻主导模式 —— 当前是**模拟盘**，下的是虚拟单。"
                    "跑几周后用 `news_factor_study --live` 看有没有 edge。重启 bot 后生效")
        else:
            note = "已切回技术指标模式 —— 指标评分选股，新闻只做注释。重启 bot 后生效"
        return jsonify({"ok": True, "note": note})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500



# ── parameter console ────────────────────────────────────────────────────────
# Every strategy parameter in config/parameters.json, on one page.
#
# The honest part is the hot/cold split. runtime_config._param() re-reads the
# file on every call, so the twelve parameters with an accessor there take
# effect on the worker's NEXT SCAN with nothing restarted. The other
# thirty-eight are read through `settings`, which is evaluated once at process
# start — writing them changes the file and nothing else until the worker is
# restarted. Presenting all fifty as "saved, done" would be the same lie the
# strategy-mode selector told for a day.
# Grouped by WHICH STRATEGY THE PARAMETER BELONGS TO, because that is the
# question the page has to answer: change this, and does it affect what I am
# running? A parameter under 新闻指标模式 does nothing while STRATEGY_MODE is
# technical, and saying so is the whole point of the split.
#
# The membership is read off the code, not chosen. news-only means every
# reader sits inside `if news_driven.enabled():` — that is where FINBERT_ENABLED
# lands, because main.py's finbert_crosscheck call is inside that branch.
# technical-only means the readers sit under `not news_driven.enabled()`.
# Everything else applies to whatever the selected layer picked.
def _lang() -> str:
    """Which language the panel is asking for. The selector is client-side, so
    it travels as ?lang=; anything else falls back to Chinese, the language the
    page was written in."""
    return "en" if (request.args.get("lang") or "").lower() == "en" else "zh"


# Every row carries BOTH languages: (KEY, 中文, English). Section and group
# names are (中文, English) pairs. The panel asks for one with ?lang= and gets
# that one — it used to get Chinese whatever the language selector said, so the
# English UI had an English frame around a Chinese page.
PARAM_SECTIONS = [
 ("mode", ("策略模式 — 由哪一层选股",
           "Strategy mode — which layer picks the stocks"), [
   (("策略模式", "Strategy mode"), [
     ("STRATEGY_MODE",
      "technical=指标评分选股，新闻只做注释；news=新闻主导选股。"
      "这是这一页唯一会<b>换掉策略</b>的设置，下面两组各自只在对应模式下生效。",
      "technical = indicators score and pick; news only annotates. "
      "news = the news read picks. This is the one setting on this page that "
      "<b>swaps the strategy</b>; each of the two sections below is live only "
      "under its own mode."),
   ]),
 ]),
 ("shared", ("共用参数 — 两个模式都生效",
             "Shared — live under both modes"), [
   (("入场 Entry", "Entry"), [
     ("ENTRY_SCORE_THRESHOLD",
      "指标总分门槛。新闻模式下它仍是底线，再加上 NEWS_DRIVEN_THRESHOLD_DELTA。",
      "Indicator score a name must clear. Still the floor under news mode, "
      "plus NEWS_DRIVEN_THRESHOLD_DELTA."),
     ("TIMEFRAME",
      "K 线周期。改这个等于换一套指标周期，回测口径也跟着变。",
      "Candle period. Changing it changes every indicator's period, and the "
      "basis the backtest measures on."),
     ("MAX_GAP_PCT",
      "跳空上限 %。开盘跳空超过它就不追。",
      "Gap ceiling, %. A name that opens gapped wider than this is not chased."),
     ("BREADTH_BLOCKING",
      "市场广度不健康时是否阻止开新仓（false=仅提示）。",
      "Whether unhealthy market breadth blocks new entries (false = warn only)."),
     ("SMART_REGIME_ENABLED",
      "启用带滞回的市场状态判定（而非裸标签）。",
      "Regime detection with hysteresis, rather than a bare label."),
     ("REGIME_BULL_MULT",
      "牛市时的仓位放大系数。",
      "Position multiplier while the regime reads bull."),
     ("REGIME_VIX_CALM",
      "VIX 低于此值算「平静」。",
      "VIX below this counts as calm."),
   ]),
   (("出场 Exit", "Exit"), [
     ("SL_ATR_MULT",
      "止损距离 = 这个倍数 × ATR（Wilder）。",
      "Stop distance = this multiple × ATR (Wilder)."),
     ("TP_ATR_MULT",
      "止盈距离 = 这个倍数 × ATR。",
      "Target distance = this multiple × ATR."),
     ("MAX_HOLD_DAYS",
      "最长持仓天数，到期无条件平仓。",
      "Longest hold in days; the position is closed when it expires, "
      "unconditionally."),
     ("USE_SCALE_OUT",
      "分批止盈。关着的时候 TP1_R / TP2_R 不起作用。",
      "Scale out in tranches. TP1_R / TP2_R do nothing while this is off."),
     ("TP1_R", "第一批止盈的 R 倍数。", "R multiple for the first tranche."),
     ("TP2_R", "第二批止盈的 R 倍数。", "R multiple for the second tranche."),
     ("USE_BREAKEVEN_STOP",
      "到 +1R 后把止损上移到成本价。",
      "Move the stop to entry once the trade reaches +1R."),
     ("REAL_USE_SOFT_EXITS",
      "实盘用软出场（本地循环）而非券商挂单。",
      "In live trading, exit from the local loop rather than through a resting "
      "broker order."),
   ]),
   (("风控与资金 Risk & Capital", "Risk & capital"), [
     ("__BUDGET__",
      "分配给交易的本金。改了下次扫描生效（免重启）；仓位与风控都由它派生，"
      "并且会同步重锚回撤高水位 —— 不重锚的话回撤熔断会失效。",
      "Capital allocated to trading. Effective next scan, no restart. Position "
      "sizes and every risk limit derive from it, and changing it re-anchors "
      "the drawdown high-water mark — without that re-anchor the drawdown "
      "breaker stops working in both directions."),
     ("RISK_PER_TRADE",
      "单笔风险占预算比例。仓位大小由它和止损距离反推。",
      "Risk per trade as a fraction of the budget. Position size is derived "
      "from it and the stop distance."),
     ("MAX_POSITION_PCT",
      "单个标的最大仓位占预算比例。",
      "Largest share of the budget any one name may hold."),
     ("MAX_POSITIONS",
      "同时最多持有几个标的。",
      "How many names may be held at once."),
     ("ACCOUNT_USD",
      "账户资金基数（回测与派生上限用）。",
      "Account size used by the backtest and by derived caps."),
     ("DAILY_DRAWDOWN_STOP",
      "当日回撤达到此比例停止开新仓。",
      "Stop opening new positions once the day is down this much."),
     ("DD_HALT_PCT",
      "总回撤达到此百分比触发 halt。",
      "Total drawdown that triggers a halt, in percent."),
     ("DD_SIZE_CUT_PCT",
      "总回撤达到此百分比开始减半仓位。",
      "Total drawdown at which position sizes are halved, in percent."),
     ("PARAMS_FROZEN",
      "参数冻结：挡住一切自动化写入。你在这一页的修改不受它限制。",
      "Parameter freeze: blocks every automated write. Your own edits on this "
      "page are not affected."),
     ("AUTO_APPLY_PARAMS",
      "允许优化器自动应用参数。",
      "Let the optimizer apply parameters on its own."),
     ("PARAM_TUNE_MODE",
      "<b>谁来发起回测调参</b>。"
      "<b>manual 手动</b>=没人自动动参数；每周的 AI 优化器、half-Kelly、"
      "网格搜索、月度 lever 复核全部停手，改由你点下面的按钮跑同一套流程，"
      "跑完当场逐条确认。<b>weekly 每周</b>=周一自动跑，结果进审批队列等你批。<br>"
      "默认 manual —— 建议是进队列的，一个会自己变长的队列只会让人养成"
      "「清掉」而不是「读完」的习惯。",
      "<b>Who starts a tuning run</b>. "
      "<b>manual</b> = nothing tunes on its own. The weekly AI optimizer, "
      "half-Kelly, the grid sweep and the monthly lever recheck all stand "
      "down; you run the same chain from the button below and confirm each "
      "change on the spot. <b>weekly</b> = it runs itself on Monday and the "
      "survivors wait in the approval queue.<br>"
      "manual is the default because the proposals go to a queue, and a queue "
      "that fills itself between visits trains you to clear it rather than "
      "read it."),
     ("AUTO_BUDGET_ENABLED",
      "自动复利调整预算。",
      "Compound the budget automatically."),
   ]),
   (("选股池 Universe", "Universe"), [
     ("SCAN_INTERVAL_MIN", "扫描间隔（分钟）。", "Scan interval, in minutes."),
     ("DYNAMIC_UNIVERSE_ENABLED",
      "按规则定期重建观察池（关=用固定 watchlist）。",
      "Rebuild the watch pool on a schedule (off = use the fixed watchlist)."),
     ("UNIVERSE_TOP_N",
      "观察池保留前 N 个标的。",
      "Keep the top N names in the pool."),
     ("UNIVERSE_REFRESH_FREQ", "重建频率。", "How often the pool is rebuilt."),
     ("UNIVERSE_ETF_SLOTS",
      "观察池里留给 ETF 的名额。",
      "Slots in the pool reserved for ETFs."),
     ("UNIVERSE_EXIT_RANK",
      "跌出这个排名才移出观察池（滞回，避免反复进出）。",
      "A name leaves the pool only after falling past this rank — hysteresis, "
      "so names don't churn in and out."),
     ("SIGNAL_WATCHLIST",
      "盯盘信号台的额外关注列表。",
      "Extra names watched by the signal desk."),
   ]),
   (("持仓保护 Gap sentinel", "Gap sentinel"), [
     ("GAP_SENTINEL_ENABLED",
      "持仓跳空哨兵。",
      "Overnight gap sentinel for open positions."),
     ("GAP_SENTINEL_AI",
      "哨兵调用 AI 判断跳空原因。",
      "Let the sentinel ask the AI why a name gapped."),
     ("GAP_SENTINEL_AI_INTRADAY",
      "盘中也跑哨兵。",
      "Run the sentinel intraday as well."),
     ("GAP_SENTINEL_AI_MIN_CONF",
      "哨兵采信 AI 结论的最低置信度。",
      "Lowest AI confidence the sentinel will act on."),
     ("GAP_EXIT_EARNINGS_DAYS",
      "财报前几天开始规避跳空风险。",
      "How many days before earnings to start avoiding gap risk."),
   ]),
   (("AI 引擎 AI Engine", "AI engine"), [
     ("AI_PROVIDER",
      "AI 供应商。当前只接 DeepSeek。",
      "AI provider. DeepSeek is the only one wired up."),
     ("__AI_MODEL__",
      "所选引擎当前可用的模型（实时从 API 拉取）。改了下次扫描即生效，无需重启。",
      "Models the selected provider currently offers, fetched live from its "
      "API. Effective next scan, no restart."),
     ("AI_VETO_BLOCKING",
      "AI 否决是否真的挡下单。false=咨询发生在下单之后，改不了决策。",
      "Whether an AI veto actually blocks the order. false = the consult "
      "happens after the order and cannot change the decision."),
   ]),
   (("新闻源 News sources", "News sources"), [
     ("FINNHUB_ENABLED",
      "Finnhub 新闻源 — 按代码标注、可查历史某一天。需先填 FINNHUB_API_KEY。",
      "Finnhub news — tagged by ticker, and queryable for a past date. Needs "
      "FINNHUB_API_KEY."),
     ("MOO_NOTICES_ENABLED",
      "moomoo 转发的 SEC 申报与评级变动。只知道「发了 8-K」，不知道内容。",
      "SEC filings and rating changes relayed by moomoo. It tells you an 8-K "
      "was filed, not what is in it."),
   ]),
   (("期权信号 Options", "Options"), [
     ("OPTIONS_STATS_ENABLED",
      "启用期权统计因子。",
      "Enable the options-statistics factor."),
     ("OPTIONS_STATS_SIZING",
      "让期权因子参与仓位大小。",
      "Let the options factor affect position size."),
     ("OPTIONS_STATS_MIN_RVOL",
      "期权相对成交量下限。",
      "Minimum relative options volume."),
     ("OPTIONS_STATS_MAX_MULT",
      "期权因子对仓位的最大放大倍数。",
      "Most the options factor may scale a position up by."),
   ]),
 ]),
 ("technical", ("技术指标模式专属", "Technical mode only"), [
   (("情绪打分 Sentiment", "Sentiment"), [
     ("SENTIMENT_SCORING_ENABLED",
      "对候选标的做 AI 情绪打分。<b>只在技术指标模式下调用</b> —— "
      "新闻模式有自己的新闻打分，不会再跑这个。",
      "Score candidates for sentiment with the AI. <b>Called only under "
      "technical mode</b> — news mode has its own news score and never runs "
      "this one."),
     ("SENTIMENT_SIZING",
      "让情绪分参与仓位大小（最多放大 1.25×）。",
      "Let the sentiment score affect position size (up to 1.25×)."),
     ("SENTIMENT_BUDGET",
      "每轮扫描最多几次情绪 AI 调用。",
      "Most sentiment AI calls allowed per scan."),
   ]),
 ]),
 ("news", ("新闻指标模式专属", "News mode only"), [
   (("入场门槛 Entry gate", "Entry gate"), [
     ("NEWS_DRIVEN_MIN_SCORE",
      "新闻分低于它就不入场（0–100）。",
      "No entry below this news score (0–100)."),
     ("NEWS_DRIVEN_REQUIRE_CATALYST",
      "是否必须有明确催化剂事件。",
      "Whether an explicit catalyst event is required."),
     ("NEWS_DRIVEN_THRESHOLD_DELTA",
      "在指标门槛上的增减。负数=新闻模式对指标分更宽松。",
      "Added to the indicator threshold. Negative means news mode is more "
      "forgiving of the indicator score."),
     ("NEWS_DRIVEN_MAX_MULT",
      "新闻分很高时，仓位最多放大到几倍。",
      "Most a very high news score may scale a position up by."),
     ("NEWS_DRIVEN_BUDGET",
      "每轮扫描最多几次新闻 AI 调用。",
      "Most news AI calls allowed per scan."),
   ]),
   (("日内平仓 Intraday flatten", "Intraday flatten"), [
     ("NEWS_DRIVEN_EOD_FLATTEN",
      "收盘前平掉新闻模式开的仓（不留隔夜）。",
      "Close news-mode positions before the bell — nothing held overnight."),
     ("NEWS_DRIVEN_FLATTEN_ET",
      "平仓时刻（美东时间 HH:MM）。",
      "When to flatten (US Eastern, HH:MM)."),
     ("NEWS_DRIVEN_MIN_HOLD_MIN",
      "最短持有分钟数，避免刚开就被平仓时刻扫掉。",
      "Shortest hold in minutes, so a fresh entry is not swept out by the "
      "flatten time."),
   ]),
   (("本地打分模型 FinBERT", "FinBERT (local model)"), [
     ("FINBERT_ENABLED",
      "本地 FinBERT 对 AI 读过的<b>同一批</b>标题给确定性的第二意见，"
      "两个分数一起记进成交记录。<b>仅供参考，不参与下单决策。</b>"
      "需先在下方下载模型（约 120 MB）。",
      "A local FinBERT gives a deterministic second opinion on the <b>same</b> "
      "headlines the AI read, and both scores are recorded with the trade. "
      "<b>Reference only — it takes no part in the order decision.</b> "
      "Download the model below first (~120 MB)."),
   ]),
 ]),
]

# Flattened for the code that only needs "key -> description".
PARAM_GROUPS = [(g, items) for _, _, groups in PARAM_SECTIONS
                for g, items in groups]


# A parameter can be alive in the code and inert in this configuration,
# because another parameter switches off the feature it belongs to. That is
# not a defect — but a row that looks identical to a live one is, since this
# page is what the owner reads to find out what the bot is doing.
#
# GEMINI_MODEL was the version of this that WAS a defect: a live-looking
# setting for a provider ai.PROVIDERS could not select. It is deleted. These
# are the honest ones, and they name the switch responsible.
PARAM_INERT_WHEN = {
    "TP1_R":                 ("USE_SCALE_OUT", "false"),
    "TP2_R":                 ("USE_SCALE_OUT", "false"),
    "AUTO_APPLY_PARAMS":     ("PARAMS_FROZEN", "true"),
    "AUTO_BUDGET_ENABLED":   ("PARAMS_FROZEN", "true"),
    "OPTIONS_STATS_SIZING":  ("OPTIONS_STATS_ENABLED", "false"),
    "OPTIONS_STATS_MIN_RVOL": ("OPTIONS_STATS_ENABLED", "false"),
    "OPTIONS_STATS_MAX_MULT": ("OPTIONS_STATS_SIZING", "false"),
    "GAP_SENTINEL_AI":       ("GAP_SENTINEL_ENABLED", "false"),
    "GAP_SENTINEL_AI_INTRADAY": ("GAP_SENTINEL_AI", "false"),
    "GAP_SENTINEL_AI_MIN_CONF": ("GAP_SENTINEL_AI", "false"),
    "REGIME_BULL_MULT":      ("SMART_REGIME_ENABLED", "false"),
    "REGIME_VIX_CALM":       ("SMART_REGIME_ENABLED", "false"),
    "UNIVERSE_TOP_N":        ("DYNAMIC_UNIVERSE_ENABLED", "false"),
    "UNIVERSE_ETF_SLOTS":    ("DYNAMIC_UNIVERSE_ENABLED", "false"),
    "UNIVERSE_EXIT_RANK":    ("DYNAMIC_UNIVERSE_ENABLED", "false"),
    "UNIVERSE_REFRESH_FREQ": ("DYNAMIC_UNIVERSE_ENABLED", "false"),
    "SENTIMENT_SIZING":      ("SENTIMENT_SCORING_ENABLED", "false"),
    "SENTIMENT_BUDGET":      ("SENTIMENT_SCORING_ENABLED", "false"),
    "NEWS_DRIVEN_FLATTEN_ET":   ("NEWS_DRIVEN_EOD_FLATTEN", "false"),
    "NEWS_DRIVEN_MIN_HOLD_MIN": ("NEWS_DRIVEN_EOD_FLATTEN", "false"),
}


def _param_rows(lang: str = "zh"):
    """The console's contents: sections → groups → rows, in `lang`.

    Two rows are not parameters and are marked `special`. The budget lives in
    db-state and must go through risk_manager.set_budget, which re-anchors the
    drawdown high-water mark — skipping that silently disables the breaker in
    both directions. The AI model is a db-state override over a list fetched
    live from the provider. Both were on the settings page; they belong with
    the numbers they interact with, but they cannot be written like the rest,
    so the save path handles them separately rather than pretending.
    """
    from src import runtime_config as rc, risk_manager, ai
    doc = rc._read_file() or {}
    vals = doc.get("params") or {}
    hot = set(rc._FILE_KEY.values())
    bounds = {rc._FILE_KEY[k]: v for k, v in rc.ALLOWED_PARAMS.items()
              if k in rc._FILE_KEY}
    described = {k for _, items in PARAM_GROUPS for k, _, _ in items}
    en = lang == "en"
    pick = (lambda pair: pair[1] if en else pair[0])

    def row(key, desc_zh, desc_en):
        desc = desc_en if en else desc_zh
        if key == "__BUDGET__":
            return {"key": key, "value": f"{risk_manager.budget_usd():.0f}",
                    "desc": desc, "kind": "num", "hot": True, "inert": None,
                    "band": None, "special": "budget",
                    "label": "Budget" if en else "预算 Budget"}
        if key == "__AI_MODEL__":
            return {"key": key, "value": ai.active_model(), "desc": desc,
                    "kind": "choice", "hot": True, "inert": None, "band": None,
                    "special": "ai_model",
                    "label": "Model" if en else "模型 Model", "options": []}
        raw = str(vals.get(key, ""))
        low = raw.strip().lower()
        if key == "STRATEGY_MODE":
            kind, opts = "choice", list(STRATEGY_MODE_INFO)
        elif key == "PARAM_TUNE_MODE":
            from src import param_tune
            kind, opts = "choice", list(param_tune.MODES)
        elif low in ("true", "false"):
            kind, opts = "bool", []
        else:
            try:
                float(raw); kind, opts = "num", []
            except ValueError:
                kind, opts = "text", []
        b = bounds.get(key)
        inert = None
        gate = PARAM_INERT_WHEN.get(key)
        if gate and str(vals.get(gate[0], "")).strip().lower() == gate[1]:
            inert = f"{gate[0]}={gate[1]}"
        if key == "REAL_USE_SOFT_EXITS" and \
                (settings.moo_trade_env or "").upper() == "SIMULATE":
            inert = "MOO_TRADE_ENV=SIMULATE"
        # `inert` is a KEY=VALUE, the same in both languages on purpose: it
        # names the switch responsible, and a translated switch name would not
        # be findable on this page.
        return {"key": key, "value": raw, "desc": desc, "kind": kind,
                "hot": key in hot, "inert": inert, "options": opts,
                "band": [b[0], b[1]] if b else None, "special": None,
                "label": key}

    mode = str(vals.get("STRATEGY_MODE", "technical")).strip().lower()
    out = []
    for sid, label, groups in PARAM_SECTIONS:
        gs = []
        for gname, items in groups:
            rows = [row(k, dz, de) for k, dz, de in items
                    if k.startswith("__") or k in vals]
            if rows:
                gs.append({"name": pick(gname), "params": rows})
        if gs:
            out.append({"id": sid, "label": pick(label), "groups": gs,
                        # A whole section can be inert: everything under
                        # 新闻指标模式 does nothing while the mode is technical.
                        # Greyed rather than hidden — hiding it is how a setting
                        # becomes something nobody remembers is there.
                        "inert": (sid in ("technical", "news") and sid != mode)})

    extra = [row(k, "", "") for k in sorted(vals) if k not in described]
    if extra:
        out.append({"id": "other",
                    "label": ("Other (keys this page has not described yet)"
                              if en else "其它 Other（这一页尚未描述的键）"),
                    "groups": [{"name": "", "params": extra}], "inert": False})
    return out


@app.route("/api/params")
def api_params():
    from src import db as _db
    # The model list comes from the same cache /api/ai-models serves, and
    # falls back to the static one rather than making the page wait: a
    # dropdown that blocks on a provider's API is a page that fails to open
    # when the provider is down.
    import time as _t
    prov = ai.active_provider()
    cached = _AI_MODELS_CACHE.get(prov)
    models = (cached[1] if cached and (_t.time() - cached[0] < _AI_MODELS_TTL)
              else list(ai._FALLBACK_MODELS.get(prov, [])))
    cur = ai.active_model()
    if cur and cur not in models:
        models = [cur] + models
    return jsonify({
        "sections": _param_rows(_lang()),
        "ai_models": models,
        "worker_running": bool(_db.get_state().get("worker_strategy_mode")),
    })


@app.route("/api/params", methods=["POST"])
def api_save_params():
    """Save the edited parameters, and restart the worker if any need it.

    "Save and it is in use" is only true for the hot twelve on its own. For
    the rest the file changes and the running worker goes on with the values
    it read at start — so this restarts it when a cold parameter moved, and
    says which ones forced that.
    """
    from src import runtime_config as rc
    changes = (request.json or {}).get("params") or {}
    if not isinstance(changes, dict):
        return jsonify({"ok": False, "error": "bad payload"}), 400

    current = (rc._read_file() or {}).get("params") or {}
    known = set(current)
    hot = set(rc._FILE_KEY.values())

    applied, cold, rejected = [], [], {}

    # The two rows that are not parameters. They were on the settings page and
    # moved here to sit with the numbers they interact with, but neither can be
    # written like the rest, so they are handled rather than pretended about.
    budget = changes.pop("__BUDGET__", None)
    if budget is not None:
        try:
            from src import risk_manager
            # Through set_budget, never by writing budget_usd: it re-anchors
            # the drawdown high-water mark, and skipping that disables the
            # breaker in both directions — upward it pins drawdown at 0%
            # forever, downward it reads a phantom drawdown and halts.
            risk_manager.set_budget(float(budget), source="param-console")
            applied.append("预算 Budget")
        except Exception as e:
            rejected["__BUDGET__"] = str(e)[:200]

    ai_model = changes.pop("__AI_MODEL__", None)
    if ai_model is not None:
        try:
            db.update_state({"ai_model": str(ai_model).strip()})
            applied.append("AI 模型")      # db-state, read per call — hot
        except Exception as e:
            rejected["__AI_MODEL__"] = str(e)[:200]

    for key, val in changes.items():
        if key not in known:
            rejected[key] = "not a known parameter"
            continue
        new = str(val).strip()
        if new == str(current.get(key, "")).strip():
            continue
        if new == "":
            rejected[key] = "empty — delete it from the file by hand if intended"
            continue
        try:
            rc.write_setting(key, new, source="param-console")
        except Exception as e:
            rejected[key] = str(e)[:200]
            continue
        applied.append(key)
        if key not in hot:
            cold.append(key)

    # Switching the strategy invalidates the cached preflight verdicts, which
    # were computed about the other mode's requirements.
    if "STRATEGY_MODE" in applied:
        try:
            from src import preflight
            preflight.invalidate()
        except Exception:
            pass

    restarted = False
    error = None
    if cold:
        try:
            from src import start_protocol
            start_protocol.stop("web")
            start_protocol.start("web")
            restarted = True
        except Exception as e:
            error = (f"参数已保存，但交易循环没能重启({type(e).__name__}: {e})。"
                     f"这些参数要重启后才生效：{', '.join(cold)}")

    return jsonify({"ok": not rejected and not error, "applied": applied,
                    "needed_restart": cold, "restarted": restarted,
                    "rejected": rejected, "error": error})


@app.route("/api/settings/key/reveal", methods=["POST"])
def api_reveal_key():
    """Hand the panel one credential in the clear, for the eye toggle.

    POST, not GET: a GET would land in the browser history and in any proxy
    log between here and the tab. The value is returned and never logged —
    log_redact would scrub it from our own logs, but the point is not to have
    written it. Auth is the panel's, which is the same gate that already
    protects changing the trade environment.
    """
    k = (request.json or {}).get("key")
    if k not in SETTING_KEYS:
        return jsonify({"ok": False, "error": "unknown key"}), 400
    return jsonify({"ok": True, "key": k, "value": _read_env().get(k, "")})


@app.route("/api/settings/toggles")
def api_settings_toggles():
    """The .env switches the panel can flip. Values are what the file says, i.e.
    what will be in force after a restart — same contract as the key rows."""
    env = _read_env()
    return jsonify({"toggles": [{"key": k, "desc": d, "on": _truthy(env.get(k, ""))}
                                for k, d in SETTING_TOGGLES.items()]})


@app.route("/api/settings/toggle", methods=["POST"])
def api_set_toggle():
    body = request.json or {}
    k, on = body.get("key"), bool(body.get("on"))
    if k not in SETTING_TOGGLES:
        return jsonify({"ok": False, "error": "unknown toggle"}), 400
    try:
        _write_env_key(k, "true" if on else "false")
        note = "已写入 .env — 重启 bot 后生效"
        # Turning news-driven mode on is a strategy swap, so say which account
        # is about to run it. The switch itself is not the dangerous part —
        # doing it on the REAL account with no factor study behind the mode is,
        # and that combination is easy to arrive at without noticing.
        if k == "NEWS_DRIVEN_ENABLED" and on:
            if (settings.moo_trade_env or "").upper() == "SIMULATE":
                note = ("已开启新闻主导模式 —— 当前是**模拟盘**，下的是虚拟单。"
                        "跑几周后用 `news_factor_study --live` 看有没有 edge，"
                        "再决定要不要切实盘。重启 bot 后生效")
            else:
                note = ("⚠️ 已开启新闻主导模式，而当前是**实盘**。这个模式没有因子"
                        "研究背书，实盘结果本身就是实验 —— 建议先切模拟盘跑几周。"
                        "重启 bot 后生效")
        try:
            from src import preflight
            preflight.invalidate()
        except Exception:
            pass
        return jsonify({"ok": True, "note": note})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── FinBERT local model: status / consented download / removal ───────────────
# The download is a few hundred MB of the user's disk, so it happens ONLY on an
# explicit POST from the confirm dialog. GET is always safe and never downloads.
_finbert_job: dict = {"running": False, "file": "", "done": 0, "total": 0,
                      "ok": None, "detail": ""}


@app.route("/api/finbert")
def api_finbert_status():
    from src import news_score_local as nsl
    st = nsl.status()
    st["job"] = dict(_finbert_job)
    return jsonify(st)


@app.route("/api/finbert/download", methods=["POST"])
def api_finbert_download():
    """Explicit consent to spend disk. Runs in a thread so the request returns
    immediately and the panel can poll progress."""
    from src import news_score_local as nsl
    if _finbert_job["running"]:
        return jsonify({"ok": True, "note": "download already running"})
    if nsl.is_downloaded():
        return jsonify({"ok": True, "note": "already downloaded"})

    def _progress(name, done, total):
        _finbert_job.update(file=name, done=done, total=total)

    def _run():
        _finbert_job.update(running=True, ok=None, detail="", done=0, total=0)
        try:
            ok, detail = nsl.ensure_model(progress=_progress)
        except Exception as e:                                  # noqa: BLE001
            ok, detail = False, str(e)
        _finbert_job.update(running=False, ok=ok, detail=detail)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "note": "download started",
                    "path": str(nsl.model_home())})


@app.route("/api/finbert/remove", methods=["POST"])
def api_finbert_remove():
    from src import news_score_local as nsl
    ok, detail = nsl.remove_model()
    return jsonify({"ok": ok, "note": detail})


@app.route("/api/preflight")
def api_preflight():
    """Startup readiness — see src/preflight.py for why this is not a gate.

    `can_start` is False only on a real blocker (no broker). Everything else is
    reported as degraded WITH the behaviour it disables, so a user can decide
    knowingly instead of discovering four days later in a log file.
    """
    from src import preflight
    fresh = request.args.get("fresh") in ("1", "true", "yes")
    try:
        return jsonify(preflight.run_all(use_cache=not fresh))
    except Exception as e:
        return jsonify({"can_start": True, "error": str(e), "checks": [],
                        "summary": f"预检失败（不阻止启动）: {e}",
                        "summary_en": f"Preflight failed (not blocking): {e}"}), 200


@app.route("/api/preflight/<check_id>", methods=["POST"])
def api_preflight_recheck(check_id):
    """Re-run ONE check, bypassing the cache — this is the 'I just pasted a new
    key, try again' path, so it must never serve the stale result it was
    written to replace."""
    from src import preflight
    if check_id not in preflight.ORDER:
        return jsonify({"ok": False, "error": "unknown check"}), 400
    try:
        preflight.invalidate(check_id)
        return jsonify(preflight.run_one(check_id, use_cache=False).as_dict())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/settings/key", methods=["POST"])
def api_set_key():
    body = request.json or {}
    k, v = body.get("key"), body.get("value", "")
    if k not in SETTING_KEYS:
        return jsonify({"ok": False, "error": "unknown key"}), 400
    try:
        _write_env_key(k, v.strip())
        # A saved key makes every cached preflight probe of it stale, and the
        # very next thing the user does is press retry. Serving the pre-edit
        # result there would make the fix look like it failed.
        try:
            from src import preflight
            preflight.invalidate()
        except Exception:
            pass
        if k == "WEB_PASSWORD":
            note = ("访问密码已更新 — 下次打开面板需要登录(本机也是)。"
                    if v.strip() else "已清除访问密码 — 面板将不再需要登录。")
        else:
            note = "已写入 .env — 重启 bot 后生效"
        return jsonify({"ok": True, "note": note})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/budget", methods=["POST"])
def api_budget():
    try:
        val = float(request.json.get("value"))
        assert val > 0
    except Exception:
        return jsonify({"ok": False, "error": "bad value"}), 400
    # Re-anchoring the DD breaker's peak to the new capital base is NOT optional
    # — see risk_manager.set_budget for both ways it fails when skipped. That
    # logic used to be inlined right here, which is exactly why a later writer
    # elsewhere missed it. One path now, for every caller.
    from src import risk_manager
    try:
        res = risk_manager.set_budget(val, source="web-panel")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "budget": val, "peak_equity": res["peak_equity"],
                    "note": "预算已更新，回撤峰值已重锚到新资金基数"})


# ── Auto-compounding budget: "money making more money" ───────────────────────
# Runtime toggle + arm/disarm + manual recompute. While armed, a daily EOD job
# grows/shrinks the deployable budget off realized profit (high-water + give-
# back), bounded by seed-relative floor/ceil + live equity, Telegram-notified.
# The DD breaker is unaffected (equity stays anchored to the frozen seed).
@app.route("/api/auto-budget", methods=["GET", "POST"])
def api_auto_budget():
    from src import auto_budget
    if request.method == "GET":
        return jsonify({"ok": True, **auto_budget.status()})
    body = request.json or {}
    action = (body.get("action") or "").lower()
    # While the param freeze is on, refuse anything that turns compounding back
    # ON, and say so. auto_budget.enabled() already ignores the db toggle under
    # the freeze, so letting the write succeed would show the panel a state the
    # bot does not act on — worse than an error.
    from src import runtime_config
    _turning_on = action == "arm" or (
        "enabled" in body and not action and bool(body["enabled"]))
    if _turning_on and runtime_config.frozen():
        return jsonify({"ok": False, "error": (
            "参数冻结中 (PARAMS_FROZEN)，不能启用自动复利预算。"
            "先修好 sandbox↔backtest_v3 一致性再解冻。")}), 409
    try:
        if "enabled" in body and not action:
            db.update_state({"auto_budget_enabled": bool(body["enabled"])})
        elif action == "arm":
            seed = body.get("seed")
            auto_budget.arm(float(seed) if seed is not None else None)
            db.update_state({"auto_budget_enabled": True})
        elif action == "disarm":
            auto_budget.disarm()
        elif action == "recompute":
            res = auto_budget.recompute_and_apply()
            return jsonify({"ok": True, "result": res, **auto_budget.status()})
        else:
            return jsonify({"ok": False, "error": "unknown action"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, **auto_budget.status()})


# ── Bear-market cash-yield sweep toggle ──────────────────────────────────────
@app.route("/api/cash-yield", methods=["GET", "POST"])
def api_cash_yield():
    from src import cash_yield
    def _snap():
        st = db.get_state()
        return {"enabled": cash_yield.enabled(), "symbol": cash_yield.symbol(),
                "only_bear": settings.cash_yield_only_bear,
                "history": (st.get("cash_yield_history", []) or [])[-10:]}
    if request.method == "GET":
        return jsonify({"ok": True, **_snap()})
    body = request.json or {}
    if "enabled" in body:
        db.update_state({"cash_yield_enabled": bool(body["enabled"])})
        return jsonify({"ok": True, **_snap()})
    return jsonify({"ok": False, "error": "expected {enabled}"}), 400


# ── Inverse-ETF sleeve toggle (NOT VALIDATED — keep off until backtest passes) ─
@app.route("/api/inverse-sleeve", methods=["GET", "POST"])
def api_inverse_sleeve():
    from src import inverse_sleeve
    def _snap():
        st = db.get_state()
        return {"enabled": inverse_sleeve.enabled(), "symbol": inverse_sleeve.symbol(),
                "position": st.get("inverse_sleeve_position"),
                "history": (st.get("inverse_sleeve_history", []) or [])[-10:]}
    if request.method == "GET":
        return jsonify({"ok": True, **_snap()})
    body = request.json or {}
    if "enabled" in body:
        db.update_state({"inverse_sleeve_enabled": bool(body["enabled"])})
        return jsonify({"ok": True, **_snap()})
    return jsonify({"ok": False, "error": "expected {enabled}"}), 400


# ── AI engine: provider + live model dropdown ────────────────────────────────
# One provider (ai.PROVIDERS). The dropdown stays because the MODEL list is
# still a live fetch and still worth choosing from.
# The active provider + model are a RUNTIME db-state override (no restart): the
# running scheduler reads them per AI call (see src/ai.py). The model list is
# fetched LIVE from each provider so a newly released model is selectable without
# a code change. Live results are cached briefly to keep the dropdown snappy.
_AI_MODELS_CACHE: dict = {}        # provider -> (ts, [models])
_AI_MODELS_TTL = 300               # seconds


@app.route("/api/ai-provider", methods=["GET", "POST"])
def api_ai_provider():
    if request.method == "GET":
        cur = ai.active_provider()
        return jsonify({
            "provider": cur,
            "model": ai.active_model(cur),
            "supports_vision": ai.supports_vision(cur),
            "providers": [
                {"id": p, "label": ai.PROVIDER_LABELS[p],
                 "has_key": ai.has_key(p), "default_model": ai.default_model(p),
                 "vision": ai.supports_vision(p)}
                for p in ai.PROVIDERS
            ],
        })
    body = request.json or {}
    provider = str(body.get("provider", "")).strip().lower()
    if provider not in ai.PROVIDERS:
        return jsonify({"ok": False, "error": "未知 AI 引擎"}), 400
    if not ai.has_key(provider):
        label = ai.PROVIDER_LABELS[provider]
        return jsonify({"ok": False,
                        "error": f"请先在上方填入 {label} 的 API Key 再切换。"}), 400
    model = str(body.get("model", "")).strip() or ai.default_model(provider)
    db.update_state({"ai_provider": provider, "ai_model": model})
    note = (f"已切换到 {ai.PROVIDER_LABELS[provider]} · {model} — "
            f"下一次扫描即生效，无需重启。")
    if provider == "deepseek":
        note += " 注意：DeepSeek 无图表视觉，形态视觉确认层会自动跳过。"
    return jsonify({"ok": True, "provider": provider, "model": model, "note": note})


@app.route("/api/ai-models")
def api_ai_models():
    import time as _t
    provider = (request.args.get("provider") or ai.active_provider()).strip().lower()
    if provider not in ai.PROVIDERS:
        return jsonify({"ok": False, "error": "未知 AI 引擎"}), 400
    cached = _AI_MODELS_CACHE.get(provider)
    if cached and (_t.time() - cached[0] < _AI_MODELS_TTL):
        return jsonify({"ok": True, "provider": provider, "models": cached[1],
                        "cached": True})
    try:
        # Hard wall-clock timeout: never let a stalled SDK/network call hang the
        # dropdown — fall back to the static list if the live fetch is slow.
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
        with ThreadPoolExecutor(max_workers=1) as ex:
            models = ex.submit(ai.list_models, provider).result(timeout=12)
        if models:
            _AI_MODELS_CACHE[provider] = (_t.time(), models)
        return jsonify({"ok": True, "provider": provider, "models": models})
    except Exception as e:
        # Offline / no key / slow API → static fallback so the dropdown still works.
        msg = "实时拉取超时" if e.__class__.__name__ == "TimeoutError" else str(e)[:120]
        return jsonify({"ok": True, "provider": provider,
                        "models": ai.fallback_models(provider),
                        "error": msg, "fallback": True})


# _spawn_scheduler() used to live here. It is gone rather than merely unused:
# it called Popen with no `env=`, so the worker inherited this process's
# environment, and that is the whole of the 2026-08-11 incident. A dead function
# of exactly the right shape, sitting next to the route that used to call it, is
# an invitation to wire it back. src/start_protocol.py:commit() replaces it and
# builds the child environment instead of passing one along.
#
# _start_opend() above is kept — it starts OpenD, not a trader, and the protocol
# needs it before a worker can connect.


@app.route("/api/scheduler/<action>", methods=["POST"])
def api_scheduler(action):
    if action == "start":
        # One entry point, shared with the macOS shell and the CLI. This route
        # used to spawn the worker itself, with no `env=`, so it inherited
        # whatever this process happened to hold — on 2026-08-11 a day-old copy
        # of a configuration corrected hours earlier, which the worker then
        # traded on for two hours. Nothing here decides anything now; every
        # rule, refusal and audit record lives in start_protocol, so the web
        # panel cannot start a worker under different rules than the CLI.
        from src import start_protocol
        opend_err = _start_opend()
        if opend_err:
            return jsonify({"ok": False, "error": opend_err,
                            "code": "opend_unavailable"}), 503
        try:
            result = start_protocol.start("web")
        except start_protocol.StartRefused as e:
            # 409: the request was understood and refused on its merits. The
            # code is the machine-readable half; detail says what to fix.
            return jsonify({"ok": False, "error": e.detail,
                            "code": e.code}), 409
        return jsonify({"ok": True, **result})
    if action == "stop":
        # Stop closes the process, the session and the lease together. Killing
        # the process alone left a session with no ended_at — so open_sessions()
        # reported a worker that was not there — and a lease naming a dead pid,
        # which the next start had to break before it could proceed.
        from src import start_protocol
        result = start_protocol.stop("web")
        had_opend = _stop_opend()
        return jsonify({
            "ok": True, "running": False, **result,
            "note": ("not running" if not result["stopped"] else
                     "stopped (worker + OpenD)" if had_opend else
                     "stopped (worker; OpenD was already off)")})
    if action == "restart":
        # Stop, then start, both through the protocol — rather than the old
        # twin of start that re-spawned with this process's environment. Its
        # comment claimed it "picks up the latest .env"; it picked up the latest
        # FILE for whatever the worker read itself, and this process's stale
        # copy for everything else.
        from src import start_protocol
        start_protocol.stop("web")
        try:
            result = start_protocol.start("web")
        except start_protocol.StartRefused as e:
            # The stop already happened. Say so plainly: reporting a failed
            # restart without mentioning that nothing is running now is how an
            # operator walks away believing the old worker is still trading.
            return jsonify({
                "ok": False, "code": e.code,
                "error": f"stopped, but did not start again — {e.detail}",
                "stopped": True, "running": False}), 409
        return jsonify({"ok": True, "note": "restarted", **result})
    return jsonify({"ok": False, "error": "bad action"}), 400


@app.route("/api/halt", methods=["GET"])
def api_halt_status():
    from src import risk_manager
    return jsonify({"ok": True, **risk_manager.halt_status()})


@app.route("/api/halt/release", methods=["POST"])
def api_halt_release():
    """Clear a halt. Requires a name, and records it.

    Halts that describe a discrepancy between what this software believes and
    what the broker holds cannot be cleared by time — see
    risk_manager.MANUAL_RELEASE_REASONS. This is the only route that clears
    them, and it exists so that clearing one is a deliberate act by a person
    who has looked at the account rather than a side effect of a new day.
    """
    from src import risk_manager
    data = request.get_json(silent=True) or {}
    who = str(data.get("who") or "").strip()
    if not who:
        return jsonify({"ok": False, "code": "who_required",
                        "error": "name the person releasing this halt — it is "
                                 "recorded, and a halt of this kind means "
                                 "someone checked the account"}), 400
    out = risk_manager.release_halt(who, str(data.get("note") or ""))
    return jsonify({"ok": True, **out})


@app.route("/api/trade-env", methods=["GET", "POST"])
def api_trade_env():
    """SIMULATE ⟷ REAL toggle. The trade env is read from .env when the scheduler
    process starts, so a change is written to .env here and applied by restarting
    the scheduler (the toggle UI offers the restart). GET reports the .env value
    (what the next start uses), the live value the running scheduler last reported,
    and the open-position count for the go-live safety check."""
    if request.method == "GET":
        env_file = (_read_env().get("MOO_TRADE_ENV") or "SIMULATE").upper()
        acct = _read_json(ACCOUNT_FILE, {})
        env_live = (acct.get("trade_env") or "").upper() or None
        try:
            n_open = len(db.load_open_trades())
        except Exception:
            n_open = 0
        return jsonify({
            "env_file": env_file, "env_live": env_live,
            "running": _scheduler_running(), "open_positions": n_open,
            "pending_restart": bool(env_live and env_live != env_file),
        })

    body = request.json or {}
    target = (body.get("env") or "").upper()
    if target not in ("SIMULATE", "REAL"):
        return jsonify({"ok": False, "error": "env 必须是 SIMULATE 或 REAL"}), 400

    # Going REAL is real money — guard it: explicit confirm, trade password set,
    # and a FLAT book (so the local open-trades state can't be mistaken for / act
    # on the real account it was never opened in).
    if target == "REAL":
        if not body.get("confirm"):
            return jsonify({"ok": False, "error": "切换到实盘需要二次确认"}), 400
        if not _read_env().get("MOO_TRADE_PWD"):
            return jsonify({"ok": False,
                            "error": "未设置 MOO_TRADE_PWD（6 位交易密码）— 无法切到实盘"}), 400
        try:
            n_open = len(db.load_open_trades())
        except Exception:
            n_open = 0
        if n_open > 0:
            return jsonify({"ok": False,
                            "error": f"当前还有 {n_open} 个未平仓持仓（模拟盘）。请先全部平仓再切实盘，"
                                     f"否则本地持仓状态会和实盘账户串号。"}), 409

    _write_env_key("MOO_TRADE_ENV", target)
    note = ("已切到实盘 💵 — 点「重启」后生效。首次实盘务必只放小额，先验证下单链路。"
            if target == "REAL"
            else "已切回模拟 🧪 — 点「重启」后生效。")
    return jsonify({"ok": True, "env": target, "note": note,
                    "restart_required": True, "running": _scheduler_running()})


# ── keep-awake toggle ("caffeinate" — Amphetamine-style) ───────────────────────
# All the logic lives in src/keepawake.py so the web dashboard and the menu-bar app
# behave identically. Layer 1 = `caffeinate -i -s` (no idle/system sleep, screen may
# still turn off). Layer 2 = `sudo pmset disablesleep` (also blocks lid-closed sleep)
# via a one-time, tightly-scoped sudoers rule that the first enable installs through
# a native macOS auth dialog.
@app.route("/api/caffeinate", methods=["GET", "POST"])
def api_caffeinate():
    if request.method == "POST":
        on = bool((request.get_json(silent=True) or {}).get("on"))
        return jsonify(keepawake.turn_on() if on else keepawake.turn_off())
    return jsonify(keepawake.status())


# ── web access: toggle LAN/phone exposure from the panel (restarts this server) ─
def _lan_ip() -> str | None:
    """Primary LAN IPv4 (the address phones on the same WiFi would use)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _tailscale_ip() -> str | None:
    """Tailscale IPv4 (100.64.0.0/10) — lets the phone connect from anywhere."""
    for c in ("/Applications/Tailscale.app/Contents/MacOS/Tailscale",
              "tailscale", "/usr/local/bin/tailscale"):
        try:
            out = subprocess.run([c, "ip", "-4"], capture_output=True, text=True, timeout=2)
            for line in out.stdout.splitlines():
                ip = line.strip()
                if ip.startswith("100."):
                    return ip
        except Exception:
            continue
    return None


def _current_web_host() -> str:
    return os.getenv("WEB_HOST") or _read_env().get("WEB_HOST") or "127.0.0.1"


def _schedule_web_restart(port: int, host: str) -> None:
    """Detached relauncher: wait for the HTTP response to flush, kill this server,
    then start a fresh one bound to `host`. Survives our death via start_new_session.
    The host is passed EXPLICITLY (not via .env) because dotenv loads .env into
    os.environ with override=False, so a stale inherited WEB_HOST would otherwise win
    over the freshly-written .env value. Werkzeug sets SO_REUSEADDR for prompt rebind."""
    oldpid = os.getpid()
    p = int(port)
    h = shlex.quote(host)
    # Dev relaunches the server as a script under the venv python. Frozen has
    # neither — re-exec the bundled binary with no args, which entry.py routes
    # to server mode.
    relaunch = (shlex.quote(sys.executable) if IS_FROZEN
                else f"{shlex.quote(str(VENV_PY))} web/server.py")
    script = (
        f"sleep 1; kill {oldpid} 2>/dev/null; "
        # wait up to 5s for graceful exit, then force-kill
        f"for i in $(seq 1 10); do kill -0 {oldpid} 2>/dev/null || break; sleep 0.5; done; "
        f"kill -9 {oldpid} 2>/dev/null; "
        # wait until the port is actually released (avoids EADDRINUSE on rebind)
        f"for i in $(seq 1 20); do lsof -nP -iTCP:{p} -sTCP:LISTEN >/dev/null 2>&1 || break; sleep 0.5; done; "
        f"cd {shlex.quote(str(ROOT))}; "
        f"WEB_HOST={h} WEB_PORT={p} nohup {relaunch} "
        f"> logs/web.log 2>&1 & echo $! > logs/web.pid"
    )
    if proc.IS_WINDOWS:
        # The script above is bash: sleep, kill -0, lsof, nohup. Rather than
        # translate it into cmd.exe and ship an untested restart path, say so —
        # a restart that half-works leaves no server listening at all, and the
        # user cannot get back into the panel to fix it.
        raise RuntimeError(
            "Changing the web host requires restarting the server, which this "
            "build cannot do automatically on Windows. Stop the panel and run "
            "windows-start-web.bat again; the new setting is already saved.")
    subprocess.Popen(["/bin/bash", "-lc", script], **proc.spawn_kwargs(),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@app.route("/api/web-access", methods=["GET", "POST"])
def api_web_access():
    port = int(os.getenv("WEB_PORT", "8770"))
    if request.method == "GET":
        host = _current_web_host()
        return jsonify({
            "mode": "lan" if host != "127.0.0.1" else "local",
            "password_set": bool(_web_password()),
            "port": port,
            "lan_ip": _lan_ip(),
            "tailscale_ip": _tailscale_ip(),
            # Plain HTTP: on open WiFi the password + cookie travel unencrypted.
            # Prefer the Tailscale address (encrypted tunnel) over the raw LAN IP.
            "plaintext_warning": "局域网为明文 HTTP，密码/会话在同网段可被嗅探；"
                                 "公共 WiFi 下建议走 Tailscale 地址而非裸 LAN IP。",
        })
    mode = (request.json or {}).get("mode")
    if mode not in ("lan", "local"):
        return jsonify({"ok": False, "error": "mode must be lan|local"}), 400
    if mode == "lan" and not _web_password():
        return jsonify({"ok": False,
                        "error": "请先设置「网页访问密码」再开启手机/局域网访问。"}), 400
    host = "0.0.0.0" if mode == "lan" else "127.0.0.1"
    _write_env_key("WEB_HOST", host)   # persist for future manual launches
    if IS_FROZEN:
        # Packaged app: the web server is a thread of the GUI process — no
        # detached self-restart possible. The setting is saved; takes effect
        # on the next app launch.
        return jsonify({"ok": True, "mode": mode, "restarting": False,
                        "note": "设置已保存 — 重启 App 后生效"})
    _schedule_web_restart(port, host)  # but relaunch binds `host` explicitly
    return jsonify({"ok": True, "mode": mode, "restarting": True})


# ── signal watchlist editor (config/signal_watchlist.json) ─────────────────────
@app.route("/api/signal-watchlist", methods=["GET", "POST"])
def api_signal_watchlist():
    if request.method == "GET":
        data = _read_json(SIGNAL_WL_FILE, {})
        tickers = [str(t).strip().upper() for t in (data.get("tickers") or []) if str(t).strip()]
        return jsonify({"tickers": tickers})
    body = request.get_json(silent=True) or {}
    raw = body.get("tickers")
    if not isinstance(raw, list):
        return jsonify({"ok": False, "error": "tickers must be a list"}), 400
    # Normalize: uppercase, strip, dedupe (preserve order) — same shape the bot reads.
    seen, tickers = set(), []
    for t in raw:
        s = str(t).strip().upper()
        if s and s not in seen:
            seen.add(s); tickers.append(s)
    try:
        SIGNAL_WL_FILE.parent.mkdir(parents=True, exist_ok=True)
        SIGNAL_WL_FILE.write_text(
            json.dumps({"tickers": tickers}, indent=2, ensure_ascii=False)
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "tickers": tickers})


# ── 三日概率预测面板（merged from the Stock Probability Prediction Platform）───
#
# The forecast is RESEARCH ONLY and every payload says so: `actionable` is False
# and no execution path imports src.signal_service. These routes read the broker
# and write the research SQLite store; they never touch an order.
#
# Each forecast costs ~3 broker kline requests and ~0.5s of numpy, and the kline
# rate limiter is shared process-wide with the live trading loop. A short TTL
# cache in front, plus a single-flight lock, keeps a user clicking Refresh from
# spending the execution loop's request budget.
_FORECAST_TTL = 120.0
_forecast_lock = threading.Lock()
_forecast_cache: dict = {"ts": {}, "data": {}}


def _signal_service():
    from src.signal_service import service
    return service()


def _bad_symbol(raw):
    sym = (raw or "").strip().upper()
    if not sym or len(sym) > 12 or not all(c.isalnum() or c in ".-" for c in sym):
        return None
    return sym


# Symbol lives in the path, not the query string. That also keeps these out of
# test_web_smoke's parameterless-GET sweep, which requires every such route to
# answer < 400 — one that demands a symbol cannot.
@app.route("/api/signal-forecast/<symbol>")
def api_signal_forecast(symbol):
    """One symbol's three-day probability forecast, with context and quality."""
    sym = _bad_symbol(symbol)
    if not sym:
        return jsonify({"ok": False, "error": "bad symbol"}), 400
    force = request.args.get("force") in ("1", "true", "yes")
    now = time.time()
    if not force:
        with _forecast_lock:
            if now - _forecast_cache["ts"].get(sym, 0.0) < _FORECAST_TTL:
                return jsonify(_forecast_cache["data"][sym])
    try:
        from src.moo_client import client as _mc
        with _mc() as c:
            data = _signal_service().forecast(c, sym, force=force)
    except Exception as e:
        return jsonify({"ok": False, "symbol": sym,
                        "error": {"code": "service_failure", "message": str(e)}}), 500
    with _forecast_lock:
        _forecast_cache["ts"][sym] = time.time()
        _forecast_cache["data"][sym] = data
    return jsonify(data)


@app.route("/api/signal-scan")
def api_signal_scan():
    """Watchlist + bounded discovery ranking. Cached once per NY trading day."""
    force = request.args.get("force") in ("1", "true", "yes")
    try:
        from src.moo_client import client as _mc
        with _mc() as c:
            return jsonify(_signal_service().scan(c, force=force))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/signal-forecast-history/<symbol>")
def api_signal_forecast_history(symbol):
    """Prior trading days' frozen forecasts and, where settled, their errors."""
    sym = _bad_symbol(symbol)
    if not sym:
        return jsonify({"ok": False, "error": "bad symbol"}), 400
    n = max(1, min(int(request.args.get("n", 40)), 200))
    try:
        svc = _signal_service()
        return jsonify({"ok": True, "symbol": sym,
                        "history": svc.history(sym, limit=n),
                        "learning": svc.store.learning_summary(sym, "3D")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/signal-settle", methods=["POST"])
def api_signal_settle():
    """Pull the newest bars and score whatever forecasts they now settle."""
    sym = _bad_symbol((request.get_json(silent=True) or {}).get("symbol"))
    if not sym:
        return jsonify({"ok": False, "error": "bad symbol"}), 400
    try:
        from src.moo_client import client as _mc
        with _mc() as c:
            return jsonify(_signal_service().settle(c, sym))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/signal-optimizer/<symbol>")
def api_signal_optimizer(symbol):
    """The weekly factor-error optimizer's state and any pending proposal."""
    sym = _bad_symbol(symbol)
    if not sym:
        return jsonify({"ok": False, "error": "bad symbol"}), 400
    try:
        return jsonify({"ok": True, **_signal_service().optimizer_status(sym)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/signal-proposal/propose", methods=["POST"])
def api_signal_optimizer_propose():
    sym = _bad_symbol((request.get_json(silent=True) or {}).get("symbol"))
    if not sym:
        return jsonify({"ok": False, "error": "bad symbol"}), 400
    force = bool((request.get_json(silent=True) or {}).get("force"))
    try:
        return jsonify({"ok": True, **_signal_service().propose(sym, force=force)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/signal-proposal/decide", methods=["POST"])
def api_signal_optimizer_decide():
    """Approve or reject a from-to parameter proposal. Nothing else activates one."""
    body = request.get_json(silent=True) or {}
    proposal_id = str(body.get("proposal_id") or "").strip()
    if not proposal_id or len(proposal_id) > 64:
        return jsonify({"ok": False, "error": "bad proposal_id"}), 400
    if "approve" not in body:
        return jsonify({"ok": False, "error": "approve must be true or false"}), 400
    try:
        return jsonify({"ok": True,
                        **_signal_service().decide(proposal_id, bool(body["approve"]))})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/signal-forecast-health")
def api_signal_forecast_health():
    try:
        return jsonify({"ok": True, **_signal_service().health()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── weekly self-review ("retrain") — analyze real fills → suggestions ──────────
@app.route("/api/self-review")
def api_self_review():
    """Last persisted weekly self-review result (for the dashboard pill)."""
    data = _read_json(SELF_REVIEW_FILE, {})
    return jsonify(data)


@app.route("/api/self-review/run", methods=["POST"])
def api_self_review_run():
    """Trigger the weekly self-review now — same job as the Sunday cron
    (analyze last 7d fills → notify → enqueue suggestions → optimizer proposals).
    Runs detached; the dashboard polls /api/self-review for the fresh result."""
    log = (ROOT / "logs" / "self_review.log").open("a")
    subprocess.Popen(
        _worker_cmd("src.main", "review"),
        cwd=str(ROOT), stdout=log, stderr=log, **proc.spawn_kwargs(),
    )
    return jsonify({"ok": True, "started": True})


# ── on-demand backtest tuning run ─────────────────────────────────────────────
# The weekly chain (real fills → AI candidates → honest-engine backtest of each
# → survivors), started by hand and narrated while it runs.
#
# It replaced the "Honest Backtest" tab, which ran the same engine and printed
# four numbers you could not act on. This runs it for the reason the numbers
# existed — to decide whether a parameter should move — and ends on that
# decision, one row at a time.
#
# One run at a time: each candidate costs a full engine pass over the prefetched
# window, and two concurrent runs would fight over OpenD and hand back deltas
# measured against two different baselines.
_TUNE = {
    "status": "idle",       # idle | running | stopping | stopped | done | error
    "stage": "",
    "log": [],              # [{"t": "HH:MM:SS", "msg": ...}]
    "candidates": [],
    "notes": [],
    "review": {},
    "error": None,
    "started_at": None,
    "finished_at": None,
    "decided": {},          # key → "applied" / "rejected" / a refusal string
    "lang": "zh",           # the language its progress lines were written in
}
_TUNE_LOCK = threading.Lock()
_TUNE_LOG_CAP = 400
# Cooperative, because a thread cannot be killed and one engine pass over the
# prefetched window is a single uninterruptible call. Stopping therefore lands
# at the next checkpoint — after the candidate being measured finishes — and
# what was already measured comes back rather than being discarded.
_TUNE_CANCEL = threading.Event()

# "in flight" is BOTH of these: while a run is winding down from a stop request
# it still owns OpenD, so a second run must not start beside it.
_TUNE_BUSY = ("running", "stopping")


def _tune_say(msg: str) -> None:
    _TUNE["stage"] = msg
    _TUNE["log"].append({"t": time.strftime("%H:%M:%S"), "msg": str(msg)})
    del _TUNE["log"][:-_TUNE_LOG_CAP]


def _run_tune() -> None:
    from src import param_tune
    try:
        out = param_tune.run(on_progress=_tune_say,
                             should_cancel=_TUNE_CANCEL.is_set,
                             lang=_TUNE["lang"])
        _TUNE["candidates"] = out["candidates"]
        _TUNE["notes"] = out["notes"]
        _TUNE["review"] = out["review"]
        # A stopped run is not a failed one: the candidates it did measure are
        # as real as any, and they stay confirmable.
        _TUNE["status"] = "stopped" if out.get("cancelled") else "done"
    except Exception as e:
        log.exception("param tuning run failed")
        _TUNE["error"] = f"{type(e).__name__}: {e}"
        _TUNE["status"] = "error"
        _tune_say((f"✗ Run failed: {_TUNE['error']}") if _TUNE["lang"] == "en"
                  else f"✗ 运行失败：{_TUNE['error']}")
    finally:
        _TUNE["finished_at"] = _now_iso()


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


@app.route("/api/param-tune")
def api_param_tune():
    from src import param_tune, runtime_config
    return jsonify({**_TUNE, "mode": param_tune.mode(),
                    "frozen": runtime_config.frozen()})


@app.route("/api/param-tune/run", methods=["POST"])
def api_param_tune_run():
    """Start a run. Idempotent while one is in flight — the button can be
    double-clicked and the second click reports the run already going rather
    than starting a second one against the same OpenD session."""
    with _TUNE_LOCK:
        if _TUNE["status"] in _TUNE_BUSY:
            return jsonify({"ok": True, "already_running": True,
                            "status": _TUNE["status"]})
        _TUNE_CANCEL.clear()
        lang = _lang()
        _TUNE.update({"status": "running", "log": [],
                      "stage": "Starting…" if lang == "en" else "启动…",
                      "candidates": [], "notes": [], "review": {},
                      "error": None, "decided": {}, "lang": lang,
                      "started_at": _now_iso(), "finished_at": None})
        _tune_say("Starting the tuning run — it changes nothing on its own; "
                  "you confirm each result afterwards."
                  if lang == "en" else
                  "开始回测调参 —— 这一轮不会自动改任何参数，跑完由你逐条确认。")
        threading.Thread(target=_run_tune, daemon=True).start()
    return jsonify({"ok": True, "status": "running"})


@app.route("/api/param-tune/stop", methods=["POST"])
def api_param_tune_stop():
    """Ask the run to stop at its next checkpoint.

    Deliberately NOT a kill. The thread is inside an engine pass more often than
    not, and there is no way to interrupt one — so this raises the flag, says so
    in the log, and the run ends after the candidate it is measuring. Promising
    an instant stop and then taking forty seconds is worse than saying which it
    is.
    """
    with _TUNE_LOCK:
        if _TUNE["status"] not in _TUNE_BUSY:
            return jsonify({"ok": True, "status": _TUNE["status"],
                            "note": "没有正在运行的回测"})
        if _TUNE["status"] == "stopping":
            return jsonify({"ok": True, "status": "stopping",
                            "note": "已经在停了"})
        _TUNE_CANCEL.set()
        _TUNE["status"] = "stopping"
        _tune_say("■ Stop requested — it will stop after the candidate it is "
                  "measuring (one engine pass cannot be interrupted). "
                  "Whatever finished is kept."
                  if _TUNE["lang"] == "en" else
                  "■ 收到停止请求 —— 当前这一条回测跑完就停"
                  "（引擎的单次回测无法中途打断）。已测完的结果会保留。")
    return jsonify({"ok": True, "status": "stopping"})


@app.route("/api/param-tune/apply", methods=["POST"])
def api_param_tune_apply():
    """Write the changes the owner ticked; record the ones they crossed out.

    Only keys from THIS run's candidate list are writable — the payload names a
    key and a value, and without that check the endpoint would be a general
    "set any tunable to anything" write path wearing a confirmation dialog. The
    value is taken from the candidate too, not from the request, for the same
    reason.
    """
    from src import param_tune
    body = request.json or {}
    want = {str(k) for k in (body.get("accept") or [])}
    rejected = [str(k) for k in (body.get("reject") or [])]
    by_key = {c["key"]: c for c in _TUNE["candidates"]}
    unknown = sorted(want - set(by_key))
    accepted = [{"key": k, "value": by_key[k]["value"]}
                for k in by_key if k in want]

    result = param_tune.apply_confirmed(accepted) if accepted else \
        {"applied": [], "failed": {}}
    for a in result["applied"]:
        _TUNE["decided"][a["key"]] = "applied"
    for k, why in result["failed"].items():
        _TUNE["decided"][k] = why
    for k in rejected:
        _TUNE["decided"].setdefault(k, "rejected")
    if unknown:
        for k in unknown:
            result["failed"][k] = (
                "not among this run's candidates — run it again"
                if _TUNE["lang"] == "en" else "不在这一轮的候选里 — 请重新运行")

    if result["applied"]:
        try:
            from src import notifier
            notifier.send("🎛 手动回测调参 — 你确认应用了 " + "，".join(
                f"{a['key']} {a['old']} → {a['new']}" for a in result["applied"]))
        except Exception as e:
            log.warning("param-tune notify failed: %s", e)
    return jsonify({"ok": not result["failed"], **result,
                    "rejected": rejected, "decided": _TUNE["decided"]})


def _self_review_catchup_on_boot() -> None:
    """If the weekly self-review is overdue (laptop was off on its scheduled day),
    fire it ONCE on startup — detached, so it never blocks the server. The trading
    scheduler has its own catchup; this covers the common case where the user only
    opens the web dashboard. cron_state.record_run() makes it idempotent: once it
    runs, last_run is fresh and a quick restart won't re-fire it."""
    try:
        from src import cron_state
        # 2026-07-27: was expected_last_fire_weekly(6, 23, 0) — Sun 23:00 ET —
        # while main.py's catchup used Mon 20:15 KL for this same job key. The
        # two disagreed by ~9h, so a restart in between satisfied one caller and
        # not the other and the review ran TWICE (22:30 here, 22:37 from the
        # scheduler), each running optimizer_ai's auto-apply. Both now read the
        # one schedule in cron_state.WEEKLY_SCHEDULE.
        expected = cron_state.expected_last_fire("self_review")
        if not cron_state.needs_catchup("self_review", expected):
            return
        last = cron_state.last_run("self_review")
        print(f"🧠 Self-review overdue (last run: {last or 'never'}) — running catchup now.")
        log = (ROOT / "logs" / "self_review.log").open("a")
        subprocess.Popen(
            _worker_cmd("src.main", "review"),
            cwd=str(ROOT), stdout=log, stderr=log, **proc.spawn_kwargs(),
        )
    except Exception as e:
        print(f"self-review catchup check skipped: {e}", file=sys.stderr)


# ── exit UI: kill everything (OpenD + scheduler + web server) ──────────────────
@app.route("/api/exit", methods=["POST"])
def api_exit():
    """Exit the entire moo-trader system: stops OpenD and the scheduler,
    then kills this web server. The browser will show a brief confirmation
    before the connection drops."""
    _stop_opend()
    # Through the protocol, like every other stop. This used to call
    # _stop_pid(SCHED_PID), which reads logs/scheduler.pid — the file nothing
    # has written since the spawn path that wrote it was replaced. So "Exit"
    # signalled a pid it never found and left the worker running, holding its
    # lease, with an open session and no ended_at. Same dead file, second
    # consequence.
    try:
        from src import start_protocol
        start_protocol.stop("web")
    except Exception as e:
        log.warning("exit: could not stop the worker through the protocol: %s", e)

    def _shutdown():
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_shutdown, daemon=True).start()
    return jsonify({"ok": True, "note": "Shutting down — OpenD, scheduler, and web UI are all stopping now."})


def _quiet_access_log() -> None:
    """Drop Werkzeug's per-request access log; keep warnings and errors.

    2026-07-27: logs/web.log had reached 47 MB and was ~100% access lines. The
    dashboard polls six endpoints (/api/status, /api/log, /api/sectors,
    /api/closed, /api/caffeinate, /api/approvals) on a loop, and nothing ever
    rotated this file — the launcher and the Swift shell both append to it
    (`>>`), so it survives every restart. src/main.py rotates trader.log via
    RotatingFileHandler(10 MB); this stream had no equivalent.

    Suppressing at WARNING keeps real problems (tracebacks, bind failures, the
    startup banner — those go through print/stderr) while dropping the 200-OK
    noise. Set WEB_ACCESS_LOG=1 to get it back for debugging."""
    if os.getenv("WEB_ACCESS_LOG", "").strip() in ("1", "true", "yes"):
        return
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def main():
    _quiet_access_log()
    _self_review_catchup_on_boot()
    port = int(os.getenv("WEB_PORT", "8770"))
    # Env override wins; otherwise the in-app toggle (persisted to .env) decides;
    # default localhost-only.
    host = os.getenv("WEB_HOST") or _read_env().get("WEB_HOST") or "127.0.0.1"
    # Safety: never expose the control panel to the network without a password.
    if host != "127.0.0.1" and not _web_password():
        print(f"⚠️  WEB_HOST={host} would expose the panel to the network, but no "
              f"WEB_PASSWORD is set.\n    Refusing — falling back to 127.0.0.1. Set a "
              f"password (Settings ⚙ on this Mac, or WEB_PASSWORD in .env) then restart.",
              file=sys.stderr)
        host = "127.0.0.1"
    if host != "127.0.0.1":
        print(f"🌐 Dashboard exposed on http://{host}:{port}  (password protected)")
        print("⚠️  Plain HTTP — password + session cookie travel UNENCRYPTED on the "
              "LAN. On open WiFi, reach it via Tailscale (encrypted) rather than the "
              "raw LAN IP.", file=sys.stderr)
    # load_dotenv=False: Flask would otherwise silently load .env/.flaskenv from
    # the CWD into os.environ — our env story is owned by src/config.py alone.
    app.run(host=host, port=port, threaded=True, load_dotenv=False)


if __name__ == "__main__":
    main()
