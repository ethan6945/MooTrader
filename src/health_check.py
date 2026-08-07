"""API / subscription health watchdog with edge-triggered Telegram alerts.

Two silent dependencies can lapse WITHOUT crashing the bot — so the owner would
never know until trades quietly degrade:

  • broker options market-data subscription — if it lapses (or OpenD hasn't
    refreshed the entitlement), options_flow can't fetch chains/snapshots; the
    API returns "No permission ...".
  • Gemini API balance/quota — if the key is exhausted, every AI layer (smart
    exit / sentiment / pattern vision / news veto) fail-safes to neutral/hold.
    The model cascade is single-model now (gemini-3.5-flash, no silent lite
    downgrade), so an exhausted key = AI effectively OFF until topped up.

run() probes both and Telegrams ONLY on a STATE CHANGE (ok→fail and fail→
recovered) so it never spams. State persists in db so a restart doesn't re-alert.
Transient blips (OpenD simply offline overnight, a 503) are classified "skip" and
never toggle state or alert — only OWNER-ACTIONABLE failures (subscription denied,
quota/balance out, key invalid, geo-block) raise an alert.
"""
from __future__ import annotations

import logging

from .config import settings
from . import db, notifier

log = logging.getLogger(__name__)

_K_OPT = "health_options_ok"
_K_OPT_STATS = "health_options_stats_ok"
_K_GEM = "health_gemini_ok"
_K_AI_CALLS = "health_ai_calls_ok"

# Consecutive REAL call failures before the runtime ledger is called bad. The
# probe below asks "can I reach the provider right now"; this asks "are the
# calls this bot actually makes working" — different questions, and the probe
# can pass while the real ones fail (a prompt the model rejects, a per-model
# entitlement, a payload the layer builds wrong). 3 rides out one flaky scan.
_CALL_FAIL_THRESHOLD = 3

# Debounce: a transition must be seen on this many CONSECUTIVE checks before we
# flip state + alert. Stops a flaky/intermittent failure (e.g. an occasional
# Gemini geo-block) from spamming fail/recover messages. A first-ever BAD reading
# still alerts immediately (so a fresh start surfaces a real outage now).
_STREAK_NEEDED = 2


def _edge(state_key: str, status: str, fail_msg: str, recover_msg: str) -> None:
    """status: 'ok' | 'bad' | 'skip'. 'skip' is a no-op (inconclusive/transient).
    Alerts only on a CONFIRMED state change (debounced by _STREAK_NEEDED)."""
    if status == "skip":
        return
    raw_ok = status == "ok"
    s = db.get_state()
    confirmed = s.get(state_key)                # None (never), True, or False
    streak_key = state_key + "_streak"

    try:
        # First-ever observation: set the baseline; alert only if already bad.
        if confirmed is None:
            db.atomic_state(lambda _s: {state_key: raw_ok, streak_key: 0})
            if not raw_ok:
                notifier.send(fail_msg)
            return
        # Reading agrees with the confirmed state → reset the disagree streak.
        if raw_ok == confirmed:
            if s.get(streak_key):
                db.atomic_state(lambda _s: {streak_key: 0})
            return
        # Disagrees → need _STREAK_NEEDED consecutive disagreements to flip.
        streak = int(s.get(streak_key) or 0) + 1
        if streak >= _STREAK_NEEDED:
            db.atomic_state(lambda _s: {state_key: raw_ok, streak_key: 0})
            notifier.send(recover_msg if raw_ok else fail_msg)
        else:
            db.atomic_state(lambda _s: {streak_key: streak})
    except Exception as e:
        log.debug("health edge persist/alert failed: %s", e)


def check_options(client, symbol: str = "AAPL") -> tuple[str, str]:
    """('ok'|'bad'|'skip', detail). 'bad' ONLY on a permission/subscription
    denial (owner-actionable); OpenD-down / timeout / connection issues →
    'skip' (expected when the bot host is off — don't false-alarm)."""
    try:
        from . import options_flow
        ok, detail = options_flow.probe(symbol, client)
    except Exception as e:
        return "skip", f"probe error ({e})"
    if ok:
        return "ok", detail
    d = detail.lower()
    if "permission" in d or "无权限" in detail:
        return "bad", detail            # subscription lapsed / OpenD not refreshed
    return "skip", detail               # timeout / OpenD down / unknown — not actionable


def check_ai() -> tuple[str, str]:
    """Probe the ACTIVE AI provider (Gemini or DeepSeek). ('ok'|'bad'|'skip',
    detail). 'bad' ONLY on owner-actionable failures (quota/balance, invalid key,
    geo-block). Transient 503 / no-key → 'skip'."""
    from . import ai
    provider = ai.active_provider()
    if not ai.has_key(provider):
        return "skip", f"no {ai.PROVIDER_LABELS[provider]} key configured"
    try:
        ai.generate("ping")
        return "ok", "ok"
    except Exception as e:
        last_err = str(e)
    el = last_err.lower()
    if any(x in el for x in ("503", "500", "unavailable", "deadline", "timeout")):
        return "skip", f"transient ({last_err[:60]})"
    if "429" in last_err or "resource_exhausted" in el or "quota" in el or "insufficient" in el:
        return "bad", f"配额/余额耗尽 ({last_err[:90]})"
    if any(x in el for x in ("api key", "permission_denied", "unauthenticated",
                             "401", "403", "invalid argument: api", "authentication")):
        return "bad", f"API key 失效/无权限 ({last_err[:90]})"
    if "location" in el or "failed_precondition" in el:
        return "bad", f"地区限制 / location not supported ({last_err[:90]})"
    # A retired / misspelled model name (2026-08-07). This bucket did not exist
    # and it is precisely the hole DeepSeek's 2026-07-24 alias retirement fell
    # through: the 404 matched nothing above, landed in "unclassified" → 'skip',
    # and 'skip' never toggles state or alerts. So the AI was dead for days
    # while this watchdog — the thing built to catch exactly that — stayed
    # silent. It is owner-actionable (change one setting), so it is 'bad'.
    if ("404" in last_err or "model not found" in el or "does not exist" in el
            or "unknown model" in el or "model_not_found" in el):
        return "bad", f"模型不存在/已下线 ({last_err[:90]})"
    return "skip", f"unclassified ({last_err[:90]})"


def run(client=None) -> None:
    """Probe options + Gemini and fire edge-triggered Telegram alerts. Never
    raises. Disabled via HEALTH_CHECK_ENABLED=false."""
    if not settings.health_check_enabled:
        return

    # --- options data ---
    # 2026-07-27: don't probe (or report bad) while the consumer is switched off.
    # OPTIONS_FLOW_ENABLED defaults false and options_flow has exactly one gated
    # call site (smart_exit), so with the flag off a lapsed/absent options
    # entitlement degrades nothing — yet every health run logged
    # "options=bad (No permission to get quotes for US.AAPL)" and burned an API
    # call doing it. A permanent red that cannot be acted on trains you to ignore
    # the health line, which is the opposite of what this watchdog is for. Turn
    # OPTIONS_FLOW_ENABLED on (after subscribing) and the probe comes back.
    if not settings.options_flow_enabled:
        log.debug("health: options probe skipped — OPTIONS_FLOW_ENABLED is off")
    else:
        try:
            if client is not None:
                opt_status, opt_detail = check_options(client)
            else:
                from .moo_client import client as _client
                with _client() as c:
                    opt_status, opt_detail = check_options(c)
        except Exception as e:
            opt_status, opt_detail = "skip", f"client error ({e})"
        log.info("health: options=%s (%s)", opt_status, opt_detail)
        _edge(_K_OPT, opt_status,
              fail_msg=("⚠️ *期权数据抓取失败*\n" + opt_detail +
                        "\n可能原因：券商美股期权/正股行情订阅已过期，或 OpenD 未刷新权限"
                        "（新订阅常需退出 OpenD 重新登录）。请检查/续订后重启 OpenD。"),
              recover_msg="✅ 期权数据已恢复，可正常抓取（volume + 未平仓量可用）。")

    # --- options AGGREGATE feed (2026-08-05) ---
    # Separate entitlement from the chain probe above: this one needs no options
    # quote subscription, so it can be healthy while the chain stays denied. Same
    # discipline as that probe — only report while the consumer is armed, so an
    # unread red doesn't train you to ignore the health line.
    if not settings.options_stats_enabled:
        log.debug("health: options-stats probe skipped — OPTIONS_STATS_ENABLED is off")
    else:
        try:
            from . import options_stats
            if client is not None:
                st_ok, st_detail = options_stats.probe("AAPL", client)
            else:
                from .moo_client import client as _client
                with _client() as c:
                    st_ok, st_detail = options_stats.probe("AAPL", c)
            st_status = "ok" if st_ok else "bad"
        except Exception as e:
            st_status, st_detail = "skip", f"client error ({e})"
        log.info("health: options-stats=%s (%s)", st_status, st_detail)
        _edge(_K_OPT_STATS, st_status,
              fail_msg=("⚠️ *期权聚合数据抓取失败*\n" + st_detail +
                        "\nget_option_underlying_his_statistic 无数据 —"
                        " call_rvol 因子将静默失效（降级为无意见，不会误下单）。"),
              recover_msg="✅ 期权聚合数据已恢复，call_rvol 因子重新可用。")

    # --- AI provider (Gemini / DeepSeek) ---
    try:
        from . import ai
        provider_label = ai.PROVIDER_LABELS.get(ai.active_provider(), "AI")
    except Exception:
        provider_label = "AI"
    try:
        gem_status, gem_detail = check_ai()
    except Exception as e:
        gem_status, gem_detail = "skip", f"check error ({e})"
    log.info("health: ai=%s (%s)", gem_status, gem_detail)
    # News-driven mode changes what an AI outage MEANS. Normally the loop keeps
    # trading on technicals and the AI layers fail-safe to neutral — annoying,
    # not urgent. With NEWS_DRIVEN_ENABLED the news IS the thesis, so no AI is
    # no trades at all: the bot sits out the whole session. Same alert channel,
    # but the message has to say which of those two situations you are in.
    try:
        from . import news_driven
        nd_on = news_driven.enabled()
    except Exception:
        nd_on = False
    _consequence = (
        "\n🚨 新闻主导模式已开启 —— 读不到新闻就不下单，"
        "所以现在是**完全停止交易**，不是降级运行。"
        if nd_on else
        "\nAI 层会 fail-safe（不会乱动仓位），但智能退出/情绪选股会变盲。"
    )
    _edge(_K_GEM, gem_status,
          fail_msg=(f"⚠️ *{provider_label} API 不可用*：" + gem_detail +
                    _consequence +
                    "\n请尽快充值或检查 API key / 模型名，或在面板切换 AI 引擎。"),
          recover_msg=f"✅ {provider_label} API 已恢复正常。")

    # --- real call outcomes (2026-08-07) ---
    # Second, independent signal: what the calls this bot actually makes did.
    # The probe above can be green while these fail. Only meaningful once a key
    # exists — with no key nothing is attempted and the streak stays 0 forever,
    # which check_ai() already reports as 'skip'.
    try:
        from . import ai as _ai
        ch = _ai.call_health()
        streak = int(ch.get("fail_streak") or 0)
        if not _ai.has_key():
            call_status, call_detail = "skip", "no key — nothing attempted"
        elif streak >= _CALL_FAIL_THRESHOLD:
            call_status = "bad"
            call_detail = f"最近 {streak} 次调用连续失败：{ch.get('last_error', '')[:90]}"
        elif streak == 0 and ch.get("last_ok"):
            call_status, call_detail = "ok", "recent calls succeeding"
        else:
            # 1-2 failures, or nothing recorded yet — inconclusive on purpose.
            call_status, call_detail = "skip", f"streak={streak} (inconclusive)"
    except Exception as e:
        call_status, call_detail = "skip", f"ledger error ({e})"
    log.info("health: ai_calls=%s (%s)", call_status, call_detail)
    _edge(_K_AI_CALLS, call_status,
          fail_msg=(f"⚠️ *{provider_label} 实际调用连续失败*\n" + call_detail +
                    _consequence +
                    "\n（探针可能仍显示正常 —— 这条看的是机器人真正发出的请求。）"),
          recover_msg=f"✅ {provider_label} 调用已恢复，AI 层重新生效。")
