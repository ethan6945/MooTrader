"""Auto-compounding budget — "money making more money", done safely.

WHY THIS EXISTS
  The owner's `budget_usd` (deployable capital cap) is a STATIC number typed
  into the web panel. Realized profit piles up in `realized_pnl_total` but
  never feeds back into how much capital the bot puts to work — so a validated
  daily edge stays LINEAR forever. This module makes it GEOMETRIC: as the bot's
  realized equity rises above the line where compounding was armed, the
  deployable budget grows with it (and shrinks in drawdown). Bigger validated
  edge × bigger base = exponential, not additive. That is the whole "money
  earning more money" idea, reduced to one safe feedback loop.

THE MATH
  Let  seed         = deployable budget at the moment compounding was armed
       base_realized= cumulative realized PnL at that same moment
       profit_since = realized_pnl_total − base_realized   (bot-earned since arm)
  Target deployable budget:
       target = clamp( seed + REINVEST_FRAC · profit_since,  floor, ceil )
       floor  = seed · MIN_FRAC      (never de-risk below this — give-back stop)
       ceil   = seed · MAX_MULT      (never let it run away)
  Then capped by live account equity (can't deploy money you don't have).
  REINVEST_FRAC = 1.0 (default) → symmetric: +$1 earned grows the base by $1,
  −$1 lost shrinks it by $1. That is "press winners, cut size when losing" —
  the core risk reflex of every desk that survives.

WHY IT CAN'T BREAK THE DRAWDOWN BREAKER (the load-bearing invariant)
  risk_manager computes equity = equity_baseline() + realized_pnl_total for its
  DD circuit breaker. If that baseline were the *compounding* budget, realized
  PnL would be counted twice and the breaker would mis-fire. So while
  compounding is armed, equity_baseline() returns the FROZEN seed — the DD math
  is byte-identical to "budget frozen at seed", completely independent of how
  the deployable budget moves. (When never armed, it returns the live budget,
  so legacy behavior is unchanged.)

AUTONOMY (owner chose "fully auto, with guardrails")
  Runs once daily after the close. Every change is bounded by floor/ceil + the
  live-equity cap, recorded to an audit history, and pushed to Telegram (铁律:
  never silent). Default OFF — flip AUTO_BUDGET_ENABLED=true (or the web toggle)
  to arm. Arming is neutral: target == seed until the bot earns new profit.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import db
from .config import settings

log = logging.getLogger(__name__)

# db-state keys (all namespaced so they never collide with param_* overrides).
_K_ENABLED = "auto_budget_enabled"        # runtime toggle (web), falls back to .env
_K_SEED = "auto_budget_seed"              # frozen deployable base at arm time
_K_BASE_REALIZED = "auto_budget_base_realized"  # cumulative realized at arm time
_K_HISTORY = "auto_budget_history"        # audit list of applied changes
_K_DISARMED = "auto_budget_disarmed"      # sticky owner stop — only arm() clears it


# ── toggle + armed-state readers ─────────────────────────────────────────────

def enabled() -> bool:
    """Runtime db-state toggle wins (web, no restart); else the .env default.

    The param freeze overrides both. Without that, the db toggle was a freeze
    bypass: `AUTO_BUDGET_ENABLED=false` in .env plus one panel click writing
    `auto_budget_enabled: True` to db-state, and compounding was live again —
    the db value is read first, so the .env setting never got a say.
    """
    from . import runtime_config
    if runtime_config.frozen():
        return False
    try:
        v = db.get_state().get(_K_ENABLED)
        if v is not None:
            return bool(v)
    except Exception:
        pass
    return settings.auto_budget_enabled


def is_armed() -> bool:
    """True once a seed has been captured (compounding has a reference line)."""
    return seed_usd() is not None


def seed_usd() -> float | None:
    """Frozen deployable base captured at arm time, or None if never armed."""
    try:
        v = db.get_state().get(_K_SEED)
        return float(v) if v is not None and float(v) > 0 else None
    except (TypeError, ValueError):
        return None


def base_realized() -> float:
    """Cumulative realized PnL snapshot taken at arm time (0.0 if unset)."""
    try:
        return float(db.get_state().get(_K_BASE_REALIZED) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def equity_baseline() -> float:
    """Capital base for risk_manager's equity/DD math.

    While armed → the FROZEN seed (so the compounding budget can't double-count
    realized PnL into equity). Otherwise → the live budget (legacy behavior,
    byte-identical to before this module existed). risk_manager delegates here.
    """
    s = seed_usd()
    if s is not None:
        return s
    from . import risk_manager
    return risk_manager.budget_usd()


# ── pure target computation (unit-testable, no I/O) ──────────────────────────

def compute_target(seed: float, base_real: float, realized: float,
                   live_equity: float | None) -> tuple[float, dict]:
    """Return (target_budget, diagnostics). Pure: no state reads/writes.

    diagnostics carries the inputs so the caller can log/notify transparently.
    """
    profit_since = realized - base_real
    raw = seed + settings.auto_budget_reinvest_frac * profit_since
    floor = seed * settings.auto_budget_min_frac
    ceil = seed * settings.auto_budget_max_mult
    target = max(floor, min(raw, ceil))
    capped_by_equity = False
    if live_equity is not None and live_equity > 0 and target > live_equity:
        target = live_equity
        capped_by_equity = True
    diag = {
        "seed": round(seed, 2),
        "base_realized": round(base_real, 2),
        "realized": round(realized, 2),
        "profit_since": round(profit_since, 2),
        "raw": round(raw, 2),
        "floor": round(floor, 2),
        "ceil": round(ceil, 2),
        "capped_by_equity": capped_by_equity,
        "target": round(target, 2),
    }
    return round(target, 2), diag


# ── arm / disarm ─────────────────────────────────────────────────────────────

def arm(seed: float | None = None, force: bool = False) -> dict:
    """Capture the compounding reference line. seed defaults to the live budget.

    Refuses while the param freeze is on: arming is what makes the budget start
    moving on its own, which is the whole thing the freeze exists to stop.
    `disarm()` is deliberately NOT guarded — it only ever moves toward less
    autonomy, and blocking the safe direction helps nobody.

    Idempotent-ish: re-arming overwrites the reference (use when the owner
    deposits/withdraws capital and wants a fresh starting line). Returns the
    stored reference.
    """
    from . import risk_manager, runtime_config
    if runtime_config.frozen() and not force:
        raise runtime_config.ParamsFrozen(
            "param freeze active (PARAMS_FROZEN) — refused to arm auto-budget "
            "compounding. Fix sandbox↔backtest_v3 parity first."
        )
    s = float(seed) if seed is not None else risk_manager.budget_usd()
    state = db.get_state()
    base = float(state.get("realized_pnl_total") or 0.0)
    # Arming is the one deliberate act that clears a sticky disarm.
    db.update_state({_K_SEED: s, _K_BASE_REALIZED: base, _K_DISARMED: False})
    log.info("auto_budget armed: seed=$%.0f base_realized=$%.2f", s, base)
    return {"seed": s, "base_realized": base}


def disarm() -> None:
    """Permanently stop compounding until the owner explicitly re-arms.

    Clearing the seed alone was not enough: recompute_and_apply() treats "no
    seed" as "first run" and arms itself. So a disarm followed by any run with
    the toggle on silently re-armed at whatever the budget happened to be —
    disarm looked like an off switch and behaved like a reset button.

    The sticky flag is what makes it terminal. arm() clears it, so re-arming is
    still a single deliberate call; nothing else does.

    Does NOT touch budget_usd (it stays at whatever compounding last set), so
    the owner can set it manually afterward.
    """
    db.update_state({_K_SEED: None, _K_BASE_REALIZED: None, _K_DISARMED: True,
                     _K_ENABLED: False})
    log.info("auto_budget disarmed — sticky; equity baseline reverts to live budget")


def is_disarmed() -> bool:
    """True once disarm() ran and nobody has explicitly re-armed since."""
    try:
        return bool(db.get_state().get(_K_DISARMED))
    except Exception:
        return False


# ── the daily job ────────────────────────────────────────────────────────────

def recompute_and_apply() -> dict:
    """Daily: recompute the compounding target and apply it if it moved enough.

    Steps:
      1. no-op if disabled.
      2. arm on first run (capture seed + base_realized; neutral, no jump).
      3. compute target from realized PnL, bounded by floor/ceil + live equity.
      4. apply only if |target − current| clears the hysteresis step (avoids
         churning the budget — and Telegram — on noise).
      5. notify + record to audit history.

    Returns a structured result (also handy for tests / the web panel).
    """
    # Phase 0 freeze, checked BEFORE enabled(). enabled() already returns False
    # while frozen, so testing it first would report "disabled" — technically
    # true, but it sends whoever reads the log looking for a toggle that is not
    # the reason. Say which gate actually stopped it.
    #
    # This is its own gate because budget_usd is written straight to db-state
    # here, not through runtime_config.set_param, so AUTO_APPLY_PARAMS never
    # covered it. The 2026-08-10 audit found an armed seed of $5,000 that would
    # have walked an owner-set $10,000 budget back to ~$4,549 at the next daily
    # close — and the budget used to drive derive_max_positions() too, so an
    # unattended write here also changed how many names the bot holds at once.
    from . import runtime_config
    if runtime_config.frozen():
        return {"applied": False, "reason": "params frozen (PARAMS_FROZEN)"}

    # A sticky disarm outranks the toggle. Without this the auto-arm below reads
    # "no seed" as "first run" and re-arms, so disarm behaved as a reset.
    if is_disarmed():
        return {"applied": False, "reason": "disarmed by owner (sticky)"}

    if not enabled():
        return {"applied": False, "reason": "disabled"}

    from . import risk_manager

    # (2) arm on first run.
    if not is_armed():
        ref = arm()
        msg = (f"🌱 *自动复利已启动* — 基准 ${ref['seed']:.0f}\n"
               f"  之后机器人每赚到的利润会自动滚入可投入预算（涨随高水位、"
               f"跌则缩减），全自动并每次变动通知你。")
        _notify(msg)
        _record({"ts": _now(), "event": "armed", "seed": ref["seed"],
                 "base_realized": ref["base_realized"]})
        return {"applied": False, "reason": "armed", "seed": ref["seed"]}

    seed = seed_usd() or risk_manager.budget_usd()
    base = base_realized()
    realized = float(db.get_state().get("realized_pnl_total") or 0.0)
    live_eq = risk_manager._live_equity_cache.get("value")

    target, diag = compute_target(seed, base, realized, live_eq)
    current = risk_manager.budget_usd()

    step = max(settings.auto_budget_min_step_usd,
               settings.auto_budget_min_step_pct * current)
    if abs(target - current) < step:
        log.info("auto_budget: target $%.0f ≈ current $%.0f (Δ<$%.0f) — hold",
                 target, current, step)
        return {"applied": False, "reason": "below_step", "target": target,
                "current": current, "diag": diag}

    # (4) apply — write the same db-state key the owner's panel uses, so the GUI
    # reflects it and sizing_capital()/max_positions() pick it up next scan.
    db.update_state({"budget_usd": target})
    direction = "↑ 增投" if target > current else "↓ 缩减"
    log.warning("auto_budget %s: $%.0f → $%.0f (profit_since=$%.2f)",
                direction, current, target, diag["profit_since"])
    rec = {"ts": _now(), "event": "apply", "old": round(current, 2),
           "new": target, "profit_since": diag["profit_since"],
           "capped_by_equity": diag["capped_by_equity"]}
    _record(rec)
    _notify(_format_telegram(current, target, diag))
    return {"applied": True, "old": current, "new": target, "diag": diag}


# ── helpers ──────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _record(entry: dict) -> None:
    try:
        hist = list(db.get_state().get(_K_HISTORY, []) or [])
        hist.append(entry)
        db.update_state({_K_HISTORY: hist[-100:]})
    except Exception as e:
        log.debug("auto_budget history write skipped: %s", e)


def _notify(msg: str) -> None:
    try:
        from . import notifier
        notifier.send(msg)
    except Exception as e:
        log.debug("auto_budget notify skipped: %s", e)


def _format_telegram(old: float, new: float, diag: dict) -> str:
    arrow = "📈" if new > old else "📉"
    head = "增投" if new > old else "缩减(防守)"
    lines = [
        f"{arrow} *自动复利 · {head}*",
        f"  可投入预算: ${old:.0f} → *${new:.0f}*  (Δ${new - old:+.0f})",
        f"  机器人净赚(自启动): ${diag['profit_since']:+.2f}  |  基准 ${diag['seed']:.0f}",
    ]
    if diag.get("capped_by_equity"):
        lines.append("  ⚠ 已被实际账户净值上限封顶（不会投入超过账户里的钱）。")
    lines.append("  下限 ${:.0f} / 上限 ${:.0f}。回复可随时手动改预算或暂停复利。".format(
        diag["floor"], diag["ceil"]))
    return "\n".join(lines)


def status() -> dict:
    """Snapshot for the web panel / status endpoint."""
    from . import risk_manager
    s = seed_usd()
    realized = float(db.get_state().get("realized_pnl_total") or 0.0)
    out = {
        "enabled": enabled(),
        "armed": is_armed(),
        "seed": s,
        "current_budget": round(risk_manager.budget_usd(), 2),
        "realized_total": round(realized, 2),
    }
    if s is not None:
        live_eq = risk_manager._live_equity_cache.get("value")
        target, diag = compute_target(s, base_realized(), realized, live_eq)
        out["target"] = target
        out["profit_since_arm"] = diag["profit_since"]
    return out
