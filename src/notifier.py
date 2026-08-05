"""Telegram push — silent fallback to console if token unset.

All message bodies route through src.i18n so the language matches the GUI
preference (data/prefs.json).  Switching the GUI to Chinese also switches
the Telegram notifications, without restarting the scheduler.
"""
from __future__ import annotations

import logging
import time

import requests

from .config import settings
from .i18n import t

log = logging.getLogger(__name__)


def _md_escape(s: str) -> str:
    """Neutralise Telegram legacy-Markdown control characters in text that came
    from data rather than from a message template. Templates use *bold* and
    `code` on purpose and must NOT be run through this."""
    for ch in ("_", "*", "`", "["):
        s = s.replace(ch, "\\" + ch)
    return s


def send(text: str) -> None:
    if not settings.telegram_token or not settings.telegram_chat_id:
        log.info("[telegram disabled] %s", text)
        return
    url = f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage"
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    for attempt in range(3):
        try:
            r = requests.post(url, data=payload, timeout=15)
            if r.ok:
                return
            # 2026-07-06: Markdown parse errors (HTTP 400 "can't parse entities")
            # happen when message text contains unclosed formatting chars (* or _).
            # Fall back to plain-text delivery so the notification still arrives.
            # Retry IMMEDIATELY instead of `continue` — a continue on the last
            # loop iteration would silently drop the message.
            if r.status_code == 400 and "parse" in r.text.lower() and payload.get("parse_mode"):
                # Log the offending text: without it the warning says something
                # broke but not what, and the message is gone by the time anyone
                # looks. (2026-08-05: cost a full forensic pass to identify.)
                log.warning("telegram markdown parse error — retrying as plain "
                            "text. offending message: %r", text[:200])
                payload.pop("parse_mode", None)
                r = requests.post(url, data=payload, timeout=15)
                if r.ok:
                    return
            log.warning("telegram send HTTP %s: %s", r.status_code, r.text[:120])
            return
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                log.warning("telegram send failed: %s", e)


def signal_msg(signal, ai_reason: str, qty: int) -> str:
    return t(
        "tg_buy",
        symbol=signal.symbol,
        price=signal.price,
        score=signal.score,
        qty=qty,
        cost=qty * signal.price,
        stop=signal.stop_loss,
        tp=signal.take_profit,
        atr=signal.atr,
        reasons="; ".join(signal.reasons),
        ai_reason=ai_reason,
    )


def skip_msg(symbol: str, reason: str) -> str:
    return t("tg_skip", symbol=symbol, reason=reason)


def exec_fail_msg(symbols: list[str]) -> str:
    return t("tg_exec_fail", symbols=", ".join(symbols))


def clock_check_msg(st: dict, session: str) -> str:
    """Daily pre-open time check. `st` is clock.status() taken right after a
    force_refresh; source containing 'system' means every network time source
    failed and the drift figure is meaningless. Returns "" when nothing should
    push (owner request 2026-07-11: OK / drift-corrected results are log-only;
    only unverifiable time or a holiday warrants a Telegram)."""
    if session == "holiday":
        return t("tg_clock_holiday")
    source = st.get("source") or "system"
    if "system" in source:
        return t("tg_clock_fail")
    return ""


def trade_action_msg(action: dict) -> str:
    """Render one executor action as a Telegram message.

    EVERY type executor.manage_open_trades can emit must have a branch here.
    2026-08-05: five of eleven did not (breakeven / scale_out / tp_full /
    cancel_stale / edit_stop), so they fell through to the old `str(action)`
    fallback and shipped a raw Python dict. Each of those type names contains a
    single "_", which Telegram's legacy Markdown reads as an unclosed italic —
    the API rejected the message with HTTP 400 and notifier.send() had to resend
    it as plain text. Two of the branches below (tp_half / trail) were also dead:
    the executor renamed those actions to scale_out / breakeven and the message
    layer was never updated, so the handlers never matched.
    """
    kind = action.get("type")
    if kind in ("tp_half", "scale_out"):
        return t("tg_scale_out" if kind == "scale_out" else "tg_tp_half",
                 symbol=action["symbol"], qty=action["qty"],
                 price=action["price"], pnl=action.get("pnl", 0),
                 tranche=action.get("tranche", 1))
    if kind == "tp_full":
        return t("tg_tp_full", symbol=action["symbol"], qty=action["qty"],
                 price=action["price"], pnl=action.get("pnl", 0))
    if kind in ("trail", "breakeven"):
        return t("tg_trail", symbol=action["symbol"], new_stop=action["new_stop"])
    if kind == "cancel_stale":
        return t("tg_cancel_stale", symbol=action["symbol"],
                 age_min=action.get("age_min", 0))
    if kind == "edit_stop":
        return t("tg_edit_stop", symbol=action["symbol"],
                 old=float(action.get("old") or 0), new=float(action.get("new") or 0))
    if kind in ("stop_hit", "stop_hit_bracket"):
        return t("tg_stop_hit", symbol=action["symbol"], qty=action["qty"],
                 pnl=action.get("pnl", 0), price=action["price"],
                 stop=action.get("stop", action["price"]))
    if kind in ("max_hold", "max_hold_bracket"):
        return t("tg_max_hold", symbol=action["symbol"], qty=action["qty"],
                 age_days=action.get("age_days", 0), pnl=action.get("pnl", 0))
    if kind == "tp_hit_bracket":
        # Bracket TP filled at broker — surface as a positive close.
        return t("tg_tp_half", symbol=action["symbol"], qty=action["qty"],
                 price=action["price"], pnl=action.get("pnl", 0))
    if kind == "manual_close":
        return t("tg_stop_hit", symbol=action["symbol"], qty=action["qty"],
                 pnl=action.get("pnl", 0), price=action["price"],
                 stop=action["price"])
    # Unknown type — a new action was added upstream without a branch here. Keep
    # the information, but never emit a raw dict: its repr carries "_" and "*"
    # that break Markdown, which is how this fallback was discovered at all.
    # Sorted keys so the same action always renders the same way.
    kv = " ".join(f"{k}={v}" for k, v in sorted(action.items()) if k != "type")
    return f"ℹ️ {_md_escape(str(kind))} {_md_escape(kv)}"
