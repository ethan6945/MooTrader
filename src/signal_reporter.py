"""
Signal Reporter — 盯盘信号推送模块
====================================

设计对齐市面盯盘工具（TradingView Alerts / 券商到价提醒）：
定时简报给"全景"，盘中监控给"异动"——没有异动就完全静默。

每个交易日的推送节奏:

  run_daily_brief("premarket")   08:30 ET  盘前简报（AI+搜索+支撑阶梯+TP）
  run_monitor()                  09:30-16:00 每 5 分钟盯盘扫描（事件驱动）
                                 突破/放量/VWAP翻转/RSI极值/评分转向 → 即时推送
                                 每类事件带冷却期，同一信号不重复轰炸
  run_daily_brief("open1h")      10:30 ET  开盘1小时复盘（修正盘前预判）
  run_daily_brief("close")       16:10 ET  收盘对账（预测 vs 实际 + 归因）

旧模式（保留，手动可用）:

  run_premarket()   盘前完整信号卡片（15-min K线 + 技术指标 + AI 评分）
  run_intraday()    全 watchlist 合并一条压缩快讯（纯技术，无 AI）

Watchlist 管理（独立于交易 watchlist）:
  add_ticker(sym)      加入信号 watchlist
  remove_ticker(sym)   移除
  load_watchlist()     查看当前列表

CLI（在项目根目录运行）:
  python -m src.main signal list
  python -m src.main signal add NVDA
  python -m src.main signal remove NVDA

为什么不用 moo-trader 的评分模型:
  indicators.py 的 EMA20/50、ADX14 是为 daily/1H swing 校准的，
  在 5-min 上指标严重滞后。这里改用短线参数:
    RSI(7)         →  7×5min = 35 min 响应
    MACD(5/13/5)   →  最慢 EMA 65 min
    EMA9/21        →  45 / 105 min
    VWAP(日内)      →  当日成交量加权均价，日内基准线
    Stoch(9,3)     →  45 min K 线
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

import pandas as pd
from moomoo import KLType

from .config import settings
from . import ai, news_fetcher, notifier
from .moo_client import client as _moo_client


# ─── Context helpers (macro + sector + insider + pre-market) ──────────────────
# Wiring these into the premarket card lets the trader see at a glance:
#   • Is the broader market risk-on or risk-off (VIX + SPY)?
#   • Is this name leading or lagging its sector?
#   • Is earnings imminent?
#   • Liquidity OK?
#   • Pre-market price action?
#   • Recent insider activity?

# sector → ETF lookup — now shared with ml.features; import the single source
# of truth from src.sector so the signal card and the ML feature use identical
# ETF picks.
from .sector import SECTOR_TO_ETF as _SECTOR_TO_ETF  # noqa: E402


def _vix_label(vix: float) -> str:
    """Color-coded VIX bucket label. Matches the live risk_manager bands:
    25 = halve sizes, 35 = quarter sizes."""
    if vix > 35:
        return "🔴 crisis"
    if vix > 25:
        return "🟡 elevated"
    if vix > 20:
        return "🟡 cautious"
    return "🟢 benign"


def _is_premarket_now() -> bool:
    """True when current NY time is in 04:00-09:30 ET (US pre-market window).

    Why this matters: the broker's snapshot `last_price` does NOT refresh during
    pre-market for most subscription tiers — it still holds yesterday's regular
    session close until 9:30 ET. If we display `last_price` as "current price"
    at 08:30 ET, the card shows yesterday's close + yesterday's daily change,
    which doesn't match what the user observes in actual pre-market trading.
    Solution: in this window, prefer `pre_price` / compute chg vs prev_close.
    """
    import pytz
    from datetime import datetime
    now = datetime.now(pytz.timezone("America/New_York"))
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 4 * 60 <= minutes < 9 * 60 + 30


def _live_snap(c, sym: str) -> dict | None:
    """Return {price, prev, chg_pct, src} where src ∈ {'pre','last'}.

    In pre-market hours: use snapshot.pre_price (today's last pre-market trade)
    and compute chg vs prev_close_price. Otherwise: use snapshot.last_price.
    Returns None when prev_close is missing or chosen price is unusable.
    """
    try:
        snap = c.get_snapshot(sym)
        prev = float(snap.get("prev_close_price") or 0)
        if prev <= 0:
            return None
        if _is_premarket_now():
            pre = float(snap.get("pre_price") or 0)
            if pre > 0:
                return {"price": pre, "prev": prev,
                        "chg_pct": round((pre / prev - 1) * 100, 2),
                        "src": "pre"}
        last = float(snap.get("last_price") or 0)
        if last <= 0:
            return None
        return {"price": last, "prev": prev,
                "chg_pct": round((last / prev - 1) * 100, 2),
                "src": "last"}
    except Exception as e:
        log.debug("snapshot %s failed: %s", sym, e)
        return None


def _snap_chg(c, sym: str) -> dict | None:
    """Return a snapshot dict {last, prev, chg_pct} for `sym` or None.

    `last` is the *user-visible* price — pre_price during pre-market window,
    last_price otherwise. Key name kept as 'last' for legacy callers.
    """
    s = _live_snap(c, sym)
    if s is None:
        return None
    return {"last": s["price"], "prev": s["prev"], "chg_pct": s["chg_pct"]}


def _sector_snap(c, ticker: str) -> dict | None:
    """Look up ticker's sector and return that sector ETF's snapshot.

    None when ticker has no sector mapping or ETF data unavailable.
    """
    from .sector import get_sector
    sector = get_sector(ticker)
    etf = _SECTOR_TO_ETF.get(sector)
    if not etf:
        return None
    snap = _snap_chg(c, etf)
    if snap is None:
        return None
    snap.update({"etf": etf, "sector": sector})
    return snap


def _earnings_info(symbol: str) -> dict | None:
    """Days-until-next-earnings + ISO date. Uses the cache in earnings.py."""
    from datetime import date
    from .earnings import get_next_earnings
    next_date = get_next_earnings(symbol)
    if not next_date:
        return None
    try:
        d = date.fromisoformat(next_date)
    except ValueError:
        return None
    return {"date": next_date, "days_until": (d - date.today()).days}


def _premarket(symbol: str) -> dict | None:
    """yfinance pre-market quote, available 04:00–09:30 ET only.

    yfinance keys: preMarketPrice / preMarketChangePercent. Returns None when
    outside the pre-market window or when the field isn't populated.
    """
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        pre_price = info.get("preMarketPrice")
        pre_change = info.get("preMarketChangePercent")
        if pre_price and pre_change is not None:
            return {"price": float(pre_price),
                    "change_pct": float(pre_change)}
    except Exception as e:
        log.debug("pre-market fetch %s failed: %s", symbol, e)
    return None


def _insider_30d(symbol: str) -> dict | None:
    """Last-30-day insider activity from yfinance.

    Returns {buys, sells, buy_value, sell_value, net_value}.
    None if no data (ETFs, mutual funds, missing).
    """
    try:
        import yfinance as yf
        from datetime import date, timedelta
        trans = yf.Ticker(symbol).insider_transactions
        if trans is None or trans.empty:
            return None
        cutoff = pd.to_datetime(date.today() - timedelta(days=30))
        # yfinance's "Start Date" can be datetime or string — be tolerant.
        trans = trans.copy()
        trans["Start Date"] = pd.to_datetime(trans["Start Date"], errors="coerce")
        recent = trans[trans["Start Date"] >= cutoff]
        if recent.empty:
            return {"buys": 0, "sells": 0, "buy_value": 0.0,
                    "sell_value": 0.0, "net_value": 0.0}
        buys_mask = recent["Transaction"].astype(str).str.contains(
            "Purchase", case=False, na=False)
        sells_mask = recent["Transaction"].astype(str).str.contains(
            "Sale", case=False, na=False)
        buy_val = float(recent.loc[buys_mask, "Value"].sum() or 0)
        sell_val = float(recent.loc[sells_mask, "Value"].sum() or 0)
        return {
            "buys": int(buys_mask.sum()),
            "sells": int(sells_mask.sum()),
            "buy_value": buy_val,
            "sell_value": sell_val,
            "net_value": buy_val - sell_val,
        }
    except Exception as e:
        log.debug("insider fetch %s failed: %s", symbol, e)
        return None


def _build_context_block(ticker_chg_pct: float, vix: float | None,
                         spy: dict | None, sector: dict | None,
                         earnings: dict | None, premkt: dict | None,
                         insider: dict | None, spread_pct: float | None
                         ) -> str:
    """Assemble the 'Macro & Context' multi-line block. Lines with no data
    are omitted so the card stays compact for low-info names."""
    lines: list[str] = []

    # Line 1 — VIX state + SPY chg + ticker vs SPY relative strength.
    vix_part = f"VIX {vix:.1f} {_vix_label(vix)}" if vix is not None else "VIX n/a"
    if spy is not None:
        rs = ticker_chg_pct - spy["chg_pct"]
        rs_tag = "强势" if rs > 0.5 else ("弱势" if rs < -0.5 else "持平")
        spy_part = f"SPY {spy['chg_pct']:+.2f}%"
        rs_part = f"vs SPY {rs:+.2f}% {rs_tag}"
        lines.append(f"📊 {vix_part} | {spy_part} | {rs_part}")
    else:
        lines.append(f"📊 {vix_part}")

    # Line 2 — Earnings countdown (always show if data exists).
    if earnings is not None:
        d = earnings["days_until"]
        if 0 <= d <= 5:
            emoji = "⚠️"   # tight window — caller may want to skip the trade
        elif d < 0:
            emoji = "🕓"   # past — should be refreshed by cache TTL
        else:
            emoji = "📅"
        lines.append(f"{emoji} Earnings: {d}d ({earnings['date']})")

    # Line 3 — Sector ETF strength + relative.
    if sector is not None:
        diff = ticker_chg_pct - sector["chg_pct"]
        if diff > 0.3:
            tag = "强势"
        elif diff < -0.3:
            tag = "弱势"
        else:
            tag = "同步"
        lines.append(
            f"🏭 Sector {sector['etf']}({sector['sector']}) "
            f"{sector['chg_pct']:+.2f}% — vs sector {diff:+.2f}% {tag}"
        )

    # Line 4 — Liquidity + pre-market action.
    parts: list[str] = []
    if spread_pct is not None and spread_pct > 0:
        if spread_pct < 0.05:
            qual = "优"
        elif spread_pct < 0.15:
            qual = "良"
        else:
            qual = "⚠ 差"
        parts.append(f"spread {spread_pct:.2f}% {qual}")
    if premkt is not None:
        arrow = "📈" if premkt["change_pct"] >= 0 else "📉"
        parts.append(f"Pre-mkt ${premkt['price']:.2f} {arrow}{premkt['change_pct']:+.2f}%")
    if parts:
        lines.append("💧 " + " | ".join(parts))

    # Line 5 — Insider 30d (only show if any activity).
    if insider is not None and (insider["buys"] + insider["sells"] > 0):
        if insider["net_value"] > 0:
            emoji, tag = "🟢", "净买入"
        elif insider["net_value"] < 0:
            emoji, tag = "🔴", "净卖出"
        else:
            emoji, tag = "⚪️", "持平"
        net_m = insider["net_value"] / 1e6
        lines.append(
            f"👔 Insider 30d: {emoji} {insider['buys']} buys / "
            f"{insider['sells']} sells — {tag} ${net_m:+.1f}M"
        )

    return "\n".join(lines)

log = logging.getLogger(__name__)

# ─── WATCHLIST FILE ───────────────────────────────────────────────────────────

_WL_FILE: Path = settings.root / "config" / "signal_watchlist.json"

COMPANY_NAMES: dict[str, str] = {
    "AAPL": "Apple",      "MSFT": "Microsoft",  "NVDA": "NVIDIA",
    "TSLA": "Tesla",      "AMZN": "Amazon",     "GOOGL": "Alphabet",
    "META": "Meta",       "AMD":  "AMD",         "INTC": "Intel",
    "SPY":  "S&P500",     "QQQ":  "纳指ETF",    "MU":   "Micron",
    "SNDK": "SanDisk",    "ARM":  "Arm",         "PLTR": "Palantir",
    "AVGO": "Broadcom",   "TSM":  "台积电",      "NFLX": "Netflix",
    "JPM":  "摩根大通",   "COIN": "Coinbase",    "UBER": "Uber",
    "SMH":  "半导体ETF",  "QCOM": "Qualcomm",   "DDOG": "Datadog",
    "DELL": "Dell",       "SWKS": "Skyworks",    "SOFI": "SoFi",
}


def load_watchlist() -> list[str]:
    """Load from JSON file; fall back to SIGNAL_WATCHLIST env var."""
    if _WL_FILE.exists():
        try:
            data = json.loads(_WL_FILE.read_text())
            tickers = [s.strip().upper() for s in data.get("tickers", []) if s.strip()]
            if tickers:
                return tickers
        except Exception as e:
            log.warning("signal_watchlist.json parse error: %s", e)
    # Fallback: env var
    return list(settings.signal_watchlist)


def _save_watchlist(tickers: list[str]) -> None:
    data = {
        "_comment": "Managed via: python -m src.main signal add/remove/list",
        "tickers": tickers,
    }
    _WL_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def add_ticker(sym: str) -> str:
    sym = sym.strip().upper()
    tickers = load_watchlist()
    if sym in tickers:
        return f"{sym} 已在信号 watchlist 中"
    tickers.append(sym)
    _save_watchlist(tickers)
    return f"✅ {sym} 已加入信号 watchlist（共 {len(tickers)} 支）"


def remove_ticker(sym: str) -> str:
    sym = sym.strip().upper()
    tickers = load_watchlist()
    if sym not in tickers:
        return f"{sym} 不在信号 watchlist 中"
    tickers.remove(sym)
    _save_watchlist(tickers)
    return f"🗑 {sym} 已从信号 watchlist 移除（剩余 {len(tickers)} 支）"


def list_tickers() -> str:
    tickers = load_watchlist()
    if not tickers:
        return "信号 watchlist 为空"
    names = [f"{t}({COMPANY_NAMES.get(t, t)})" for t in tickers]
    return "📋 信号 watchlist:\n" + "\n".join(f"  {i+1}. {n}" for i, n in enumerate(names))


# ─── SHORT-TERM INDICATORS (5-min / 15-min optimized) ────────────────────────

def _rsi(close: pd.Series, period: int = 7) -> float:
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss
    val   = (100 - 100 / (1 + rs)).iloc[-1]
    return round(float(val), 1)


def _macd(close: pd.Series, fast=5, slow=13, signal=5) -> dict:
    ema_f = close.ewm(span=fast,   adjust=False).mean()
    ema_s = close.ewm(span=slow,   adjust=False).mean()
    m     = ema_f - ema_s
    sig   = m.ewm(span=signal, adjust=False).mean()
    hist  = m - sig
    return {
        "hist":       round(float(hist.iloc[-1]), 4),
        "cross_up":   bool(len(hist) >= 2 and hist.iloc[-2] < 0 and hist.iloc[-1] > 0),
        "cross_down": bool(len(hist) >= 2 and hist.iloc[-2] > 0 and hist.iloc[-1] < 0),
        "above":      bool(hist.iloc[-1] > 0),
    }


def _ema(close: pd.Series, period: int) -> float:
    return round(float(close.ewm(span=period, adjust=False).mean().iloc[-1]), 4)


def _bollinger(close: pd.Series, period=20) -> dict:
    sma   = close.rolling(period).mean()
    std   = close.rolling(period).std()
    upper = sma + 2 * std
    lower = sma - 2 * std
    price = float(close.iloc[-1])
    cu, cl = float(upper.iloc[-1]), float(lower.iloc[-1])
    pct_b  = (price - cl) / (cu - cl) if (cu - cl) != 0 else 0.5
    return {
        "pct_b":    round(pct_b, 3),
        "breakout": "up" if price > cu else ("down" if price < cl else "inside"),
    }


def _stochastic(high: pd.Series, low: pd.Series, close: pd.Series, k=9, d=3) -> dict:
    """Stochastic %K/%D with relaxed cross thresholds.

    Original logic only fired cross_up when k < 35 / cross_down when k > 65.
    Audit (2026-05-28): in trending markets, valid stoch crosses commonly occur
    above 50 — restricting to <35 / >65 silently skipped half the signals.
    Loosened to <50 / >50 so we catch in-trend rotations too.
    """
    ll  = low.rolling(k).min()
    hh  = high.rolling(k).max()
    kv  = 100 * (close - ll) / (hh - ll + 1e-9)
    dv  = kv.rolling(d).mean()
    k_n = round(float(kv.iloc[-1]), 1)
    d_n = round(float(dv.iloc[-1]), 1)
    kp  = float(kv.iloc[-2]) if len(kv) >= 2 else k_n
    dp  = float(dv.iloc[-2]) if len(dv) >= 2 else d_n
    return {
        "k":          k_n,
        "d":          d_n,
        "cross_up":   bool(kp < dp and k_n > d_n and k_n < 50),
        "cross_down": bool(kp > dp and k_n < d_n and k_n > 50),
        "oversold":   k_n < 20,
        "overbought": k_n > 80,
    }


def _vwap_intraday(df: pd.DataFrame) -> float:
    """Rolling 20-bar VWAP — works on any timeframe."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    pv = typical * df["volume"]
    return round(float(
        pv.rolling(20).sum().iloc[-1] / df["volume"].rolling(20).sum().iloc[-1]
    ), 2)


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period=14) -> float:
    """Wilder ATR — the same one the trader sizes and stops against.

    This was a rolling mean of true range. That is a different number from
    ta.atr by a median 8% on real bars, so the report showed the operator one
    ATR while the bot acted on another.
    """
    import pandas_ta_classic as _ta
    v = _ta.atr(high, low, close, length=period)
    return round(float(v.iloc[-1]), 4) if v is not None and len(v) else 0.0


def _volume_surge(volume: pd.Series, lookback=20) -> dict:
    avg   = float(volume.rolling(lookback).mean().iloc[-1])
    cur   = float(volume.iloc[-1])
    ratio = round(cur / avg, 2) if avg > 0 else 1.0
    return {"ratio": ratio, "surge": ratio >= 1.8}


def _momentum(close: pd.Series, period=6) -> float:
    if len(close) < period + 1:
        return 0.0
    return round((float(close.iloc[-1]) / float(close.iloc[-1 - period]) - 1) * 100, 2)


def _sr(df_higher: pd.DataFrame, lookback=10) -> dict:
    """Support / resistance from higher timeframe (daily) data."""
    r = df_higher.tail(lookback)
    return {
        "support":    round(float(r["low"].min()), 2),
        "resistance": round(float(r["high"].max()), 2),
    }


# ─── SCORING (0-10 buy / 0-10 sell) ──────────────────────────────────────────

ACTION_EMOJI = {
    "强烈买入": "🚀", "买入": "🟢", "观望": "🟡",
    "卖出": "🔴",    "强烈卖出": "❌",
}

RISK_EMOJI = {"低": "🟢", "中": "🟡", "高": "🔴"}
CONF_EMOJI = {"高": "💪", "中": "✋", "低": "⚠️"}


def _score(price, rsi, macd, ema9, ema21, boll, vol, stoch, vwap, mom
           ) -> tuple[int, int, list[str]]:
    buy, sell, alerts = 0, 0, []

    # RSI(7) — 快速超买超卖
    if rsi <= 22:     buy += 2; alerts.append(f"RSI超卖({rsi})")
    elif rsi <= 33:   buy += 1
    elif rsi >= 78:   sell += 2; alerts.append(f"RSI超买({rsi})")
    elif rsi >= 67:   sell += 1

    # MACD(5/13/5)
    if macd["cross_up"]:      buy += 2;  alerts.append("MACD金叉")
    elif macd["cross_down"]:  sell += 2; alerts.append("MACD死叉")
    elif macd["above"]:       buy += 1
    else:                     sell += 1

    # EMA9/21 短期趋势
    if ema9 > ema21 and price > ema9:    buy  += 1
    elif ema9 < ema21 and price < ema9:  sell += 1

    # VWAP — 日内机构基准
    if price > vwap * 1.003:    buy  += 1
    elif price < vwap * 0.997:  sell += 1

    # Bollinger Bands
    # 2026-05-28: relaxed pct_b thresholds from 0.08/0.92 → 0.15/0.85.
    # Original 0.08 needed price almost touching the band — fired so rarely
    # that the score signal was effectively binary. 0.15/0.85 captures the
    # "near band" zone where mean-reversion is statistically meaningful.
    if boll["breakout"] == "up":    buy += 2;  alerts.append("BB突破上轨")
    elif boll["breakout"] == "down": sell += 2; alerts.append("BB跌破下轨")
    elif boll["pct_b"] < 0.15:  buy  += 1
    elif boll["pct_b"] > 0.85:  sell += 1

    # Volume surge
    if vol["surge"]:
        if mom > 0:   buy += 1;  alerts.append(f"放量{vol['ratio']}x↑")
        else:         sell += 1; alerts.append(f"放量{vol['ratio']}x↓")

    # Stochastic(9,3)
    if stoch["cross_up"]:     buy  += 1; alerts.append("Stoch底部金叉")
    elif stoch["cross_down"]: sell += 1; alerts.append("Stoch顶部死叉")
    elif stoch["oversold"]:   buy  += 1
    elif stoch["overbought"]: sell += 1

    # 6-bar 动能
    if mom >= 1.5:    buy  += 1
    elif mom <= -1.5: sell += 1

    return min(buy, 10), min(sell, 10), alerts


def _action_from_scores(buy: int, sell: int) -> str:
    net = buy - sell
    if net >= 5:    return "强烈买入"
    if net >= 3:    return "买入"
    if net <= -5:   return "强烈卖出"
    if net <= -3:   return "卖出"
    return "观望"


def _score_bar(score: int) -> str:
    s     = max(1, min(10, int(score)))
    emoji = "🟩" if s >= 7 else ("🟥" if s <= 4 else "🟨")
    return emoji * s + "⬜" * (10 - s) + f" *{s}/10*"


# ─── AI (盘前 only) ──────────────────────────────────────────────────────────
# These were _call_ai / _ai_signal / _call_ai_text. Every one of
# them calls ai.generate(), which dispatches through ai.active_provider() —
# DeepSeek since 2026-07-22. The names said Gemini and the behaviour was
# DeepSeek, which is how the owner came to believe a provider was still in
# use that had been gone for a month.

def _call_ai(prompt: str) -> Optional[dict]:
    if not ai.has_key():
        return None
    try:
        # Low temperature → stable, repeatable decisions (less run-to-run swing in
        # score/action). Default (~1.0) made the same setup swing e.g. 5/10 ↔ 8/10.
        text, model_name = ai.generate(prompt, temperature=0.15)
        raw = re.sub(r"```json|```", "", text.strip()).strip()
        result = json.loads(raw)
        result["_model"] = model_name
        return result
    except Exception:
        return None


def _ai_signal(tech: dict, ticker_news: list, macro_news: list) -> dict:
    news_text  = "\n".join(
        f"- {n['title'][:80]}: {n['content'][:100]}" for n in ticker_news[:4]
    ) or "无近期新闻"
    macro_text = "\n".join(f"- {n['title'][:70]}" for n in macro_news[:3]) or "无宏观新闻"

    prompt = f"""你是专业短线交易员（持仓 日内~2天）。基于 15min K线技术指标 + 近期新闻，给出今日/明日交易信号。

【{tech['ticker']} ({tech['company']})】 现价 ${tech['price']} | 日涨跌 {tech['chg1']:+.2f}%

技术指标 (15min K线):
- RSI(7)={tech['rsi']} | MACD(5/13/5) hist={tech['macd']['hist']:+.4f} (金叉={tech['macd']['cross_up']}, 死叉={tech['macd']['cross_down']})
- EMA9={tech['ema9']:.2f} / EMA21={tech['ema21']:.2f} | BB%B={tech['boll']['pct_b']} ({tech['boll']['breakout']})
- Stoch K={tech['stoch']['k']} D={tech['stoch']['d']} (超买={tech['stoch']['overbought']}, 超卖={tech['stoch']['oversold']})
- VWAP=${tech['vwap']} | 量比={tech['vol']['ratio']}x | 6-bar动能={tech['mom']:+.2f}%
- ATR={tech['atr']} | 支撑=${tech['sr']['support']} | 压力=${tech['sr']['resistance']}
- 技术评分: 买 {tech['buy_score']}/10 | 卖 {tech['sell_score']}/10
- 警报: {', '.join(tech['alerts']) or '无'}

近3天个股新闻（优先权高于技术面）:
{news_text}

近3天宏观新闻:
{macro_text}

评分 1-10: 9-10=极强买 | 7-8=强买 | 6=买 | 5=观望 | 4=卖 | 2-3=强卖 | 1=极强卖
财报前3天必须观望/日内。新闻强正催化→7+；强负催化→3以下。

严格返回 JSON（无代码块）:
{{"score":整数1-10,"action":"强烈买入/买入/观望/卖出/强烈卖出","hold_period":"日内/隔夜/2天",
  "entry_low":数字,"entry_high":数字,"target_1":数字,"target_2":数字,"stop_loss":数字,
  "reason":"35字内核心理由","news_impact":"20字内","catalyst":"20字内",
  "risk_level":"低/中/高","confidence":"高/中/低","summary":"40字内执行建议"}}"""

    result = _call_ai(prompt)
    if result:
        log.info("premarket AI [%s] %s → %s (%s/10)",
                 result.get("_model", "?"), tech["ticker"],
                 result.get("action"), result.get("score"))
        return result

    # Fallback: pure technical
    net   = tech["buy_score"] - tech["sell_score"]
    score = max(1, min(10, 5 + net // 2))
    p, atr_v = tech["price"], tech["atr"]
    action = _action_from_scores(tech["buy_score"], tech["sell_score"])
    return {
        "score": score, "action": action, "hold_period": "日内",
        "entry_low":  round(p - atr_v * 0.3, 2),
        "entry_high": round(p + atr_v * 0.3, 2),
        "target_1":   round(p + atr_v * 1.5, 2),
        "target_2":   round(p + atr_v * 2.5, 2),
        "stop_loss":  round(p - atr_v * 2.0, 2),
        "reason": "AI 不可用，仅技术面",
        "news_impact": "无", "catalyst": "未知",
        "risk_level": "中", "confidence": "低",
        "summary": "AI 暂不可用，参考技术指标判断。",
        "_model": "fallback",
    }


# ─── 盘前全面分析（完整卡片，带 AI）────────────────────────────────────────

def _ai_num(ai: dict, key: str, default: float = 0.0) -> float:
    """Get a numeric field from AI response, treating None/missing as default."""
    v = ai.get(key)
    return float(v) if v is not None else default


def _build_premarket_card(tech: dict, ai: dict,
                          context_block: str = "") -> str:
    score  = int(ai.get("score") or 5)
    act_e  = ACTION_EMOJI.get(ai.get("action", "观望"), "🟡")
    risk_e = RISK_EMOJI.get(ai.get("risk_level", "中"), "🟡")
    conf_e = CONF_EMOJI.get(ai.get("confidence", "中"), "✋")
    chg1   = tech.get("chg1") or 0.0
    arrow  = "📈" if chg1 >= 0 else "📉"
    # Tag the price source so the user knows whether this is regular-session
    # last or today's pre-market last. Critical at 08:30 ET where the broker's
    # last_price is stale and we fall back to pre_price.
    src    = tech.get("price_src", "last")
    src_tag = " 🕓盘前" if src == "pre" else ""
    # Context block is optional — empty string when nothing was fetchable.
    context_section = f"\n{context_block}\n" if context_block else ""

    return (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*{tech['ticker']}* {tech['company']} | "
        f"${tech['price']} {arrow} {chg1:+.2f}%{src_tag}\n"
        f"\n"
        f"{act_e} *{ai.get('action', '?')}*  {_score_bar(score)}\n"
        f"   {ai.get('reason', '')}\n"
        f"{context_section}"
        f"\n"
        f"⏱ 持仓 {ai.get('hold_period', '?')} | "
        f"{risk_e} 风险 {ai.get('risk_level', '?')} | "
        f"{conf_e} 信心 {ai.get('confidence', '?')}\n"
        f"\n"
        f"🎯 入场 ${_ai_num(ai, 'entry_low'):.2f} ~ ${_ai_num(ai, 'entry_high'):.2f}\n"
        f"📍 目标1 *${_ai_num(ai, 'target_1'):.2f}* | 目标2 *${_ai_num(ai, 'target_2'):.2f}*\n"
        f"🛑 止损 *${_ai_num(ai, 'stop_loss'):.2f}*\n"
        f"\n"
        f"📊 技术 买{tech['buy_score']}/10 | 卖{tech['sell_score']}/10\n"
        f"   RSI={tech['rsi']} · MACD={tech['macd']['hist']:+.3f} · BB%B={tech['boll']['pct_b']}\n"
        f"   VWAP=${tech['vwap']} · Vol {tech['vol']['ratio']}x · 动能{tech.get('mom', 0):+.2f}%\n"
        f"   支撑${tech['sr']['support']} · 压力${tech['sr']['resistance']} · ATR={tech['atr']}\n"
        f"   ⚡ {' · '.join(tech['alerts']) if tech['alerts'] else '无警报'}\n"
        f"\n"
        f"📰 {ai.get('news_impact') or '-'}\n"
        f"🔮 {ai.get('catalyst') or '-'}\n"
        f"💡 {ai.get('summary') or '-'}\n"
        f"🤖 {ai.get('_model', 'unknown')}"
    )


def _build_premarket_summary(results: list) -> str:
    scores     = [int(r["ai"].get("score", 5)) for r in results]
    strong_buy = sum(1 for s in scores if s >= 8)
    buy        = sum(1 for s in scores if 6 <= s < 8)
    hold       = sum(1 for s in scores if 4 < s < 6)
    sell       = sum(1 for s in scores if s <= 4)

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━",
        "*盘前汇总*",
        f"🚀 强烈买 {strong_buy} | 🟢 买 {buy} | 🟡 观望 {hold} | 🔴 卖/强卖 {sell}",
    ]
    top = [r for r in results if int(r["ai"].get("score", 5)) >= 7]
    if top:
        lines.append("⭐ *重点: * " + "  ".join(
            f"{r['tech']['ticker']}({r['ai']['score']})" for r in top
        ))
    fallback = [r["tech"]["ticker"] for r in results if r["ai"].get("_model") == "fallback"]
    if fallback:
        lines.append(f"⚠️ AI 不可用（纯技术）: {', '.join(fallback)}")
    lines += ["", "仅供参考，不构成投资建议，请严格执行止损。"]
    return "\n".join(lines)


# ─── 盘中快讯（压缩格式，无 AI）──────────────────────────────────────────────

def _build_intraday_line(sym: str, price: float, chg1: float,
                         buy: int, sell: int, rsi: float,
                         macd: dict, vol: dict, alerts: list) -> str:
    action = _action_from_scores(buy, sell)
    ae     = ACTION_EMOJI.get(action, "🟡")
    arrow  = "📈" if chg1 >= 0 else "📉"

    macd_sym = "↑" if macd["cross_up"] else ("↓" if macd["cross_down"] else ("▲" if macd["above"] else "▼"))
    vol_str  = f" Vol{vol['ratio']}x" if vol["surge"] else ""
    alert_str = " · ".join(alerts[:2])  # 最多 2 个警报，保持紧凑
    extra    = f"  ⚡{alert_str}" if alert_str else ""

    return (
        f"{ae} *{sym}* ${price} {arrow}{chg1:+.1f}%  "
        f"买{buy}/卖{sell}  RSI={rsi} MACD{macd_sym}{vol_str}{extra}"
    )


def _build_intraday_msg(lines: list[str], ts: str) -> str:
    header = f"📡 *盘中快讯* | {ts}\n5-min K线 · 纯技术 · 无 AI"
    return header + "\n━━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines)


# ─── FETCH HELPERS ────────────────────────────────────────────────────────────

def _today_session_hl(df: pd.DataFrame) -> dict:
    """今天已完成 bars 的最高/最低（排除最后一根可能未收完的 bar）。

    盯盘监控用它判定「突破日内高点 / 跌破日内低点」。要求今天至少 4 根已完成
    bar 才给出高低点——开盘头几分钟只有一两根 bar 时，价格越过它们太容易，
    会造成假突破轰炸。
    """
    try:
        if "time_key" not in df.columns:
            return {}
        today = _ny_now().strftime("%Y-%m-%d")
        day = df[df["time_key"].astype(str).str.startswith(today)]
        if len(day) < 5:
            return {}
        prior = day.iloc[:-1]
        return {"high": round(float(prior["high"].max()), 2),
                "low":  round(float(prior["low"].min()), 2)}
    except Exception:
        return {}


def _fetch_tech(c, sym: str, ktype: KLType, bars: int,
                df_daily_cache: dict) -> Optional[dict]:
    """Fetch klines and compute all technical indicators. Returns tech dict or None."""
    try:
        df = c.get_kline(sym, bars=bars, ktype=ktype)
    except Exception as e:
        log.warning("signal_reporter: kline %s %s failed: %s", sym, ktype, e)
        return None

    if df is None or len(df) < 40:
        return None

    # Daily kline for S/R (fetch once and cache per run)
    if sym not in df_daily_cache:
        try:
            df_daily_cache[sym] = c.get_kline(sym, bars=30, ktype=KLType.K_DAY)
        except Exception:
            df_daily_cache[sym] = None
    df_d = df_daily_cache[sym]

    close = df["close"]
    high  = df["high"]
    low   = df["low"]
    vol   = df["volume"]

    # Use real-time snapshot for current price and daily change.
    # request_history_kline only returns completed bars, so close.iloc[-1] may be
    # 5–15 min stale during trading hours. Snapshot gives the live last price.
    # _live_snap auto-swaps to pre_price during 04:00-09:30 ET — the broker's
    # last_price field is stuck on yesterday's close in that window, which
    # used to make the premarket card show yesterday's price + yesterday's chg.
    price = round(float(close.iloc[-1]), 2)  # fallback
    chg1  = 0.0
    price_src = "kline"
    snap_info = _live_snap(c, sym)
    if snap_info is not None:
        price = round(snap_info["price"], 2)
        chg1 = snap_info["chg_pct"]
        price_src = snap_info["src"]
        if price_src == "pre":
            log.info("signal_reporter: %s using pre-market price $%.2f (%+.2f%%)",
                     sym, price, chg1)
    elif df_d is not None and len(df_d) >= 2:
        # snapshot unavailable → fallback to daily kline for chg
        prev_close = float(df_d["close"].iloc[-1])
        if prev_close > 0:
            chg1 = round((price / prev_close - 1) * 100, 2)

    chg5 = round((price / float(df_d["close"].iloc[-6]) - 1) * 100, 2) \
           if (df_d is not None and len(df_d) >= 6) else 0.0

    rsi_v   = _rsi(close)
    macd_v  = _macd(close)
    ema9    = _ema(close, 9)
    ema21   = _ema(close, 21)
    boll_v  = _bollinger(close)
    stoch_v = _stochastic(high, low, close)
    vwap_v  = _vwap_intraday(df)
    atr_v   = _atr(high, low, close)
    vol_v   = _volume_surge(vol)
    mom_v   = _momentum(close)
    sr_v    = _sr(df_d) if df_d is not None else {"support": 0, "resistance": 0}
    # Intraday swing S/R from the bars themselves (last 20) — tight, scalp-relevant
    # levels, unlike the wide daily 30-bar range. Critical for ultra-short entries.
    isr_v   = _sr(df, lookback=20)

    buy_s, sell_s, alerts = _score(price, rsi_v, macd_v, ema9, ema21,
                                   boll_v, vol_v, stoch_v, vwap_v, mom_v)

    return {
        "ticker":     sym,
        "company":    COMPANY_NAMES.get(sym, sym),
        "price":      price,
        "price_src":  price_src,   # 'pre' | 'last' | 'kline' — surfaced in card header
        "chg1":       chg1,
        "chg5":       chg5,
        "rsi":        rsi_v,
        "macd":       macd_v,
        "ema9":       ema9,
        "ema21":      ema21,
        "boll":       boll_v,
        "stoch":      stoch_v,
        "vwap":       vwap_v,
        "atr":        atr_v,
        "vol":        vol_v,
        "mom":        mom_v,
        "sr":         sr_v,
        "isr":        isr_v,   # intraday swing S/R (tight) — used by the brief card
        "day_hl":     _today_session_hl(df),  # 今日已完成 bars 高低 — 盯盘突破判定
        "buy_score":  buy_s,
        "sell_score": sell_s,
        "alerts":     alerts,
    }


# ─── 当日简报（盘前 08:30 + 开盘1小时 10:30 复盘）────────────────────────────
# 用户需求 (2026-06-12)：每个交易日对 watchlist 两次推送——
#   ① 盘前简报：宏观催化 + 盘前动态 + 开盘方向预判 + 支撑阶梯 + 日内多空 TP
#   ② 开盘1小时复盘：结合①的盘前资料 + 开盘后实际走势，修正方向与 TP
# 格式刻意压缩成 Telegram 一屏可读的紧凑卡片，只留可操作的关键信息。
# 盘前简报会存盘到 data/daily_analysis/，10:30 复盘时读回来喂给 AI 对比。

_DAILY_DIR = settings.root / "data" / "daily_analysis"

_BRIEF_FORMAT = """输出格式（Telegram 紧凑卡片，纯文本+emoji，总长 ≤900 字，严格按此结构，不要代码块/表格/多余寒暄）:

🌐 宏观: <今日最核心的1-2条宏观/行业催化，及对市场情绪的影响，≤60字>
📊 动态: <现价/盘前价与涨跌幅、盘前高低点(如有)、主力情绪，≤50字>
🧭 开盘预判: <高开上升 / 低开回调 / 震荡，一句理由>
🎯 支撑阶梯:
  ① $<价> 短线交战区
  ② $<价> 较安全买点
  ③ $<价> 黄金抄底价
⚡ 日内TP:
  📈 做多: TP1 $<价> · TP2 $<价>
  📉 做空: TP1 $<价> · TP2 $<价>
🛠 提醒: <今日最核心的一句风控/操盘提示，≤40字>"""

_CLOSE_FORMAT = """输出格式（Telegram 紧凑卡片，纯文本+emoji，总长 ≤1000 字，严格按此结构，不要代码块/表格/多余寒暄）:

📈 今日走势: <开盘价→日内高低→收盘价与涨跌幅，一句话概括全天形态，≤60字>
🔍 预测对账:
  🌅 盘前预判: <当时的开盘方向预判> → <✅达标/⚠️部分/❌偏差> <一句实际对比>
  🎯 支撑/TP: <盘前给的关键支撑位和TP是否被触及/守住，逐个简短判定，≤80字>
  🔄 盘中修正: <10:30复盘的修正是否更准，≤40字>
📊 达标结论: <✅基本达标 / ⚠️部分达标 / ❌失准> + <一句总评，哪里对哪里错>
🧠 为什么这么走: <今天盘面按此趋势运行的根本原因——宏观/资金/筹码/消息，2-3句，≤100字>
💡 明日启示: <对明天操作最有价值的一条经验，≤50字>"""


def _call_ai_text(prompt: str, use_search: bool = True) -> Optional[str]:
    """像 _call_ai，但返回纯文本（简报卡片）。曾在 Gemini 上启用
    google_search grounding 让模型自己补全最新宏观/个股新闻（不支持 tools 的
    模型自动降级为无搜索）。DeepSeek 无 grounding，按纯文本调用。"""
    if not ai.has_key():
        return None
    try:
        return ai.generate_text(prompt, temperature=0.3, search=use_search)
    except Exception:
        return None


def _weekly_sr(c, sym: str) -> dict:
    """周线级别大区间（52 周高低 + 近 10 周低点），给支撑阶梯当锚。"""
    try:
        df_w = c.get_kline(sym, bars=52, ktype=KLType.K_WEEK)
        if df_w is None or len(df_w) < 5:
            return {}
        return {
            "low_52w":  round(float(df_w["low"].min()), 2),
            "high_52w": round(float(df_w["high"].max()), 2),
            "low_10w":  round(float(df_w["low"].tail(10).min()), 2),
        }
    except Exception as e:
        log.debug("weekly kline %s failed: %s", sym, e)
        return {}


def _first_hour_stats(c, sym: str) -> dict:
    """开盘后第一小时的 5M 高/低/量（取当日 09:30 起的 bars）。"""
    try:
        df = c.get_kline(sym, bars=80, ktype=KLType.K_5M)
        if df is None or df.empty:
            return {}
        if "time_key" in df.columns:
            import pytz
            from datetime import datetime
            today = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d")
            day = df[df["time_key"].astype(str).str.startswith(today)]
            if not day.empty:
                df = day
        df = df.tail(13)  # 最多一小时多一点
        return {
            "high": round(float(df["high"].max()), 2),
            "low":  round(float(df["low"].min()), 2),
            "vol":  int(df["volume"].sum()),
        }
    except Exception as e:
        log.debug("first hour stats %s failed: %s", sym, e)
        return {}


def _day_stats(c, sym: str) -> dict:
    """当日完整 OHLC（收盘复盘用）。取日线最后一根 bar。"""
    try:
        df_d = c.get_kline(sym, bars=2, ktype=KLType.K_DAY)
        if df_d is None or df_d.empty:
            return {}
        last = df_d.iloc[-1]
        prev_close = float(df_d["close"].iloc[-2]) if len(df_d) >= 2 else None
        out = {
            "open":  round(float(last["open"]), 2),
            "high":  round(float(last["high"]), 2),
            "low":   round(float(last["low"]), 2),
            "close": round(float(last["close"]), 2),
            "vol":   int(last["volume"]),
        }
        if prev_close:
            out["chg_pct"] = round((out["close"] / prev_close - 1) * 100, 2)
        return out
    except Exception as e:
        log.debug("day stats %s failed: %s", sym, e)
        return {}


def _daily_brief_data(c, sym: str) -> Optional[dict]:
    """采集一只股票做简报所需的全部数据。"""
    df_d_cache: dict = {}
    tech = _fetch_tech(c, sym, KLType.K_15M, 120, df_d_cache)
    if not tech:
        return None
    snap = _live_snap(c, sym)
    try:
        vix_value = c.get_vix()
    except Exception:
        vix_value = None
    spy_snap = _snap_chg(c, "SPY")
    return {
        "tech": tech,
        "snap": snap,
        "vix": vix_value,
        "spy": spy_snap,
        "weekly": _weekly_sr(c, sym),
        "premkt": _premarket(sym),
        "earnings": _earnings_info(sym),
    }


def _brief_prompt(sym: str, d: dict, phase: str,
                  prev_brief: Optional[str], first_hour: Optional[dict],
                  open1h_brief: Optional[str] = None,
                  day: Optional[dict] = None) -> str:
    tech = d["tech"]
    name = COMPANY_NAMES.get(sym, sym)
    premkt = d.get("premkt")
    premkt_line = (f"盘前 ${premkt['price']:.2f} ({premkt['change_pct']:+.2f}%)"
                   if premkt else "盘前数据不可用")
    spy = d.get("spy")
    spy_line = f"SPY {spy['chg_pct']:+.2f}%" if spy else "SPY n/a"
    vix_line = f"VIX {d['vix']:.1f}" if d.get("vix") else "VIX n/a"
    w = d.get("weekly") or {}
    earn = d.get("earnings")
    earn_line = f"下次财报 {earn['date']} ({earn['days_until']}天后)" if earn else "财报日期未知"

    if phase == "premarket":
        role = "现在是美股盘前。请生成今日盘前简报。"
        extra = ""
        fmt = _BRIEF_FORMAT
    elif phase == "open1h":
        role = "现在是开盘约1小时后。请结合下面的【盘前简报】与开盘后实际走势，重新分析一次：确认或修正开盘预判、支撑阶梯与日内 TP。"
        fh = first_hour or {}
        extra = (f"\n开盘第一小时: 高 ${fh.get('high','?')} / 低 ${fh.get('low','?')}"
                 f" / 量 {fh.get('vol','?')}\n"
                 f"【盘前简报】:\n{prev_brief or '（盘前简报缺失）'}\n")
        fmt = _BRIEF_FORMAT
    else:  # close — 收盘复盘对账
        role = ("现在已收盘。请把今天的【盘前简报】和【盘中复盘】里的预判（开盘方向/支撑阶梯/日内TP）"
                "与下面的当日实际 OHLC 逐项对账：判定是否达标，并解释今天盘面为什么按这个趋势运行。"
                "客观打分，错了就直说，不要为预测找借口。")
        dd = day or {}
        extra = (f"\n当日实际: 开 ${dd.get('open','?')} 高 ${dd.get('high','?')}"
                 f" 低 ${dd.get('low','?')} 收 ${dd.get('close','?')}"
                 f" ({dd.get('chg_pct','?')}%) 量 {dd.get('vol','?')}\n"
                 f"【盘前简报】:\n{prev_brief or '（缺失）'}\n"
                 f"【盘中复盘(10:30)】:\n{open1h_brief or '（缺失）'}\n")
        fmt = _CLOSE_FORMAT

    return f"""你是美股日内技术与新闻首席分析师。{role}
如可用，请用搜索工具查最新的今日宏观新闻（CPI/PPI/美联储/地缘）与 {sym} 个股新闻。

【{sym} {name}】实时数据（券商源）:
- 现价 ${tech['price']} 当日 {tech['chg1']:+.2f}% | {premkt_line}
- {vix_line} | {spy_line} | {earn_line}
- 15M: RSI(7)={tech['rsi']} MACD={tech['macd']['hist']:+.4f} VWAP=${tech['vwap']} 量比{tech['vol']['ratio']}x ATR={tech['atr']}
- 日内S/R: ${tech['isr']['support']} / ${tech['isr']['resistance']}
- 日线30日区间: ${tech['sr']['support']} ~ ${tech['sr']['resistance']}
- 周线: 52周 ${w.get('low_52w','?')}~${w.get('high_52w','?')} | 近10周低点 ${w.get('low_10w','?')}
{extra}
支撑阶梯与 TP 必须有技术依据（前高低点/VWAP/整数关口/ATR），点位贴合上面真实数据。
{fmt}"""


def _brief_file(sym: str) -> Path:
    import pytz
    from datetime import datetime
    today = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d")
    return _DAILY_DIR / f"{today}_{sym}.json"


_BRIEF_TITLES = {
    "premarket": "🌅 *盘前简报*",
    "open1h":    "🔄 *开盘1小时复盘*",
    "close":     "🏁 *收盘对账*",
}


def run_daily_brief(phase: str = "premarket") -> None:
    """当日简报。phase='premarket'（08:30）/ 'open1h'（10:30 复盘）/
    'close'（16:10 收盘对账：预测 vs 实际 + 走势归因）。"""
    watchlist = load_watchlist()
    if not watchlist:
        log.info("daily brief: watchlist empty")
        return
    import pytz
    from datetime import datetime
    ts = datetime.now(pytz.timezone("America/New_York")).strftime("%m-%d %H:%M ET")
    title = _BRIEF_TITLES.get(phase, phase)
    _DAILY_DIR.mkdir(parents=True, exist_ok=True)

    with _moo_client() as c:
        for sym in watchlist:
            try:
                d = _daily_brief_data(c, sym)
                if not d:
                    log.warning("daily brief: skip %s (data insufficient)", sym)
                    continue
                # 读回今天已存的简报（盘前/盘中），供后续 phase 对比。
                saved: dict = {}
                f = _brief_file(sym)
                if f.exists():
                    try:
                        saved = json.loads(f.read_text())
                    except Exception:
                        saved = {}
                prev_brief = saved.get("brief")
                first_hour = _first_hour_stats(c, sym) if phase == "open1h" else None
                day = _day_stats(c, sym) if phase == "close" else None
                prompt = _brief_prompt(sym, d, phase, prev_brief, first_hour,
                                       open1h_brief=saved.get("open1h_brief"),
                                       day=day)
                brief = _call_ai_text(prompt)
                if not brief:
                    log.warning("daily brief: AI unavailable for %s", sym)
                    notifier.send(f"⚠️ {sym} 简报生成失败（AI 不可用）")
                    continue
                card = f"{title} {sym} | {ts}\n━━━━━━━━━━━━━━━━━━━━━━\n{brief}"
                notifier.send(card)
                # 存盘：premarket 覆盖新建当日文件；open1h/close 追加到同一文件。
                if phase == "premarket":
                    saved = {"ts": ts, "price": d["tech"]["price"], "brief": brief}
                elif phase == "open1h":
                    saved["open1h_brief"] = brief
                else:
                    saved["close_brief"] = brief
                    saved["day"] = day
                f.write_text(json.dumps(saved, ensure_ascii=False, indent=2))
                log.info("daily brief [%s] %s sent", phase, sym)
            except Exception as e:
                log.exception("daily brief %s failed: %s", sym, e)
            time.sleep(0.3)


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

def run_premarket() -> None:
    """盘前全面分析：08:30 ET 触发，15-min K线 + AI + 完整卡片。"""
    watchlist = load_watchlist()
    if not watchlist:
        log.info("signal_reporter: watchlist empty")
        return

    log.info("signal_reporter premarket: %s", watchlist)
    import pytz
    from datetime import datetime
    now = datetime.now(pytz.timezone("America/New_York"))
    ts  = now.strftime("%Y-%m-%d %H:%M ET")

    notifier.send(
        f"🌅 *盘前分析* | {ts}\n"
        f"15-min K线 · AI · 近3天新闻\n"
        f"共 {len(watchlist)} 支，按 AI 评分高→低"
    )

    macro_news = news_fetcher.fetch_macro_news()
    results    = []
    df_d_cache: dict = {}

    with _moo_client() as c:
        # Fetch macro context ONCE per premarket — SPY snapshot + VIX. These
        # are shared across every ticker card so each one shows the same
        # market backdrop.
        spy_snap = _snap_chg(c, "SPY")
        try:
            vix_value = c.get_vix()
            log.info("signal_reporter premarket: VIX=%.1f, SPY %s",
                     vix_value, f"{spy_snap['chg_pct']:+.2f}%" if spy_snap else "n/a")
        except Exception as e:
            log.warning("VIX fetch failed: %s — defaulting to 15.0", e)
            vix_value = 15.0

        for sym in watchlist:
            tech = _fetch_tech(c, sym, KLType.K_15M, 120, df_d_cache)
            if not tech:
                log.warning("signal_reporter: skip %s (data insufficient)", sym)
                continue
            ticker_news = news_fetcher.fetch_ticker_news(sym)
            # Macro & per-ticker context. yfinance fetches (pre-market, insider)
            # add ~1-2s/ticker; acceptable since premarket runs once/day.
            sector_data = _sector_snap(c, sym)
            earnings_data = _earnings_info(sym)
            premkt_data = _premarket(sym)
            insider_data = _insider_30d(sym)
            spread_pct = None
            try:
                spread_pct = c.get_spread_pct(sym)
            except Exception:
                pass
            context_block = _build_context_block(
                ticker_chg_pct=tech.get("chg1", 0) or 0,
                vix=vix_value,
                spy=spy_snap,
                sector=sector_data,
                earnings=earnings_data,
                premkt=premkt_data,
                insider=insider_data,
                spread_pct=spread_pct,
            )
            ai = _ai_signal(tech, ticker_news, macro_news)
            results.append({
                "tech": tech, "ai": ai,
                "context_block": context_block,
            })
            time.sleep(0.3)

    if not results:
        notifier.send("⚠️ 盘前分析：所有股票数据获取失败，请检查 OpenD 连接")
        return

    results.sort(key=lambda r: -int(r["ai"].get("score", 5)))
    for r in results:
        notifier.send(_build_premarket_card(
            r["tech"], r["ai"],
            context_block=r.get("context_block", "")))
    notifier.send(_build_premarket_summary(results))
    log.info("signal_reporter premarket done — %d cards", len(results))


def run_intraday() -> None:
    """盘中快讯：5-min K线，纯技术，无 AI，全 watchlist 合并一条消息。"""
    watchlist = load_watchlist()
    if not watchlist:
        return

    import pytz
    from datetime import datetime
    now = datetime.now(pytz.timezone("America/New_York"))
    ts  = now.strftime("%H:%M ET")

    log.info("signal_reporter intraday: %s", ts)
    lines      = []
    df_d_cache: dict = {}

    with _moo_client() as c:
        for sym in watchlist:
            tech = _fetch_tech(c, sym, KLType.K_5M, 80, df_d_cache)
            if not tech:
                continue
            line = _build_intraday_line(
                sym, tech["price"], tech["chg1"],
                tech["buy_score"], tech["sell_score"],
                tech["rsi"], tech["macd"], tech["vol"], tech["alerts"],
            )
            lines.append(line)
            time.sleep(0.2)

    if not lines:
        log.warning("signal_reporter intraday: no data for any ticker")
        return

    notifier.send(_build_intraday_msg(lines, ts))
    log.info("signal_reporter intraday done — %d tickers", len(lines))


# ─── 盘中盯盘监控（事件驱动，只在异动时提醒）──────────────────────────────────
# 概念对齐市面盯盘工具：scheduler 每 5 分钟扫一遍 watchlist → 检测事件（突破/
# 放量/VWAP 翻转/RSI 极值/技术面转向）→ 有事件才推 Telegram，无事件完全静默。
# 每类事件独立冷却期，冷却内同类信号不重复。产出两个文件供 Web 盯盘面板读取:
#   data/signal_monitor_state.json  每支股票最新 tick 快照（价格/RSI/量比/评分…）
#   data/signal_alerts.json         警报流（web 全量展示；Telegram 只推 push 级）

_MONITOR_STATE_FILE = settings.root / "data" / "signal_monitor_state.json"
_ALERTS_FILE        = settings.root / "data" / "signal_alerts.json"
_ALERTS_KEEP        = 400          # 警报流上限 — 防止文件无限增长
MONITOR_INTERVAL_MIN = 5

# type → (emoji, 冷却分钟, 默认级别)   push=推 Telegram / info=仅 web 警报流+日志
_EVENT_SPEC: dict[str, tuple[str, int, str]] = {
    "high_break": ("🚀", 45, "push"),
    "low_break":  ("🧨", 45, "push"),
    "vol_spike":  ("⚡", 45, "push"),
    "vwap_up":    ("📈", 30, "info"),   # 放量确认（≥1.3x）时升级为 push
    "vwap_down":  ("📉", 30, "info"),
    "rsi_hot":    ("🔥", 60, "push"),
    "rsi_cold":   ("🧊", 60, "push"),
    "score_bull": ("🟢", 60, "push"),
    "score_bear": ("🔴", 60, "push"),
    "macd_up":    ("〽️", 45, "info"),
    "macd_down":  ("〰️", 45, "info"),
    "bb_break":   ("🎯", 45, "info"),
}


def _load_monitor_state() -> dict:
    """读取盯盘状态；跨交易日自动重置（日内高低点/冷却/计数都只属于当天）。"""
    today = _ny_now().strftime("%Y-%m-%d")
    try:
        st = json.loads(_MONITOR_STATE_FILE.read_text())
        if st.get("date") == today and isinstance(st.get("symbols"), dict):
            return st
    except Exception:
        pass
    return {"date": today, "symbols": {}}


def _save_monitor_state(st: dict) -> None:
    _MONITOR_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _MONITOR_STATE_FILE.write_text(json.dumps(st, ensure_ascii=False))


def _append_alerts(alerts: list[dict]) -> None:
    try:
        feed = json.loads(_ALERTS_FILE.read_text())
        if not isinstance(feed, list):
            feed = []
    except Exception:
        feed = []
    feed.extend(alerts)
    _ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _ALERTS_FILE.write_text(json.dumps(feed[-_ALERTS_KEEP:], ensure_ascii=False))


def _cooled_down(prev: dict, etype: str, now) -> bool:
    last = (prev.get("alert_ts") or {}).get(etype)
    if not last:
        return True
    try:
        from datetime import datetime
        return (now - datetime.fromisoformat(last)).total_seconds() \
            >= _EVENT_SPEC[etype][1] * 60
    except Exception:
        return True


def _detect_events(sym: str, tech: dict, prev: dict, now) -> list[dict]:
    """对比当前 tick 与上一 tick，返回触发的事件列表（已按冷却期过滤）。

    噪音控制三原则:
      • 区域进入型事件（RSI 极值/评分转向/VWAP 翻转）需要上一 tick 做对比——
        当天第一次扫描只建立基线不触发，避免开盘一上来就刷屏。
      • 每类事件独立冷却（30-60 min），冷却期内同类不重复。
      • info 级只进 web 警报流；push 级才发 Telegram。
    """
    events: list[dict] = []
    price  = tech["price"]
    chg    = tech["chg1"]
    rsi    = tech["rsi"]
    mom    = tech["mom"]
    vol_r  = tech["vol"]["ratio"]
    vwap   = tech["vwap"]
    buy_s, sell_s = tech["buy_score"], tech["sell_score"]
    net    = buy_s - sell_s
    vwap_pct = (price / vwap - 1) * 100 if vwap else 0.0
    detail = (f"RSI {rsi:.0f} · Vol {vol_r:.1f}x · VWAP{vwap_pct:+.1f}% · "
              f"买{buy_s}/卖{sell_s}")

    def fire(etype: str, title: str, level: str | None = None) -> None:
        if not _cooled_down(prev, etype, now):
            return
        emoji, _cd, default_level = _EVENT_SPEC[etype]
        events.append({
            "ts_iso": now.isoformat(timespec="seconds"),
            "sym": sym, "type": etype, "level": level or default_level,
            "emoji": emoji, "title": title, "detail": detail,
            "price": price, "chg": chg,
        })

    # ① 日内高/低点突破（day_hl 要求当天 ≥4 根完成 bar，见 _today_session_hl）
    hl = tech.get("day_hl") or {}
    if hl.get("high") and price > hl["high"]:
        fire("high_break", f"突破日内高点 ${hl['high']:.2f}")
    elif hl.get("low") and price < hl["low"]:
        fire("low_break", f"跌破日内低点 ${hl['low']:.2f}")

    # ② 放量异动 — 量比 ≥2.5 且带方向（30min 动能 ≥0.5%）
    if vol_r >= 2.5 and abs(mom) >= 0.5:
        fire("vol_spike",
             f"放量{'拉升' if mom > 0 else '杀跌'} {vol_r:.1f}x（30min {mom:+.1f}%）")

    # ③ VWAP 翻转（±0.1% 缓冲防贴线抖动；放量 ≥1.3x 时升级为 push）
    prev_above = prev.get("above_vwap")
    if prev_above is not None and vwap:
        if price > vwap * 1.001 and prev_above is False:
            fire("vwap_up", f"上穿 VWAP ${vwap:.2f}",
                 level="push" if vol_r >= 1.3 else "info")
        elif price < vwap * 0.999 and prev_above is True:
            fire("vwap_down", f"跌破 VWAP ${vwap:.2f}",
                 level="push" if vol_r >= 1.3 else "info")

    # ④ RSI(7) 极值 — 进入极值区那一刻提醒一次
    prev_rsi = prev.get("rsi")
    if prev_rsi is not None:
        if rsi >= 82 and prev_rsi < 82:
            fire("rsi_hot", f"RSI(7)={rsi:.0f} 严重超买 — 追高谨慎")
        elif rsi <= 18 and prev_rsi > 18:
            fire("rsi_cold", f"RSI(7)={rsi:.0f} 严重超卖 — 关注反弹")

    # ⑤ 技术面评分转向（净分跨过 ±5 = 进入强烈买/卖档位）
    prev_net = prev.get("net")
    if prev_net is not None:
        why = f" · {' · '.join(tech['alerts'][:2])}" if tech["alerts"] else ""
        if net >= 5 and prev_net < 5:
            fire("score_bull", f"技术面转强 买{buy_s}/卖{sell_s}{why}")
        elif net <= -5 and prev_net > -5:
            fire("score_bear", f"技术面转弱 买{buy_s}/卖{sell_s}{why}")

    # ⑥ 次级事件 — 只进 web 警报流，不打扰 Telegram
    if tech["macd"]["cross_up"]:
        fire("macd_up", "MACD(5/13/5) 金叉")
    elif tech["macd"]["cross_down"]:
        fire("macd_down", "MACD(5/13/5) 死叉")
    if tech["boll"]["breakout"] == "up":
        fire("bb_break", "突破布林带上轨")
    elif tech["boll"]["breakout"] == "down":
        fire("bb_break", "跌破布林带下轨")

    return events


def _tick_snapshot(tech: dict, prev: dict, events: list[dict],
                   now_iso: str) -> dict:
    """本 tick 的持久化快照 — 既是下一 tick 的对比基线，也是 Web 盯盘磁贴的数据源。"""
    alert_ts = dict(prev.get("alert_ts") or {})
    for e in events:
        alert_ts[e["type"]] = now_iso
    price, vwap = tech["price"], tech["vwap"]
    above = prev.get("above_vwap")
    if vwap:
        if price > vwap * 1.001:
            above = True
        elif price < vwap * 0.999:
            above = False
    hl = tech.get("day_hl") or {}
    pushes = [e for e in events if e["level"] == "push"]
    last = pushes[-1] if pushes else (events[-1] if events else None)
    return {
        "ts": now_iso,
        "price": price, "chg": tech["chg1"], "rsi": tech["rsi"],
        "vol": tech["vol"]["ratio"], "vwap": vwap, "above_vwap": above,
        "buy": tech["buy_score"], "sell": tech["sell_score"],
        "net": tech["buy_score"] - tech["sell_score"], "mom": tech["mom"],
        "day_high": hl.get("high"), "day_low": hl.get("low"),
        "alerts_today": int(prev.get("alerts_today") or 0) + len(events),
        "last_alert": (f"{last['emoji']} {last['title']}" if last
                       else prev.get("last_alert")),
        "alert_ts": alert_ts,
    }


def _build_alert_msg(alerts: list[dict], ts: str) -> str:
    """盯盘警报卡 — 一个 tick 内所有 push 级事件合并成一条消息。

    按股票分组：同一支股票多个事件共用一行价格头 + 一行指标尾，
    避免"5 个事件重复 5 遍价格"的刷屏感。
    """
    by_sym: dict[str, list[dict]] = {}
    for a in alerts:
        by_sym.setdefault(a["sym"], []).append(a)
    head = f"🔔 *盯盘警报* | {ts}"
    if len(alerts) > 1:
        head += f" · {len(alerts)} 条"
    lines = [head, "━━━━━━━━━━━━━━━━━━━━━━"]
    for sym, evs in by_sym.items():
        first = evs[0]
        arrow = "📈" if (first.get("chg") or 0) >= 0 else "📉"
        lines.append(f"*{sym}* ${first['price']:.2f} {arrow}{first['chg']:+.2f}%")
        for e in evs:
            lines.append(f"  {e['emoji']} {e['title']}")
        lines.append(f"  `{first['detail']}`")
    return "\n".join(lines)


def run_monitor() -> None:
    """盯盘扫描一个 tick。scheduler 盘中每 5 分钟调用；Web「立即扫描」也走这里。

    无论有没有事件都会刷新 monitor state（Web 磁贴数据）；
    只有触发 push 级事件时才发 Telegram —— 静默是常态，推送是异动。
    """
    watchlist = load_watchlist()
    if not watchlist:
        log.info("monitor: watchlist empty")
        return
    now = _ny_now()
    now_iso = now.isoformat(timespec="seconds")
    ts = now.strftime("%H:%M ET")
    state = _load_monitor_state()
    symbols = state.setdefault("symbols", {})
    fired: list[dict] = []
    df_d_cache: dict = {}
    with _moo_client() as c:
        for sym in watchlist:
            try:
                tech = _fetch_tech(c, sym, KLType.K_5M, 80, df_d_cache)
            except Exception as e:
                log.warning("monitor: %s failed: %s", sym, e)
                continue
            if not tech:
                continue
            prev = symbols.get(sym) or {}
            events = _detect_events(sym, tech, prev, now)
            symbols[sym] = _tick_snapshot(tech, prev, events, now_iso)
            fired.extend(events)
            time.sleep(0.2)
    state["last_tick"] = now_iso
    _save_monitor_state(state)
    if not fired:
        log.info("monitor %s — %d tickers scanned, quiet", ts, len(symbols))
        return
    _append_alerts(fired)
    push = [a for a in fired if a["level"] == "push"]
    if push:
        notifier.send(_build_alert_msg(push, ts))
    log.info("monitor %s — %d events (%d pushed to Telegram)",
             ts, len(fired), len(push))


# ─── STANDALONE SCHEDULER ────────────────────────────────────────────────────

def _nyse_holidays(year: int) -> set:
    from datetime import date, timedelta

    def observed(d: date) -> date:
        if d.weekday() == 5: return d - timedelta(days=1)
        if d.weekday() == 6: return d + timedelta(days=1)
        return d

    def nth_weekday(y, m, wd, n):
        d = date(y, m, 1)
        d += timedelta(days=(wd - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)

    def last_monday(y, m):
        import calendar
        last = date(y, m, calendar.monthrange(y, m)[1])
        return last - timedelta(days=last.weekday())

    def easter(y):
        a = y % 19; b, c = divmod(y, 100); d, e = divmod(b, 4)
        f = (b + 8) // 25; g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30; i, k = divmod(c, 4)
        ll = (32 + 2 * e + 2 * i - h - k) % 7
        m2 = (a + 11 * h + 22 * ll) // 451
        mo, dy = divmod(114 + h + ll - 7 * m2, 31)
        return date(y, mo, dy + 1)

    return {
        observed(date(year, 1, 1)),
        nth_weekday(year, 1, 0, 3),
        nth_weekday(year, 2, 0, 3),
        easter(year) - __import__("datetime").timedelta(days=2),
        last_monday(year, 5),
        observed(date(year, 6, 19)),
        observed(date(year, 7, 4)),
        nth_weekday(year, 9, 0, 1),
        nth_weekday(year, 11, 3, 4),
        observed(date(year, 12, 25)),
    }


def _ny_now():
    import pytz
    from datetime import datetime
    return datetime.now(pytz.timezone("America/New_York"))


def _is_trading_day() -> bool:
    now = _ny_now()
    if now.weekday() >= 5:
        return False
    return now.date() not in _nyse_holidays(now.year)


def run_loop() -> None:
    """Start the signal reporter as a standalone scheduler.

    08:30 brief · 09:30-16:00 monitor every 5 min · 10:30 open1h · 16:10 close (ET)
    """
    import pytz
    from apscheduler.schedulers.blocking import BlockingScheduler

    # Only FileHandler here — the GUI redirects stdout to signal_reporter.log when
    # launching via subprocess.Popen(stdout=open(SIGNAL_LOG_FILE)), which would
    # cause every line to appear twice if we also had a StreamHandler.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(settings.root / "logs" / "signal_reporter.log"),
        ],
    )

    NY   = pytz.timezone("America/New_York")
    sched = BlockingScheduler(timezone=NY)

    def _premarket():
        if not _is_trading_day():
            return
        try:
            run_daily_brief("premarket")
        except Exception as e:
            log.exception("premarket brief job failed: %s", e)

    def _open1h_review():
        if not _is_trading_day():
            return
        try:
            run_daily_brief("open1h")
        except Exception as e:
            log.exception("open1h review job failed: %s", e)

    def _close_review():
        if not _is_trading_day():
            return
        try:
            run_daily_brief("close")
        except Exception as e:
            log.exception("close review job failed: %s", e)

    def _monitor_tick():
        # 盯盘扫描只在常规交易时段内跑；时段外静默跳过（不进日志，避免刷屏）。
        if not _is_trading_day():
            return
        m = _ny_now()
        mins = m.hour * 60 + m.minute
        if not (9 * 60 + 30 <= mins < 16 * 60):
            return
        try:
            run_monitor()
        except Exception as e:
            log.exception("monitor tick failed: %s", e)

    sched.add_job(_premarket, "cron",
                  day_of_week="mon-fri", hour=8, minute=30,
                  coalesce=True, misfire_grace_time=300, max_instances=1)

    # 开盘 1 小时后（10:30 ET）— 用盘前简报 + 开盘后实际走势再分析一次。
    sched.add_job(_open1h_review, "cron",
                  day_of_week="mon-fri", hour=10, minute=30,
                  coalesce=True, misfire_grace_time=300, max_instances=1)

    # 收盘对账（16:10 ET）— 盘前/盘中预测 vs 当日实际，达标判定 + 走势归因。
    sched.add_job(_close_review, "cron",
                  day_of_week="mon-fri", hour=16, minute=10,
                  coalesce=True, misfire_grace_time=600, max_instances=1)

    # 盘中盯盘（09:30-16:00 ET，每 5 分钟）— 事件驱动，有异动才推 Telegram。
    sched.add_job(_monitor_tick, "cron",
                  day_of_week="mon-fri", minute=f"*/{MONITOR_INTERVAL_MIN}",
                  coalesce=True, misfire_grace_time=120, max_instances=1)

    wl = load_watchlist()
    log.info("signal_reporter started | watchlist: %s", wl)
    log.info("schedule: brief@08:30, monitor */%dmin 09:30-16:00, "
             "open1h@10:30, close@16:10 ET", MONITOR_INTERVAL_MIN)
    notifier.send(
        f"📡 *Signal Reporter 已启动*\n"
        f"👀 盯盘 {len(wl)} 支: {', '.join(wl)}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌅 08:30 盘前简报（AI+新闻+支撑阶梯）\n"
        f"🔔 09:30-16:00 每 {MONITOR_INTERVAL_MIN} 分钟盯盘扫描\n"
        f"      突破 / 放量 / VWAP / RSI 异动即时提醒，无事件不打扰\n"
        f"🔄 10:30 开盘复盘 · 🏁 16:10 收盘对账"
    )
    sched.start()


# ─── CLI ENTRY ────────────────────────────────────────────────────────────────

def main() -> None:
    """
    python -m src.signal_reporter run             # 启动独立 scheduler
    python -m src.signal_reporter list            # 查看 watchlist
    python -m src.signal_reporter add TICKER      # 加入 watchlist
    python -m src.signal_reporter remove TICKER   # 移除
    python -m src.signal_reporter brief           # 手动触发盘前简报（新版）
    python -m src.signal_reporter review          # 手动触发开盘1小时复盘
    python -m src.signal_reporter close           # 手动触发收盘对账
    python -m src.signal_reporter monitor         # 手动触发一次盯盘扫描
    python -m src.signal_reporter premarket       # 旧版盘前完整卡片
    python -m src.signal_reporter intraday        # 手动触发盘中快讯
    """
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    # For direct one-shot calls (premarket/intraday) the scheduler's basicConfig
    # is never called, so we set up logging here so warnings have timestamps.
    if cmd in ("premarket", "intraday", "brief", "review", "close", "monitor"):
        # FileHandler too — web 端「立即扫描/简报」按钮是独立进程一次性触发，
        # 写进 signal_reporter.log 后网页日志面板才能看到执行过程。
        (settings.root / "logs").mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s | %(message)s",
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler(settings.root / "logs" / "signal_reporter.log"),
            ],
        )

    if cmd == "run":
        run_loop()
    elif cmd == "list":
        print(list_tickers())
    elif cmd == "add" and len(sys.argv) > 2:
        print(add_ticker(sys.argv[2]))
    elif cmd == "remove" and len(sys.argv) > 2:
        print(remove_ticker(sys.argv[2]))
    elif cmd == "brief":
        run_daily_brief("premarket")
    elif cmd == "review":
        run_daily_brief("open1h")
    elif cmd == "close":
        run_daily_brief("close")
    elif cmd == "monitor":
        run_monitor()
    elif cmd == "premarket":
        run_premarket()
    elif cmd == "intraday":
        run_intraday()
    else:
        print(main.__doc__)


if __name__ == "__main__":
    main()
