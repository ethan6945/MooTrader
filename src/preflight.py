"""Startup preflight — "is this install actually configured to do what the user
thinks it does?" (2026-08-06)

WHY THIS EXISTS. Everything key-gated in this bot is fail-safe by design: a
missing API key makes a layer return neutral, a missing Telegram token makes
notifier.send() log instead of raise. That is correct — an AI quota running out
must not stop a trading loop. But it has a cost the owner only discovers by
reading logs: on 2026-08-01..05 this bot traded for four days with NO AI key at
all, logging "ai=skip (no DeepSeek key configured)" 198 times, and the only
alert channel for that condition was Telegram, whose token was missing from the
same file. Silent degradation plus a notification path that shares the failure
is how you end up flying blind and not knowing it.

A fresh install is worse. The packaged app seeds data/, logs/, config/ and the
config templates, but NOT .env — so a first-run user has zero keys and every
optional subsystem off, with nothing on screen saying so. They see a bot that
"works" and assume they are getting the whole product.

WHAT THIS IS NOT. It is not a gate that refuses to run without keys. Exactly one
thing is a genuine blocker (the broker connection); everything else is reported
as DEGRADED with a plain statement of what it turns off. The old first-run
wizard marked the AI key `required: True`, which is simply not true — the entry
path is OpenD → indicators → scoring → risk → order and touches no API key. A
check that cries wolf trains people to click through it.

DESIGN RULES
  • Test LIVENESS, not presence. "key is set" and "key works" are different
    facts and only the second one matters. Probes are bounded by a timeout and
    cached, so opening the dialog cannot hang or burn quota.
  • Every check says what it DISABLES, not just that it failed.
  • Every fixable check names the .env key that fixes it, so the UI can offer an
    inline edit + single-item retry.
  • Nothing here raises. A broken check reports itself as unknown.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass, field, asdict

from .config import settings

log = logging.getLogger(__name__)

# Severity ladder. Only BLOCKER may stop a start; DEGRADED is informational but
# actionable; INFO is context the user should see once.
BLOCKER, DEGRADED, INFO, OK = "blocker", "degraded", "info", "ok"

_PROBE_TTL_S = 120.0          # opening the dialog twice must not re-bill the API
_cache: dict[str, tuple[float, "Check"]] = {}
_lock = threading.Lock()


@dataclass
class Check:
    id: str
    ok: bool
    severity: str
    title: str            # zh
    title_en: str
    detail: str = ""      # what we actually found
    detail_en: str = ""
    impact: str = ""      # what is turned off / what breaks — the part that matters
    impact_en: str = ""
    fix_key: str | None = None      # .env key the UI can edit inline
    fix_hint: str = ""
    fix_hint_en: str = ""
    retryable: bool = True
    def as_dict(self) -> dict:
        return asdict(self)


# ── reading credentials the way the USER just left them ──────────────────────
# settings.* is a frozen snapshot taken at import, so a key saved from the UI is
# invisible to this process until it restarts. That is fine for the trading loop
# (which is told to restart) but fatal here: the whole point of this module is
# "paste a key, press retry, see it go green". So credential checks read the
# .env file itself, and probe with THAT value rather than the stale one.

def _live_env() -> dict:
    out: dict[str, str] = {}
    try:
        for line in (settings.root / ".env").read_text().splitlines():
            s = line.split("#", 1)[0].strip()
            if "=" in s and not s.startswith("#"):
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def _cred(env: dict, *names: str) -> str:
    """First non-empty value among `names`, .env first then the process env."""
    import os
    for n in names:
        v = (env.get(n) or os.getenv(n, "")).strip()
        if v:
            return v
    return ""


def _probe_deepseek(key: str) -> tuple[str, str]:
    """('ok'|'bad'|'skip', detail). 'bad' only for owner-fixable causes — an
    upstream 5xx is not something the user can correct by editing a key."""
    try:
        import requests
        r = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {key.split(',')[0].strip()}",
                     "Content-Type": "application/json"},
            json={"model": "deepseek-chat", "max_tokens": 1,
                  "messages": [{"role": "user", "content": "ping"}]},
            timeout=15)
    except Exception as e:
        return "skip", f"无法连接 ({str(e)[:60]})"
    if r.ok:
        return "ok", "密钥有效，调用正常"
    body = (r.text or "")[:120]
    if r.status_code in (401, 403):
        return "bad", f"密钥无效或无权限 (HTTP {r.status_code})"
    if r.status_code == 402 or "insufficient" in body.lower():
        return "bad", "账户余额不足"
    if r.status_code == 429:
        return "bad", "配额/频率超限 (HTTP 429)"
    if 500 <= r.status_code < 600:
        return "skip", f"服务端暂时故障 (HTTP {r.status_code})"
    return "skip", f"未分类响应 (HTTP {r.status_code}) {body}"


# ── individual checks ────────────────────────────────────────────────────────

def check_broker() -> Check:
    """The ONE genuine blocker: without OpenD there are no quotes and no orders."""
    host, port = settings.moo_host, settings.moo_port
    try:
        with socket.create_connection((host, port), timeout=2.0):
            reachable = True
    except OSError:
        reachable = False
    if reachable:
        return Check(id="broker", ok=True, severity=OK,
                     title="券商连接 OpenD", title_en="Broker connection (OpenD)",
                     detail=f"{host}:{port} 已连接", detail_en=f"{host}:{port} reachable")
    return Check(
        id="broker", ok=False, severity=BLOCKER,
        title="券商连接 OpenD", title_en="Broker connection (OpenD)",
        detail=f"{host}:{port} 无响应", detail_en=f"{host}:{port} not listening",
        impact="没有行情也无法下单 —— 机器人完全跑不起来。",
        impact_en="No quotes and no orders — the bot cannot run at all.",
        fix_hint="打开 moomoo OpenD 并完成登录（账号/密码/验证码），端口就绪后重试。",
        fix_hint_en="Open moomoo OpenD and finish signing in, then retry.")


def check_ai() -> Check:
    """Liveness, not presence — an expired or out-of-quota key looks identical to
    a working one until something actually calls it."""
    from . import ai
    provider = ai.active_provider()
    label = ai.PROVIDER_LABELS.get(provider, provider.title())
    impact = ("AI 信号复核、情绪打分、智能退出、自动调参会全部静默跳过。"
              "交易主循环不受影响（它本来就不用 AI）。")
    impact_en = ("AI signal review, sentiment, smart exit and auto-tuning all "
                 "silently skip. The trading loop itself is unaffected — it "
                 "never uses AI.")
    env = _live_env()
    key = _cred(env, f"{provider.upper()}_API_KEYS", f"{provider.upper()}_API_KEY")
    if not key:
        return Check(id="ai", ok=False, severity=DEGRADED,
                     title=f"{label} API Key", title_en=f"{label} API key",
                     detail="未配置", detail_en="not configured",
                     impact=impact, impact_en=impact_en,
                     fix_key=f"{provider.upper()}_API_KEY",
                     fix_hint=f"填入 {label} 的 API Key 后重试。",
                     fix_hint_en=f"Paste your {label} API key and retry.")
    # A retired model name fails every call while the key itself is perfectly
    # valid, so a key-only probe reports green on an install that cannot make a
    # single AI call. DeepSeek retired deepseek-chat / deepseek-reasoner on
    # 2026-07-24 with no soft-redirect; ai.migrate_deepseek_model() rewrites
    # them at call time, but silently living on a rewrite is how config rot
    # sets in — say it out loud.
    configured = ai.active_model(provider)
    if provider == "deepseek":
        migrated, _thinking = ai.migrate_deepseek_model(configured)
        if migrated != configured:
            return Check(id="ai", ok=False, severity=DEGRADED,
                         title=f"{label} 模型已退役", title_en=f"{label} model retired",
                         detail=f"配置的是 {configured}，已于 2026-07-24 停止解析",
                         detail_en=f"configured {configured}, retired 2026-07-24",
                         impact=f"调用已自动改用 {migrated}，所以现在还能跑；"
                                f"但配置本身是坏的，下次改动或换机器就会暴露。"
                                f"把 DEEPSEEK_MODEL 改成 {migrated} 或 deepseek-v4-pro。",
                         impact_en=f"Calls are auto-rewritten to {migrated}, so it "
                                   f"still runs — but the config itself is stale and "
                                   f"the next edit or fresh install will expose it. "
                                   f"Set DEEPSEEK_MODEL to {migrated} or deepseek-v4-pro.",
                         fix_key="DEEPSEEK_MODEL",
                         fix_hint=f"填 {migrated}（快、便宜）或 deepseek-v4-pro（会推理，慢且贵）。",
                         fix_hint_en=f"Use {migrated} (fast, cheap) or deepseek-v4-pro "
                                     "(reasoning, slower and dearer).")
        status, detail = _probe_deepseek(key)
    else:
        from . import health_check
        status, detail = health_check.check_ai()
    if status == "ok":
        stale = not ai.has_key(provider)
        return Check(id="ai", ok=True, severity=OK,
                     title=f"{label} API Key", title_en=f"{label} API key",
                     detail=detail + ("（新填入，需重启调度器才会被使用）" if stale else ""),
                     detail_en="key valid, call succeeded" +
                               (" (newly added — restart the scheduler to use it)" if stale else ""))
    # 'skip' = transient/unclassified. Do NOT call that a config error — the user
    # cannot fix someone else's 503, and telling them to would be noise.
    sev = DEGRADED if status == "bad" else INFO
    return Check(id="ai", ok=False, severity=sev,
                 title=f"{label} API Key", title_en=f"{label} API key",
                 detail=detail, detail_en=detail,
                 impact=impact if status == "bad" else "",
                 impact_en=impact_en if status == "bad" else "",
                 fix_key=f"{provider.upper()}_API_KEY" if status == "bad" else None,
                 fix_hint="更换一个有效的 Key 后重试。" if status == "bad" else "",
                 fix_hint_en="Replace with a working key and retry." if status == "bad" else "")


def check_telegram() -> Check:
    """getMe is free and unauthenticated-cheap, so liveness here costs nothing.

    Worth its own check because Telegram is the alert channel for every OTHER
    silent failure — when it is down, the bot loses its voice and the owner
    stops hearing about anything, which is the exact trap this module exists for.
    """
    env = _live_env()
    token = _cred(env, "TELEGRAM_TOKEN")
    chat = _cred(env, "TELEGRAM_CHAT_ID")
    impact = ("成交、止损、审批卡片、健康告警都发不出来 —— 其它组件出问题时"
              "你不会收到任何通知。")
    impact_en = ("No trade, stop, approval or health alerts. When something else "
                 "degrades you will not hear about it.")
    if not token or not chat:
        return Check(id="telegram", ok=False, severity=DEGRADED,
                     title="Telegram 通知", title_en="Telegram alerts",
                     detail="未配置" if not token else "缺少 Chat ID",
                     detail_en="not configured" if not token else "missing chat id",
                     impact=impact, impact_en=impact_en,
                     fix_key="TELEGRAM_TOKEN" if not token else "TELEGRAM_CHAT_ID",
                     fix_hint="填入 Bot Token 与 Chat ID 后重试。",
                     fix_hint_en="Add the bot token and chat id, then retry.")
    try:
        import requests
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=8)
        if r.ok and r.json().get("ok"):
            name = r.json().get("result", {}).get("username", "?")
            return Check(id="telegram", ok=True, severity=OK,
                         title="Telegram 通知", title_en="Telegram alerts",
                         detail=f"@{name} 可用", detail_en=f"@{name} reachable")
        return Check(id="telegram", ok=False, severity=DEGRADED,
                     title="Telegram 通知", title_en="Telegram alerts",
                     detail=f"Token 被拒绝 (HTTP {r.status_code})",
                     detail_en=f"token rejected (HTTP {r.status_code})",
                     impact=impact, impact_en=impact_en, fix_key="TELEGRAM_TOKEN",
                     fix_hint="Token 无效，请重新从 @BotFather 获取。",
                     fix_hint_en="Invalid token — get a new one from @BotFather.")
    except Exception as e:
        return Check(id="telegram", ok=False, severity=INFO,
                     title="Telegram 通知", title_en="Telegram alerts",
                     detail=f"无法连接 ({str(e)[:60]})",
                     detail_en=f"unreachable ({str(e)[:60]})")


def check_news() -> Check:
    token = _cred(_live_env(), "TAVILY_API_KEY")
    if token:
        return Check(id="news", ok=True, severity=OK,
                     title="Tavily 新闻搜索", title_en="Tavily news search",
                     detail="已配置", detail_en="configured")
    return Check(id="news", ok=False, severity=DEGRADED,
                 title="Tavily 新闻搜索", title_en="Tavily news search",
                 detail="未配置", detail_en="not configured",
                 impact="AI 判断拿不到实时新闻，只能看技术面 —— 财报/降级/诉讼这类"
                        "消息面事件它看不见。",
                 impact_en="The AI sees only technicals, never live news — it "
                           "cannot react to earnings, downgrades or lawsuits.",
                 fix_key="TAVILY_API_KEY",
                 fix_hint="在 tavily.com 注册一个免费 Key 后重试。",
                 fix_hint_en="Get a free key at tavily.com and retry.")


def check_news_driven() -> Check:
    """The one switch in this bot that invalidates its own backtest.

    Everything else AI-shaped is advisory precisely so the sandbox keeps
    describing the live system. News-driven mode changes SELECTION, and
    sandbox.py deliberately skips AI to avoid LLM look-ahead — so with this on,
    the backtest measures a strategy that is not the one running. That is a
    legitimate owner choice, but it must never be a silent one.
    """
    from . import news_driven
    if not news_driven.enabled():
        return Check(id="news_driven", ok=True, severity=OK,
                     title="新闻主导模式", title_en="News-driven mode",
                     detail="关闭 — 新闻仅作顾问，技术面选股",
                     detail_en="off — news is advisory, technicals select")

    # Ask the ACTIVE provider for its key name rather than hardcoding one.
    # ai.PROVIDERS is ("deepseek",) since 2026-07-22 — naming a Gemini key here
    # would send this install hunting for a key it deliberately deleted.
    from . import ai
    provider = ai.active_provider()
    env = _live_env()
    no_news = not _cred(env, "TAVILY_API_KEY")
    no_ai = not _cred(env, f"{provider.upper()}_API_KEYS", f"{provider.upper()}_API_KEY")
    if no_news or no_ai:
        # DEGRADED, not BLOCKER: this module's contract is that only the broker
        # may stop a start, and the failure here is safe (it trades nothing, it
        # doesn't trade wrongly). It just has to be impossible to miss.
        return Check(id="news_driven", ok=False, severity=DEGRADED,
                     title="新闻主导模式", title_en="News-driven mode",
                     detail="已开启，但新闻/AI 密钥缺失 —— 不会下任何单",
                     detail_en="on, but the news/AI keys are missing — zero orders",
                     impact="新闻主导模式下「读不到新闻」= 不开仓（这是刻意的），"
                            "所以现在这个配置一单都不会下。要么补上密钥，要么关掉 "
                            "NEWS_DRIVEN_ENABLED 回到技术面选股。",
                     impact_en="In news-driven mode 'cannot read the news' means "
                               "'do not trade' by design — so this configuration "
                               "will place zero orders. Add the keys, or turn "
                               "NEWS_DRIVEN_ENABLED off to go back to technical "
                               "selection.",
                     fix_key="TAVILY_API_KEY" if no_news else f"{provider.upper()}_API_KEY",
                     fix_hint=f"补齐 Tavily + {ai.PROVIDER_LABELS.get(provider, provider)} 密钥后重试。",
                     fix_hint_en=f"Add the Tavily + {ai.PROVIDER_LABELS.get(provider, provider)} "
                                 "keys and retry.")

    return Check(id="news_driven", ok=False, severity=DEGRADED,
                 title="新闻主导模式", title_en="News-driven mode",
                 detail=news_driven.describe(), detail_en=news_driven.describe(),
                 impact="回测不再描述实盘。新闻决定选股，而回测引擎故意跳过 AI"
                        "（避免 LLM 后见之明），所以两边跑的是不同的策略 —— "
                        "此刻的回测数字不能用来判断这个模式的好坏。而且这个模式"
                        "没有因子研究背书（对比期权放量因子的 5,980 组样本）："
                        "实盘结果本身就是实验。",
                 impact_en="The backtest no longer describes live. News now "
                           "selects trades, and the sandbox deliberately skips "
                           "AI to avoid LLM look-ahead, so the two run different "
                           "strategies — current backtest numbers cannot judge "
                           "this mode. No factor study backs it either (contrast "
                           "the options-volume factor's 5,980 name-days): live "
                           "results are the experiment.",
                 fix_key="NEWS_DRIVEN_ENABLED",
                 fix_hint="设为 false 即可恢复技术面选股 + 可回测。",
                 fix_hint_en="Set false to restore technical selection and a "
                             "meaningful backtest.",
                 retryable=False)


def check_options_feed() -> Check:
    """Only meaningful once the factor is armed — probing an endpoint nothing
    consumes would be a permanent red the user cannot act on."""
    if not settings.options_stats_enabled:
        # No fix_key: this is a switch, not a credential, and the key-write
        # endpoint only accepts SETTING_KEYS. Offering an inline edit that the
        # server would reject is worse than offering none.
        return Check(id="options", ok=True, severity=INFO,
                     title="期权放量因子", title_en="Options-volume factor",
                     detail="未启用（OPTIONS_STATS_ENABLED=false）",
                     detail_en="off (OPTIONS_STATS_ENABLED=false)",
                     fix_hint="在 .env 设 OPTIONS_STATS_ENABLED=true 后重启调度器。",
                     fix_hint_en="Set OPTIONS_STATS_ENABLED=true in .env, then "
                                 "restart the scheduler.",
                     retryable=False)
    try:
        from . import options_stats
        from .moo_client import client
        with client() as c:
            ok, detail = options_stats.probe("AAPL", c)
    except Exception as e:
        ok, detail = False, str(e)[:80]
    if ok:
        return Check(id="options", ok=True, severity=OK,
                     title="期权放量因子", title_en="Options-volume factor",
                     detail=detail, detail_en=detail)
    return Check(id="options", ok=False, severity=DEGRADED,
                 title="期权放量因子", title_en="Options-volume factor",
                 detail=detail, detail_en=detail,
                 impact="call_rvol 取不到数据，因子降级为无意见（不会误判，只是没有输入）。",
                 impact_en="call_rvol has no data; the factor degrades to "
                           "no-opinion (it cannot misfire, it just adds nothing).")


# Optional subsystems, each with the switch that turns it on. This is the check
# that answers "what am I actually running?" — the question a fresh install and
# a hand-typed .env both fail to answer.
_FEATURES = [
    ("news_driven_enabled",       "NEWS_DRIVEN_ENABLED",       "新闻主导模式",   "News-driven mode"),
    ("gap_sentinel_enabled",      "GAP_SENTINEL_ENABLED",      "盘前跳空防守",   "Pre-market gap defence"),
    ("smart_exit_enabled",        "SMART_EXIT_ENABLED",        "智能提前退出",   "Smart early exit"),
    ("sentiment_scoring_enabled", "SENTIMENT_SCORING_ENABLED", "情绪打分",       "Sentiment scoring"),
    ("options_stats_enabled",     "OPTIONS_STATS_ENABLED",     "期权放量因子",   "Options-volume factor"),
    ("dynamic_universe_enabled",  "DYNAMIC_UNIVERSE_ENABLED",  "股票池自动刷新", "Automatic universe refresh"),
    ("auto_budget_enabled",       "AUTO_BUDGET_ENABLED",       "利润复投",       "Profit compounding"),
    ("use_breakeven_stop",        "USE_BREAKEVEN_STOP",        "保本止损",       "Breakeven stop"),
]


def check_features() -> Check:
    """Not a pass/fail — an inventory. Silence about an off switch is how a user
    ends up believing they bought a feature they never enabled."""
    on, off = [], []
    for attr, key, zh, en in _FEATURES:
        try:
            (on if bool(getattr(settings, attr)) else off).append((zh, en, key))
        except AttributeError:
            continue
    return Check(
        id="features", ok=not off, severity=INFO if not off else DEGRADED,
        title="可选功能开关", title_en="Optional subsystems",
        detail=f"{len(on)} 项开启，{len(off)} 项关闭",
        detail_en=f"{len(on)} on, {len(off)} off",
        impact=("以下功能当前关闭：" + "、".join(z for z, _, _ in off)) if off else "",
        impact_en=("Currently off: " + ", ".join(e for _, e, _ in off)) if off else "",
        retryable=False)


def check_config_file() -> Check:
    """A fresh packaged install has no .env at all — every setting silently at
    its code default. Say so out loud rather than letting it look configured."""
    p = settings.root / ".env"
    if not p.exists():
        return Check(id="config", ok=False, severity=DEGRADED,
                     title="配置文件 .env", title_en="Config file (.env)",
                     detail=f"不存在：{p}", detail_en=f"missing: {p}",
                     impact="所有设置都在使用代码默认值 —— 密钥、风险参数、功能开关"
                            "全部未配置。",
                     impact_en="Every setting is at its code default — no keys, "
                               "no risk parameters, no feature switches.",
                     retryable=True)
    try:
        n = sum(1 for line in p.read_text().splitlines()
                if line.strip() and not line.strip().startswith("#") and "=" in line)
    except OSError as e:
        return Check(id="config", ok=False, severity=DEGRADED,
                     title="配置文件 .env", title_en="Config file (.env)",
                     detail=f"无法读取 ({e})", detail_en=f"unreadable ({e})")
    return Check(id="config", ok=True, severity=OK,
                 title="配置文件 .env", title_en="Config file (.env)",
                 detail=f"{n} 项设置 · {p}", detail_en=f"{n} settings · {p}")


def check_trade_env() -> Check:
    """REAL money deserves an unmissable line in the dialog."""
    env = (settings.moo_trade_env or "SIMULATE").upper()
    if env == "REAL":
        return Check(id="trade_env", ok=True, severity=DEGRADED,
                     title="交易环境", title_en="Trading environment",
                     detail="实盘 REAL — 使用真实资金",
                     detail_en="REAL — live money",
                     impact="下单会动用真实资金。确认预算、风险参数和持仓上限后再启动。",
                     impact_en="Orders use real money. Confirm budget, risk and "
                               "position limits before starting.",
                     retryable=False)
    return Check(id="trade_env", ok=True, severity=INFO,
                 title="交易环境", title_en="Trading environment",
                 detail="模拟盘 SIMULATE — 虚拟资金", detail_en="SIMULATE — paper money",
                 retryable=False)


_CHECKS = {
    "broker":    check_broker,
    "config":    check_config_file,
    "trade_env": check_trade_env,
    "ai":        check_ai,
    "telegram":  check_telegram,
    "news":      check_news,
    "options":   check_options_feed,
    "features":  check_features,
    "news_driven": check_news_driven,
}
# Dialog order: the blocker first, then identity, then the degradable layers.
# news_driven sits right after the inventory — when it is on it changes what
# every layer below it means, so it should be read before them, not after.
ORDER = ["broker", "trade_env", "config", "features", "news_driven",
         "ai", "news", "telegram", "options"]


def run_one(check_id: str, use_cache: bool = False) -> Check:
    """One check. Never raises — a check that blows up reports itself as unknown
    rather than taking the dialog down with it."""
    fn = _CHECKS.get(check_id)
    if fn is None:
        return Check(id=check_id, ok=False, severity=INFO,
                     title="未知检查", title_en="Unknown check",
                     detail=f"no such check: {check_id}",
                     detail_en=f"no such check: {check_id}", retryable=False)
    if use_cache:
        with _lock:
            hit = _cache.get(check_id)
            if hit and (time.monotonic() - hit[0]) < _PROBE_TTL_S:
                return hit[1]
    try:
        res = fn()
    except Exception as e:
        log.warning("preflight check %s failed: %s", check_id, e)
        res = Check(id=check_id, ok=False, severity=INFO,
                    title=check_id, title_en=check_id,
                    detail=f"检查本身出错 ({str(e)[:70]})",
                    detail_en=f"check itself errored ({str(e)[:70]})")
    with _lock:
        _cache[check_id] = (time.monotonic(), res)
    return res


def run_all(use_cache: bool = True) -> dict:
    """Full preflight. `can_start` is False ONLY on a blocker — a degraded
    install is still allowed to trade, deliberately, because it can."""
    checks = [run_one(cid, use_cache=use_cache) for cid in ORDER]
    blockers = [c for c in checks if c.severity == BLOCKER and not c.ok]
    degraded = [c for c in checks if c.severity == DEGRADED and not c.ok]
    return {
        "can_start": not blockers,
        "blockers": len(blockers),
        "degraded": len(degraded),
        "checks": [c.as_dict() for c in checks],
        # The one-line verdict the dialog headlines with.
        "summary": ("无法启动：" + blockers[0].title) if blockers else
                   (f"可以启动，但有 {len(degraded)} 项功能未就绪" if degraded else "全部就绪"),
        "summary_en": ("Cannot start: " + blockers[0].title_en) if blockers else
                      (f"Ready to start — {len(degraded)} item(s) degraded" if degraded
                       else "All systems ready"),
    }


def invalidate(check_id: str | None = None) -> None:
    """Drop cached results so a just-edited key is re-probed for real."""
    with _lock:
        _cache.pop(check_id, None) if check_id else _cache.clear()
