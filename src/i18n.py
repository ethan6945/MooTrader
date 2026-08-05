"""Minimal two-language i18n for GUI + Telegram notifications.

Preference stored in data/prefs.json so both the GUI and the scheduler
(separate processes) see the same language.  Default = Chinese.

Usage:
    from src.i18n import t
    label = t("btn_start")                 # → "▶ 启动" (zh) or "▶ Start" (en)
    msg = t("buy_message", symbol="AAPL")  # interpolates kwargs via .format()
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)

PREFS_FILE = settings.root / "data" / "prefs.json"
DEFAULT_LANG = "zh"
SUPPORTED = ("zh", "en")

_current_lang: str | None = None


# ---------- string tables ----------

STRINGS: dict[str, dict[str, str]] = {
    # =================================================================
    # English (canonical — every key MUST exist here as fallback)
    # =================================================================
    "en": {
        # --- GUI window title + menus ---
        "app_title": "Moo Trader",
        "menu_language": "Language",
        "menu_lang_en": "English",
        "menu_lang_zh": "中文 (Chinese)",
        "lang_switched_title": "Language switched",
        "lang_switched_body": "Restart the GUI to apply the new language.",

        # --- Status row ---
        "status_section": "Status",
        "scheduler": "Scheduler",
        "scheduler_running": "🟢 Running",
        "scheduler_stopped": "🔴 Stopped",
        "opend": "OpenD",
        "trade": "Trade",
        "trade_unlocked": "🟢 Unlocked",
        "trade_locked": "🟡 Lock OpenD!",
        "market": "Market",
        "market_open": "🟢 Open",
        "market_closed": "⚪ Closed",
        "et_prefix": "ET",
        "last_scan": "Last scan",
        "last_scan_none": "Last scan: —",
        "market_closed_badge": "🌙 market closed — last scan {ago}m ago",
        "regime": "Regime",
        "regime_none": "Regime: —",
        "ago": "ago",

        # --- Theme menu ---
        "menu_theme":        "Theme",
        "menu_theme_light":  "Light",
        "menu_theme_dark":   "Dark",
        "menu_theme_auto":   "Follow Device",
        "theme_switched_toast": "🎨 Theme: {theme} (now {resolved})",
        "menu_help":         "Help",

        # --- Budget section ---
        "budget_section": "Budget & Performance",
        "budget_label_total": "Total Budget (click to edit)",
        "budget_label_deployed": "Deployed",
        "budget_label_today_pnl": "Today's P&L",
        "budget_label_total_pnl": "Total P&L",
        "budget_template": "${invested:,.0f} / ${budget:,.0f}  ({pct:.1f}% used)",
        "pnl_template": "PnL: ${total:+,.2f}  (unreal ${unreal:+,.2f} | real ${realiz:+,.2f})",
        "today_pnl": "today {today:+,.2f}",

        # --- Account section ---
        "account_section": "Account",
        "broker_cash": "Broker cash",
        "broker_cash_waiting": "Broker cash: — (waiting for first scan)",
        "positions": "Positions",
        "halted": "Halted",
        "halted_yes": "🛑 YES",
        "halted_no": "✓ no",
        "halted_reset_label": "reset",
        "halted_label_yes": "🛑 Trading paused — reconcile/drawdown triggered (click to resume)",
        "halted_label_no": "✅ Trading active",
        "halted_reset_confirm_title": "Reset halt?",
        "halted_reset_confirm_body": ("This will clear the `halted` flag and reset "
                                       "the loss-streak counter. Use after reviewing why "
                                       "the bot halted (3+ losing days or daily drawdown)."),
        "halted_reset_done": "Halt cleared. The next scan can place new orders.",
        "reconcile": "Reconcile",
        "reconcile_none": "Reconcile: — (no scan yet)",

        # --- Strategy section ---
        "strategy_section": "Strategy",
        "ai_model": "AI",
        "entry_threshold": "Entry ≥",
        "scan_every": "Scan every",
        "max_hold": "Max hold",
        "env": "Env",
        "env_paper": "🟡 PAPER",
        "env_real": "🔴 REAL",
        "ml_on": "ML: 🟢 ON (w={weight:.2f})",
        "ml_no_model": "ML: 🟡 no model",
        "ml_off": "ML: ⚪ off",

        # --- Buttons ---
        "btn_start": "▶ Start",
        "btn_stop": "■ Stop",
        "btn_scan": "🔍 Scan Now",
        "btn_history": "📊 History",
        "btn_backtest": "📈 Backtest",
        "btn_equity": "📉 Equity",
        "btn_audit": "🤖 AI/Audit",
        "btn_watchlist": "⭐ Watchlist",
        "btn_sectors": "🟧 Sectors",
        "btn_ml": "🧠 ML",
        "btn_log": "📂 Log",
        "btn_scanning": "Scanning…",

        # --- Positions table ---
        "positions_section": "Open Positions",
        "col_symbol": "SYMBOL",
        "col_qty": "QTY",
        "col_entry": "ENTRY",
        "col_stop": "STOP",
        "col_tp": "TP",
        "col_atr": "ATR",
        "col_last": "LAST",
        "col_pnl": "PnL",

        # --- Trader log ---
        "log_section": "Trader Log (live tail)",

        # --- Right-click menu ---
        "menu_close_now": "Close Now (market)",
        "menu_edit_stop": "Edit Stop Loss…",
        "confirm_close_title": "Close {symbol} at market?",
        "confirm_close_body": "Places a limit SELL at last × 0.995. PnL will be recorded.",
        "edit_stop_title": "Edit stop for {symbol}",
        "edit_stop_body": "Current: ${current}",
        "closed_toast": "Closed {symbol} @ ${price:.2f}  PnL ${pnl:+.2f}",
        "close_failed_toast": "Close {symbol} failed: {error}",
        "stop_set_toast": "{symbol} stop → ${value:.2f}",
        "stop_failed_toast": "Edit stop failed: {error}",

        # --- Toasts / inline notifications ---
        "scheduler_already_running": "Scheduler is already running.",
        "scheduler_not_running": "Scheduler is not running.",
        "opend_unreachable": "OpenD is not reachable on 127.0.0.1:11111 — open OpenD first.",
        "scheduler_started": "Started scheduler PID {pid}",
        "scheduler_stopped_toast": "Stopped.",
        "not_in_open_trades": "{symbol} not in open_trades.json",

        # --- Telegram notifications ---
        "tg_buy": ("*BUY {symbol}* @ ${price:.2f}\n"
                   "Score: *{score:.1f}* | Qty: {qty} | Cost: ${cost:.0f}\n"
                   "SL: ${stop:.2f}  TP: ${tp:.2f}  ATR: {atr:.2f}\n"
                   "_{reasons}_\n"
                   "AI: {ai_reason}"),
        "tg_skip": "⏭ *Skip {symbol}* — {reason}",
        "tg_tp_half": ("💰 *TP half* {symbol} qty={qty} @ ${price:.2f} "
                       "PnL ${pnl:+.2f}"),
        "tg_trail": "📈 *Trail stop* {symbol} → ${new_stop:.2f}",
        "tg_tp_full": ("🎯 *TP FULL* {symbol} qty={qty} @ ${price:.2f} "
                       "PnL ${pnl:+.2f}"),
        "tg_scale_out": ("💰 *Scale-out T{tranche}* {symbol} qty={qty} @ ${price:.2f} "
                         "PnL ${pnl:+.2f}"),
        "tg_cancel_stale": ("🚫 *Stale order cancelled* {symbol} — unfilled after "
                            "{age_min} min"),
        "tg_edit_stop": "✏️ *Stop moved* {symbol} ${old:.2f} → ${new:.2f}",
        "tg_stop_hit": ("🛑 *STOP HIT* {symbol} qty={qty} "
                        "PnL ${pnl:+.2f}\n"
                        "Price ${price:.2f} ≤ Stop ${stop:.2f}"),
        "tg_max_hold": ("⏰ *MAX HOLD* {symbol} qty={qty} "
                        "age={age_days}d PnL ${pnl:+.2f}"),
        "tg_started": "🤖 *moo-trader started* env=`{env}`",
        "tg_regime_block": "⚠ Regime {label}: {note} — skipping all entries",
        "tg_exec_fail": "⚠ *Order execution failed*: {symbols} — check broker/logs",
        "tg_clock_fail": ("⚠ *Pre-open clock check* — no network time source reachable; "
                          "running on the uncorrected system clock. Verify the OS time manually."),
        "tg_clock_holiday": "🕐 Pre-open check: US market is closed today (NYSE holiday) — no trading.",

        # --- ML calibration window ---
        "ml_tab_overview": "Overview",
        "ml_tab_calibration": "Calibration",
        "ml_calibration_title": "Predicted proba vs actual winrate",
        "ml_calibration_no_data": ("No closed trades with ML proba yet. "
                                    "Calibration data appears after the bot has closed "
                                    "trades that entered while ML was active."),
        "ml_brier_score": "Brier score",
        "ml_brier_hint": "(lower = better; 0.25 = random, <0.15 = excellent)",

        # --- Strategy badge ---
        "strategy_trend": "Trend",
        "strategy_mr": "Mean-Revert",
    },

    # =================================================================
    # 中文 — leaves trader jargon (SL/TP/ATR/PnL/ML/AI) in English
    # so the message still reads naturally for someone fluent in both.
    # =================================================================
    "zh": {
        "app_title": "Moo 短线交易",
        "menu_language": "语言",
        "menu_lang_en": "English",
        "menu_lang_zh": "中文",
        "lang_switched_title": "语言已切换",
        "lang_switched_body": "请关闭并重新打开 GUI 让新语言生效。",

        "status_section": "状态",
        "scheduler": "调度器",
        "scheduler_running": "🟢 运行中",
        "scheduler_stopped": "🔴 已停止",
        "opend": "OpenD",
        "trade": "交易",
        "trade_unlocked": "🟢 已解锁",
        "trade_locked": "🟡 OpenD 未解锁!",
        "market": "市场",
        "market_open": "🟢 开盘",
        "market_closed": "⚪ 收盘",
        "et_prefix": "纽约",
        "last_scan": "上次扫描",
        "last_scan_none": "上次扫描: —",
        "market_closed_badge": "🌙 已收盘 — 上次扫描 {ago}m 前",
        "regime": "市场状态",
        "regime_none": "市场状态: —",
        "ago": "前",

        # --- Theme menu ---
        "menu_theme":        "主题",
        "menu_theme_light":  "浅色",
        "menu_theme_dark":   "深色",
        "menu_theme_auto":   "跟随系统",
        "theme_switched_toast": "🎨 主题: {theme}（当前 {resolved}）",
        "menu_help":         "帮助",

        "budget_section": "预算 & 盈亏",
        "budget_label_total": "总预算（点击修改）",
        "budget_label_deployed": "已部署",
        "budget_label_today_pnl": "今日盈亏",
        "budget_label_total_pnl": "累计盈亏",
        "budget_template": "${invested:,.0f} / ${budget:,.0f}  (已用 {pct:.1f}%)",
        "pnl_template": "盈亏: ${total:+,.2f}  (浮动 ${unreal:+,.2f} | 已结 ${realiz:+,.2f})",
        "today_pnl": "今日 {today:+,.2f}",

        "account_section": "账户",
        "broker_cash": "账户现金",
        "broker_cash_waiting": "账户现金: — (等首次扫描)",
        "positions": "持仓",
        "halted": "暂停",
        "halted_yes": "🛑 是",
        "halted_no": "✓ 否",
        "halted_reset_label": "重置",
        "halted_label_yes": "🛑 交易已暂停 — 对账/回撤触发（点击恢复）",
        "halted_label_no": "✅ 正常交易中",
        "halted_reset_confirm_title": "重置暂停?",
        "halted_reset_confirm_body": ("将清除 halted 标志 + 连亏天数计数。"
                                       "建议先确认 bot 为什么暂停（连亏 3 天 / 日内回撤超限），"
                                       "再点重置。"),
        "halted_reset_done": "暂停已清除，下次扫描可以下新单了。",
        "reconcile": "对账",
        "reconcile_none": "对账: — (还没扫描过)",

        "strategy_section": "策略",
        "ai_model": "AI",
        "entry_threshold": "入场 ≥",
        "scan_every": "扫描间隔",
        "max_hold": "最长持仓",
        "env": "环境",
        "env_paper": "🟡 模拟盘",
        "env_real": "🔴 实盘",
        "ml_on": "ML: 🟢 启用 (权重 {weight:.2f})",
        "ml_no_model": "ML: 🟡 无模型",
        "ml_off": "ML: ⚪ 关闭",

        "btn_start": "▶ 启动",
        "btn_stop": "■ 停止",
        "btn_scan": "🔍 立即扫描",
        "btn_history": "📊 历史",
        "btn_backtest": "📈 回测",
        "btn_equity": "📉 净值曲线",
        "btn_audit": "🤖 AI / 决策",
        "btn_watchlist": "⭐ 监控池",
        "btn_sectors": "🟧 板块",
        "btn_ml": "🧠 ML 模型",
        "btn_log": "📂 日志",
        "btn_scanning": "扫描中…",

        "positions_section": "当前持仓",
        "col_symbol": "代码",
        "col_qty": "数量",
        "col_entry": "成本",
        "col_stop": "止损",
        "col_tp": "止盈",
        "col_atr": "ATR",
        "col_last": "现价",
        "col_pnl": "盈亏",

        "log_section": "交易日志 (实时)",

        "menu_close_now": "立即平仓（市价）",
        "menu_edit_stop": "调整止损…",
        "confirm_close_title": "平掉 {symbol} 吗?",
        "confirm_close_body": "将以最新价 × 0.995 挂限价卖单，PnL 会记录到 trades.jsonl。",
        "edit_stop_title": "编辑 {symbol} 止损",
        "edit_stop_body": "当前止损: ${current}",
        "closed_toast": "已平仓 {symbol} @ ${price:.2f}  PnL ${pnl:+.2f}",
        "close_failed_toast": "平仓 {symbol} 失败: {error}",
        "stop_set_toast": "{symbol} 止损 → ${value:.2f}",
        "stop_failed_toast": "编辑止损失败: {error}",

        "scheduler_already_running": "调度器已经在运行了。",
        "scheduler_not_running": "调度器没在运行。",
        "opend_unreachable": "OpenD 没连上 127.0.0.1:11111 — 先启动 OpenD。",
        "scheduler_started": "调度器已启动 PID {pid}",
        "scheduler_stopped_toast": "已停止。",
        "not_in_open_trades": "{symbol} 不在 open_trades.json 里",

        "tg_buy": ("*买入 {symbol}* @ ${price:.2f}\n"
                   "评分: *{score:.1f}* | 数量: {qty} | 成本: ${cost:.0f}\n"
                   "止损: ${stop:.2f}  止盈: ${tp:.2f}  ATR: {atr:.2f}\n"
                   "_{reasons}_\n"
                   "AI: {ai_reason}"),
        "tg_skip": "⏭ *跳过 {symbol}* — {reason}",
        "tg_tp_half": ("💰 *止盈半仓* {symbol} 数量={qty} @ ${price:.2f} "
                       "PnL ${pnl:+.2f}"),
        "tg_trail": "📈 *跟踪止损* {symbol} → ${new_stop:.2f}",
        "tg_tp_full": ("🎯 *全仓止盈* {symbol} 数量={qty} @ ${price:.2f} "
                       "PnL ${pnl:+.2f}"),
        "tg_scale_out": ("💰 *分批止盈 第{tranche}档* {symbol} 数量={qty} @ ${price:.2f} "
                         "PnL ${pnl:+.2f}"),
        "tg_cancel_stale": "🚫 *撤销挂单* {symbol} — 挂了 {age_min} 分钟未成交",
        "tg_edit_stop": "✏️ *止损调整* {symbol} ${old:.2f} → ${new:.2f}",
        "tg_stop_hit": ("🛑 *触发止损* {symbol} 数量={qty} "
                        "PnL ${pnl:+.2f}\n"
                        "价格 ${price:.2f} ≤ 止损 ${stop:.2f}"),
        "tg_max_hold": ("⏰ *持仓超时* {symbol} 数量={qty} "
                        "持仓 {age_days} 天  PnL ${pnl:+.2f}"),
        "tg_started": "🤖 *moo-trader 已启动* 环境=`{env}`",
        "tg_regime_block": "⚠ 市场体制 {label}：{note} — 暂停所有新入场",
        "tg_exec_fail": "⚠ *下单执行失败*: {symbols} — 请检查券商端/日志",
        "tg_clock_fail": ("⚠ *盘前校时失败* — 所有网络时间源不可达，暂用未校验的系统时钟运行，"
                          "请手动确认系统时间。"),
        "tg_clock_holiday": "🕐 盘前校时：今日美股休市（NYSE 假期），不交易。",

        "ml_tab_overview": "概览",
        "ml_tab_calibration": "校准曲线",
        "ml_calibration_title": "预测概率 vs 实际胜率",
        "ml_calibration_no_data": ("还没有带 ML 概率的已平仓交易。"
                                    "等 bot 在 ML 启用后平掉几笔单，这里会显示校准数据。"),
        "ml_brier_score": "Brier 分数",
        "ml_brier_hint": "(越低越好；0.25 = 随机，< 0.15 = 优秀)",

        "strategy_trend": "趋势",
        "strategy_mr": "均值回归",
    },
}


# ---------- public API ----------

def current_lang() -> str:
    """Return the active language code. First call reads prefs.json + caches."""
    global _current_lang
    if _current_lang is not None:
        return _current_lang
    try:
        prefs = json.loads(PREFS_FILE.read_text())
        lang = prefs.get("lang", DEFAULT_LANG)
        if lang not in SUPPORTED:
            lang = DEFAULT_LANG
    except (FileNotFoundError, json.JSONDecodeError):
        lang = DEFAULT_LANG
    _current_lang = lang
    return lang


def set_lang(lang: str) -> None:
    """Persist the language preference. New language takes effect on next process start."""
    global _current_lang
    if lang not in SUPPORTED:
        raise ValueError(f"unsupported lang: {lang!r} (supported: {SUPPORTED})")
    try:
        prefs = json.loads(PREFS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        prefs = {}
    prefs["lang"] = lang
    PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PREFS_FILE.write_text(json.dumps(prefs, indent=2, ensure_ascii=False))
    _current_lang = lang
    log.info("i18n language set to %s", lang)


def t(key: str, **kwargs) -> str:
    """Look up `key` in the current language; fall back to English then to the
    key itself.  Interpolates kwargs via .format()."""
    lang = current_lang()
    raw = STRINGS.get(lang, {}).get(key)
    if raw is None:
        raw = STRINGS.get("en", {}).get(key, key)
    if not kwargs:
        return raw
    try:
        return raw.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        log.debug("i18n format failed for %s with %r", key, kwargs)
        return raw
