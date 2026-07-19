"""Audit internal trade state vs broker positions.

Detects three failure modes:
  • ORPHAN    — broker has the position, our records don't (manual buy or
                executor crashed before persisting open_trades.json).
  • GHOST     — our records claim a position the broker doesn't have
                (closed manually or unrecorded close).
  • MISMATCH  — both sides know the symbol but qty differs (partial fill).

We log + persist a snapshot to data/reconcile.json so the GUI can surface it.
Auto-fix is intentionally NOT done — wrong-direction repairs cost real money.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytz

from . import db
from .config import settings

log = logging.getLogger(__name__)

OPEN_TRADES_FILE = settings.root / "data" / "open_trades.json"
RECONCILE_FILE = settings.root / "data" / "reconcile.json"
NY = pytz.timezone("America/New_York")

# In-flight close grace: after the bot places a closing sell, the broker can
# keep showing the position until the fill lands. Within this window a
# just-closed symbol is NOT an orphan (2026-07-16: AMAT SL sell placed, 2s
# later reconcile adopted the in-flight position as a "manual buy" and sent
# the owner a 检测到手动持仓 alert). A real leftover — fill genuinely failed —
# outlives the window and still adopts on a later scan.
RECENT_CLOSE_GRACE_S = 15 * 60


def _iso_age_s(ts, now: datetime | None = None) -> float | None:
    """Seconds elapsed since ISO timestamp `ts` (naive = UTC). None if unparsable."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", ""))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(pytz.utc).replace(tzinfo=None)
    return ((now or datetime.utcnow()) - dt).total_seconds()


def record_recent_close(symbol: str) -> None:
    """Tombstone a bot-side close so the orphan scan ignores the broker's
    still-filling position for RECENT_CLOSE_GRACE_S. Called by
    executor._close_and_log on every close. Map is pruned to 24h so it
    stays bounded."""
    def _upd(state: dict) -> dict:
        now = datetime.utcnow()
        closes = state.get("recent_closes") or {}
        keep = {}
        for s, t in closes.items():
            age = _iso_age_s(t, now)
            # age 0.0 is falsy — compare against None explicitly, else a
            # tombstone written the same second gets pruned right away.
            if age is not None and age < 24 * 3600:
                keep[s] = t
        keep[symbol] = now.isoformat()
        return {"recent_closes": keep}
    try:
        db.atomic_state(_upd)
    except Exception as e:
        log.debug("record_recent_close %s failed: %s", symbol, e)


def _recent_close_age_s(symbol: str) -> float | None:
    try:
        ts = (db.get_state().get("recent_closes") or {}).get(symbol)
    except Exception:
        return None
    return _iso_age_s(ts) if ts else None


def reconcile(broker_positions: pd.DataFrame, auto_fix: bool = True,
              client=None) -> dict:
    """Thread-safe wrapper — reconcile's load→fix→save on the open-trades
    store must not interleave with the manage/fast-stop ticks' own
    load→mutate→save cycles (2026-07-16: a manage tick's in-memory copy
    resurrected the ghost AMAT reconcile had just dropped, so the same ghost
    was dropped twice, 15 min apart)."""
    from . import executor
    with executor._TRADES_LOCK:
        return _reconcile_locked(broker_positions, auto_fix, client)


def _reconcile_locked(broker_positions: pd.DataFrame, auto_fix: bool = True,
                      client=None) -> dict:
    """Compare our internal records to broker positions.

    2026-05-30 update: auto-fix the safe drifts instead of just logging.
      • ORPHAN (broker has, we don't) → adopt with defensive stop/TP
      • GHOST  (we track, broker doesn't) → drop our stale record
      • MISMATCH (qty drift) → adopt broker's qty (broker is authoritative)
    After auto-fix, the state is internally consistent again — no need to halt.
    Halt is reserved for repeated-after-fix drift (broken state we can't recover).
    """
    try:
        our_trades = db.load_open_trades()
    except Exception:
        # Fall back to legacy JSON mirror if SQLite is unavailable
        try:
            our_trades = json.loads(OPEN_TRADES_FILE.read_text()) if OPEN_TRADES_FILE.exists() else {}
        except json.JSONDecodeError:
            our_trades = {}

    broker_holdings: dict[str, dict] = {}    # symbol → {qty, cost_price}
    if not broker_positions.empty:
        held = broker_positions[broker_positions["qty"].astype(float) > 0]
        for _, row in held.iterrows():
            sym = row["code"].split(".")[-1]
            broker_holdings[sym] = {
                "qty": int(float(row["qty"])),
                "cost_price": float(row.get("cost_price") or 0),
            }

    our_syms = {s for s, t in our_trades.items() if t.get("qty", 0) > 0}
    broker_syms = set(broker_holdings.keys())

    # The cash-yield ETF (e.g. SGOV) is a cash equivalent managed by
    # src/cash_yield.py, NOT a strategy position — exclude it from reconcile so
    # it is never adopted as an orphan or flagged as drift. Honors the runtime
    # toggle (web), not just the .env default.
    try:
        from . import cash_yield
        if cash_yield.enabled():
            cy = cash_yield.symbol()
            broker_syms.discard(cy)
            broker_holdings.pop(cy, None)
    except Exception:
        pass
    # Same for the inverse-ETF sleeve holding (managed by src/inverse_sleeve.py).
    try:
        from . import inverse_sleeve
        if inverse_sleeve.enabled():
            iv = inverse_sleeve.symbol()
            broker_syms.discard(iv)
            broker_holdings.pop(iv, None)
    except Exception:
        pass

    orphans = [
        {"symbol": s,
         "broker_qty": broker_holdings[s]["qty"],
         "broker_cost": broker_holdings[s]["cost_price"]}
        for s in sorted(broker_syms - our_syms)
    ]
    # Filter out in-flight closes BEFORE classification (not just before
    # adoption) so they don't raise a Reconcile alert or count toward the
    # severe streak either.
    _still = []
    for o in orphans:
        age = _recent_close_age_s(o["symbol"])
        if age is not None and age < RECENT_CLOSE_GRACE_S:
            log.info("reconcile: %s closed by bot %.0fs ago — sell fill in "
                     "flight, not an orphan (grace %ds)",
                     o["symbol"], age, RECENT_CLOSE_GRACE_S)
        else:
            _still.append(o)
    orphans = _still
    ghosts = [
        {"symbol": s,
         "our_qty": our_trades[s].get("qty"),
         "entry": our_trades[s].get("entry_price")}
        for s in sorted(our_syms - broker_syms)
    ]
    mismatches = []
    for s in sorted(our_syms & broker_syms):
        our_qty = int(our_trades[s].get("qty", 0))
        broker_qty = broker_holdings[s]["qty"]
        if our_qty != broker_qty:
            # Same in-flight grace as orphans: a scale-out (TP1/TP2) places its
            # tranche sell and decrements our qty moments before reconcile
            # compares against a broker snapshot still holding the pre-sale
            # qty. "Adopting" that stale qty re-inflates the record — the next
            # stop fire would then OVERSELL. Genuine drift outlives the window.
            age = _recent_close_age_s(s)
            if age is not None and age < RECENT_CLOSE_GRACE_S:
                log.info("reconcile: %s qty drift (ours=%d broker=%d) within "
                         "close-grace window (%.0fs) — fill in flight, not a "
                         "mismatch", s, our_qty, broker_qty, age)
                continue
            mismatches.append({
                "symbol": s, "our_qty": our_qty, "broker_qty": broker_qty
            })

    fixes_applied: list[dict] = []

    if auto_fix:
        now_iso = datetime.utcnow().isoformat()

        # FIX ORPHAN: add to our tracking with defensive stop/TP defaults.
        for o in orphans:
            sym = o["symbol"]
            cost = o["broker_cost"] or 0.0
            if cost <= 0:
                log.warning("Reconcile orphan %s has zero cost_price — skipping auto-add",
                            sym)
                continue
            # ATR-derived stop/TP using the bot's LIVE exit multipliers. Use the
            # REAL ATR(14) when a client is available — the old 2%-of-price
            # proxy put stops in the wrong place for every name whose true ATR
            # wasn't 2% (the first 8 adopted orphans went 1/8 at −$193 largely
            # on fabricated levels). Falls back to the proxy only if the kline
            # fetch fails.
            from . import runtime_config
            atr_proxy = cost * 0.02
            if client is not None:
                try:
                    import pandas_ta_classic as ta
                    kdf = client.get_kline(sym, bars=30)
                    real_atr = float(ta.atr(kdf["high"], kdf["low"],
                                            kdf["close"], length=14).iloc[-1])
                    if real_atr > 0 and not pd.isna(real_atr):
                        atr_proxy = real_atr
                except Exception as e:
                    log.warning("orphan %s: real ATR fetch failed (%s) — using 2%% proxy",
                                sym, e)
            our_trades[sym] = {
                "symbol": sym, "qty": o["broker_qty"],
                "entry_price": cost,
                "stop_loss": round(cost - runtime_config.sl_atr_mult() * atr_proxy, 2),
                "take_profit": round(cost + runtime_config.tp_atr_mult() * atr_proxy, 2),
                "atr": atr_proxy,
                "half_closed": False,
                "buy_order_id": None, "stop_order_id": None, "tp_order_id": None,
                "opened_at": now_iso,
                "high_water": cost, "low_water": cost,
                "ml_proba_entry": None,
                "strategy": "reconcile_orphan_recovery",
                "stacks": 1,
                # Manual-position hand-off (2026-06-22): an orphan is a position
                # the bot never opened (your manual broker-app buy, or crash
                # recovery). Mark it owner-held + pending so NO auto-exit (soft
                # stop, fast-stop tick, blacklist/gap/over-cap flush) touches it
                # until manual_positions.review_adopted() has judged its risk and
                # either released it to the bot (NORMAL) or queued a takeover
                # approval (HIGH). review runs in the SAME scan, right after this.
                "manual_adopted": True,
                "user_managed": True,
                "pending_review": True,
            }
            fixes_applied.append({"type": "ORPHAN_ADOPTED", "symbol": sym,
                                  "qty": o["broker_qty"], "cost": cost})
            log.warning("Reconcile auto-fix: adopted orphan %s qty=%d @ $%.2f",
                        sym, o["broker_qty"], cost)

        # FIX GHOST: a tracked position the broker no longer holds = it was closed
        # ELSEWHERE — you sold it yourself in the broker app (or an unrecorded
        # close). BOOK the realised P&L at the true fill price so the trade lands
        # in trades.jsonl / win-rate / equity, THEN drop the record. Falls back to
        # a fresh last price (flagged approximate) so the trade is still recorded
        # even when the fill lookup fails. Booked once: next scan it's no longer a
        # ghost, so there's no double-count.
        from . import executor
        for g in ghosts:
            sym = g["symbol"]
            trade = our_trades.get(sym)

            # 2026-07-09 phantom guard: a "ghost" whose BUY order never filled
            # is NOT a manual close — the position never existed. (Root cause
            # incident: the OpenD-rs gateway accepted place_order but returned
            # order_id=0 and silently dropped the order, so every buy became a
            # fake MANUAL_SELL loss + instant re-buy loop.) Verify the buy
            # actually filled before booking any P&L.
            buy_oid = str((trade or {}).get("buy_order_id") or "").strip()
            buy_status = ""
            if trade is not None and client is not None and buy_oid not in ("", "0", "None"):
                buy_status = client.get_order_status(buy_oid)
            if buy_status in ("SUBMITTED", "SUBMITTING", "WAITING_SUBMIT"):
                # Buy order still live at the broker — not a ghost, the fill
                # just hasn't happened yet. Leave the record; the stale-order
                # canceller owns unfilled buys.
                log.info("reconcile: %s buy order %s still pending — skipping "
                         "ghost handling this round", sym, buy_oid)
                continue
            if trade is not None and buy_status not in ("FILLED_ALL", "FILLED_PART"):
                # Can't confirm the buy ever filled. For a RECENT entry the
                # order id would still be in the day's order list, so unknown/
                # unfilled status means a phantom: drop the record WITHOUT
                # booking a fake manual sell. Older entries fall through to
                # normal booking — their ids have rolled out of the order-list
                # window, and a position held across days must have filled.
                age_h = None
                try:
                    opened_dt = datetime.fromisoformat(
                        str(trade.get("opened_at") or "").replace("Z", ""))
                    age_h = (datetime.utcnow() - opened_dt).total_seconds() / 3600
                except ValueError:
                    pass
                if age_h is not None and age_h < 24:
                    our_trades.pop(sym, None)
                    fixes_applied.append({"type": "GHOST_DROPPED", "symbol": sym,
                                          "phantom": True})
                    log.warning("Reconcile auto-fix: dropped phantom %s — buy "
                                "order %s never filled, no P&L booked",
                                sym, buy_oid or "?")
                    continue

            booked = None
            if trade and client is not None:
                exit_price = None
                approx = False
                try:
                    fill = client.get_last_sell_fill(sym)
                except Exception as e:
                    log.warning("ghost %s: fill lookup failed: %s", sym, e)
                    fill = None
                if fill and fill.get("price", 0) > 0:
                    exit_price = float(fill["price"])
                else:
                    try:
                        lp = executor._last_price(client, sym)
                        if lp:
                            exit_price, approx = float(lp), True
                    except Exception:
                        pass
                if exit_price:
                    try:
                        qty = int(trade.get("qty") or g.get("our_qty") or 0)
                        pnl = executor._close_and_log(sym, trade, qty, exit_price,
                                                      "MANUAL_SELL")
                        booked = {"exit": exit_price, "qty": qty,
                                  "pnl": round(pnl, 2), "approx": approx}
                    except Exception as e:
                        log.warning("ghost %s: booking close failed: %s", sym, e)
            our_trades.pop(sym, None)
            fix = {"type": "GHOST_DROPPED", "symbol": sym}
            if booked:
                fix["booked"] = booked
            fixes_applied.append(fix)
            log.warning("Reconcile auto-fix: dropped ghost %s%s", sym,
                        (f" (booked manual sell @ ${booked['exit']:.2f}, "
                         f"pnl ${booked['pnl']:+.0f}"
                         f"{' ~approx' if booked['approx'] else ''})") if booked else "")
            if booked:
                try:
                    from . import notifier
                    tag = "（现价近似）" if booked["approx"] else ""
                    notifier.send(f"📝 已记录你手动平仓 {sym} {booked['qty']} 股 @ "
                                  f"${booked['exit']:.2f}{tag} — 已实现 "
                                  f"${booked['pnl']:+.0f}，已计入交易统计。")
                except Exception:
                    pass

        # FIX MISMATCH: adopt broker's qty (broker side is authoritative).
        for m in mismatches:
            sym = m["symbol"]
            if sym in our_trades:
                our_trades[sym]["qty"] = m["broker_qty"]
                fixes_applied.append({"type": "QTY_ADOPTED", "symbol": sym,
                                      "old_qty": m["our_qty"], "new_qty": m["broker_qty"]})
                log.warning("Reconcile auto-fix: %s qty %d → %d",
                            sym, m["our_qty"], m["broker_qty"])

        if fixes_applied:
            try:
                OPEN_TRADES_FILE.write_text(json.dumps(our_trades, indent=2, default=str))
                # Persist reconciled state to SQLite (primary store). Use the db
                # layer directly instead of the executor-internal _save helper so
                # reconcile doesn't couple to executor's private API surface.
                existing_db = set(db.load_open_trades().keys())
                for sym in existing_db - set(our_trades.keys()):
                    db.delete_open_trade(sym)
                for sym, t in our_trades.items():
                    t = dict(t)
                    t["symbol"] = sym
                    db.upsert_open_trade(t)
            except Exception as e:
                log.error("reconcile auto-fix write failed: %s", e)

        # Reset severe streak — drift just got fixed.
        try:
            db.update_state({"reconcile_severe_streak": 0})
        except Exception:
            pass

    # Streak tracker (legacy 2-strike halt) — only counts UNFIXED severe state.
    unfixed_severe = (not auto_fix) and (
        bool(mismatches) or (bool(orphans) and bool(ghosts))
    )
    severe_streak = int(db.get_state().get("reconcile_severe_streak", 0) or 0)
    if unfixed_severe:
        severe_streak += 1
    else:
        severe_streak = 0
    db.update_state({"reconcile_severe_streak": severe_streak})
    halt_now = unfixed_severe and severe_streak >= 2

    result = {
        "ts": datetime.now(NY).isoformat(),
        "ok": not (orphans or ghosts or mismatches),
        "severe": unfixed_severe,
        "orphans": orphans,
        "ghosts": ghosts,
        "mismatches": mismatches,
        "fixes_applied": fixes_applied,
        "summary": (
            f"OK ({len(broker_syms)} positions)"
            if not (orphans or ghosts or mismatches)
            else f"{len(orphans)} orphan, {len(ghosts)} ghost, "
                 f"{len(mismatches)} mismatch — {len(fixes_applied)} auto-fixed"
        ),
    }

    if halt_now:
        try:
            db.atomic_state(lambda s: {"halted": True})
            log.warning("Severe reconcile drift (streak=%d, no auto-fix) → halted=True",
                        severe_streak)
        except Exception as e:
            log.error("auto-halt on reconcile failed: %s", e)

    RECONCILE_FILE.parent.mkdir(parents=True, exist_ok=True)
    RECONCILE_FILE.write_text(json.dumps(result, indent=2))
    return result


def log_reconcile(result: dict) -> str:
    """Pretty-print issues to log. Returns short summary for notifier."""
    if result["ok"]:
        log.info("Reconcile OK — %s", result["summary"])
        return ""

    log.warning("⚠ RECONCILE ISSUES — %s", result["summary"])
    msg_lines = ["*Reconcile alert*"]
    for o in result["orphans"]:
        line = f"ORPHAN: {o['symbol']} qty={o['broker_qty']} in broker, not tracked"
        log.warning("  %s", line)
        msg_lines.append(line)
    for g in result["ghosts"]:
        entry = g.get("entry") or 0
        line = f"GHOST: {g['symbol']} qty={g['our_qty']} @ ${entry:.2f} tracked, broker empty"
        log.warning("  %s", line)
        msg_lines.append(line)
    for m in result["mismatches"]:
        line = f"MISMATCH: {m['symbol']} ours={m['our_qty']} broker={m['broker_qty']}"
        log.warning("  %s", line)
        msg_lines.append(line)
    return "\n".join(msg_lines)
