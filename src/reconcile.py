"""Audit internal trade state vs broker positions.

Detects four failure modes:
  • ORPHAN    — broker has the position, our records don't (manual buy or
                executor crashed before persisting open_trades.json).
  • GHOST     — our records claim a position the broker doesn't have
                (closed somewhere we didn't record — source determined from
                broker sell orders, never assumed).
  • MISMATCH  — both sides know the symbol but qty differs (partial fill).
  • SHORT_DRIFT — the broker is NET SHORT a symbol. The bot is long-only, so
                this is exposure nobody manages, and worse: a buy on that
                symbol nets against the short instead of opening a long.
                Reported and blocked, never auto-fixed (unwinding it means
                placing orders the strategy never asked for).

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


def _sell_after_entry(sell_time: str, opened_at: str) -> bool:
    """True if a broker sell at `sell_time` could have closed a position opened
    at `opened_at`. Sells that predate the entry belong to an earlier round trip
    and must not be used as evidence for this one.

    Careful with clocks: the SDK reports create_time as naive EASTERN, while
    open_trades stores opened_at as naive UTC. Comparing them raw would be off
    by 4-5 hours — enough to accept a stale sell or reject the right one.
    Unparsable input returns True so a format surprise degrades to the old
    (permissive) behaviour rather than silently discarding real evidence.
    """
    try:
        sold = datetime.fromisoformat(sell_time.strip().replace("/", "-").split(".")[0])
        entered_utc = datetime.fromisoformat(str(opened_at).replace("Z", ""))
    except ValueError:
        return True
    if entered_utc.tzinfo is not None:
        entered_utc = entered_utc.astimezone(pytz.utc).replace(tzinfo=None)
    entered_et = pytz.utc.localize(entered_utc).astimezone(NY).replace(tzinfo=None)
    return (sold - entered_et).total_seconds() > -60      # 60s clock-skew grace


def _quarantine_unexplained(symbol: str, trade: dict | None, ghost: dict) -> None:
    """Park an unexplainable close in data/unexplained_closes.jsonl.

    Keeps the event auditable without letting an unverified number into
    trades.jsonl, where it would feed win rate, half-Kelly sizing, the
    optimizer and the blacklist. Never raises — quarantine is a courtesy, the
    drop itself already happened."""
    try:
        path = settings.root / "data" / "unexplained_closes.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": datetime.now(NY).isoformat(),
            "symbol": symbol,
            "qty": (trade or {}).get("qty") or ghost.get("our_qty"),
            "entry_price": (trade or {}).get("entry_price") or ghost.get("entry"),
            "opened_at": (trade or {}).get("opened_at"),
            "buy_order_id": (trade or {}).get("buy_order_id"),
            "strategy": (trade or {}).get("strategy"),
            "note": "broker holds none and reports no dealt SELL order — "
                    "close source unknown, deliberately excluded from stats",
        }
        with path.open("a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except Exception as e:
        log.debug("quarantine %s failed: %s", symbol, e)


def net_positions(broker_positions: pd.DataFrame) -> dict[str, float]:
    """symbol → SIGNED net qty from a broker position frame (negative = short).

    The bot is long-only, so every call site used to filter `qty > 0` and drop
    the rest. That silently erased short rows from the entire system — and a
    short is NOT the same as flat: a buy nets against it instead of opening a
    long. See short_symbols() for the damage that caused.
    """
    out: dict[str, float] = {}
    if broker_positions is None or broker_positions.empty:
        return out
    for _, row in broker_positions.iterrows():
        try:
            qty = float(row["qty"])
        except (KeyError, TypeError, ValueError):
            continue
        if qty == 0:
            continue
        sym = str(row["code"]).split(".")[-1]
        out[sym] = out.get(sym, 0.0) + qty
    return out


def short_symbols(broker_positions: pd.DataFrame) -> set[str]:
    """Symbols the account is NET SHORT.

    Root cause of the 2026-07-28 DDOG incident: the account carried a −3 DDOG
    short (residue of a 2026-06-03 double-sell). The bot bought 2 shares, the
    fill netted the short to −1, no long ever existed, and reconcile — which
    only looked at `qty > 0` — read that as "broker doesn't have it" and booked
    a fabricated MANUAL_SELL against the owner. Every entry now checks this set
    first, and reconcile reports these as SHORT_DRIFT instead of pretending
    they aren't there.
    """
    return {s for s, q in net_positions(broker_positions).items() if q < 0}


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

    broker_holdings: dict[str, dict] = {}    # symbol → {qty, cost_price} (LONG only)
    broker_shorts: dict[str, dict] = {}      # symbol → {qty, cost_price} (NET SHORT)
    if not broker_positions.empty:
        for _, row in broker_positions.iterrows():
            try:
                qty = float(row["qty"])
            except (KeyError, TypeError, ValueError):
                continue
            if qty == 0:
                continue
            sym = str(row["code"]).split(".")[-1]
            rec = {"qty": int(qty), "cost_price": float(row.get("cost_price") or 0)}
            # A short is tracked separately, never silently dropped: it is not a
            # position the bot may manage (long-only), but it DOES absorb buys,
            # so entry must refuse it and ghost handling must not mistake it for
            # a sale that never happened.
            (broker_holdings if qty > 0 else broker_shorts)[sym] = rec

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

        # FIX GHOST: a tracked position the broker no longer holds. It was closed
        # somewhere we didn't record — but WHO closed it is a question to be
        # ANSWERED FROM BROKER EVIDENCE, never assumed.
        #
        # 2026-07-29 rewrite. This branch used to have exactly one story ("you
        # sold it in the broker app"), price the exit off a live quote when the
        # fill lookup failed, book it as MANUAL_SELL, and message the owner
        # asserting they'd done it. All three steps were wrong at once for DDOG
        # on 2026-07-28: no sale had happened (the buy netted against a legacy
        # short), the fill lookup CANNOT succeed under SIMULATE (paper trading
        # refuses deal queries), and the invented +$2.2 landed in trades.jsonl,
        # the win rate, half-Kelly sizing and the optimizer. Attribution now runs
        # in evidence order and books P&L only when a real sell fill backs it.
        from . import executor
        for g in ghosts:
            sym = g["symbol"]
            trade = our_trades.get(sym)

            # (0) NETTED AGAINST A SHORT — not a close at all. Our buy reduced a
            # pre-existing short instead of opening a long, so the "position we
            # think we hold" never existed. Drop the record, book nothing, and
            # shout: the short is a real, unmanaged exposure.
            if sym in broker_shorts:
                short_qty = broker_shorts[sym]["qty"]
                our_trades.pop(sym, None)
                fixes_applied.append({"type": "GHOST_DROPPED", "symbol": sym,
                                      "netted_against_short": short_qty})
                log.error("Reconcile auto-fix: dropped %s — NOT a sale. Broker is "
                          "NET SHORT %d, so our buy netted against that short and "
                          "no long was ever opened. No P&L booked.",
                          sym, short_qty)
                try:
                    from . import notifier
                    notifier.send(
                        f"⚠️ {sym} 并没有被卖出。账户在该标的上是净空头 "
                        f"({short_qty} 股)，机器人的买入被用去轧空头，多头仓位从未建立。"
                        f"已删除错误记录，未记任何盈亏。请先清掉这个空头。")
                except Exception:
                    pass
                continue

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

            # (2) WHO SOLD IT? Look for a SELL order that actually dealt. The
            # order list answers this in BOTH envs (the deal list is paper-
            # blocked), and its order_id is what separates our own exit from a
            # hand-placed one — "not in my records" is not evidence of a manual
            # sale, it's evidence of nothing.
            sells: list[dict] = []
            if trade is not None and client is not None:
                try:
                    sells = client.find_dealt_sell_orders(sym)
                except Exception as e:
                    log.warning("ghost %s: sell-order lookup failed: %s", sym, e)
            # Only sells AFTER our entry can explain THIS position.
            opened_at = str((trade or {}).get("opened_at") or "")
            our_oids = {str((trade or {}).get(k) or "")
                        for k in ("stop_order_id", "tp_order_id", "exit_order_id")}
            our_oids.discard("")
            sells = [s for s in sells
                     if not opened_at or not s["time"]
                     or _sell_after_entry(s["time"], opened_at)]

            if not sells:
                # NO evidence of any sale. Previously this invented a price off
                # the live quote and called it MANUAL_SELL. Quarantine instead:
                # the record is dropped (the broker is authoritative on what we
                # hold) but nothing enters the performance stats, because a
                # fabricated trade corrupts win rate, Kelly and the optimizer
                # far more than a missing one does.
                our_trades.pop(sym, None)
                fixes_applied.append({"type": "GHOST_DROPPED", "symbol": sym,
                                      "unexplained": True})
                _quarantine_unexplained(sym, trade, g)
                log.error("Reconcile auto-fix: dropped ghost %s — UNEXPLAINED. "
                          "Broker holds none, and has NO dealt sell order for it. "
                          "No P&L booked (quarantined to data/unexplained_closes.jsonl).",
                          sym)
                try:
                    from . import notifier
                    notifier.send(
                        f"❓ {sym} 从券商持仓中消失，但券商没有任何该标的的成交卖单。"
                        f"无法判定平仓来源，已删除本地记录、未记盈亏，明细存入 "
                        f"unexplained_closes.jsonl 待你核对。")
                except Exception:
                    pass
                continue

            last_sell = sells[-1]
            by_bot = last_sell["order_id"] in our_oids
            reason = "BOT_SELL_UNRECORDED" if by_bot else "MANUAL_SELL"
            booked = None
            try:
                qty = int(trade.get("qty") or g.get("our_qty") or 0)
                pnl = executor._close_and_log(sym, trade, qty,
                                              last_sell["price"], reason)
                booked = {"exit": last_sell["price"], "qty": qty,
                          "pnl": round(pnl, 2), "reason": reason,
                          "order_id": last_sell["order_id"],
                          "sold_at": last_sell["time"]}
            except Exception as e:
                log.warning("ghost %s: booking close failed: %s", sym, e)
            our_trades.pop(sym, None)
            fix = {"type": "GHOST_DROPPED", "symbol": sym}
            if booked:
                fix["booked"] = booked
            fixes_applied.append(fix)
            log.warning("Reconcile auto-fix: dropped ghost %s%s", sym,
                        (f" (booked {booked['reason']} @ ${booked['exit']:.2f} "
                         f"from broker order {booked['order_id']}, "
                         f"pnl ${booked['pnl']:+.0f})") if booked else "")
            if booked:
                try:
                    from . import notifier
                    who = ("机器人自己的卖单（本地未记录）" if by_bot
                           else "券商端的手动卖单（不是机器人下的）")
                    notifier.send(f"📝 {sym} {booked['qty']} 股平仓已入账 @ "
                                  f"${booked['exit']:.2f} — 来源：{who}，"
                                  f"券商订单号 {booked['order_id']}，"
                                  f"已实现 ${booked['pnl']:+.0f}。")
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

    # SHORT_DRIFT is reported, never auto-fixed: unwinding a short means placing
    # a BUY, and reconcile does not get to open orders the strategy never asked
    # for. It blocks entries (executor) and blocks ghost mis-booking (above);
    # clearing it is the owner's call.
    shorts = [{"symbol": s, "broker_qty": v["qty"], "cost_price": v["cost_price"]}
              for s, v in sorted(broker_shorts.items())]

    result = {
        "ts": datetime.now(NY).isoformat(),
        "ok": not (orphans or ghosts or mismatches),
        "severe": unfixed_severe,
        "orphans": orphans,
        "ghosts": ghosts,
        "mismatches": mismatches,
        "shorts": shorts,
        "fixes_applied": fixes_applied,
        "summary": (
            (f"OK ({len(broker_syms)} positions)"
             if not (orphans or ghosts or mismatches)
             else f"{len(orphans)} orphan, {len(ghosts)} ghost, "
                  f"{len(mismatches)} mismatch — {len(fixes_applied)} auto-fixed")
            + (f" [+{len(shorts)} SHORT drift]" if shorts else "")
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


def _shorts_alert_due() -> bool:
    """True at most once per NY day — short drift is a standing condition, not
    an event, so it must not re-alert every 15-minute scan."""
    today = datetime.now(NY).date().isoformat()
    try:
        if (db.get_state().get("shorts_alerted_day") or "") == today:
            return False
        db.update_state({"shorts_alerted_day": today})
    except Exception:
        return False
    return True


def log_reconcile(result: dict) -> str:
    """Pretty-print issues to log. Returns short summary for notifier."""
    shorts = result.get("shorts") or []
    short_lines = []
    for s in shorts:
        line = (f"SHORT DRIFT: {s['symbol']} qty={s['broker_qty']} at broker "
                f"(cost ${s['cost_price']:.2f}) — unmanaged short exposure, "
                f"entries on this symbol are blocked")
        log.warning("  %s", line)
        short_lines.append(line)

    if result["ok"]:
        log.info("Reconcile OK — %s", result["summary"])
        if short_lines and _shorts_alert_due():
            return ("*Reconcile alert*\n" + "\n".join(short_lines)
                    + "\n（机器人只做多，这些空头不归它管，请手动平掉）")
        return ""

    log.warning("⚠ RECONCILE ISSUES — %s", result["summary"])
    msg_lines = ["*Reconcile alert*"]
    if short_lines and _shorts_alert_due():
        msg_lines.extend(short_lines)
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
