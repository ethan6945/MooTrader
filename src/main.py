"""Entry point. Two modes:

    python -m src.main scan      # one-shot scan + trade decisions
    python -m src.main run       # APScheduler loop during US market hours
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytz
from logging.handlers import RotatingFileHandler
from apscheduler.schedulers.blocking import BlockingScheduler

from moomoo import KLType

from . import (
    adaptive_sizing, ai, ai_validator, approvals, audit, blacklist, breadth,
    clock, cron_state, db, executor, gap_sentinel, history, indicators,
    kill_switch, notifier, options_stats, portfolio,
    regime as regime_mod, risk_manager, runtime_config, sector, self_improve,
    self_review, strategy_momentum, strategy_mr, strategy_pattern,
    tg_approvals,
)
from .config import settings
from .earnings import earnings_block
from .moo_client import client, real_unlock_confirmed
from .reconcile import log_reconcile, reconcile

SPREAD_MAX_PCT = 0.5   # refuse entry if bid-ask spread > 0.5% of mid
# Frames where the last K-line is still forming when we fetch — drop it
# before scoring to eliminate look-ahead bias.
_INTRADAY_TFS = {"HOUR_1", "MIN_10", "MIN_30"}


def _drop_forming_bar(df, timeframe: str):
    """Look-ahead audit: strip the last (currently-forming) intraday bar.

    the broker's `request_history_kline` returns bars up to and including the bar
    that contains "now" — i.e. the last row of an intraday df is OPEN, not
    closed. Scoring on an open bar means the close, volume, and indicators
    all keep moving after we evaluate, so the live signal won't match the
    backtest's closed-bar signal. We always drop the last intraday row.

    Daily bars are left intact — the daily scan path runs once after close.
    """
    if timeframe.upper() in _INTRADAY_TFS and len(df) > 1:
        return df.iloc[:-1]
    return df

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        # Rotating file: cap each file at 10 MB, keep 5 backups → trader.log
        # family tops out at ~60 MB instead of growing without bound.
        RotatingFileHandler(
            settings.root / "logs" / "trader.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        ),
    ],
)
# Quiet the chatty INFO loggers. APScheduler prints a "Job executed
# successfully" line after EVERY job run (~77% of the old log); httpx and
# google_genai log one line per HTTP / AI call. The jobs still run — we just
# stop logging each tick at INFO. Real warnings/errors still come through.
for _noisy in ("apscheduler", "httpx", "google_genai"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("main")

WATCHLIST_FILE = settings.root / "config" / "watchlist.json"
NY = pytz.timezone("America/New_York")
# Owner's home timezone — the Monday maintenance chain (autopilot/universe/
# backtest/self-review/ML) is pinned to 20:00 KL sharp (owner request
# 2026-07-07). KL has no DST, so these fire at the same wall-clock time
# year-round regardless of US DST shifts (≈08:00 ET summer / 07:00 ET winter,
# both pre-market).
KL = pytz.timezone("Asia/Kuala_Lumpur")


def load_watchlist() -> list[str]:
    return json.loads(WATCHLIST_FILE.read_text())["tickers"]


def _refresh_account_snapshot(c, vix: float | None = None,
                              regime: "regime_mod.Regime | None" = None,
                              full: bool = True) -> None:
    """Write data/account.json — used by the GUI for status + heartbeat.

    Called from BOTH the full trade-phase scan AND the outside-trade-phase
    branch so the GUI heartbeat stays current at every scan interval, not
    just during 09:45–15:30 ET.

    When `full=False` we skip VIX + regime fetches (slow + only matter for
    entries that won't happen anyway).
    """
    try:
        cash = c.get_account_cash()
        positions = c.get_positions()
    except Exception as e:
        log.warning("snapshot fetch failed: %s", e)
        return

    if vix is None:
        if full:
            try:
                vix = c.get_vix()
            except Exception:
                vix = 0.0
        else:
            # Carry over last-known VIX from previous snapshot (don't blank it).
            try:
                prev = json.loads((settings.root / "data" / "account.json").read_text())
                vix = float(prev.get("vix") or 0)
            except Exception:
                vix = 0.0

    if regime is None:
        if full:
            try:
                spy_df = c.get_kline("SPY", bars=250, ktype=KLType.K_DAY)
                regime = regime_mod.assess(spy_df)
            except Exception:
                regime = regime_mod.Regime("NEUTRAL", 0, 0, 0, False, False, "not assessed")
        else:
            try:
                prev = json.loads((settings.root / "data" / "account.json").read_text())
                regime = regime_mod.Regime(
                    prev.get("regime", "NEUTRAL"), 0, 0, 0, False, False,
                    prev.get("regime_note", "carried over from last full scan"),
                )
            except Exception:
                regime = regime_mod.Regime("NEUTRAL", 0, 0, 0, False, False, "unknown")

    try:
        real_held = positions[positions["qty"].astype(float) > 0] if not positions.empty else positions
        invested = 0.0
        unrealized = 0.0
        symbols: list[str] = []
        # Per-position live snapshot — the GUI overlays this onto the open_trades
        # table so each row shows current price + unrealized P&L without making
        # an extra API call from the GUI thread.
        per_pos: dict[str, dict] = {}
        if not real_held.empty:
            invested = float((real_held["qty"].astype(float) * real_held["cost_price"].astype(float)).sum())
            unrealized = float(real_held.get("pl_val", 0).astype(float).sum()) if "pl_val" in real_held.columns else 0.0
            symbols = [code.split(".")[-1] for code in real_held["code"].tolist()]
            for _, row in real_held.iterrows():
                sym = row["code"].split(".")[-1]
                per_pos[sym] = {
                    "qty": int(float(row.get("qty", 0) or 0)),
                    "last": float(row.get("nominal_price") or 0)
                            or float(row.get("market_val", 0)) / max(float(row.get("qty", 1)), 1),
                    "pl_val": float(row.get("pl_val", 0) or 0),
                    "pl_ratio": float(row.get("pl_ratio", 0) or 0),
                }
        state = risk_manager._load_state()
        realized_total = float(state.get("realized_pnl_total", 0.0))
        snap = {
            "ts": clock.ny_now().isoformat(),
            "cash": cash,
            "positions_count": len(real_held),
            "invested": invested,
            "budget": risk_manager.budget_usd(),
            "budget_used_pct": round(invested / risk_manager.budget_usd() * 100, 1) if risk_manager.budget_usd() else 0,
            "unrealized_pnl": unrealized,
            "realized_pnl_total": realized_total,
            "total_pnl": unrealized + realized_total,
            "symbols": symbols,
            "ai_provider": ai.active_provider(),
            "ai_model": ai.active_model(),
            "scan_interval_min": settings.scan_interval_min,
            "entry_threshold": runtime_config.entry_threshold(),   # 2026-07-06: use runtime override, not static .env
            "max_hold_days": runtime_config.max_hold_days(),      # 2026-07-06: use runtime override, not static .env
            "timeframe": settings.timeframe,
            "trade_env": settings.moo_trade_env,
            # REAL unlock proof for the web badge: True only after a gated order
            # op succeeded this session. Always False on SIMULATE (no unlock).
            "real_unlock_confirmed": real_unlock_confirmed(),
            "vix": round(vix or 0, 1),
            "regime": regime.label,
            "regime_sub": regime.sub_label,
            "regime_confirmed": regime.confirmed,
            "regime_note": regime.note,
            "open_risk": round(portfolio.current_open_risk(), 2),
            "heat_cap": round(risk_manager.budget_usd() * portfolio.PORTFOLIO_HEAT_PCT, 2),
            "trade_stats": portfolio.trade_stats(50),
            "skip_gates": audit.gate_summary(200),
            "last_scan_utc": clock.utc_now_corrected().isoformat(),
            "phase": "trade" if full else "manage_only",
            "clock": clock.status(),
            "per_position": per_pos,
            "realized_pnl_today": float(state.get("realized_pnl_today", 0.0)),
            "budget_usd": risk_manager.budget_usd(),
            "pending_approvals": approvals.list_pending(),
        }
        (settings.root / "data" / "account.json").write_text(
            json.dumps(snap, indent=2, default=str)
        )
        if full:
            history.append({
                "invested": round(invested, 2),
                "budget": risk_manager.budget_usd(),
                "unrealized_pnl": round(unrealized, 2),
                "realized_pnl_total": round(realized_total, 2),
                "total_pnl": round(unrealized + realized_total, 2),
                "positions_count": len(real_held),
                "symbols": symbols,
                "timeframe": settings.timeframe,
            })
    except Exception as e:
        log.warning("snapshot write failed: %s", e)


def scan_once() -> None:
    log.info("=== scan start ===")
    audit.record("scan_start")
    tickers = load_watchlist()

    # Pre-kill-switch fast path: if we're outside trade phase, do management
    # work + snapshot refresh, then exit before any candidate logic runs.
    # The full kill_switch.evaluate() happens later (after we have cash + regime).
    if not kill_switch.in_trade_phase():
        log.info("Outside trade phase — managing only, no new entries")
        try:
            with client() as c:
                executor.manage_open_trades(c)
                _refresh_account_snapshot(c, full=False)
        except Exception as e:
            log.exception("manage during off-phase failed: %s", e)
        audit.record("scan_end", gate="trade_phase",
                     reason="outside trade phase — manage only")
        return

    with client() as c:
        # 1. Manage existing positions first
        try:
            actions = executor.manage_open_trades(c)
            for a in actions:
                notifier.send(notifier.trade_action_msg(a))
        except Exception as e:
            log.exception("manage_open_trades failed: %s", e)

        # 2. Refresh account state and VIX, then persist snapshot for the GUI.
        try:
            cash = c.get_account_cash()
            positions = c.get_positions()
            pending_value = c.get_pending_buy_value()
            pending_symbols = c.get_pending_symbols()
        except Exception as e:
            log.exception("account fetch failed: %s", e)
            return

        # Dynamic capital: feed live equity (cash + open-position market value)
        # to the risk manager so sizing/budget/DD derive from real account state,
        # capped by the owner's allocated budget. No restart when budget changes.
        try:
            pos_mv = 0.0
            if positions is not None and not positions.empty:
                if "market_val" in positions.columns:
                    pos_mv = float(positions["market_val"].astype(float).sum())
                elif "qty" in positions.columns:
                    px = (positions["nominal_price"] if "nominal_price" in positions.columns
                          else positions.get("cost_price", 0))
                    pos_mv = float((positions["qty"].astype(float) * px.astype(float)).sum())
            risk_manager.set_live_equity(cash + pos_mv)
        except Exception as e:
            log.debug("set_live_equity failed (sizing falls back to budget): %s", e)

        # Apply any owner-APPROVED suggestions (analyze→notify→approve→execute).
        # Nothing here runs until the owner approved it in the GUI / via CLI —
        # the feedback 铁律 choke point. Confirm what was applied.
        try:
            applied = approvals.apply_approved()
            for a in applied:
                notifier.send(f"✅ 已执行你批准的建议: {a.get('action', a.get('detail',''))}")
        except Exception as e:
            log.debug("apply_approved skipped: %s", e)

        # 2b. Reconcile broker vs internal records — catch drift before trading.
        recon: dict = {}
        try:
            recon = reconcile(positions, client=c)
            msg = log_reconcile(recon)
            if msg:
                notifier.send(msg)
        except Exception as e:
            log.warning("reconcile failed: %s", e)

        # 3. Fetch VIX + classify market regime (SPY 200MA).
        try:
            vix = c.get_vix()
            log.info("VIX=%.1f%s", vix,
                     " ⚠ HIGH — sizes halved" if 25 < vix <= 35 else
                     " ⚠ CRISIS — sizes quartered" if vix > 35 else "")
        except Exception as e:
            log.warning("VIX fetch failed: %s", e)
            vix = 15.0

        # Market regime via SPY. Pass VIX + the previous confirmed label so the
        # smart layer can compute strength / sub_label / hysteresis (all advisory
        # — the raw label is unchanged). effective_label is what the entry-block
        # and the cash-yield / inverse-sleeve sweeps gate on: the hysteresis-
        # smoothed `confirmed` label when SMART_REGIME_ENABLED, else the raw label
        # (so default behavior + backtest parity are untouched).
        regime = regime_mod.Regime("NEUTRAL", 0, 0, 0, False, False, "not assessed")
        try:
            prev_label = db.get_state().get("regime_last_label")
            spy_df = c.get_kline("SPY", bars=250, ktype=KLType.K_DAY)
            regime = regime_mod.assess(spy_df, vix=vix, prev_label=prev_label)
            log.info("Regime: %s (%s) — %s", regime.label, regime.sub_label, regime.note)
            db.update_state({"regime_last_label": regime.confirmed})
        except Exception as e:
            log.warning("regime assessment failed: %s", e)
        effective_label = regime.confirmed if settings.smart_regime_enabled else regime.label

        # P0-3 (2026-06-26): market breadth filter — prevent trading in
        # "fake bull markets" where SPY is up but breadth is collapsing.
        # When breadth is unhealthy, we skip new entries but still manage
        # open positions (stops + TPs fire as normal). Gate is advisory by
        # default (logs + notifies, doesn't block) — set BREADTH_BLOCKING=true
        # in .env to make it a hard block.
        breadth_ok = True
        breadth_note = ""
        try:
            bv = breadth.assess(c, vix=vix)
            breadth_ok = bv.healthy
            breadth_note = bv.note
            log.info("Breadth: %s", breadth_note)
        except Exception as e:
            log.warning("breadth assessment failed: %s — passing", e)

        # 2c. Supervise MANUAL positions reconcile just adopted this scan (your
        # broker-app buys). Runs BEFORE the kill-switch early-return so a bear-
        # regime adoption is still reviewed + risk-flagged. NORMAL adoptions are
        # released to the bot's stops/TP; HIGH-risk ones stay owner-held and queue
        # a Telegram takeover approval.
        try:
            adopted = [f["symbol"] for f in recon.get("fixes_applied", [])
                       if f.get("type") == "ORPHAN_ADOPTED"]
            if adopted:
                from . import manual_positions
                manual_positions.review_adopted(c, adopted, regime=regime)
        except Exception as e:
            log.warning("manual-position review failed: %s", e)

        # Daily rollover — clear stale halt / realized_pnl_today on day boundary.
        try:
            kill_switch.reset_for_new_day(cash)
        except Exception as e:
            log.warning("reset_for_new_day failed: %s", e)

        # Inverse-ETF sleeve — profit from confirmed downtrends (BEAR + inverse
        # ETF trending up). Runs BEFORE the regime early-return and BEFORE the
        # cash-yield sweep so it claims its small sleeve first; cash-yield then
        # parks whatever cash remains. No-op unless INVERSE_SLEEVE_ENABLED (and
        # it should stay OFF until scripts/inverse_sleeve_backtest.py passes).
        try:
            from . import inverse_sleeve
            inverse_sleeve.manage(c, effective_label, risk_manager.budget_usd())
        except Exception as e:
            log.warning("inverse_sleeve failed: %s", e)

        # Cash-yield sweep — runs BEFORE the regime early-return so it can park
        # idle cash in a T-bill ETF (SGOV) DURING a BEAR regime (when strategy
        # entries are paused), and unwind it back to cash on tradeable regimes
        # so longs have buying power again. No-op unless CASH_YIELD_ENABLED.
        try:
            from . import cash_yield
            cash_yield.manage(c, effective_label, cash, positions)
        except Exception as e:
            log.warning("cash_yield sweep failed: %s", e)

        # Unified kill switch — replaces three separate checks (trade_phase /
        # regime block / halt / drawdown). Single source of truth for "can we
        # open new positions right now?". manage_open_trades has already run
        # so existing stops/TPs still fire even when entries are blocked.
        #
        # P0-3: also evaluate market breadth — unhealthy breadth is a soft
        # block (advisory unless BREADTH_BLOCKING=true). When unhealthy,
        # existing positions are still managed; only new entries are skipped.
        skip_reason = ""
        if not breadth_ok:
            skip_reason = f"breadth unhealthy: {breadth_note}"
            if not settings.breadth_blocking:
                log.warning("Breadth unhealthy (advisory): %s", breadth_note)
                # advisory — let the kill switch be the final gate
            else:
                log.warning("Breadth unhealthy (blocking): %s — skipping new entries",
                            breadth_note)
                audit.record("scan_end", gate="breadth", reason=skip_reason)
                notifier.send(f"⚠ {skip_reason}")
                return

        verdict = kill_switch.evaluate(
            regime_block_new=(effective_label == "BEAR"),
            regime_label=regime.label,
            regime_note=regime.note,
            current_cash=cash,
        )
        if not verdict.can_trade:
            log.warning("kill_switch [%s]: %s", verdict.gate, verdict.reason)
            audit.record("scan_end", gate=f"kill_{verdict.gate}",
                         reason=verdict.reason)
            if verdict.gate in ("regime", "drawdown"):
                notifier.send(f"⚠ {verdict.reason}")
            return

        try:
            real_held = positions[positions["qty"].astype(float) > 0] if not positions.empty else positions
            invested = 0.0
            unrealized = 0.0
            symbols = []
            per_pos: dict[str, dict] = {}
            if not real_held.empty:
                invested = float((real_held["qty"].astype(float) * real_held["cost_price"].astype(float)).sum())
                unrealized = float(real_held.get("pl_val", 0).astype(float).sum()) if "pl_val" in real_held.columns else 0.0
                symbols = [code.split(".")[-1] for code in real_held["code"].tolist()]
                for _, row in real_held.iterrows():
                    sym = row["code"].split(".")[-1]
                    per_pos[sym] = {
                        "qty": int(float(row.get("qty", 0) or 0)),
                        "last": float(row.get("nominal_price") or 0),
                        "pl_val": float(row.get("pl_val", 0) or 0),
                        "pl_ratio": float(row.get("pl_ratio", 0) or 0),
                    }
            state = risk_manager._load_state()
            realized_total = float(state.get("realized_pnl_total", 0.0))
            snap = {
                "ts": clock.ny_now().isoformat(),
                "cash": cash,
                "positions_count": len(real_held),
                "invested": invested,
                "budget": risk_manager.budget_usd(),
                "budget_used_pct": round(invested / risk_manager.budget_usd() * 100, 1) if risk_manager.budget_usd() else 0,
                "unrealized_pnl": unrealized,
                "realized_pnl_total": realized_total,
                "total_pnl": unrealized + realized_total,
                "symbols": symbols,
                "ai_provider": ai.active_provider(),
                "ai_model": ai.active_model(),
                "scan_interval_min": settings.scan_interval_min,
                "entry_threshold": runtime_config.entry_threshold(),   # 2026-07-06: use runtime override, not static .env
                "max_hold_days": runtime_config.max_hold_days(),      # 2026-07-06: use runtime override, not static .env
                "timeframe": settings.timeframe,
                "trade_env": settings.moo_trade_env,
                "real_unlock_confirmed": real_unlock_confirmed(),
                "vix": round(vix, 1),
                "regime": regime.label,
                "regime_sub": regime.sub_label,
                "regime_confirmed": regime.confirmed,
                "regime_note": regime.note,
                "open_risk": round(portfolio.current_open_risk(), 2),
                "heat_cap": round(risk_manager.budget_usd() * portfolio.PORTFOLIO_HEAT_PCT, 2),
                "trade_stats": portfolio.trade_stats(50),
                "skip_gates": audit.gate_summary(200),
                "last_scan_utc": clock.utc_now_corrected().isoformat(),
                "phase": "trade",
                "clock": clock.status(),
                "per_position": per_pos,
                "realized_pnl_today": float(state.get("realized_pnl_today", 0.0)),
                "budget_usd": risk_manager.budget_usd(),
                "pending_approvals": approvals.list_pending(),
            }
            (settings.root / "data" / "account.json").write_text(
                json.dumps(snap, indent=2, default=str)
            )
            history.append({
                "invested": round(invested, 2),
                "budget": risk_manager.budget_usd(),
                "unrealized_pnl": round(unrealized, 2),
                "realized_pnl_total": round(realized_total, 2),
                "total_pnl": round(unrealized + realized_total, 2),
                "positions_count": len(real_held),
                "symbols": symbols,
                "timeframe": settings.timeframe,
            })
        except Exception as e:
            log.warning("snapshot/history write failed: %s", e)

        # 4. Score candidates with TWO strategies (trend + mean-revert) in parallel.
        # For each ticker, keep the higher-scoring signal — they're complementary
        # and rarely both fire (trend wants ADX↑, MR wants ADX↓).
        ranked: list[indicators.Signal] = []
        # Klines kept for pattern signals so the live-only vision layer can
        # re-render the chart in the execution loop without a second fetch.
        pattern_dfs: dict = {}
        # P0-2 (2026-06-26): keep all kline dataframes so we can compute
        # structural stops (swing-low-based) in the execution loop without
        # a second fetch.
        all_dfs: dict[str, pd.DataFrame] = {}
        # Marginal-setup buffer: anything within 10 pts BELOW threshold also
        # enters the funnel, but gets half-conviction (smaller position).
        # Without this buffer the bot scores ~60 for most US large-caps and
        # NEVER hits 70+, so top_5 = [] every scan → zero trades. This zone
        # is the same idea as the ML "neutral" band — try it, but small.
        # Runtime-overridable (owner-approved optimizer); falls back to .env.
        entry_thr = runtime_config.entry_threshold()
        # 2026-06-03 live↔backtest parity: HARD threshold (no marginal sub-70
        # band). The honest backtest uses a hard threshold and still generates
        # ~105 trades/180d on trend+momentum, and the gate-ablation proved sub-70
        # setups destroy returns (thr 65 = −$11/day). The old −10 marginal band
        # was a trend-only-era workaround; momentum scoring clears 70 fine now.
        #
        # P0-4 (2026-06-26): adaptive threshold — lower the bar in confirmed
        # BULL markets (more signals, less noise) and raise it in NEUTRAL
        # (fewer but higher-quality signals). BEAR is already blocked by the
        # regime kill-switch, but a hard 999 here double-locks. The hysteresis
        # layer (confirmed_label) prevents whipsaw so the threshold doesn't
        # oscillate on a single SPY close grazing the 200-MA.
        base_thr = entry_thr
        if effective_label == "BULL":
            # 2026-07-27: the BULL discount now requires HEALTHY breadth.
            # effective_label is the HYSTERESIS label (regime.confirmed), which
            # is what keeps the threshold from oscillating — but it also means
            # the discount survives well into a deteriorating tape. Live case
            # 2026-07-27: raw regime NEUTRAL (SPY below its 50-MA) and breadth
            # UNHEALTHY (A/D 0.40, 42% >50MA), yet confirmed=BULL pulled the bar
            # from 75 down to 65 and let a 65.2 candidate into the funnel — with
            # a 17.9% live win rate and a 12% account drawdown. Loosening and
            # deteriorating must not happen at the same time. Breadth stays
            # ADVISORY for blocking (BREADTH_BLOCKING=false, deliberate — see
            # .env); this only withholds a discount, it never blocks a trade,
            # so it cannot repeat the 2026-07-03 over-blocking regression.
            if breadth_ok:
                base_thr = max(55, entry_thr - 5)
            else:
                log.info("BULL threshold discount withheld — breadth unhealthy "
                         "(%s); holding the bar at %.0f", breadth_note, entry_thr)
        elif effective_label == "NEUTRAL":
            base_thr = min(85, entry_thr + 5)
        # BEAR: regime kill_switch already blocks — threshold is moot
        threshold_floor = base_thr
        # P1-2 (2026-06-26): Late-entry risk premium. Entries after 14:00 ET
        # face shrinking liquidity, wider spreads, and immediate overnight gap
        # exposure before the thesis has time to play out. Raise the bar by
        # 5 points so only the highest-conviction setups fire in the last 90
        # minutes. Stacking add-ons (already held, already survived at least
        # one session) are exempt — the gate only applies to brand-new names.
        _now_et = clock.ny_now()
        _late_min = 14 * 60  # 14:00 ET
        _late_cutoff = (_now_et.hour * 60 + _now_et.minute) >= _late_min
        # Refresh the blacklist before this scan's symbol loop. Cheap (file read).
        _bl_active = blacklist.get_blacklist()
        for sym in tickers:
            # Adaptive blacklist — skip symbols flagged as recent net losers.
            if sym in _bl_active:
                audit.record("skip", symbol=sym, gate="blacklist",
                             reason=_bl_active[sym].reason)
                continue
            try:
                df = c.get_kline(sym, bars=120)
                # Look-ahead audit — never score on a still-forming intraday bar.
                df = _drop_forming_bar(df, settings.timeframe)
                # P0-2: keep df for structural stop computation later
                all_dfs[sym] = df
                # Independent strategies: each can submit if its own score
                # clears the floor. 2026-05-29: added strategy_momentum
                # (stricter breakout setup) — sized identically to trend, just
                # a different filter mix biased toward explosive setups.
                # Mean-revert gated by MR_ENABLED (combo sweep showed it was
                # net-negative in current bull-skewed watchlist).
                sig_trend = indicators.evaluate(sym, df)
                sig_mom = strategy_momentum.evaluate(sym, df)
                if sig_trend.score >= threshold_floor:
                    ranked.append(sig_trend)
                if sig_mom.score >= threshold_floor:
                    ranked.append(sig_mom)
                if settings.mr_enabled:
                    sig_mr = strategy_mr.evaluate(sym, df)
                    if sig_mr.score >= threshold_floor:
                        ranked.append(sig_mr)
                # Pattern strategy (chart-pattern recognition) — gated like MR.
                # Keep the df so pattern_vision can re-render this exact chart in
                # the execution loop (live-only) without re-fetching klines.
                if settings.pattern_enabled:
                    sig_pattern = strategy_pattern.evaluate(sym, df)
                    if sig_pattern.score >= threshold_floor:
                        ranked.append(sig_pattern)
                        pattern_dfs[sym] = df
            except Exception as e:
                log.warning("scoring %s failed: %s", sym, e)

        ranked.sort(key=lambda s: s.score, reverse=True)
        log.info("top 5: %s", [(s.symbol, s.score) for s in ranked[:5]])

        # ----- Portfolio-full notification (edge-triggered) -----
        # Compute current unique-symbol count (matches risk_manager's
        # MAX_POSITIONS semantics: 1 ticker = 1 slot regardless of stacks).
        held_syms = set(symbols) | set(pending_symbols)
        cap = risk_manager.max_positions()
        currently_full = len(held_syms) >= cap
        was_full = bool(risk_manager._load_state().get("portfolio_full_notified"))
        if currently_full and not was_full:
            notifier.send(
                f"📦 仓位已满 {len(held_syms)}/{cap} — "
                f"暂停寻新单（仍会扫加仓机会）。\n"
                f"持仓: {', '.join(sorted(held_syms))}"
            )
            db.update_state({"portfolio_full_notified": True})
        elif not currently_full and was_full:
            db.update_state({"portfolio_full_notified": False})

        # Per-scan cap on brand-new tickers (stacking add-ons exempt).
        new_names_opened = 0
        new_names_limit = settings.max_new_names_per_scan

        # (Regime / halt / drawdown kill switches already evaluated above
        # via kill_switch.evaluate() — by this point we're cleared to trade.)

        # 5. Candidate funnel — 3-layer decision architecture:
        #    Rule score (0-100)   → ranking + initial pass (≥ entry_threshold)
        #    ML proba   (0-1)     → conviction multiplier (veto if too low)
        #    AI verdict (pass/veto) → independent veto (no score blend!)
        #
        # No more "final_score" — each layer answers its own question:
        #   • Rule:  "is this a textbook technical setup?"          → rank + filter
        #   • ML:    "did similar setups historically work out?"    → size
        #   • AI:    "is there fresh news that contradicts this?"   → veto
        #
        # AI budget: Gemini 2.5-flash free tier is 50 RPM / 1500 RPD. With 12
        # scans/day (trade-phase only) × budget=5 = 60 calls/day → well under
        # quota. Previous budget=2 was leaving most candidates unchecked.
        ai_budget = 10
        # Per-scan budget for the pattern-vision Gemini calls (cost control, same
        # idea as ai_budget). Only pattern signals consume it.
        vision_budget = settings.pattern_vision_budget
        # Per-scan budget for the Phase 2B sentiment Gemini calls.
        sentiment_budget = settings.sentiment_budget
        scan_skips: list[tuple[str, str]] = []   # (symbol, gate) — summarised at scan end

        for sig in ranked:
            # Ranked is sorted desc by score; stop once we drop below the floor.
            if sig.score < threshold_floor:
                break

            # Concentration gate — once per-scan new-names budget is spent,
            # only stacking add-ons (already-held symbol) may proceed. Silently
            # skip extras (don't audit or Telegram-summarise — it's by design).
            is_stack_candidate = sig.symbol in held_syms
            if not is_stack_candidate and new_names_opened >= new_names_limit:
                continue

            # P1-2: Late-entry gate — new names after 14:00 ET must clear a
            # higher bar (+5 pts). Stacking add-ons skip this (they already
            # survived a session and the thesis was already validated).
            if _late_cutoff and not is_stack_candidate and sig.score < min(88, threshold_floor + 8):
                _late_reason = (
                    f"late session ({_now_et:%H:%M} ET) — new-name entry requires score ≥ "
                    f"{min(88, threshold_floor + 8)} (got {sig.score})"
                )
                log.info("Skip %s [late_entry]: %s", sig.symbol, _late_reason)
                audit.record("skip", symbol=sig.symbol, gate="late_entry",
                             reason=_late_reason, score=sig.score)
                scan_skips.append((sig.symbol, "late_entry"))
                continue

            # Stacking add-ons require the rule score to clear full threshold —
            # don't pyramid on a marginal setup, even if base setup was strong.
            if is_stack_candidate and sig.score < entry_thr:
                continue

            # "Marginal" = rule score in [threshold-10, threshold). These setups
            # still enter the funnel but get sized down via conviction.
            marginal_setup = sig.score < entry_thr
            setup_conviction = 0.5 if marginal_setup else 1.0

            def _skip(gate: str, reason: str, _sig=sig) -> None:
                log.info("Skip %s [%s]: %s", _sig.symbol, gate, reason)
                audit.record("skip", symbol=_sig.symbol, gate=gate,
                             reason=reason, score=_sig.score)
                # Silence "max positions" / "stacking gate" reasons from the
                # Telegram skip summary — they're routine, not actionable.
                if gate == "risk" and (
                    "max positions" in reason
                    or "max stacks" in reason
                    or "unrealised" in reason
                    or "stacking disabled" in reason
                ):
                    return
                scan_skips.append((_sig.symbol, gate))

            # --- Context fetches (daily df for MTF/gap, ML proba already cached) ---
            df_d = None
            try:
                df_d = c.get_kline(sig.symbol, bars=60, ktype=KLType.K_DAY)
            except Exception as e:
                log.warning("daily fetch failed %s: %s — skipping MTF/gap", sig.symbol, e)

            if df_d is not None:
                if settings.timeframe == "HOUR_1":
                    daily_ok, daily_reason = indicators.daily_trend_bullish(df_d)
                    if not daily_ok:
                        _skip("mtf", daily_reason)
                        continue
                # Directional gap filter: trend/momentum strategies WANT gap-ups
                # (they ARE the breakout signal). Only mean-reversion and pattern
                # strategies should block positive gaps (chase risk → reversion).
                # Gap-downs are ALWAYS blocked — catching a falling knife is never
                # the strategy's edge regardless of direction.
                _sig_strat = getattr(sig, 'strategy', 'trend')
                _block_up = _sig_strat not in ('trend', 'momentum_break')
                gap_ok, gap_reason = indicators.check_gap(
                    df_d, max_gap_pct=settings.max_gap_pct, block_up_gaps=_block_up)
                if not gap_ok:
                    _skip("gap", gap_reason)
                    continue

            # --- Earnings / spread / sector (cheap context veto) ---
            # Sector-exposure gate (2026-06-26: re-enabled with MAX_PER_SECTOR=3).
            # Prevents >60% of capital from concentrating in one sector bucket —
            # natural hedge against sector-wide gap risk (e.g. SOX −5%).
            sector_ok, sector_reason = sector.check_sector_exposure(
                sig.symbol, positions, pending_symbols)
            if not sector_ok:
                _skip("sector", sector_reason)
                continue
            ern_blocked, ern_reason = earnings_block(sig.symbol)
            if ern_blocked:
                _skip("earnings", ern_reason)
                continue

            try:
                spread_pct = c.get_spread_pct(sig.symbol)
                if spread_pct > SPREAD_MAX_PCT:
                    _skip("spread", f"bid-ask {spread_pct:.2f}% > {SPREAD_MAX_PCT}%")
                    continue
            except Exception:
                pass

            # Position conviction (ML subsystem fully removed 2026-07-08; with
            # the hard threshold, setup_conviction is 1.0 — kept for the
            # VIX/DD sizing path).
            conviction = setup_conviction

            # --- AI verdict (Gemini + Tavily news, independent advisory) ---
            # Stacking add-ons skip AI consult — we've already done the diligence
            # on the original entry, and the AI budget should reserve for fresh
            # names where context might differ. (Audit 2026-05-28.)
            #
            # 2026-06-11 reorder: when AI is ADVISORY (default, parity with the
            # backtest which has no AI layer), the consult runs AFTER the order
            # is placed — it cannot change the decision, and running it first
            # injected median 42s / p90 99s between signal and order, which the
            # 0.2% chase tolerance now converts into missed fills. Only a
            # BLOCKING veto (AI_VETO_BLOCKING=true) still pays latency up front.
            ai_deferred = False
            if is_stack_candidate:
                ai_pass, ai_score, ai_reason = True, 60, "stack — AI re-check skipped"
            elif settings.ai_veto_blocking:
                if ai_budget <= 0:
                    ai_pass, ai_score, ai_reason = True, 50, "AI budget exhausted — neutral"
                else:
                    ai_pass, ai_score, ai_reason = ai_validator.validate(sig)
                    ai_budget -= 1
                if not ai_pass:
                    _skip("ai_veto", ai_reason)
                    continue
            else:
                ai_deferred = True
                ai_pass, ai_score, ai_reason = True, None, "advisory — consulted post-order"
            log.info("%s rule=%.1f ai=%s conviction=%.2f (%s)",
                     sig.symbol, sig.score,
                     "pass" if ai_pass else "veto", conviction, ai_reason)

            # P0-5 (2026-06-26): pattern_vision removed from live path.
            # Geometric detection (pattern_detect.py) is deterministic, fast,
            # and works in both live + backtest. AI chart-image confirmation
            # was slow, expensive, and added latency without verified edge.
            # The module stays for future opt-in, but the live path skips it.
            vision_conf = vision_label = vision_reason = None

            # --- broker-style sentiment read (Phase 2B; advisory) ---
            # 看好/中性/看空 multi-factor score. ADVISORY — never changes which
            # trade fires (parity). Optional SENTIMENT_SIZING folds the 0-100
            # score into conviction (sizing only). FAIL-SAFE → neutral 50.
            sent_verdict = sent_reason = None
            sent_score = None
            # 2026-07-27: don't pay for sentiment on a name that cannot be sized
            # no matter what the score comes back as. SENTIMENT_SIZING can only
            # scale conviction by up to 1.25 (see below), so if qty is already 0
            # at that BEST case, no verdict can rescue the entry — skip before
            # spending the call. Purely a cost saving: any name that could pass
            # still reaches the real gate below with its true conviction.
            _best_conv = conviction * (1.25 if settings.sentiment_sizing else 1.0)
            if risk_manager.calc_position_size(
                    sig, vix=vix, conviction=_best_conv,
                    regime_mult=max(1.0, settings.regime_bull_mult)) <= 0:
                _skip("risk", "qty=0 even at best-case conviction — "
                              "skipped before sentiment spend")
                continue
            if (settings.sentiment_scoring_enabled and not is_stack_candidate
                    and sentiment_budget > 0):
                # P0-5 (2026-06-26): options_flow removed — noise > signal at $5K
                # scale. AI validator handles sentiment from news better.
                try:
                    sent_verdict, sent_score, sent_reason = \
                        ai_validator.assess_sentiment(sig, "")
                except Exception as e:
                    sent_verdict, sent_score, sent_reason = "neutral", 50, f"sentiment error: {e}"
                sentiment_budget -= 1
                log.info("%s sentiment=%s score=%s (%s)",
                         sig.symbol, sent_verdict, sent_score, sent_reason)
                if settings.sentiment_sizing and sent_score is not None:
                    conviction *= max(0.5, min(1.25, sent_score / 50.0))

            # --- aggregate options flow (2026-08-05; advisory) ---
            # call volume vs its own 20d mean. Measured monotonic against
            # forward return over 5,980 name-days; the put/call SKEW was flat,
            # so only the volume is read here. Earnings days are excluded inside
            # assess() — they are 39% of the spikes and a different, much fatter
            # distribution. ADVISORY: recorded and logged, never changes which
            # trade fires. Only OPTIONS_STATS_SIZING lets it touch conviction,
            # and then only upward on a spike (see conviction_multiplier).
            # Placed after the qty>0 pre-check above so a name that cannot be
            # sized never spends an API call. FAIL-SAFE -> no-opinion.
            opt_stats = None
            if settings.options_stats_enabled and not is_stack_candidate:
                try:
                    opt_stats = options_stats.assess(sig.symbol, c)
                except Exception as e:
                    log.warning("%s options-stats failed: %s", sig.symbol, e)
                    opt_stats = None
                if opt_stats is not None and opt_stats.ok:
                    log.info("%s options: call_rvol=%.2f p/c=%s %s (%s)",
                             sig.symbol, opt_stats.call_rvol or 0,
                             opt_stats.put_call_ratio, opt_stats.label,
                             "advisory" if not settings.options_stats_sizing else "sizing armed")
                    conviction *= options_stats.conviction_multiplier(opt_stats)

            # Regime up-scaling (owner-approved tailwind press) — mirror the honest
            # engine exactly: boost size ONLY in a confirmed strong bull AND calm
            # VIX. settings.regime_bull_mult defaults to 1.0 (inert) until the
            # owner sets REGIME_BULL_MULT in .env. Computed BEFORE the risk gate
            # (2026-07-07) so can_open_new's cash/budget checks price the SAME
            # qty we actually order below.
            regime_mult = (settings.regime_bull_mult
                           if (regime is not None and regime.bullish
                               and vix < settings.regime_vix_calm)
                           else 1.0)

            # --- Risk / heat / sizing (all factor in conviction) ---
            ok, reason = risk_manager.can_open_new(
                sig, positions, cash, pending_value, pending_symbols,
                vix=vix, conviction=conviction, regime_mult=regime_mult,
            )
            if not ok:
                _skip("risk", reason)
                continue

            qty = risk_manager.calc_position_size(
                sig, vix=vix, conviction=conviction, regime_mult=regime_mult)

            # 2026-06-03: portfolio.heat_check gate removed — structurally
            # non-binding (heat cap = 20% of account while per-trade risk is also
            # a % of account, so both scale together and it never fires).
            # portfolio.heat_check() is kept as the heat primitive but not called.

            # P0-2 (2026-06-26): structural stop — compute a swing-low-based
            # stop and pre-set it on the signal. executor then takes the TIGHTER
            # of the ATR stop and this structural level. Market-makers can scan
            # for ATR clusters; they can't see a swing low.
            sig_df = all_dfs.get(sig.symbol)
            if sig_df is not None:
                try:
                    from . import pattern_detect
                    sig.structural_stop = pattern_detect.structural_stop(
                        sig_df, sig.price, sig.stop_loss)
                except Exception:
                    pass  # structural stop is optional — never block on it

            try:
                opened = executor.open_position(c, sig, qty)
                if opened is None:
                    # Executor declined the entry (chase / stale signal /
                    # cooldown / malformed levels). Not an error — audit the
                    # real gate so skips stay distinguishable.
                    _gate, _why = executor.last_entry_skip()
                    audit.record("skip", symbol=sig.symbol, gate=_gate,
                                 reason=_why)
                    continue
                if not is_stack_candidate:
                    new_names_opened += 1
                    held_syms.add(sig.symbol)
                # Deferred advisory consult — order is already resting, so this
                # latency is free. Wrapped: an AI/network error must never lose
                # the audit record of an order we just placed.
                if ai_deferred:
                    if ai_budget > 0:
                        try:
                            ai_pass, ai_score, ai_reason = ai_validator.validate(sig)
                        except Exception as e:
                            ai_pass, ai_score, ai_reason = True, None, f"AI consult failed: {e}"
                        ai_budget -= 1
                        if not ai_pass:
                            ai_reason = f"[advisory veto, post-order] {ai_reason}"
                    else:
                        ai_score, ai_reason = 50, "AI budget exhausted — neutral"
                audit.record("buy", symbol=sig.symbol, score=sig.score,
                             reason=ai_reason,
                             extra={"qty": qty,
                                    # 2026-07-16: record the trade's ACTUAL
                                    # fill-anchored levels (executor re-anchors
                                    # stop/TP to the entry price); keep the
                                    # signal-bar price for staleness forensics.
                                    "price": opened.get("entry_price", sig.price),
                                    "signal_price": sig.price,
                                    "stop": opened.get("stop_loss", sig.stop_loss),
                                    "tp": opened.get("take_profit", sig.take_profit),
                                    "vix": vix, "regime": regime.label,
                                    "conviction": conviction,
                                    # 2026-06-11: persist the numeric advisory
                                    # verdict — without it no AI-vs-outcome
                                    # calibration is possible (keep/drop the
                                    # Gemini layer needs ~50 scored trades).
                                    "ai_score": ai_score,
                                    "ai_pass": bool(ai_pass),
                                    "setup_quality": "marginal" if marginal_setup else "full",
                                    "is_stack": is_stack_candidate,
                                    "strategy": getattr(sig, "strategy", "trend"),
                                    # Phase 2B sentiment (advisory; None when off).
                                    "sentiment_verdict": sent_verdict,
                                    "sentiment_score": sent_score,
                                    # Aggregate options flow (advisory; None when
                                    # off). Persisted so the factor can be scored
                                    # against real outcomes later — the 1y study
                                    # behind it is bull-market-only, so forward
                                    # samples are the only way it earns trust.
                                    "call_rvol": (opt_stats.call_rvol
                                                  if opt_stats and opt_stats.ok else None),
                                    "options_label": (opt_stats.label
                                                      if opt_stats and opt_stats.ok else None),
                                    # Pattern strategy: persist what was detected +
                                    # the vision verdict so the dashboard can show
                                    # it and we can calibrate vision-vs-outcome.
                                    **({"pattern_type": sig.meta.get("pattern_type"),
                                        "pattern_confidence": sig.meta.get("pattern_confidence"),
                                        "key_levels": sig.meta.get("key_levels"),
                                        "vision_confidence": vision_conf,
                                        "vision_label": vision_label,
                                        "vision_reason": vision_reason}
                                       if sig.strategy == "pattern" else {})})
                notifier.send(notifier.signal_msg(sig, ai_reason, qty))
                cash -= qty * sig.price
                pending_value += qty * sig.price
                pending_symbols.add(sig.symbol)
            except Exception as e:
                log.exception("open_position failed for %s: %s", sig.symbol, e)
                audit.record("error", symbol=sig.symbol, reason=str(e))
                # Buffer into the end-of-scan summary so several failures on
                # the same broker-side issue collapse into one notification.
                scan_skips.append((sig.symbol, "exec_error"))

    # Skip summaries are routine, not actionable — log only, no Telegram
    # (owner request 2026-07-11). Exception: exec_error means an order
    # failed at the broker, which needs eyes, so that alone still pushes.
    if scan_skips:
        by_gate: dict[str, list[str]] = {}
        for sym, gate in scan_skips:
            by_gate.setdefault(gate, []).append(sym)
        summary = "; ".join(f"{gate}: {', '.join(syms)}"
                            for gate, syms in sorted(by_gate.items()))
        log.info("Scan skips (%d): %s", len(scan_skips), summary)
        if "exec_error" in by_gate:
            notifier.send(notifier.exec_fail_msg(by_gate["exec_error"]))

    audit.record("scan_end")
    log.info("=== scan end ===")


# Canonical NYSE calendar now lives in clock.py (shared with the web dashboard).
# Kept as an alias because executor.py imports it via `from .main import _nyse_holidays`.
_nyse_holidays = clock.nyse_holidays


def in_market_hours() -> bool:
    now = clock.ny_now()   # drift-corrected
    session = clock.market_session(now)
    if session == "holiday":
        log.info("NYSE holiday today (%s) — skipping scan", now.date())
    return session == "open"


def _universe_refresh_job() -> None:
    """Sunday 22:00 ET — recompute the rule-based trading universe (Phase 1).

    watchlist := top UNIVERSE_TOP_N of the liquidity pool by 6-1 momentum
    (src/universe.py — same rule the backtest replays walk-forward, so live
    trades exactly what the validation measured). Flag-gated by
    DYNAMIC_UNIVERSE_ENABLED; every change is Telegram-notified (铁律: no
    silent universe drift). Runs BEFORE the 22:30 weekly backtest so the
    health check scores the list the bot will actually trade next week.
    Positions in dropped names are unaffected — exits manage them out."""
    if not settings.dynamic_universe_enabled:
        cron_state.record_run("universe_refresh")   # keep catchup quiet while off
        return
    log.info("universe refresh: starting")
    try:
        from . import universe
        with client() as c:
            new_list = universe.compute_live_universe(c)
        if len(new_list) < max(3, settings.universe_top_n // 2):
            raise RuntimeError(
                f"selection returned only {len(new_list)} names — refusing to "
                "shrink the watchlist on bad/missing data")
        old_list = load_watchlist()
        if set(new_list) == set(old_list):
            log.info("universe refresh: unchanged (%d names)", len(new_list))
        else:
            adds = sorted(set(new_list) - set(old_list))
            drops = sorted(set(old_list) - set(new_list))
            WATCHLIST_FILE.write_text(json.dumps({
                "_comment": ("AUTO-GENERATED by the weekly universe refresh "
                             "(src/universe.py: top-N of config/universe_pool.json "
                             "by 6-1 momentum). Hand edits are overwritten on the "
                             "next refresh — edit the POOL (liquidity criterion "
                             "only) or UNIVERSE_TOP_N instead."),
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "tickers": new_list,
            }, indent=2))
            notifier.send(
                "🔄 *Weekly universe refresh* (rule: top-{n} by 6-1 momentum)\n"
                "  + {adds}\n  − {drops}\n  → {full}".format(
                    n=settings.universe_top_n,
                    adds=", ".join(adds) or "—",
                    drops=", ".join(drops) or "—",
                    full=", ".join(new_list)))
            log.info("universe refresh: %d adds, %d drops", len(adds), len(drops))
        cron_state.record_run("universe_refresh")
    except Exception as e:
        log.exception("universe refresh failed: %s", e)
        notifier.send(f"⚠ Weekly universe refresh failed (watchlist unchanged): {e}")


def _weekly_backtest_validation_job() -> None:
    """Sunday 22:30 ET — runs a 90-day backtest under current .env settings.

    Purpose: continuous health-check. If the strategy starts degrading (rule
    changes in the market, watchlist drift, etc.) the Sortino on a rolling
    90-day window will fall before live results do, giving early warning.

    Result is logged + a one-line summary is sent to Telegram. Does NOT
    interrupt or modify the live trading loop — purely diagnostic.
    """
    log.info("weekly backtest validation: starting")
    try:
        from .backtest import run_backtest
        from .optimizer_ai import _base_cfg
        # 2026-06-11: the health check MUST score the strategy the bot actually
        # runs. _base_cfg handles both parity rules in one place: (a) runtime-
        # effective threshold/tp/sl/risk/budget (not frozen .env — after an
        # applied param_change the old construction health-checked parameters
        # the bot no longer ran), and (b) the dynamic universe replayed
        # WALK-FORWARD over the full pool. Building this cfg from the live
        # watchlist file instead would backtest a list selected on recent
        # momentum over the very window that selected it — the exact hindsight
        # bias Phase 1 exists to kill, inflating the one signal that is
        # supposed to catch strategy decay.
        cfg = _base_cfg(days=90)
        result = run_backtest(cfg)
        m = result.get("metrics", {})
        mc = m.get("monte_carlo", {})
        prob_str = (f"{mc['prob_profitable_pct']}%"
                    if "prob_profitable_pct" in mc else "—")
        notifier.send(
            f"📊 *Weekly backtest* ({cfg.timeframe}, {cfg.days}d)\n"
            f"  Trades:     {m.get('total_trades', 0)}\n"
            f"  Win rate:   {m.get('win_rate_pct', 0)}%\n"
            f"  Sortino:    {m.get('sortino_ratio', 0)}\n"
            f"  Profit factor: {m.get('profit_factor', 0)}\n"
            f"  Net PnL:    ${m.get('net_pnl_usd', 0):+.2f}  "
            f"({m.get('total_return_pct', 0):+.1f}%)\n"
            f"  Max DD:     {m.get('max_drawdown_pct', 0):.1f}%\n"
            f"  P(profit):  {prob_str}"
        )
        log.info("weekly backtest done: trades=%d sortino=%s",
                 m.get("total_trades", 0), m.get("sortino_ratio", 0))
        cron_state.record_run("weekly_backtest")
    except Exception as e:
        log.exception("weekly backtest failed: %s", e)
        notifier.send(f"⚠ Weekly backtest failed: {e}")


def _monthly_optuna_job() -> None:
    """1st of each month 03:00 ET — runs a 30-trial Optuna walk-forward study.

    Best params are saved to `data/optimizer/*.json` and a summary is sent
    to Telegram. Does **NOT** auto-update `.env` — the user reviews and
    decides whether to apply. This is intentional: blindly chasing the latest
    Optuna winner is overfitting to last month's regime.
    """
    log.info("monthly Optuna optimization: starting")
    try:
        from .optimizer import run_study
        summary = run_study(
            n_trials=30,
            days=60,
            n_folds=3,
            min_trades=30,
            timeframe=settings.timeframe,
            fast_mode=True,
        )
        bp = summary.get("best_params", {})
        attrs = summary.get("best_user_attrs", {})
        msg = (
            f"🧪 *Monthly Optuna* ({summary['base_config']['timeframe']}, "
            f"{summary['base_config']['days']}d, {summary['n_trials']} trials)\n"
            f"  Best Sortino: {summary.get('best_value_sortino', '?')}\n"
            f"  n_trades:     {attrs.get('n_trades', '?')}\n"
            f"  worst-fold:   {attrs.get('sortino_min', '?')}\n"
            f"\n*Suggested params* (review before applying):\n"
            f"  threshold     = {bp.get('threshold', '?')}\n"
            f"  tp_atr_mult   = {bp.get('tp_atr_mult', '?')}\n"
            f"  sl_atr_mult   = {bp.get('sl_atr_mult', '?')}\n"
            f"  max_gap_pct   = {bp.get('max_gap_pct', '?')}\n"
            f"  base_slip_bp  = {bp.get('base_slip_bp', '?')}\n"
            f"\n*Live .env now*: thr={settings.entry_threshold}, "
            f"tp={settings.tp_atr_mult}, sl={settings.sl_atr_mult}, "
            f"gap={settings.max_gap_pct}"
        )
        notifier.send(msg)
        log.info("monthly Optuna done: best_sortino=%s params=%s",
                 summary.get("best_value_sortino"), bp)
        cron_state.record_run("monthly_optuna")
    except Exception as e:
        log.exception("monthly Optuna failed: %s", e)
        notifier.send(f"⚠ Monthly Optuna failed: {e}")


def _monthly_lever_recheck_job() -> None:
    """1st of each month 03:30 ET — re-validate the data-derived exit levers.

    Born from the 2026-06-25 MAE/MFE → lever sweep: most single-trade heuristics
    failed portfolio validation; only widening TP (8→10) helped. This re-runs the
    TP/SL drift check monthly so a regime shift that moves the optimum is flagged
    early. Telegrams a one-screen summary; like monthly Optuna it **does NOT auto-
    edit .env** — the owner reviews. Runs after the 03:00 Optuna study.
    """
    log.info("monthly lever recheck: starting")
    try:
        from . import lever_recheck
        res = lever_recheck.monthly_recheck(days=180)
        notifier.send(lever_recheck.format_telegram(res))
        enqueued = lever_recheck.apply_suggestions(res)
        if enqueued:
            notifier.send(
                f"📥 已把 {len(enqueued)} 条 lever 建议放进审批队列 — 在 Telegram/GUI "
                f"一键批准即生效（实盘热改、无需重启、带自动回滚）。")
        log.info("monthly lever recheck done: suggestions=%d enqueued=%d",
                 len(res["suggestions"]), len(enqueued))
        cron_state.record_run("monthly_lever_recheck")
    except Exception as e:
        log.exception("monthly lever recheck failed: %s", e)
        notifier.send(f"⚠ Monthly lever recheck failed: {e}")


def _daily_blacklist_review_job() -> None:
    """Daily 23:00 ET — adaptive blacklist evaluation.

    Adds new chronic losers, removes recovered names, extends review periods
    for symbols that are still bad. Telegram summary fires only when anything
    changed (no spam on quiet days).
    """
    log.info("daily blacklist review: starting")
    try:
        summary = blacklist.evaluate_all(notifier_send=notifier.send)
        log.info("blacklist review done: %s", summary)
        cron_state.record_run("daily_blacklist")
    except Exception as e:
        log.exception("blacklist review failed: %s", e)


def _daily_auto_budget_job() -> None:
    """Daily after the close (16:45 ET) — recompute the auto-compounding budget.

    No-op unless AUTO_BUDGET_ENABLED (or the web toggle) is on. When armed, it
    grows/shrinks the deployable budget off realized profit, bounded by the
    seed-relative floor/ceil + live account equity, and Telegram-notifies any
    change (铁律: never silent). The DD breaker is unaffected — equity stays
    anchored to the frozen seed (see src/auto_budget.py / equity_baseline)."""
    try:
        from . import auto_budget
        res = auto_budget.recompute_and_apply()
        if res.get("applied"):
            log.warning("auto_budget applied: $%.0f → $%.0f",
                        res.get("old", 0), res.get("new", 0))
        else:
            log.info("auto_budget: %s", res.get("reason", "no change"))
        cron_state.record_run("auto_budget")
    except Exception as e:
        log.exception("auto_budget recompute failed: %s", e)


def _weekly_autopilot_job() -> None:
    """Monday 20:00 KL (timezone-pinned) — DeepSeek autonomous portfolio manager."""
    log.info("weekly autopilot: starting")
    try:
        from . import autopilot
        result = autopilot.weekly_autopilot()
        log.info("weekly autopilot done: %s", result.get("status", "unknown"))
        cron_state.record_run("weekly_autopilot")
    except Exception as e:
        log.exception("weekly autopilot failed: %s", e)
        notifier.send(f"⚠ Weekly autopilot failed: {e}")


def _weekly_self_review_job() -> None:
    """Sunday 23:00 ET — review the past week's REAL fills and emit suggestions
    (analyze→notify→approve). Never changes live behavior on its own. Each
    autonomous step runs in its OWN try, so a failure in one (e.g. a notify hiccup
    in the review) can't silently skip self-improvement or the optimizer."""
    log.info("weekly self-review: starting")
    # Autopilot rollback check FIRST — if an auto-applied param is hurting live
    # results, revert it before this week's review/optimizer reason about it.
    try:
        from . import autopilot
        for note in autopilot.check_and_rollback():
            notifier.send(note)
    except Exception as e:
        log.exception("autopilot rollback check failed: %s", e)
    report = None
    try:
        report = self_review.run_and_notify(days=7)
        log.info("weekly self-review done: %d trades, $%.2f/day, %d suggestions",
                 report.get("n_trades", 0), report.get("per_day", 0.0),
                 len(report.get("suggestions", [])))
    except Exception as e:
        log.exception("weekly self-review failed: %s", e)
        try:
            notifier.send(f"⚠ Weekly self-review failed: {e}")
        except Exception:
            pass
    # Evidence-based self-improvement (half-Kelly risk + universe review) → approval
    # queue. Reads real fills directly, so it's independent of the review/notify above.
    try:
        si = self_improve.run_all()
        log.info("self-improve: kelly_proposed=%s universe_drops=%s",
                 si.get("kelly_proposed"), si.get("universe_dropped_proposed"))
    except Exception as e:
        log.warning("self-improve proposals failed: %s", e)
    # Autonomous Gemini optimizer — INDEPENDENT step so a review/notify failure
    # doesn't silently skip the one auto path that proposes param changes. Reuses
    # this run's report, else recomputes. No-op until GEMINI_API_KEYS is set.
    try:
        from . import optimizer_ai
        rev = report if report is not None else self_review.weekly_review(days=7)
        n = optimizer_ai.propose_from_review(rev)
        if n:
            # Each change already got its own detailed notification from
            # propose_from_review (auto-applied vs queued); this is just the
            # weekly summary line. Pre-2026-06-12 it claimed everything was
            # "待批准", which misled the owner when auto-apply was on.
            if settings.auto_apply_params:
                notifier.send(
                    f"🤖 Gemini 优化器: {n} 条参数变更通过了回测验证 — "
                    f"边界内的已自动应用(见上方单独通知), 越界的才会出现在审批队列。")
            else:
                notifier.send(f"🤖 Gemini 优化器提了 {n} 条参数建议 — 待你在 GUI/CLI/Telegram 批准。")
    except Exception as e:
        log.warning("weekly optimizer step failed: %s", e)
    # Bookkeeping — also independent so it always runs.
    try:
        approvals.purge_resolved()
        cron_state.record_run("self_review")
    except Exception as e:
        log.warning("self-review bookkeeping failed: %s", e)


def _grid_sweep_job(daily: bool) -> None:
    """Parameter grid sweep — Mon 07:00 KL (quick, 27 combos) / weekdays 09:00 KL
    (daily neighborhood walk). Both slots are 19:00-21:00 ET, market CLOSED.

    2026-07-28: this used to live in the user's crontab calling
    cron/optimize_and_apply.sh. Two problems with that: the crontab entry had an
    unquoted path with a space, so it had never run once since 2026-07-07; and
    more fundamentally a crontab is per-machine, so anyone installing the
    packaged .app got no optimization pipeline at all. Scheduling it here means
    it ships with the app and inherits the app's own file-access permissions
    instead of needing /usr/sbin/cron to be separately granted.

    Runs IN-PROCESS rather than as a subprocess, deliberately: the frozen build
    has no .venv to re-exec (web.server._worker_cmd points at ROOT/.venv, which
    doesn't exist under ~/Library/Application Support), so a subprocess would
    work in dev and silently fail in the packaged app — the exact class of bug
    this change exists to remove. In-process is safe here because the sweep
    injects grid params into live db-state and therefore only ever runs with the
    market closed, when scan_once / the manage tick / the fast-stop tick all
    no-op on their own in_market_hours() checks. optimize() additionally refuses
    outright if the session is open, so a DST shift or a manual trigger can't
    catch us mid-session.
    """
    key = "grid_sweep_daily" if daily else "grid_sweep_weekly"
    label = "daily neighborhood walk" if daily else "weekly quick grid"
    log.info("grid sweep (%s): starting", label)
    try:
        from .optimize_system import optimize
        result = optimize(quick=not daily, daily=daily, quiet=True)
    except Exception as e:
        log.exception("grid sweep (%s) failed: %s", label, e)
        try:
            notifier.send(f"⚠ 参数网格搜索({label})失败: {e}")
        except Exception:
            pass
        return

    if result.get("refused"):
        # Market open (DST shift, or a manual run) — not an error, just skipped.
        log.warning("grid sweep (%s) refused: %s", label, result.get("note", ""))
        return

    applied, best = result.get("applied"), result.get("best") or {}
    if applied:
        notifier.send(
            f"🔧 *参数网格搜索已应用* ({label})\n"
            f"  THR={best.get('th', '—')} TP={best.get('tp', '—')} "
            f"SL={best.get('sl', '—')} agg={best.get('agg_score', '—')}\n"
            f"  combos={result.get('combos_tested', '?')} "
            f"windows={len(result.get('windows', []))} "
            f"{result.get('elapsed_sec', '?')}s")
        log.info("grid sweep (%s): APPLIED %s", label, best)
    else:
        # Weekly reports even on no-change (matches the old script's behaviour);
        # daily stays quiet so there's no every-morning "nothing happened" ping.
        log.info("grid sweep (%s): no change (baseline holds)", label)
        if not daily:
            notifier.send(
                f"🔧 参数网格搜索({label}): 无变更，当前参数仍最优 "
                f"(combos={result.get('combos_tested', '?')})")
    try:
        cron_state.record_run(key)
    except Exception as e:
        log.warning("grid sweep bookkeeping failed: %s", e)


def _weekly_sandbox_diff_job() -> None:
    """Monday 20:25 KL — trade-level differential between the sandbox replay and
    the backtest_v3 live-fidelity engine (scripts/sandbox_vs_backtest.py).

    Catches silent drift between the two INDEPENDENT strategy implementations at
    the individual-trade level (engine_compare only cross-checks the two fast
    engines). Runs as a SUBPROCESS so a crash/hang can never touch the trading
    loop. Telegram ONLY on breach or on the check itself failing (owner
    preference: actionable events only) — a clean pass is logged + audited.
    """
    from .config import IS_FROZEN
    if IS_FROZEN:
        # Dev-only cross-check: scripts/ isn't shipped in the packaged .app, and
        # re-exec'ing the bundled binary with a script path wouldn't work anyway.
        log.info("weekly sandbox diff: skipped (packaged build)")
        return
    log.info("weekly sandbox diff: starting")
    try:
        import subprocess
        root = Path(__file__).resolve().parent.parent
        proc = subprocess.run(
            [sys.executable, str(root / "scripts" / "sandbox_vs_backtest.py"),
             "--days", "30"],
            cwd=str(root), capture_output=True, text=True, timeout=1800)
        if proc.returncode == 0:
            log.info("weekly sandbox diff: OK — within tolerance")
            db.audit_insert("sandbox_diff", reason="OK — within tolerance")
        elif proc.returncode == 2:
            s, detail = {}, ""
            try:
                rep = json.loads((root / "data" / "sandbox_vs_backtest.json").read_text())
                s = rep.get("summary", {})
                detail = "\n".join(f"  ✗ {b}" for b in rep.get("breaches", []))
            except Exception:
                detail = "  (report unreadable — see data/sandbox_vs_backtest.json)"
            notifier.send(
                "⚠ *Sandbox↔backtest 分歧超容差* (30d)\n"
                f"{detail}\n"
                f"  matched {s.get('n_matched', '?')} | sandbox {s.get('n_sandbox', '?')} "
                f"vs v3 {s.get('n_v3', '?')} 笔\n"
                f"  net: sb ${s.get('net_pnl_sandbox', 0):+,.0f} vs v3 "
                f"${s.get('net_pnl_v3', 0):+,.0f}\n"
                "  详见 data/sandbox_vs_backtest.json — 两个引擎有一个漂了")
            db.audit_insert("sandbox_diff", reason=f"BREACH: {detail[:200]}")
        else:
            tail = (proc.stderr or proc.stdout or "no output").strip()[-300:]
            notifier.send(f"⚠ Weekly sandbox diff 运行失败 (exit {proc.returncode}):\n{tail}")
        cron_state.record_run("sandbox_diff")
    except Exception as e:
        log.exception("weekly sandbox diff failed: %s", e)
        try:
            notifier.send(f"⚠ Weekly sandbox diff failed: {e}")
        except Exception:
            pass


def _run_catchup_on_startup() -> None:
    """Detect and run any scheduled jobs that were missed while the laptop was off.

    Compares each known job's persisted `last_run` to its most-recent expected
    fire time. If `last_run < expected`, we fire the job ONCE (no backlog
    flooding — one catchup per missed cycle is enough).

    On a first-ever boot we seed the state with NOW so we don't run every
    job at once. Sends a Telegram summary so the user knows what fired.
    """
    # Seed empty state — fresh installs don't fire anything.
    is_first_boot = cron_state.initialize_if_empty()
    if is_first_boot:
        log.info("catchup: first boot detected — cron_state seeded, no catchup needed")
        return

    # Map each known job → (expected_last_fire, runner_function, label).
    plan = [
        ("daily_blacklist",
         cron_state.expected_last_fire_daily(23, 0, weekdays_only=True),
         _daily_blacklist_review_job, "Daily blacklist review"),
        ("auto_budget",
         cron_state.expected_last_fire_daily(16, 45, weekdays_only=True),
         _daily_auto_budget_job, "Daily auto-compounding budget"),
        # watchlist_refresh + ml_retrain catch-up removed 2026-06-03 (see run_loop):
        # both were silent-execution violations; now manual GUI actions only.
        # universe_refresh (Phase 1) is different: it's a RULE the owner enabled
        # explicitly via DYNAMIC_UNIVERSE_ENABLED, and every change telegrams.
        # ⚠ The weekly expected-fire times live in cron_state.WEEKLY_SCHEDULE —
        # the single source of truth shared with web/server.py's boot catchup
        # (2026-07-27). They MUST match the run_loop cron schedule below (Mon
        # 20:00-20:25 KL, timezone-pinned). If they drift apart, every restart
        # after the real run sees last_run < expected and re-fires the whole
        # Monday chain — double autopilot param applies, duplicate Telegram
        # storms. (P1 fix 2026-07-07; de-duplicated across processes 2026-07-27.)
        ("universe_refresh",
         cron_state.expected_last_fire("universe_refresh"),
         _universe_refresh_job, "Weekly universe refresh"),
        ("weekly_backtest",
         cron_state.expected_last_fire("weekly_backtest"),
         _weekly_backtest_validation_job, "Weekly backtest"),
        ("weekly_autopilot",
         cron_state.expected_last_fire("weekly_autopilot"),
         _weekly_autopilot_job, "Weekly autopilot (DeepSeek)"),
        ("self_review",
         cron_state.expected_last_fire("self_review"),
         _weekly_self_review_job, "Weekly self-review"),
        ("sandbox_diff",
         cron_state.expected_last_fire("sandbox_diff"),
         _weekly_sandbox_diff_job, "Weekly sandbox↔backtest diff"),
        # Grid sweep (2026-07-28, ex-crontab). Both entries are safe to catch up
        # because optimize() refuses outright while the market is open, so a
        # restart during RTH re-schedules rather than sweeping mid-session.
        ("grid_sweep_weekly",
         cron_state.expected_last_fire("grid_sweep_weekly"),
         lambda: _grid_sweep_job(daily=False), "Weekly parameter grid sweep"),
        ("grid_sweep_daily",
         cron_state.expected_last_fire_daily_kl("grid_sweep_daily"),
         lambda: _grid_sweep_job(daily=True), "Daily parameter re-validation"),
        ("monthly_optuna",
         cron_state.expected_last_fire_monthly(1, 3, 0),
         _monthly_optuna_job, "Monthly Optuna optimization"),
        ("monthly_lever_recheck",
         cron_state.expected_last_fire_monthly(1, 3, 30),
         _monthly_lever_recheck_job, "Monthly lever recheck"),
    ]

    missed = [(name, fn, label) for name, exp, fn, label in plan
              if cron_state.needs_catchup(name, exp)]

    if not missed:
        log.info("catchup: no missed jobs — all schedules up to date")
        return

    labels = [label for _, _, label in missed]
    log.info("catchup: %d missed job(s) → %s", len(missed), labels)
    try:
        notifier.send("🔄 *Catching up missed scheduled jobs*\n  "
                      + "\n  ".join(f"• {label}" for label in labels)
                      + "\n  (laptop was off when these were due)")
    except Exception as e:
        log.warning("catchup: notifier failed: %s", e)

    for name, fn, label in missed:
        log.info("catchup: running %s (%s)", label, name)
        try:
            fn()
        except Exception as e:
            log.exception("catchup: %s failed: %s", label, e)
            try:
                notifier.send(f"⚠ Catchup '{label}' failed: {e}")
            except Exception:
                pass

    try:
        notifier.send(f"✓ Catchup complete — ran {len(missed)} missed job(s)")
    except Exception:
        pass


def _preopen_clock_check_job() -> None:
    """PRE-OPEN (08:30 ET, one hour before the bell): force a network time
    re-check so the trading day starts on verified time. Quiet by design
    (owner request 2026-07-11): OK and drift-corrected results are log-only;
    Telegram fires only when the time can't be verified (all network sources
    down) or the market is closed for a NYSE holiday."""
    try:
        st = clock.force_refresh()
        session = clock.market_session(clock.ny_now())
        log.info("preopen clock check: session=%s drift=%+.1fs source=%s offset=%s",
                 session, st.get("last_drift_sec", 0.0), st.get("source"),
                 st.get("offset_sec"))
        msg = notifier.clock_check_msg(st, session)
        if msg:
            notifier.send(msg)
        cron_state.record_run("preopen_clock_check")
    except Exception as e:
        log.exception("preopen clock check failed: %s", e)


def _premarket_gap_sentinel_job() -> None:
    """PRE-OPEN (09:00 ET): pull the latest overnight news for each HELD name and
    AI-assess overnight gap-down risk, then alert + QUEUE the flagged names for
    exit at the open. Analysis only — places no orders (pre-market liquidity is
    thin; the actual sell runs at the open against real liquidity)."""
    if not settings.gap_sentinel_enabled:
        return
    try:
        with client() as c:
            positions = c.get_positions()
        held: list[str] = []
        if positions is not None and not positions.empty:
            rh = positions[positions["qty"].astype(float) > 0]
            held = [code.split(".")[-1] for code in rh["code"].tolist()]
        flagged: list[tuple[str, str]] = []
        for sym in held:
            try:
                ok, reason = gap_sentinel.assess(sym)
                if ok:
                    flagged.append((sym, reason))
            except Exception as e:
                log.warning("premarket sentinel assess %s failed: %s", sym, e)
        gap_sentinel.queue_exits(flagged)
        if flagged:
            lines = "\n".join(f"• {s} — {r}" for s, r in flagged)
            notifier.send(f"🌅 盘前跳空预警 — 开盘清仓:\n{lines}")
        else:
            log.info("premarket gap sentinel: %d holdings checked, none flagged", len(held))
        cron_state.record_run("premarket_gap_sentinel")
    except Exception as e:
        log.exception("premarket gap sentinel failed: %s", e)


def _open_gap_exit_job() -> None:
    """AT THE OPEN (09:31 ET): sell the names the pre-market sentinel flagged, at
    a fresh real-time price (real liquidity). Runs before the 09:45 entry scan so
    a known overnight catalyst is exited at the open, not 15 minutes into it."""
    if not settings.gap_sentinel_enabled:
        return
    pending = gap_sentinel.pending_exits()
    if not pending:
        return
    try:
        with client() as c:
            for sym, reason in pending:
                try:
                    action = executor.close_position(c, sym, "GAP_RISK")
                    notifier.send(f"⚠️ 开盘跳空清仓: {sym} @ ${action['price']:.2f} "
                                  f"(已实现 ${action['pnl']:+.0f}) — {reason}")
                except Exception as e:
                    # Already closed (stopped out overnight), halted, or untracked
                    # — log + skip; never abort the rest of the queue.
                    log.warning("open gap-exit %s skipped: %s", sym, e)
        gap_sentinel.clear_queue()
        cron_state.record_run("open_gap_exit")
    except Exception as e:
        log.exception("open gap-exit job failed: %s", e)


def _startup_protect_stops() -> None:
    """Run ONE protective-exit pass before startup catchup (see run_loop).

    No-op when flat or outside regular hours — that's the common restart case
    and it must stay free. Never raises: the scheduler has to arm regardless."""
    try:
        if not executor.has_open_trades():
            return
        if not in_market_hours():
            log.info("startup: holding but market closed — stop check deferred "
                     "to the open")
            return
        log.info("startup: open positions during market hours — running a "
                 "protective stop pass BEFORE catchup")
        with client() as c:
            actions = executor.manage_stops_only(c)
            for a in actions:
                notifier.send(notifier.trade_action_msg(a))
            if actions:
                _refresh_account_snapshot(c, full=False)
        log.info("startup: protective pass done — %d action(s)", len(actions))
    except Exception as e:
        log.exception("startup protective stop pass failed (%s) — continuing to "
                      "catchup/scheduler; the fast-stop loop will retry", e)


def run_loop() -> None:
    # Protective exits come FIRST — before catchup, before the scheduler.
    #
    # 2026-07-27 incident: the process started at 10:30 ET holding DELL + CAT,
    # both already through their stops after the weekend. _run_catchup_on_startup
    # then spent 12m09s on five missed weekly jobs (universe refresh, backtest,
    # DeepSeek autopilot, self-review, sandbox diff, plus three full v3 backtests
    # inside the optimizer) and the first stop check didn't happen until 10:42.
    # CAT exited at −2.09R and DELL at −1.13R; ~$43 of that was the delay, not
    # the gap. SIMULATE has no broker-side stop, so a soft stop is only as good
    # as the next poll — there must never be an unpolled minute while holding.
    #
    # This is deliberately the cheapest possible pass: manage_stops_only (soft
    # stops + breakeven + bracket fill-check), no scoring, no AI, no entries.
    # Wrapped so a broker hiccup here can never stop the scheduler from arming.
    _startup_protect_stops()

    # Detect missed scheduled jobs and run them once before the normal loop
    # takes over. Synchronous on purpose — a quick Telegram lets the user know
    # what's happening. Long jobs (Optuna ~10 min) delay the first ENTRY scan,
    # which is fine; they no longer delay protective exits (see above).
    _run_catchup_on_startup()

    sched = BlockingScheduler(timezone=NY)

    def job():
        if in_market_hours():
            scan_once()
        else:
            log.info("outside market hours, skipping")

    # Misfire policy:
    #   • coalesce=True       → if N runs were missed (laptop slept etc), only fire ONCE
    #   • misfire_grace_time  → drop runs more than this many seconds late
    #   • max_instances=1     → never let two scans overlap (prevents API rate-limit blowups)
    #
    # Phase 0 (2026-06-10): scans ALIGN to hourly-bar closes (:30) instead of
    # free-running from process start. A free-running 30-min interval could
    # fire at e.g. :17/:47 — half a bar stale on every signal. :31 sees the
    # freshly closed hourly bar; :01 is the mid-bar management/missed-fill pass.
    _ALIGNED_MINUTES = {30: "1,31", 15: "1,16,31,46", 10: "1,11,21,31,41,51",
                        60: "1", 20: "1,21,41", 5: "1,6,11,16,21,26,31,36,41,46,51,56"}
    if settings.scan_interval_min in _ALIGNED_MINUTES:
        # Align to bar closes (+1 min) — a free-running interval drifts to
        # arbitrary offsets (:07/:22/…) and scores every signal up to a full
        # sub-bar stale. 2026-07-07: extended beyond the original 30-min case
        # after SCAN_INTERVAL_MIN moved to 15 and silently lost alignment.
        sched.add_job(
            job, "cron", minute=_ALIGNED_MINUTES[settings.scan_interval_min],
            next_run_time=clock.ny_now(),
            coalesce=True,
            misfire_grace_time=60,
            max_instances=1,
        )
    else:
        sched.add_job(
            job, "interval",
            minutes=settings.scan_interval_min,
            next_run_time=clock.ny_now(),
            coalesce=True,
            misfire_grace_time=60,
            max_instances=1,
        )

    # Phase 0 (2026-06-10): 5-minute position-management tick between scans.
    # Live soft stops were filled a full scan late (measured −1.38R average vs
    # the −1R model — ≈$212 of the first −$572); checking every 5 minutes
    # shrinks the overshoot ~6× and catches TP touches the 30-min grid missed.
    # Cheap: skips without a broker connection when the book is flat, and the
    # executor lock serializes it against the scan's own manage pass.
    def _manage_tick():
        if not in_market_hours():
            return
        if not executor.has_open_trades():
            return
        try:
            with client() as c:
                actions = executor.manage_open_trades(c)
                for a in actions:
                    notifier.send(notifier.trade_action_msg(a))
                if actions:
                    _refresh_account_snapshot(c, full=False)
        except Exception as e:
            log.exception("manage tick failed: %s", e)

    sched.add_job(_manage_tick, "cron", minute="6,11,16,21,26,36,41,46,51,56",
                  coalesce=True, misfire_grace_time=60, max_instances=1)

    # Fast protective-stop loop (2026-06-21): an INDEPENDENT, lightweight tick that
    # runs every FAST_STOP_SECONDS and checks ONLY soft stops + breakeven (soft
    # positions) and broker bracket fills (REAL) — see executor.manage_stops_only.
    # SIMULATE has no native STOP order, so without this a soft stop waits for the
    # 5-min tick above (live audit: ~−1.38R late-fill overshoot); this shrinks that
    # to seconds. No-op (no broker connection) outside market hours or when flat, so
    # it's cheap enough to run every minute on the dedicated host. Runs in BOTH
    # modes by design; set FAST_STOP_SECONDS=0 to disable and fall back to 5-min.
    if settings.fast_stop_seconds > 0:
        def _fast_stop_tick():
            if not in_market_hours():
                return
            if not executor.has_open_trades():
                return
            try:
                with client() as c:
                    actions = executor.manage_stops_only(c)
                    for a in actions:
                        notifier.send(notifier.trade_action_msg(a))
                    if actions:
                        _refresh_account_snapshot(c, full=False)
            except Exception as e:
                log.exception("fast-stop tick failed: %s", e)

        sched.add_job(_fast_stop_tick, "interval",
                      seconds=settings.fast_stop_seconds,
                      coalesce=True, misfire_grace_time=30, max_instances=1)
        log.info("fast-stop loop armed — every %ds (market hours, when holding)",
                 settings.fast_stop_seconds)

    # 2026-06-03: watchlist is PINNED to 10; universe changes go through the
    # weekly self-review → approval queue (no auto-refresh, no manual button).
    # ML retrain removed entirely (subsystem deleted after proving inert).

    # Gap-risk sentinel — pre-open news/AI analysis (09:00 ET) + at-open exit
    # (09:31 ET), weekdays. Analyze overnight catalysts on holdings BEFORE the
    # open, then sell the flagged names at the open against real liquidity (a
    # stop can't catch a gap). No-op unless GAP_SENTINEL_ENABLED.
    # Pre-open clock check — 08:30 ET, one hour before the bell (owner request
    # 2026-07-11): force-refresh network time before the trading day. Quiet —
    # Telegram only on unverifiable time or NYSE holiday.
    sched.add_job(_preopen_clock_check_job, "cron",
                  day_of_week="mon-fri", hour=8, minute=30,
                  coalesce=True, misfire_grace_time=1200, max_instances=1)

    sched.add_job(_premarket_gap_sentinel_job, "cron",
                  day_of_week="mon-fri", hour=9, minute=0,
                  coalesce=True, misfire_grace_time=1200, max_instances=1)
    sched.add_job(_open_gap_exit_job, "cron",
                  day_of_week="mon-fri", hour=9, minute=31,
                  coalesce=True, misfire_grace_time=600, max_instances=1)

    # Weekly jobs — Monday 20:00 KL SHARP (owner request 2026-07-07), pinned to
    # Asia/Kuala_Lumpur so US DST never shifts the wall-clock time (KL has no
    # DST). ≈ Monday 08:00 ET in summer / 07:00 ET in winter — both pre-market.
    # Staggered by 5 min so each finishes before the next starts.
    # Order: autopilot (no deps) → universe → backtest (needs universe) → self-review

    # 2026-07-27: the (weekday, HH:MM) for each of these lives ONLY in
    # cron_state.WEEKLY_SCHEDULE, which the catchup planners also read — so a
    # schedule change can no longer land in one place and not the other. The
    # per-job comments below describe WHY each slot is where it is; the times
    # themselves come from the table.
    #
    # Order (and the 5-minute stagger) is load-bearing:
    #   autopilot (no deps) → universe → backtest (needs universe) → self-review
    #   → sandbox diff (measures the config the week will actually run)
    _WEEKLY_JOBS = [
        # (job key, runner, misfire_grace_time, why this slot)
        ("weekly_autopilot", _weekly_autopilot_job, 3600,
         "DeepSeek reviews the week, proposes + backtest-validates params, "
         "auto-applies within guardrails. FIRST so applied changes feed the rest."),
        ("universe_refresh", _universe_refresh_job, 1800,
         "rule-based watchlist rebuild, BEFORE the backtest so the health check "
         "scores next week's actual list. No-op unless DYNAMIC_UNIVERSE_ENABLED."),
        ("weekly_backtest", _weekly_backtest_validation_job, 1800,
         "health-check the strategy on a rolling 90-day window, on the fresh "
         "ticker set."),
        ("self_review", _weekly_self_review_job, 1800,
         "reviews what the bot ACTUALLY did (real fills, not a backtest) and "
         "emits suggestions for owner approval."),
        ("sandbox_diff", _weekly_sandbox_diff_job, 1800,
         "trade-level sandbox↔backtest differential. LAST in the Monday chain. "
         "Subprocess-isolated; Telegram only on breach."),
        ("grid_sweep_weekly", lambda: _grid_sweep_job(daily=False), 3600,
         "27-combo parameter grid at Mon 07:00 KL (19:00 ET Sun, market shut). "
         "Moved off the crontab 2026-07-28 so it ships with the app."),
    ]
    for _key, _fn, _grace, _why in _WEEKLY_JOBS:
        _wd, _h, _m = cron_state.WEEKLY_SCHEDULE[_key]
        sched.add_job(_fn, "cron", day_of_week=_wd, hour=_h, minute=_m,
                      timezone=KL, coalesce=True, misfire_grace_time=_grace,
                      max_instances=1)
        log.debug("weekly job %s armed for %d %02d:%02d KL — %s",
                  _key, _wd, _h, _m, _why)

    # Monthly Optuna re-optimization: 1st of each month at 03:00 ET.
    # Telegrams suggested params; user reviews before editing .env.
    sched.add_job(_monthly_optuna_job, "cron", day=1, hour=3, minute=0,
                  coalesce=True, misfire_grace_time=3600, max_instances=1)

    # Monthly lever recheck: 1st of each month at 03:30 ET, AFTER Optuna. Re-runs
    # the 2026-06-25 TP/SL drift check on the watchlist and telegrams a one-screen
    # summary. Suggestion-only — never edits .env (owner reviews, same as Optuna).
    sched.add_job(_monthly_lever_recheck_job, "cron", day=1, hour=3, minute=30,
                  coalesce=True, misfire_grace_time=3600, max_instances=1)

    # Telegram approval sync: every 5 min, any time of day. Posts pending
    # suggestion cards with Approve/Reject buttons, processes your taps into the
    # shared approval queue, and applies anything you approved (same queue the
    # GUI reads, so both stay in sync). Cheap HTTP; no-op if Telegram unset.
    # NOTE: this is also what applies owner-approved suggestions off-hours, so an
    # approval can take up to ~5 min to take effect (during market hours the main
    # scan also calls apply_approved every scan_interval_min). The GUI popup tells
    # the owner about this latency.
    def _tg_sync():
        try:
            tg_approvals.sync()
            approvals.apply_approved()   # apply off-hours approvals promptly
        except Exception as e:
            log.debug("tg approval sync failed: %s", e)
    sched.add_job(_tg_sync, "interval", minutes=5,
                  coalesce=True, misfire_grace_time=120, max_instances=1)

    # Daily grid sweep — 09:00 KL weekdays (21:00 ET, market shut). Neighborhood
    # walk around the CURRENT params, so every trading day opens on values
    # re-validated against data through the previous close. Replaces the
    # `0 9 * * 2-6` crontab entry (2026-07-28).
    _gs_h, _gs_m = cron_state.DAILY_KL_SCHEDULE["grid_sweep_daily"]
    sched.add_job(lambda: _grid_sweep_job(daily=True), "cron",
                  day_of_week="mon-fri", hour=_gs_h, minute=_gs_m, timezone=KL,
                  coalesce=True, misfire_grace_time=3600, max_instances=1)

    # Autopilot health watchdog: 17:30 ET weekdays (after the close) — detects
    # silent scan stalls, garbage backtest results, overdue jobs, reconcile
    # drift, and reminds about active runtime overrides. Notifies only when
    # something is wrong (no daily noise).
    def _watchdog_job():
        try:
            from . import autopilot
            issues = autopilot.health_check()
            if issues:
                notifier.send("🩺 *Autopilot 健康检查*\n" +
                              "\n".join(f"  • {i}" for i in issues))
                log.warning("watchdog: %d issue(s): %s", len(issues), issues)
            else:
                log.info("watchdog: all healthy")
        except Exception as e:
            log.exception("watchdog failed: %s", e)
    sched.add_job(_watchdog_job, "cron", day_of_week="mon-fri", hour=17, minute=30,
                  coalesce=True, misfire_grace_time=3600, max_instances=1)

    # API/subscription health watchdog: probe broker options data + Gemini every
    # HEALTH_CHECK_INTERVAL_MIN min and Telegram the owner ONLY on a state change
    # (subscription lapsed / Gemini quota out → top up). Owner-requested; edge-
    # triggered so no spam. Also runs once at startup for an immediate status.
    def _api_health_job():
        try:
            from . import health_check
            health_check.run()
        except Exception as e:
            log.exception("api health check failed: %s", e)
    if settings.health_check_enabled:
        sched.add_job(_api_health_job, "interval",
                      minutes=settings.health_check_interval_min,
                      coalesce=True, misfire_grace_time=600, max_instances=1)
        try:
            _api_health_job()          # immediate check on boot
        except Exception as e:
            log.warning("startup health check failed: %s", e)

    # Daily blacklist review: 23:00 ET every weekday. Reads recent closed
    # trades, adds chronic losers, removes recovered names, extends watch on
    # symbols still in the doghouse. Notifies via Telegram only on changes.
    sched.add_job(_daily_blacklist_review_job, "cron",
                  day_of_week="mon-fri", hour=23, minute=0,
                  coalesce=True, misfire_grace_time=1800, max_instances=1)

    # Auto-compounding budget: 16:45 ET every weekday (after the 16:00 close so
    # the day's realized PnL is final). No-op unless armed. Grows/shrinks the
    # deployable budget off realized profit and Telegram-notifies any change.
    sched.add_job(_daily_auto_budget_job, "cron",
                  day_of_week="mon-fri", hour=16, minute=45,
                  coalesce=True, misfire_grace_time=1800, max_instances=1)

    log.info(
        "scheduler started — scan=%dm, "
        "weekly autopilot/backtest/review=Mon 20:00 KL (timezone-pinned), "
        "monthly Optuna=1st@03:00, daily blacklist=23:00 ET",
        settings.scan_interval_min,
    )
    from .i18n import t
    notifier.send(t("tg_started", env=settings.moo_trade_env))
    sched.start()


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "scan":
        scan_once()
    elif cmd == "run":
        run_loop()
    elif cmd == "review":
        # On-demand weekly self-review (also the Sunday cron). analyze→notify.
        _weekly_self_review_job()
    elif cmd == "approvals":
        # List pending owner-approval suggestions.
        pend = approvals.list_pending()
        if not pend:
            print("No pending approvals.")
        for a in pend:
            print(f"[{a['id']}] {a['kind']}: {a.get('detail','')}\n"
                  f"        → {a.get('action','')}")
    elif cmd in ("approve", "reject") and len(sys.argv) >= 3:
        ok = approvals.resolve(sys.argv[2], approved=(cmd == "approve"))
        print(f"{cmd} {sys.argv[2]}: {'done — applies next scan' if ok else 'not found / already resolved'}"
              if cmd == "approve" else
              f"{cmd} {sys.argv[2]}: {'rejected' if ok else 'not found / already resolved'}")
    else:
        print(f"unknown command: {cmd}")
        print("commands: scan | run | review | approvals | approve <id> | reject <id>")
        sys.exit(2)


if __name__ == "__main__":
    main()
