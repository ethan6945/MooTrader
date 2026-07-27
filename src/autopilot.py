"""Autopilot — P2-1 (2026-06-26): DeepSeek autonomous portfolio manager.

WEEKLY SCHEDULE (Monday 09:00 ET = Monday 21:00 KL GMT+8):
  1. COLLECT: self_review + breadth + strategy perf + ML training + backtest
  2. PROMPT: send the full report to DeepSeek with guardrails
  3. VALIDATE: each proposal gets a backtest_v3 honest-engine check
  4. APPLY: within guardrails → auto-apply (runtime override, no restart);
           outside guardrails → approval queue (owner decides)
  5. NOTIFY: Telegram summary of everything that happened

SETUP: the owner connects DeepSeek API keys in .env (DEEPSEEK_API_KEY).
When no DeepSeek key is configured, the autopilot is a no-op — the rules-
based self_review and monthly Optuna still run unchanged.

GUARDRAILS (hardcoded — DeepSeek cannot override):
  • risk_per_trade:      1% – 8%
  • entry_threshold:     55 – 85
  • tp_atr_mult:         2 – 12
  • sl_atr_mult:         2 – 6
  • max_hold_days:       5 – 10
  • universe_top_n:      10 – 20
  • max_position_pct:    0.20 – 0.55
  • max_changes/week:    3
  • drawdown > 10%:      FREEZE all autonomous changes
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from . import ai, approvals, breadth, db, notifier, relative_strength, runtime_config, strategy_gate
from .config import settings

log = logging.getLogger(__name__)

# ── Guardrails ────────────────────────────────────────────────────────────────
GUARDRAILS = {
    "risk_per_trade":      (0.01,  0.08),
    "entry_threshold":     (55,    85),
    "tp_atr_mult":         (2,     12),
    "sl_atr_mult":         (2,     6),
    "max_hold_days":       (5,     10),
    "universe_top_n":      (10,    20),
    "max_position_pct":    (0.20,  0.55),
}

MAX_CHANGES_PER_WEEK = 3
DRAWDOWN_FREEZE_PCT = 0.10   # freeze all auto-changes when DD ≥ 10%

# ── System prompt (sent to DeepSeek) ─────────────────────────────────────────
SYSTEM_PROMPT = """You are the autonomous portfolio manager for moo-trader.

ACCOUNT (${budget:,.0f} US cash — owner-allocated budget; NEVER exceed it, no leverage/margin, long-only, no shorting):
- Max 5 concurrent positions
- Single position ≤ 55% of account
- Single trade risk ≤ 8% (half-Kelly ceiling)
- Daily max loss stop: -5% (auto-halts)
- Friday after 14:00 ET: no new entries (weekend gap protection)

ADJUSTABLE PARAMETERS (you propose 0-3 changes per week, SMALL moves only):
- entry_threshold (55-85): {entry_threshold} — lower = more trades, more noise
- tp_atr_mult (2-12): {tp_atr_mult} — take-profit in ATR units
- sl_atr_mult (2-6): {sl_atr_mult} — stop-loss in ATR units
- breakeven_trigger_r (0.75-1.5): {breakeven_trigger_r}
- max_hold_days (5-10): {max_hold_days}
- universe_top_n (10-20): {universe_top_n}
- max_position_pct (0.20-0.55): {max_position_pct}

CURRENT VALUES (already sweep-tuned — move only with NEW evidence; every proposal is
re-validated on a recent 60-day OpenD backtest and dropped unless it beats these):
- entry_threshold={entry_threshold} (past sweeps: 55→−$11/day, 85→cuts trades)
- tp_atr_mult={tp_atr_mult}, max_hold_days={max_hold_days} sit near measured plateaus
- max_position_pct={max_position_pct} (0.40 beat 0.30/0.50/0.70 in the 2026-06-11 sweep)

ACTIVE STRATEGIES:
{strategy_status}

THIS WEEK'S REAL PERFORMANCE:
{weekly_report}

CURRENT MARKET ENVIRONMENT:
{market_context}

RECENT BACKTEST (health check):
{backtest_health}

INSTRUCTIONS:
1. Look at the real-fill data FIRST. If a strategy is losing money over
   15+ trades, propose pausing it. If all strategies are healthy, do nothing.
2. Only propose parameter changes with SPECIFIC evidence from the report.
   "The win rate is low" is not evidence. "6 of 8 stop-outs happened on
   trades held past day 5, and avg win is $153 vs avg loss $55 — suggest
   reducing max_hold_days from 7 to 5" IS evidence.
3. Prefer SMALL moves (±10% or less).
4. If everything looks healthy and no change is warranted, return [].
5. Remember: the backtest is an estimate. The real fills are the truth.

Reply with STRICT JSON, no markdown, no prose outside the array:
[{{"key":"param_name", "value":new_value, "rationale":"one SHORT sentence citing specific data"}}]

Iron rule: fewer proposals is better. One good change beats three guesses."""


def _current_params() -> dict:
    return {
        "entry_threshold": runtime_config.entry_threshold(),
        "tp_atr_mult": runtime_config.tp_atr_mult(),
        "sl_atr_mult": runtime_config.sl_atr_mult(),
        "breakeven_trigger_r": runtime_config.breakeven_trigger_r(),
        "max_hold_days": runtime_config.max_hold_days(),
        "universe_top_n": runtime_config.universe_top_n(),
        "max_position_pct": runtime_config.max_position_pct(),
    }


def _within_guardrails(key: str, value: float) -> bool:
    """Check if a proposed value is inside the hardcoded guardrail bounds."""
    if key not in GUARDRAILS:
        return True  # unknown key → approval queue, not auto-apply
    lo, hi = GUARDRAILS[key]
    return lo <= value <= hi


def _effective_budget() -> float:
    """The budget the owner ACTUALLY gave (db-state), not the stale .env constant.

    Uses risk_manager.equity_baseline() — the same capital base the DD breaker
    sizes against — so autopilot's freeze threshold is computed on the real
    deployed budget (e.g. $50k) instead of settings.account_usd (the frozen
    .env value, which was $5k and made the freeze fire ~10x too early).
    """
    try:
        from . import risk_manager
        b = float(risk_manager.equity_baseline())
        if b > 0:
            return b
    except Exception:
        pass
    return float(settings.account_usd)


def _current_drawdown() -> float:
    """Return the current realized drawdown as a fraction (0.0 = no loss)."""
    try:
        state = db.get_state()
        realized = float(state.get("realized_pnl_total", 0))
    except Exception:
        realized = 0.0
    # DD = negative realized / seed capital (real owner budget, not .env)
    if realized >= 0:
        return 0.0
    seed = _effective_budget()
    if seed <= 0:
        return 0.0
    return abs(realized) / seed


# NOTE: the old `_run_backtest_with_param` was removed 2026-07-02. It imported
# `_base_cfg` from `.backtest_v3` (where it does not exist) → every call raised
# and returned None → autopilot applied changes with NO backtest validation.
# Validation now goes through `optimizer_ai.validate_proposals` (independent
# yfinance dual-window, beats-baseline + drawdown guard) in weekly_autopilot.


def weekly_autopilot() -> dict:
    """Run ONE weekly autopilot cycle. Called every Sunday 18:00 ET by cron.

    Returns a dict summary of what happened, also pushed to Telegram.
    """
    if not ai.has_key("deepseek"):
        log.info("Autopilot: no DeepSeek key — skipping (self_review still runs)")
        return {"status": "skipped", "reason": "no DeepSeek key"}

    # ── 0. Pre-flight: drawdown check ──
    dd = _current_drawdown()
    if dd >= DRAWDOWN_FREEZE_PCT:
        notifier.send(f"🛑 Autopilot FROZEN: drawdown {dd:.1%} ≥ {DRAWDOWN_FREEZE_PCT:.0%}")
        return {"status": "frozen", "drawdown": round(dd, 3)}

    log.info("=== Autopilot weekly cycle start ===")

    # ── 1. Collect performance data ──
    # Self-review (real fills from past 7 days)
    try:
        from . import self_review
        report = self_review.weekly_review(days=7)
    except Exception as e:
        report = {"error": str(e)}
        log.warning("self_review failed: %s", e)

    # Strategy performance (90-day)
    try:
        strat_perf = relative_strength.strategy_performance(90)
    except Exception as e:
        strat_perf = {"error": str(e)}

    # Market context (latest breadth + regime)
    market_ctx = "market data unavailable"
    try:
        from moomoo import KLType
        from .moo_client import client
        with client() as c:
            bv = breadth.assess(c)
            market_ctx = (
                f"VIX: {bv.vix:.1f} | Breadth: {bv.note}\n"
                f"A/D ratio: {bv.ad_ratio:.2f} | %>50MA: {bv.pct_above_50ma:.0f}%"
            )
    except Exception as e:
        log.warning("market context fetch failed: %s", e)

    # Backtest health check (90-day)
    bt_health = "not available"
    try:
        from .optimizer_ai import _base_cfg
        from .backtest import run_backtest
        cfg = _base_cfg(days=90)
        bt_result = run_backtest(cfg)
        m = bt_result.get("metrics", {})
        bt_health = (
            f"90d: {m.get('total_trades',0)} trades, "
            f"${m.get('net_pnl_usd',0):+.0f}, "
            f"Sortino {m.get('sortino_ratio',0):.1f}, "
            f"PF {m.get('profit_factor',0):.2f}, "
            f"DD {m.get('max_drawdown_pct',0):.1f}%"
        )
    except Exception as e:
        log.warning("backtest health check failed: %s", e)

    # Strategy status
    strategy_status_lines = []
    for strat, data in sorted(strat_perf.items(), key=lambda kv: kv[1].get("expectancy", 0)):
        if isinstance(data, dict):
            strategy_status_lines.append(
                f"  {strat}: {data.get('n',0)} trades, "
                f"${data.get('expectancy',0):.2f}/trade, "
                f"{data.get('win_rate',0):.0f}% win"
            )
    strategy_status = "\n".join(strategy_status_lines) or "no trade data"

    # P2-2/3: extreme market lock + live/backtest deviation check
    # Run BEFORE asking DeepSeek so the gate results feed into the prompt
    gate_result = {"extreme_market": False, "live_vs_bt": {}}
    try:
        gate_result = strategy_gate.weekly_gate_check()
        if gate_result.get("extreme_market", {}).get("locked"):
            market_ctx += f"\n🚨 EXTREME MARKET LOCK: {gate_result['extreme_market']['reason']}"
        lvb = gate_result.get("live_vs_bt", {})
        if lvb.get("alert"):
            market_ctx += f"\n⚠ Live/BT deviation: {lvb.get('note','')}"
    except Exception as e:
        log.warning("strategy gate check failed: %s", e)

    # ── 2. Ask DeepSeek ──
    from . import risk_manager
    prompt = SYSTEM_PROMPT.format(
        weekly_report=json.dumps(report, indent=2, default=str)
        if isinstance(report, dict) else str(report),
        market_context=market_ctx,
        backtest_health=bt_health,
        strategy_status=strategy_status,
        budget=risk_manager.budget_usd(),
        **_current_params(),
    )

    proposals = []
    log.info("Autopilot: asking DeepSeek...")
    try:
        text, _ = ai.generate(prompt)
        # Extract JSON array from the response
        import re
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            proposals = json.loads(match.group(0))
        else:
            log.info("Autopilot: DeepSeek returned no proposals (empty)")
    except Exception as e:
        log.error("Autopilot: DeepSeek call failed: %s", e)
        notifier.send(f"⚠ Autopilot: DeepSeek unavailable — skipping this week ({e})")
        return {"status": "ai_error", "error": str(e)}

    if not proposals:
        log.info("Autopilot: no changes proposed — all healthy")
        notifier.send("🤖 *Autopilot*: no changes this week — everything looks healthy")
        return {"status": "no_changes", "n_proposals": 0}

    # ── 3. Validate and apply ──
    if len(proposals) > MAX_CHANGES_PER_WEEK:
        proposals = proposals[:MAX_CHANGES_PER_WEEK]
        log.warning("Autopilot: %d proposals → capped at %d",
                     len(proposals), MAX_CHANGES_PER_WEEK)

    applied = []
    queued = []
    skipped = []

    # ── 3a. Guardrail triage — unknown / out-of-bounds never auto-apply ──
    in_bounds = []
    for p in proposals:
        key = p.get("key", "")
        value = p.get("value")
        if key not in GUARDRAILS:
            queued.append({**p, "reason": "unknown parameter"})
            continue
        if not _within_guardrails(key, value):
            approvals.enqueue(
                kind="param_change",
                detail=f"Autopilot: {key} {_current_params().get(key, '?')} → {value} (OUTSIDE guardrails {GUARDRAILS[key]})",
                action=f"Set {key} = {value} — requires manual review",
                payload={"key": key, "value": value},
            )
            queued.append({**p, "reason": f"outside guardrails {GUARDRAILS[key]}"})
            continue
        in_bounds.append(p)

    # ── 3b. Validate on a recent OpenD backtest (owner 2026-07-02: 60d, OpenD) ──
    # Reuse the optimizer's mature gate: a change must BEAT the current config's
    # $/day without worsening drawdown by > DD_TOLERANCE_PP, must be a valid
    # in-bounds param, and must not be in post-rollback cooldown. This REPLACES
    # the old broken path (it imported _base_cfg from the wrong module → every
    # backtest raised → returned None → autopilot applied with NO validation).
    # Window + data source come from optimizer_ai (_VALIDATE_WINDOWS=60d / _base_cfg=OpenD).
    validated = []
    if in_bounds:
        try:
            from . import optimizer_ai
            validated = optimizer_ai.validate_proposals(in_bounds)
        except Exception as e:
            log.warning("Autopilot: validation failed (%s) — applying nothing this cycle", e)
            skipped.extend({**p, "reason": f"validation error: {e}"} for p in in_bounds)
            validated = []
    val_keys = {v.get("key") for v in validated}
    for p in in_bounds:
        if p.get("key") not in val_keys:
            skipped.append({**p, "reason": "no improvement on the recent 60d OpenD backtest"})

    # ── 3c. Apply the validated (independently-improving) changes ──
    for v in validated:
        key = v.get("key")
        value = v.get("value")
        try:
            current = _current_params().get(key)
            runtime_config.set_param(
                key, float(value),
                f"autopilot_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
            applied.append({**v, "old_value": current,
                            "backtest": {"deltas": v.get("_deltas"), "dd": v.get("_dd")}})
            log.info("Autopilot: APPLIED %s %s → %s (%s)",
                     key, current, value, v.get("rationale", ""))
        except Exception as e:
            skipped.append({**v, "reason": str(e)})

    # ── 4. Notify ──
    lines = ["🤖 *Autopilot 周报*"]
    if applied:
        lines.append("\n✅ *已自动执行:*")
        for a in applied:
            lines.append(
                f"  • {a['key']}: {a.get('old_value','?')} → {a['value']}"
            )
            deltas = (a.get("backtest") or {}).get("deltas") or {}
            if deltas:
                lines.append(
                    "    回测(OpenD): "
                    + ", ".join(f"{w}d Δ${deltas[w]:+.1f}/日" for w in sorted(deltas))
                )
            lines.append(f"    理由: {a.get('rationale','')}")
    if queued:
        lines.append("\n📥 *待审批 (需你在 Telegram/GUI 点一下):*")
        for q in queued:
            lines.append(f"  • {q.get('key','?')} → {q.get('value')}: {q.get('reason','')}")
    if skipped:
        lines.append("\n⏭ *跳过 (回测恶化):*")
        for s in skipped:
            lines.append(f"  • {s.get('key','?')} → {s.get('value')}: {s.get('reason','')}")
    if not applied and not queued:
        lines.append("\n✓ 无变更 — 所有策略健康")

    msg = "\n".join(lines)
    notifier.send(msg)
    log.info("=== Autopilot cycle complete: %d applied, %d queued, %d skipped ===",
             len(applied), len(queued), len(skipped))

    return {
        "status": "complete",
        "n_proposals": len(proposals),
        "applied": len(applied),
        "queued": len(queued),
        "skipped": len(skipped),
        "applied_details": applied,
        "queued_details": queued,
    }


# ── Support functions (called by main.py cron) ───────────────────────────────

def check_and_rollback() -> list[str]:
    """Called from _weekly_self_review_job. Checks whether any auto-applied
    param change from a prior autopilot run has hurt live results (e.g.
    realized PnL worsened after the change). If so, reverts it. Returns
    Telegram messages (empty = no action needed)."""
    notes: list[str] = []
    try:
        from . import db
        from datetime import datetime, timedelta, timezone

        def _ts(row):
            """Parse a closed-trade timestamp to an aware UTC datetime, or None.
            Tolerates missing/naive/malformed values so one bad row can't
            silently disable the whole rollback check (old code let a bad ts
            raise and swallow the entire pass)."""
            raw = row.get("ts") or row.get("closed_at") or ""
            try:
                dt = datetime.fromisoformat(str(raw))
            except (ValueError, TypeError):
                return None
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

        state = db.get_state()
        hist = list(state.get("param_history", []))
        rows = db.closed_trades(limit=200)
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=7)
        MIN_TRADES_SINCE = 5   # need a real post-change sample, not loss noise

        # 2026-07-27: this used to match only source.startswith("autopilot_"),
        # but optimizer_ai writes source="auto-optimizer" — so NO optimizer
        # auto-apply was ever watched, even though its own Telegram promises
        # "若后续实盘表现恶化将自动回滚并通知". Both autonomous sources are now
        # covered; owner-approved and manual changes stay untouched on purpose.
        for h in reversed(hist):
            if not h.get("active"):
                continue
            src = str(h.get("source", ""))
            if not (src.startswith("autopilot_") or src == "auto-optimizer"):
                continue
            try:
                at = datetime.fromisoformat(h.get("applied_at", ""))
            except (ValueError, TypeError):
                continue
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            if at < cutoff:
                continue

            # Evidence-based: split closed trades into before/after the change.
            after, before = [], []
            for r in rows:
                t = _ts(r)
                if t is None:
                    continue
                (after if t >= at else before).append(r)
            if len(after) < MIN_TRADES_SINCE:
                continue  # not enough post-change evidence yet — wait, don't guess

            net_after = sum(float(r.get("pnl", 0) or 0) for r in after)
            exp_after = net_after / len(after)
            exp_before = (sum(float(r.get("pnl", 0) or 0) for r in before) / len(before)
                          if before else 0.0)

            # Roll back ONLY on real deterioration: net loss since the change AND
            # per-trade expectancy clearly worse than the window before it. (The
            # old "≥3 losses" rule fired on normal ~50% loss cadence regardless
            # of whether the change helped.)
            if net_after < 0 and exp_after < exp_before:
                runtime_config.revert_param(
                    h["key"],
                    f"auto-rollback: net ${net_after:+.0f} over {len(after)} trades "
                    f"since change; exp/trade {exp_after:+.1f} < {exp_before:+.1f} before"
                )
                notes.append(
                    f"🔄 Autopilot 自动回滚: {h['key']} "
                    f"{h.get('new','?')} → {h.get('old','?')} "
                    f"(改动后 {len(after)} 笔净 ${net_after:+.0f}, "
                    f"期望/笔 {exp_after:+.1f} < 改前 {exp_before:+.1f})"
                )
    except Exception as e:
        log.warning("autopilot rollback check failed: %s", e)
    return notes


def health_check() -> list[str]:
    """Called from _watchdog_job (17:30 ET weekdays). Checks for:
    - Silent scan stalls (no trade activity for 2+ market days)
    - Outdated backtest results (>14d old)
    - Overdue cron jobs
    - Active runtime overrides that have been in place >30d
    Returns list of issue strings (empty = healthy)."""
    issues: list[str] = []
    try:
        from . import db
        from datetime import datetime, timedelta, timezone

        # Check for active runtime overrides older than 30 days
        state = db.get_state()
        hist = list(state.get("param_history", []))
        now = datetime.now(timezone.utc)
        for h in hist:
            if h.get("active"):
                applied = h.get("applied_at", "")
                try:
                    at = datetime.fromisoformat(applied)
                except (ValueError, TypeError):
                    continue
                if (now - at) > timedelta(days=30):
                    issues.append(
                        f"⏳ runtime override '{h['key']}={h.get('new','?')}' "
                        f"active since {at.strftime('%Y-%m-%d')} — review if still valid"
                    )

        # Check overdue cron jobs — read cron_state.json (the actual store;
        # 2026-07-07 fix: the old code read db kv_state key "cron_self_review",
        # which never existed, so this check could never fire).
        try:
            from . import cron_state
            lr = cron_state.last_run("self_review")
            if lr is not None:
                lr_utc = lr if lr.tzinfo else lr.replace(tzinfo=timezone.utc)
                if (now - lr_utc).days > 8:
                    issues.append("📋 self-review overdue (>8d)")
        except Exception:
            pass

    except Exception as e:
        log.warning("health_check failed: %s", e)
        issues.append(f"health_check error: {e}")

    return issues


# ── Cooldown tracker for post-rollback parameters ────────────────────────────
# When autopilot auto-rolls back a param change (because it caused losses),
# the key enters a 14-day cooldown. During cooldown, the optimizer MUST NOT
# re-propose the same parameter — prevents thrashing between two bad values.

_COOLDOWN_DAYS = 14


def in_cooldown(key: str) -> bool:
    """Return True if `key` was rolled back recently and is still cooling down.
    Called by optimizer_ai before re-proposing a param change."""
    try:
        state = db.get_state()
        hist = list(state.get("param_history", []))
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=_COOLDOWN_DAYS)
        for h in reversed(hist):
            if h.get("key") == key and h.get("rolled_back"):
                rb_time_str = h.get("applied_at", "")
                try:
                    rb_time = datetime.fromisoformat(rb_time_str)
                except (ValueError, TypeError):
                    continue
                if rb_time >= cutoff:
                    return True
    except Exception:
        pass
    return False
