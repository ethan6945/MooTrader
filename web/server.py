"""Web dashboard backend (Flask) — monitor + control the bot from a browser.

Runs as its OWN background process (start-web.command). The trading scheduler
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
  GET  /api/signal-log?n=    → tail of logs/signal_reporter.log
  GET  /api/signal-monitor   → per-symbol latest monitor tick (盯盘磁贴数据)
  GET  /api/signal-alerts?n= → alert feed, newest first (盯盘警报流)
  POST /api/budget           → {value} set runtime budget (no restart)
  POST /api/scheduler/<start|stop>
  POST /api/signal-run       → fire the signal reporter once (background)
  POST /api/backtest         → run a 180d honest backtest (background thread)
  GET  /api/backtest         → last backtest result/status
"""
from __future__ import annotations

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
from src.config import BUNDLE_DIR, IS_FROZEN, ROOT, settings  # noqa: E402

# In the frozen .app the static assets ship inside the bundle; in dev
# BUNDLE_DIR == repo root, so this resolves to web/static either way.
STATIC = BUNDLE_DIR / "web" / "static"
# Override only for running an isolated/secondary instance (e.g. tests). Default = repo .env.
ENV_FILE = Path(os.getenv("WEB_ENV_FILE") or (ROOT / ".env"))
ACCOUNT_FILE = ROOT / "data" / "account.json"
OPEN_TRADES_FILE = ROOT / "data" / "open_trades.json"
TRADER_LOG = ROOT / "logs" / "trader.log"
SIGNAL_LOG = ROOT / "logs" / "signal_reporter.log"
SIGNAL_PID = ROOT / "logs" / "signal_reporter.pid"
SIGNAL_WL_FILE = ROOT / "config" / "signal_watchlist.json"
SIGNAL_MONITOR_FILE = ROOT / "data" / "signal_monitor_state.json"
SIGNAL_ALERTS_FILE = ROOT / "data" / "signal_alerts.json"
SELF_REVIEW_FILE = ROOT / "data" / "self_review_last.json"
SCHED_PID = ROOT / "logs" / "scheduler.pid"
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
    if IS_FROZEN:
        return [sys.executable, "--worker", module, *args]
    return [str(VENV_PY), "-m", module, *args]


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


def _scheduler_running() -> bool:
    return _pid_running(SCHED_PID) is not None


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
            subprocess.run(["pkill", "-x", "futu-opend"], timeout=3)
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
        os.killpg(os.getpgid(pid), signal.SIGTERM)
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
        os.killpg(os.getpgid(pid), signal.SIGTERM)
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


def _opend_status(acct: dict, sched_running: bool) -> tuple[str, str]:
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
    """
    try:
        with socket.create_connection((settings.moo_host, settings.moo_port), timeout=0.6):
            pass
    except Exception:
        return "red", "OpenD 未启动"

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
    prefix = "模拟盘" if not is_real else "已解锁"

    if sched_running and snapshot_ok and market_open and fresh:
        if is_real and not unlock_ok:
            return "yellow", "已连接 · 解锁未确认 — 请在 OpenD 窗口点击解锁"
        return "green", f"{prefix} · 可交易"

    # Off-hours: scanning intentionally pauses, so a stale snapshot is expected.
    # Tolerate a Fri-close→Mon-open weekend plus an adjacent holiday.
    OFF_HOURS_GRACE = 4 * 24 * 3600   # 4 days
    if sched_running and snapshot_ok and not market_open and age < OFF_HOURS_GRACE:
        rest = {
            "premarket":  "待开盘",
            "afterhours": "已收盘",
            "weekend":    "周末休市",
            "holiday":    "假期休市",
        }.get(session, "休市")
        if is_real and not unlock_ok:
            # Nothing trades off-hours, so blue (not alarming) — but flag that
            # the unlock still needs doing before the next open.
            return "blue", f"{rest} · 解锁未确认(开盘前请解锁)"
        return "blue", f"{prefix} · {rest}"

    # 走到这里 = 既非绿(可交易)也非蓝(休市)。真正的问题在调度器/快照管道,
    # 不在锁本身 —— 前缀只报能证实的事。
    if snapshot_ok:
        if not sched_running:
            return "yellow", f"{prefix if unlock_ok or not is_real else '已连接'} · 调度器已停"
        return "yellow", f"{prefix if unlock_ok or not is_real else '已连接'} · 连接中…"
    return "yellow", "已连接 · 等待首次账户查询"


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
    acct["opend_status"], acct["opend_label"] = _opend_status(acct, sched)
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
        _watchdog_ok = _st.get("health_gemini_ok")
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
        trades = _read_json(OPEN_TRADES_FILE, {})
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
    return jsonify(acct)


@app.route("/api/approvals")
def api_approvals():
    items = approvals.list_all()
    items.sort(key=lambda a: (a.get("status") != "pending", a.get("created_at", "")))
    return jsonify(items)


@app.route("/api/closed")
def api_closed():
    n = int(request.args.get("n", 100))
    rows = db.closed_trades(limit=10_000)
    return jsonify(rows[-n:])


@app.route("/api/log")
def api_log():
    n = int(request.args.get("n", 40))
    return jsonify(_tail(TRADER_LOG, n))


@app.route("/api/signal-log")
def api_signal_log():
    n = int(request.args.get("n", 120))
    return jsonify(_tail(SIGNAL_LOG, n))


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
    df = yf.download(syms, period="5d", interval="1d",
                     progress=False, auto_adjust=False, threads=True)
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


_options_cache: dict = {"ts": {}, "data": {}}
_options_lock = threading.Lock()
_OPTIONS_TTL = 300.0   # the underlying rows move once a day; the unusual feed
                       # is a 7-day window. 5 min is plenty and keeps this panel
                       # from competing with the scan loop for OpenD quota.


@app.route("/api/options/<symbol>")
def api_options(symbol):
    """READ-ONLY aggregate options view for one symbol.

    Three things the account CAN reach without the (absent) US options quote
    subscription: the daily call/put-volume history that backs the call_rvol
    factor, the live IV/HV overview, and the 7-day large-trade feed.

    Deliberately not wired to anything that trades. The call_rvol factor is
    advisory even when armed (see src/options_stats.py), and the unusual-trade
    feed has NO history behind it — 7 days, no way to backtest — so nothing it
    reports has been validated. It is here to be looked at, and that is all.
    """
    sym = (symbol or "").strip().upper()
    if not sym or not sym.isalnum() or len(sym) > 8:
        return jsonify({"ok": False, "error": "bad symbol"}), 400
    now = time.time()
    with _options_lock:
        if now - _options_cache["ts"].get(sym, 0.0) < _OPTIONS_TTL:
            return jsonify(_options_cache["data"][sym])
    try:
        from src import options_stats
        from src.moo_client import client as _mc
        with _mc() as c:
            # assess() honours OPTIONS_STATS_ENABLED; this panel should show the
            # numbers whether or not the factor is armed for trading, so read
            # the history directly and run the same pure computation on it.
            hist = options_stats.fetch_history(sym, c)
            stat = options_stats.compute(hist, sym)
            near, edate = options_stats._earnings_window(sym)
            unusual = options_stats.unusual_flow(sym, c)
            ov = options_stats.overview(sym, c)
        series = []
        if hist is not None:
            tail = hist.tail(60)
            series = [{"t": str(t)[:10], "call": float(cv), "put": float(pv)}
                      for t, cv, pv in zip(tail["time"], tail["call_volume"],
                                           tail["put_volume"])]
        data = {
            "ok": bool(stat.get("ok")),
            "symbol": sym,
            "detail": stat.get("detail"),
            "call_rvol": stat.get("call_rvol"),
            "put_rvol": stat.get("put_rvol"),
            "put_call_ratio": stat.get("put_call_ratio"),
            "asof": stat.get("asof"),
            "near_earnings": near,
            "earnings_date": edate,
            "min_rvol": settings.options_stats_min_rvol,
            "armed": settings.options_stats_enabled,
            "sizing": settings.options_stats_sizing,
            "series": series,
            "unusual": unusual,
            "overview": ov,
            # Stated in the payload so a future UI cannot quietly imply
            # otherwise: this endpoint informs, it never decides.
            "advisory_only": True,
        }
    except Exception as e:
        return jsonify({"ok": False, "symbol": sym, "error": str(e)}), 502
    with _options_lock:
        _options_cache["ts"][sym] = now
        _options_cache["data"][sym] = data
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
SETTING_KEYS = {
    "WEB_PASSWORD": "网页访问密码 — 设了之后，从手机/局域网打开面板要先登录(本机也是)。空=不需要密码(仅本机可用)。这是开放手机访问的前提。",
    "DEEPSEEK_API_KEY": "DeepSeek API Key(可逗号分隔多个)— 所有 AI 分析(信号/情绪/退出/优化/入场验证)都用它。",
    "TAVILY_API_KEY": "Tavily 新闻搜索 Key — 给 AI 提供实时新闻上下文。",
    "FINNHUB_API_KEY": "Finnhub Key(免费 60次/分)— 按股票代码标注的新闻，"
                       "且能查历史某一天(Tavily 只能查\"现在\")。需 FINNHUB_ENABLED=true。",
    "TELEGRAM_TOKEN": "Telegram Bot Token — 推送交易通知 + 审批卡片。",
    "TELEGRAM_CHAT_ID": "Telegram Chat ID — 接收通知的聊天 ID。",
}


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
    if not v:
        return ""
    return ("•" * max(0, min(len(v) - 4, 12))) + v[-4:] if len(v) > 4 else "set"


def _write_env_key(key: str, value: str) -> None:
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    for i, l in enumerate(lines):
        st = l.strip()
        if st.startswith(key + "=") or st.startswith("#" + key + "="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n")


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

    provider = (val("AI_PROVIDER", "deepseek") or "deepseek").lower()
    ai_key = val("DEEPSEEK_API_KEYS") or val("DEEPSEEK_API_KEY")
    if provider == "gemini":
        ai_key = val("GEMINI_API_KEYS") or val("GEMINI_API_KEY")

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
    keys = [{"key": k, "desc": d, "masked": _mask(env.get(k, "")), "set": bool(env.get(k))}
            for k, d in SETTING_KEYS.items()]
    return jsonify({"keys": keys})


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
    # Re-anchor the DD breaker's peak to the NEW capital base + realized PnL.
    # 2026-07-07: a peak recorded under the old budget is meaningless under the
    # new one — after a 50k→5k change the bot computed a phantom 90% drawdown
    # and halted all new entries until the 7-day auto-release.
    def _apply(s: dict) -> dict:
        realized = float(s.get("realized_pnl_total") or 0.0)
        base = float(s.get("auto_budget_seed") or val)
        return {"budget_usd": val,
                "peak_equity": max(base + realized, 1.0),
                "halt_started_at": None}
    db.atomic_state(_apply)
    return jsonify({"ok": True, "budget": val,
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


# ── AI engine: Gemini ⟷ DeepSeek toggle + live model dropdown ─────────────────
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
                 "vision": p == "gemini"}
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


def _spawn_scheduler() -> int:
    """Launch the trading scheduler as a detached, caffeinated process and record
    its pid. Shared by the start + restart actions so they stay byte-identical.
    Ensures OpenD is running before launching the scheduler."""
    # Ensure OpenD is up first
    err = _start_opend()
    if err:
        raise RuntimeError(err)
    log = (ROOT / "logs" / "scheduler.log").open("a")
    proc = subprocess.Popen(
        ["/usr/bin/caffeinate", "-is", *_worker_cmd("src.main", "run")],
        cwd=str(ROOT), stdout=log, stderr=log, start_new_session=True,
    )
    SCHED_PID.write_text(str(proc.pid))
    return proc.pid


@app.route("/api/scheduler/<action>", methods=["POST"])
def api_scheduler(action):
    if action == "start":
        if _scheduler_running():
            return jsonify({"ok": True, "note": "already running"})
        # Ensure OpenD is up before spawning scheduler
        opend_err = _start_opend()
        if opend_err:
            return jsonify({"ok": False, "error": opend_err}), 500
        return jsonify({"ok": True, "pid": _spawn_scheduler()})
    if action == "stop":
        had_sched = _stop_pid(SCHED_PID)
        had_opend = _stop_opend()
        return jsonify({"ok": True,
                        "running": _scheduler_running(),
                        "note": "not running" if not had_sched else (
                            "stopped (scheduler + OpenD)" if had_opend else "stopped (scheduler; OpenD was already off)")})
    if action == "restart":
        # _stop_pid blocks until the old process group is gone (or ~3s), so a
        # fresh spawn afterwards can't collide with it. Picks up the latest .env
        # (e.g. a just-flipped MOO_TRADE_ENV).
        _stop_pid(SCHED_PID)
        return jsonify({"ok": True, "pid": _spawn_scheduler(), "note": "restarted"})
    return jsonify({"ok": False, "error": "bad action"}), 400


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
    subprocess.Popen(["/bin/bash", "-lc", script], start_new_session=True,
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


@app.route("/api/signal-run/<mode>", methods=["POST"])
def api_signal_run(mode):
    if mode not in ("brief", "review", "close", "premarket", "intraday", "monitor"):
        return jsonify({"ok": False,
                        "error": "mode must be brief|review|close|premarket|intraday|monitor"}), 400
    subprocess.Popen(_worker_cmd("src.signal_reporter", mode),
                     cwd=str(ROOT), start_new_session=True)
    return jsonify({"ok": True, "mode": mode})


# ── 盯盘监控数据（由 src.signal_reporter.run_monitor 每 5 分钟写盘）────────────
@app.route("/api/signal-monitor")
def api_signal_monitor():
    """每支股票最新 tick 快照（价格/RSI/量比/评分/日内高低/最近警报）。"""
    return jsonify(_read_json(SIGNAL_MONITOR_FILE, {}))


@app.route("/api/signal-alerts")
def api_signal_alerts():
    """警报流，最新在前。web 展示全量（push+info）；Telegram 只推 push 级。"""
    n = max(1, min(int(request.args.get("n", 80)), 400))
    feed = _read_json(SIGNAL_ALERTS_FILE, [])
    if not isinstance(feed, list):
        feed = []
    return jsonify(feed[-n:][::-1])


# ── signal reporter scheduler (persistent loop) — mirrors the desktop GUI ──────
@app.route("/api/signal-status")
def api_signal_status():
    pid = _pid_running(SIGNAL_PID)
    return jsonify({"running": pid is not None, "pid": pid})


@app.route("/api/signal-scheduler/<action>", methods=["POST"])
def api_signal_scheduler(action):
    if action == "start":
        if _pid_running(SIGNAL_PID) is not None:
            return jsonify({"ok": True, "running": True, "note": "already running"})
        SIGNAL_LOG.parent.mkdir(parents=True, exist_ok=True)
        log = SIGNAL_LOG.open("a")
        proc = subprocess.Popen(
            _worker_cmd("src.signal_reporter", "run"),
            cwd=str(ROOT), stdout=log, stderr=log, start_new_session=True,
        )
        SIGNAL_PID.write_text(str(proc.pid))
        return jsonify({"ok": True, "running": True, "pid": proc.pid})
    if action == "stop":
        had = _stop_pid(SIGNAL_PID)
        return jsonify({"ok": True, "running": _pid_running(SIGNAL_PID) is not None,
                        "note": "not running" if not had else "stopped"})
    return jsonify({"ok": False, "error": "bad action"}), 400


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
        cwd=str(ROOT), stdout=log, stderr=log, start_new_session=True,
    )
    return jsonify({"ok": True, "started": True})


# ── backtest (background thread) ──────────────────────────────────────────────
_bt = {"status": "idle", "result": None}


def _run_bt(days: int):
    _bt["status"] = "running"
    try:
        from src.backtest import prefetch_data, _run_live_engine
        # 2026-07-07: use _base_cfg — the SAME runtime-effective config the
        # weekly health check scores (db-state overrides for threshold/tp/sl/
        # risk/budget + the walk-forward universe). The old hand-built cfg here
        # used frozen .env values and a hardcoded 10-ticker list, so the panel
        # backtested a strategy the bot wasn't actually running.
        from src.optimizer_ai import _base_cfg
        cfg = _base_cfg(days=days)
        m = _run_live_engine(cfg, prefetch_data(cfg), rich_metrics=True)["metrics"]
        _bt["result"] = {
            "days": days, "per_day": round(m["net_pnl_usd"] / days, 2),
            "net": m["net_pnl_usd"], "trades": m.get("total_trades", 0),
            "win": m.get("win_rate_pct", 0), "dd": m.get("max_dd_mtm_pct", 0),
            "pf": m.get("profit_factor", 0), "sortino": m.get("sortino_ratio", 0),
        }
        _bt["status"] = "done"
    except Exception as e:
        _bt["status"] = "error"
        _bt["result"] = {"error": str(e)}


@app.route("/api/backtest", methods=["GET", "POST"])
def api_backtest():
    if request.method == "POST":
        if _bt["status"] != "running":
            days = int((request.json or {}).get("days", 180))
            threading.Thread(target=_run_bt, args=(days,), daemon=True).start()
        return jsonify({"ok": True, "status": _bt["status"]})
    return jsonify(_bt)


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
            cwd=str(ROOT), stdout=log, stderr=log, start_new_session=True,
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
    _stop_pid(SCHED_PID)

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
