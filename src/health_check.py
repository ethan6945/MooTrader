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
_K_GEM = "health_gemini_ok"

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
    _edge(_K_GEM, gem_status,
          fail_msg=(f"⚠️ *{provider_label} API 不可用*：" + gem_detail +
                    "\nAI 层会 fail-safe（不会乱动仓位），但智能退出/情绪选股会变盲。"
                    "请尽快充值或检查 API key，或在面板切换到另一个 AI 引擎。"),
          recover_msg=f"✅ {provider_label} API 已恢复正常。")
