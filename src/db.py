"""SQLite persistence layer.

Replaces these crash-vulnerable JSON files with transactional tables:
  • open_trades.json   →  open_trades   (positions currently held)
  • state.json         →  kv_state      (loss streak, halted, realized PnL)
  • audit.jsonl        →  audit         (every entry decision)
  • trades.jsonl       →  closed_trades (R-multiple log)
  • history.jsonl      →  history       (per-scan equity snapshots)

Snapshot/cache files stay as JSON (overwritten atomically each scan):
  • account.json, reconcile.json, earnings.json

Design choices:
  • WAL mode → reader (GUI) doesn't block writer (scheduler).
  • Per-call connection inside `with conn()` context → no thread-affinity bugs.
  • One-time JSON → SQLite migration on first connection (idempotent).
  • Schema versioned via PRAGMA user_version (future migrations).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)

DB_FILE = settings.root / "data" / "trader.db"
SCHEMA_VERSION = 4          # v4: account/session identity model

# kv_state keys that belong to a brokerage ACCOUNT rather than to the software.
# Everything not listed here stays global (account_id=''): strategy params, AI
# provider, health probes, the telegram cursor, the market regime label.
#
# The split is "would this value be wrong if read against the other account?"
# Budget, drawdown peak, realized PnL, halt state and the compounding seed all
# would be — the paper account is carrying -$625 and 8 legacy naked shorts.
ACCOUNT_SCOPED_KEYS = frozenset({
    "budget_usd", "peak_equity", "starting_cash",
    "realized_pnl_total", "realized_pnl_today",
    "halted", "halt_started_at", "loss_streak_days", "last_close_day", "day",
    "auto_budget_seed", "auto_budget_base_realized", "auto_budget_enabled",
    "auto_budget_disarmed", "auto_budget_history",
    "recent_closes", "reentry_cooldown", "gap_exit_queue",
    "shorts_alerted_day", "reconcile_severe_streak",
    "portfolio_full_notified", "stats_reset_at", "pending_approvals",
})
GLOBAL_SCOPE = ""
_init_lock = threading.Lock()
_initialised = False


SCHEMA = """
-- ── Identity model (v4) ─────────────────────────────────────────────────────
-- An account is identified by an INTERNAL uuid that this software mints and
-- never changes. The broker's own id is stored beside it as a locator, not as
-- the identity: it is assigned by moomoo, differs between paper and live, and
-- can change when the OpenD environment is rebuilt. Keying our own records on
-- it would mean re-migrating every time that happened.
--
-- SIMULATE and REAL are two different accounts, always. Their positions, fills,
-- PnL, drawdown baseline and budget must never mix — the paper account is
-- currently carrying -$625 of realized loss and 8 legacy naked shorts, and a
-- single row of that leaking into a live account's risk state is a wrong-sized
-- order, not a cosmetic bug.
CREATE TABLE IF NOT EXISTS accounts (
    account_id     TEXT PRIMARY KEY,          -- internal uuid4, stable forever
    trade_env      TEXT NOT NULL
                   CHECK (trade_env IN ('SIMULATE', 'REAL')),
    broker_acc_id  TEXT,                      -- moomoo acc_id: locator only
    label          TEXT,
    created_at     TEXT NOT NULL,
    UNIQUE (trade_env, broker_acc_id)
);

-- One row per Start→Stop of a real worker process. trade_env is copied here and
-- is immutable for the life of the session: that is what makes "this run is
-- paper" a fact recorded at startup rather than a setting someone can flip
-- underneath running orders. The authorization columns are filled by the OpenD
-- startup check (Phase 0B-2); they exist now so that step needs no migration.
CREATE TABLE IF NOT EXISTS execution_sessions (
    session_id     TEXT PRIMARY KEY,          -- internal uuid4
    account_id     TEXT NOT NULL REFERENCES accounts(account_id),
    trade_env      TEXT NOT NULL              -- immutable; == accounts.trade_env
                   CHECK (trade_env IN ('SIMULATE', 'REAL')),
    pid            INTEGER NOT NULL,          -- the actual worker
    host           TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    ended_at       TEXT,
    end_reason     TEXT,
    auth_mode      TEXT,                      -- how trade_env was decided
    authorized_at  TEXT,
    authorized_by  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_account ON execution_sessions(account_id);
CREATE INDEX IF NOT EXISTS idx_sessions_open
    ON execution_sessions(ended_at) WHERE ended_at IS NULL;

CREATE TABLE IF NOT EXISTS open_trades (
    symbol          TEXT PRIMARY KEY,
    qty             INTEGER NOT NULL,
    entry_price     REAL NOT NULL,
    stop_loss       REAL NOT NULL,
    take_profit     REAL NOT NULL,
    atr             REAL,
    half_closed     INTEGER NOT NULL DEFAULT 0,
    buy_order_id    TEXT,
    stop_order_id   TEXT,
    tp_order_id     TEXT,
    opened_at       TEXT NOT NULL,
    high_water      REAL,         -- highest price seen since entry (for MFE on close)
    low_water       REAL,         -- lowest price seen since entry  (for MAE on close)
    ml_proba_entry  REAL,         -- ML proba captured at entry (used for calibration)
    strategy        TEXT,         -- which strategy fired the entry
    extra           TEXT,
    -- v4 identity. A position outlives the process that opened it, so the
    -- session here is the one that OPENED it and is never required — a restart
    -- must not orphan or duplicate a live position. The account is required:
    -- a position always belongs to exactly one account.
    account_id         TEXT,
    opened_session_id  TEXT
);

-- Scoped key-value state. account_id='' is the GLOBAL scope (strategy params,
-- AI provider, health probes, telegram cursor) — things that belong to the
-- software rather than to a brokerage account. A real account_id scopes the
-- state that must never cross between paper and live: budget, drawdown peak,
-- realized PnL, halt state, compounding seed.
CREATE TABLE IF NOT EXISTS kv_state (
    account_id TEXT NOT NULL DEFAULT '',   -- '' = global
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,              -- JSON-encoded
    PRIMARY KEY (account_id, key)
);

CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    action  TEXT NOT NULL,             -- skip | buy | error | scan_start | scan_end
    symbol  TEXT,
    gate    TEXT,
    reason  TEXT,
    score   REAL,
    extra   TEXT,                      -- JSON
    account_id  TEXT,                  -- v4 identity
    session_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts     ON audit(ts);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit(action);
CREATE INDEX IF NOT EXISTS idx_audit_symbol ON audit(symbol);

CREATE TABLE IF NOT EXISTS closed_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    qty             INTEGER NOT NULL,
    entry           REAL NOT NULL,
    stop            REAL NOT NULL,
    exit            REAL NOT NULL,
    exit_reason     TEXT,
    pnl             REAL,
    pnl_pct         REAL,
    r_multiple      REAL,
    opened_at       TEXT,
    mfe_pct         REAL,        -- max favorable excursion while open (% above entry)
    mae_pct         REAL,        -- max adverse excursion (% below entry)
    ml_proba_entry  REAL,        -- ML model's proba at entry (for calibration)
    strategy        TEXT,         -- "trend" | "mean_revert"
    extra           TEXT,         -- JSON blob for forward-compat fields (ml_features, etc.)
    -- v4 identity. A close IS a fill, so session_id is required for anything
    -- recorded from here on. It is NULL only for rows migrated from before the
    -- identity model existed; `migrated_from` says so explicitly rather than
    -- leaving a bare NULL to be guessed at later.
    account_id     TEXT,
    session_id     TEXT,
    migrated_from  TEXT
);
CREATE INDEX IF NOT EXISTS idx_closed_ts     ON closed_trades(ts);
CREATE INDEX IF NOT EXISTS idx_closed_symbol ON closed_trades(symbol);
-- idx_closed_strategy created inside _migrate_v2 once the column exists.

CREATE TABLE IF NOT EXISTS history (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 TEXT NOT NULL,
    week               TEXT,
    day                TEXT,
    invested           REAL,
    budget             REAL,
    unrealized_pnl     REAL,
    realized_pnl_total REAL,
    total_pnl          REAL,
    positions_count    INTEGER,
    symbols            TEXT,
    timeframe          TEXT,
    account_id         TEXT           -- v4 identity: equity curves never merge
);
CREATE INDEX IF NOT EXISTS idx_history_ts   ON history(ts);
CREATE INDEX IF NOT EXISTS idx_history_week ON history(week);
"""


# ---------- core: connection + init ----------

@contextmanager
def conn():
    """Yield a sqlite3 connection with WAL + foreign keys. Auto-commit on exit
    if no exception; rollback on exception."""
    _ensure_initialised()
    c = sqlite3.connect(str(DB_FILE), timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=5000")
    try:
        yield c
    finally:
        c.close()


@contextmanager
def transaction():
    """Yield a connection with an explicit BEGIN…COMMIT transaction.
    Rolls back on any exception — use for multi-row writes that must be atomic."""
    _ensure_initialised()
    c = sqlite3.connect(str(DB_FILE), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=5000")
    try:
        c.execute("BEGIN IMMEDIATE")
        yield c
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    finally:
        c.close()


def _ensure_initialised() -> None:
    """Run schema + migration exactly once per process."""
    global _initialised
    if _initialised:
        return
    with _init_lock:
        if _initialised:
            return
        DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(DB_FILE), timeout=10)
        try:
            c.execute("PRAGMA journal_mode=WAL")
            user_v = c.execute("PRAGMA user_version").fetchone()[0]
            # Run SCHEMA (idempotent CREATE IF NOT EXISTS) — works for fresh DBs
            # and is a no-op for already-migrated DBs.
            c.executescript(SCHEMA)
            if user_v == 0:
                _migrate_from_json(c)
            if user_v < 2:
                _migrate_v2(c)
            if user_v < 3:
                _migrate_v3(c)
            if user_v < 4:
                _migrate_v4(c)
            if user_v < SCHEMA_VERSION:
                c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            c.commit()
        finally:
            c.close()
        _initialised = True
        log.info("SQLite initialised at %s", DB_FILE)


# ---------- schema migrations ----------

def _migrate_v2(c: sqlite3.Connection) -> None:
    """v1 → v2: add MFE/MAE + ml_proba + strategy + water-mark columns.
    Uses ALTER TABLE ADD COLUMN — SQLite supports this with a NULL default."""
    new_cols_open = [
        ("high_water", "REAL"),
        ("low_water", "REAL"),
        ("ml_proba_entry", "REAL"),
        ("strategy", "TEXT"),
    ]
    new_cols_closed = [
        ("mfe_pct", "REAL"),
        ("mae_pct", "REAL"),
        ("ml_proba_entry", "REAL"),
        ("strategy", "TEXT"),
        ("extra", "TEXT"),          # P1-2: ML feature vectors + forward-compat
    ]
    existing_open = {r[1] for r in c.execute("PRAGMA table_info(open_trades)").fetchall()}
    for col, typ in new_cols_open:
        if col not in existing_open:
            c.execute(f"ALTER TABLE open_trades ADD COLUMN {col} {typ}")
    existing_closed = {r[1] for r in c.execute("PRAGMA table_info(closed_trades)").fetchall()}
    for col, typ in new_cols_closed:
        if col not in existing_closed:
            c.execute(f"ALTER TABLE closed_trades ADD COLUMN {col} {typ}")
    # Strategy index — created after the column exists.
    c.execute("CREATE INDEX IF NOT EXISTS idx_closed_strategy ON closed_trades(strategy)")
    log.info("schema migrated to v2 (added MFE/MAE/ml_proba/strategy columns)")


def _migrate_v3(c: sqlite3.Connection) -> None:
    """v2 → v3: add missing `extra` column to closed_trades.

    v2's CREATE TABLE and migration both included `extra` in the code, but if
    the DB was created before `extra` was added to those lists, user_version was
    already bumped to 2 — so the migration never re-ran and the column was
    silently missing. Every closed_trade INSERT failed with "no column named
    extra". v3 fixes this by checking the column independently of the version
    gate and adding it if absent.
    """
    existing = {r[1] for r in c.execute("PRAGMA table_info(closed_trades)").fetchall()}
    if "extra" not in existing:
        c.execute("ALTER TABLE closed_trades ADD COLUMN extra TEXT")
        log.info("schema migrated to v3: added extra column to closed_trades")


def _migrate_v4(c: sqlite3.Connection) -> None:
    """v3 → v4: account + execution-session identity.

    Everything that already exists belongs to the paper account, because REAL
    has never been enabled here (MOO_TRADE_ENV has been SIMULATE throughout).
    So this mints one account row for SIMULATE, stamps every existing row with
    it, and marks the history as pre-identity rather than inventing sessions it
    cannot know.

    `migrated_from` is set instead of leaving a bare NULL session: a NULL that
    means "before we tracked this" and a NULL that means "a bug dropped it" look
    identical six months later, and only one of them is acceptable.
    """
    import uuid as _uuid
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    # ── 1. columns on the existing tables ──
    adds = {
        "open_trades":   [("account_id", "TEXT"), ("opened_session_id", "TEXT")],
        "closed_trades": [("account_id", "TEXT"), ("session_id", "TEXT"),
                          ("migrated_from", "TEXT")],
        "audit":         [("account_id", "TEXT"), ("session_id", "TEXT")],
        "history":       [("account_id", "TEXT")],
    }
    for table, cols in adds.items():
        existing = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, typ in cols:
            if col not in existing:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    # ── 2. the account every existing row belongs to ──
    env = (settings.moo_trade_env or "SIMULATE").upper()
    if env not in ("SIMULATE", "REAL"):
        env = "SIMULATE"
    row = c.execute("SELECT account_id FROM accounts WHERE trade_env = ? "
                    "ORDER BY created_at LIMIT 1", (env,)).fetchone()
    if row:
        account_id = row[0]
    else:
        account_id = str(_uuid.uuid4())
        c.execute(
            "INSERT INTO accounts (account_id, trade_env, broker_acc_id, label, "
            "created_at) VALUES (?, ?, NULL, ?, ?)",
            (account_id, env, f"migrated {env} account (schema v4)", now))
        log.info("v4: minted %s account %s", env, account_id)

    # ── 3. stamp the existing rows ──
    for table in ("open_trades", "closed_trades", "audit", "history"):
        c.execute(f"UPDATE {table} SET account_id = ? WHERE account_id IS NULL",
                  (account_id,))
    c.execute(
        "UPDATE closed_trades SET migrated_from = ? "
        "WHERE session_id IS NULL AND migrated_from IS NULL",
        (f"schema-v3 (pre-identity, {now})",))

    # ── 4. kv_state gains an account scope ──
    # SQLite cannot ALTER a PRIMARY KEY, so this is a rebuild. Existing rows land
    # in the global scope first, then the account-scoped ones are moved across —
    # keeping it a single UPDATE means a key can never briefly exist in both.
    cols = {r[1] for r in c.execute("PRAGMA table_info(kv_state)").fetchall()}
    if "account_id" not in cols:
        c.execute("""
            CREATE TABLE kv_state_v4 (
                account_id TEXT NOT NULL DEFAULT '',
                key        TEXT NOT NULL,
                value      TEXT NOT NULL,
                PRIMARY KEY (account_id, key)
            )""")
        c.execute("INSERT INTO kv_state_v4 (account_id, key, value) "
                  "SELECT '', key, value FROM kv_state")
        c.execute("DROP TABLE kv_state")
        c.execute("ALTER TABLE kv_state_v4 RENAME TO kv_state")
        placeholders = ",".join("?" for _ in ACCOUNT_SCOPED_KEYS)
        moved = c.execute(
            f"UPDATE kv_state SET account_id = ? WHERE key IN ({placeholders})",
            (account_id, *sorted(ACCOUNT_SCOPED_KEYS))).rowcount
        log.info("v4: kv_state rebuilt — %d key(s) moved to account %s",
                 moved, account_id)

    # Indexes on the v4 columns — created here, not in SCHEMA, because SCHEMA
    # runs before this migration and the columns do not exist yet.
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_open_account ON open_trades(account_id)",
        "CREATE INDEX IF NOT EXISTS idx_closed_account ON closed_trades(account_id)",
        "CREATE INDEX IF NOT EXISTS idx_closed_session ON closed_trades(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_audit_account ON audit(account_id)",
        "CREATE INDEX IF NOT EXISTS idx_history_account ON history(account_id)",
    ):
        c.execute(stmt)

    log.info("schema migrated to v4 (account/session identity, env=%s)", env)


# ---------- one-time JSON → SQLite migration ----------

def _migrate_from_json(c: sqlite3.Connection) -> None:
    """Read pre-existing JSON files and load them into the freshly-created tables.
    Idempotent because tables were just created empty."""
    data_dir = settings.root / "data"

    # open_trades.json
    f = data_dir / "open_trades.json"
    if f.exists():
        try:
            for sym, t in json.loads(f.read_text()).items():
                c.execute("""
                    INSERT OR REPLACE INTO open_trades
                    (symbol, qty, entry_price, stop_loss, take_profit, atr,
                     half_closed, buy_order_id, stop_order_id, tp_order_id,
                     opened_at, extra)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    sym, int(t.get("qty", 0)),
                    float(t.get("entry_price", 0)),
                    float(t.get("stop_loss", 0)),
                    float(t.get("take_profit", 0)),
                    float(t.get("atr") or 0),
                    1 if t.get("half_closed") else 0,
                    t.get("buy_order_id"),
                    t.get("stop_order_id"),
                    t.get("tp_order_id"),
                    t.get("opened_at") or datetime.utcnow().isoformat(),
                    None,
                ))
            log.info("migrated open_trades.json")
        except Exception as e:
            log.warning("open_trades migration failed: %s", e)

    # state.json
    f = data_dir / "state.json"
    if f.exists():
        try:
            for k, v in json.loads(f.read_text()).items():
                c.execute(
                    "INSERT OR REPLACE INTO kv_state (key, value) VALUES (?, ?)",
                    (k, json.dumps(v)),
                )
            log.info("migrated state.json")
        except Exception as e:
            log.warning("state migration failed: %s", e)

    # audit.jsonl
    f = data_dir / "audit.jsonl"
    if f.exists():
        try:
            n = 0
            for line in f.read_text().strip().split("\n"):
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                extras = {k: r[k] for k in r if k not in
                          {"ts", "action", "symbol", "gate", "reason", "score"}}
                c.execute("""
                    INSERT INTO audit (ts, action, symbol, gate, reason, score, extra)
                    VALUES (?,?,?,?,?,?,?)
                """, (
                    r.get("ts", ""), r.get("action", ""), r.get("symbol"),
                    r.get("gate"), r.get("reason"), r.get("score"),
                    json.dumps(extras) if extras else None,
                ))
                n += 1
            log.info("migrated audit.jsonl (%d rows)", n)
        except Exception as e:
            log.warning("audit migration failed: %s", e)

    # trades.jsonl (closed trades with R-multiple)
    f = data_dir / "trades.jsonl"
    if f.exists():
        try:
            n = 0
            for line in f.read_text().strip().split("\n"):
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                c.execute("""
                    INSERT INTO closed_trades
                    (ts, symbol, qty, entry, stop, exit, exit_reason,
                     pnl, pnl_pct, r_multiple, opened_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    r.get("ts", ""), r.get("symbol", ""),
                    r.get("qty", 0),
                    r.get("entry", 0), r.get("stop", 0), r.get("exit", 0),
                    r.get("exit_reason", ""),
                    r.get("pnl", 0), r.get("pnl_pct", 0),
                    r.get("r_multiple", 0),
                    r.get("opened_at", ""),
                ))
                n += 1
            log.info("migrated trades.jsonl (%d rows)", n)
        except Exception as e:
            log.warning("trades migration failed: %s", e)

    # history.jsonl (per-scan equity snapshots)
    f = data_dir / "history.jsonl"
    if f.exists():
        try:
            n = 0
            for line in f.read_text().strip().split("\n"):
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                c.execute("""
                    INSERT INTO history
                    (ts, week, day, invested, budget, unrealized_pnl,
                     realized_pnl_total, total_pnl, positions_count,
                     symbols, timeframe)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    r.get("ts", ""), r.get("week", ""), r.get("day", ""),
                    r.get("invested", 0), r.get("budget", 0),
                    r.get("unrealized_pnl", 0),
                    r.get("realized_pnl_total", 0),
                    r.get("total_pnl", 0),
                    r.get("positions_count", 0),
                    json.dumps(r.get("symbols", [])),
                    r.get("timeframe", ""),
                ))
                n += 1
            log.info("migrated history.jsonl (%d rows)", n)
        except Exception as e:
            log.warning("history migration failed: %s", e)


# ---------- open_trades operations ----------

def load_open_trades() -> dict[str, dict]:
    """Return the same dict shape we used to read from open_trades.json."""
    with conn() as c:
        rows = c.execute("SELECT * FROM open_trades").fetchall()
    return {r["symbol"]: _row_to_trade_dict(r) for r in rows}


def get_open_trade(symbol: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM open_trades WHERE symbol = ?", (symbol,)).fetchone()
    return _row_to_trade_dict(r) if r else None


_OPEN_TRADE_COLUMNS = {
    "symbol", "qty", "entry_price", "stop_loss", "take_profit", "atr",
    "half_closed", "buy_order_id", "stop_order_id", "tp_order_id",
    "opened_at", "high_water", "low_water", "ml_proba_entry", "strategy",
    "extra",
}


def upsert_open_trade(trade: dict) -> None:
    # Auto-collect any non-column keys (e.g. `stacks`, `last_stack_at`)
    # into the `extra` JSON blob so forward-compat fields survive round-trips.
    extra = dict(trade.get("extra") or {}) if isinstance(trade.get("extra"), dict) else {}
    for k, v in trade.items():
        if k not in _OPEN_TRADE_COLUMNS:
            extra[k] = v
    with transaction() as c:
        c.execute("""
            INSERT INTO open_trades
            (symbol, qty, entry_price, stop_loss, take_profit, atr,
             half_closed, buy_order_id, stop_order_id, tp_order_id,
             opened_at, high_water, low_water, ml_proba_entry, strategy, extra)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol) DO UPDATE SET
              qty=excluded.qty, entry_price=excluded.entry_price,
              stop_loss=excluded.stop_loss, take_profit=excluded.take_profit,
              atr=excluded.atr, half_closed=excluded.half_closed,
              buy_order_id=excluded.buy_order_id,
              stop_order_id=excluded.stop_order_id,
              tp_order_id=excluded.tp_order_id,
              opened_at=excluded.opened_at,
              high_water=excluded.high_water, low_water=excluded.low_water,
              ml_proba_entry=excluded.ml_proba_entry, strategy=excluded.strategy,
              extra=excluded.extra
        """, (
            trade["symbol"], int(trade["qty"]),
            float(trade["entry_price"]),
            float(trade["stop_loss"]),
            float(trade["take_profit"]),
            float(trade.get("atr") or 0),
            1 if trade.get("half_closed") else 0,
            trade.get("buy_order_id"),
            trade.get("stop_order_id"),
            trade.get("tp_order_id"),
            trade.get("opened_at") or datetime.utcnow().isoformat(),
            trade.get("high_water"),
            trade.get("low_water"),
            trade.get("ml_proba_entry"),
            trade.get("strategy") or "trend",
            json.dumps(extra, default=str) if extra else None,
        ))


def delete_open_trade(symbol: str) -> None:
    with transaction() as c:
        c.execute("DELETE FROM open_trades WHERE symbol = ?", (symbol,))


def _row_to_trade_dict(r: sqlite3.Row) -> dict:
    keys = r.keys()
    out = {
        "symbol": r["symbol"],
        "qty": int(r["qty"]),
        "entry_price": float(r["entry_price"]),
        "stop_loss": float(r["stop_loss"]),
        "take_profit": float(r["take_profit"]),
        "atr": float(r["atr"] or 0),
        "half_closed": bool(r["half_closed"]),
        "buy_order_id": r["buy_order_id"],
        "stop_order_id": r["stop_order_id"],
        "tp_order_id": r["tp_order_id"],
        "opened_at": r["opened_at"],
    }
    # v2 columns (may not exist on freshly-migrated old data)
    for k in ("high_water", "low_water", "ml_proba_entry", "strategy"):
        if k in keys:
            out[k] = r[k]
    # Merge `extra` JSON (used for forward-compat fields like `stacks`,
    # `entries`, etc.) so the dict matches what callers stored.
    if "extra" in keys and r["extra"]:
        try:
            extra = json.loads(r["extra"])
            if isinstance(extra, dict):
                for k, v in extra.items():
                    out.setdefault(k, v)
        except (json.JSONDecodeError, TypeError):
            pass
    return out


# ---------- kv_state operations ----------
#
# Scope routing is automatic and lives here on purpose. Callers pass a flat dict
# exactly as before; each key is filed under the global scope or the active
# account according to ACCOUNT_SCOPED_KEYS. Making every one of the ~40 call
# sites choose correctly would only mean the isolation holds until the first
# person forgets — and "forgot to scope realized_pnl_total" is a live account
# inheriting the paper account's -$625 drawdown.

def _active_account_id() -> str:
    """Account whose state this process reads/writes, or '' before one exists.

    Deliberately tolerant: during _ensure_initialised (and in tests that fake
    out db) there may be no account row yet, and state access must not explode.
    """
    try:
        from . import identity
        return identity.active_account_id() or GLOBAL_SCOPE
    except Exception:
        return GLOBAL_SCOPE


def _scope_for(key: str, account_id: str) -> str:
    return account_id if key in ACCOUNT_SCOPED_KEYS else GLOBAL_SCOPE


def _read_scoped(c, account_id: str) -> dict:
    """Global rows overlaid with this account's rows."""
    out = {}
    for scope in (GLOBAL_SCOPE, account_id) if account_id else (GLOBAL_SCOPE,):
        for r in c.execute("SELECT key, value FROM kv_state WHERE account_id = ?",
                           (scope,)).fetchall():
            try:
                out[r["key"]] = json.loads(r["value"])
            except json.JSONDecodeError:
                out[r["key"]] = r["value"]
    return out


def _write_scoped(c, updates: dict, account_id: str) -> None:
    for k, v in updates.items():
        c.execute(
            "INSERT OR REPLACE INTO kv_state (account_id, key, value) "
            "VALUES (?, ?, ?)",
            (_scope_for(k, account_id), k, json.dumps(v, default=str)),
        )


def get_state() -> dict:
    """Global state overlaid with the active account's — flat, as before."""
    acct = _active_account_id()
    with conn() as c:
        return _read_scoped(c, acct)


def save_state(state: dict) -> None:
    """Replace kv_state entirely — atomic via transaction."""
    acct = _active_account_id()
    with transaction() as c:
        _write_scoped(c, state, acct)


def update_state(updates: dict) -> dict:
    """Atomically merge `updates` into kv_state. For dependent updates
    (next = f(current)), use `atomic_state(fn)` instead — this method's read
    happens before the lock and is unsafe under concurrency.

    Also mirrors to state.json so the GUI/legacy consumers stay in sync
    after runtime_config.set_param() writes directly to DB (2026-07-06 fix:
    state.json was drifting stale because only risk_manager._save_state()
    updated it — and that only fires on trade-close/day-reset)."""
    acct = _active_account_id()
    with transaction() as c:
        _write_scoped(c, updates, acct)
        out = _read_scoped(c, acct)
    # ── Mirror to legacy state.json (2026-07-06) ──
    _mirror_state_json(out)
    return out


def atomic_state(fn) -> dict:
    """Race-safe read-modify-write of kv_state.

    `fn(current_state: dict) -> dict_of_updates`  is called *inside* the
    transaction with the latest committed state.  Whatever dict it returns
    is written back atomically.  Returns the full post-write state."""
    acct = _active_account_id()
    with transaction() as c:
        current = _read_scoped(c, acct)
        updates = fn(current) or {}
        _write_scoped(c, updates, acct)
        out = _read_scoped(c, acct)
    # ── Mirror to legacy state.json (2026-07-06) ──
    _mirror_state_json(out)
    return out


# ── state.json mirror (2026-07-06) ──────────────────────

_STATE_JSON = settings.root / "data" / "state.json"


def _mirror_state_json(state: dict) -> None:
    """Write current kv_state to the legacy state.json file so GUI and
    external scripts always see the latest state — even after
    runtime_config.set_param() writes directly to DB without going
    through risk_manager._save_state().

    Atomic (tmp + os.replace): the scheduler and the web server both come
    through here from separate processes — a plain write_text truncates
    first, so a concurrent reader could catch a half-written file."""
    try:
        import os as _os
        _STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_JSON.with_suffix(f".tmp.{_os.getpid()}")
        tmp.write_text(json.dumps(state, indent=2, default=str))
        _os.replace(tmp, _STATE_JSON)
    except Exception as e:
        log.warning("state.json mirror failed: %s", e)


# ---------- audit ----------

def audit_insert(action: str, symbol: str = "", gate: str = "", reason: str = "",
                 score: float = 0.0, extra: dict | None = None,
                 ts: str | None = None) -> None:
    with conn() as c:
        c.execute("""
            INSERT INTO audit (ts, action, symbol, gate, reason, score, extra)
            VALUES (?,?,?,?,?,?,?)
        """, (
            ts or datetime.utcnow().isoformat(),
            action, symbol, gate, reason, score,
            json.dumps(extra, default=str) if extra else None,
        ))


def audit_recent(limit: int = 100, action: str | None = None) -> list[dict]:
    q = "SELECT * FROM audit"
    args: tuple = ()
    if action:
        q += " WHERE action = ?"
        args = (action,)
    q += " ORDER BY id DESC LIMIT ?"
    args = args + (limit,)
    with conn() as c:
        rows = c.execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("extra"):
            try:
                d.update(json.loads(d["extra"]))
            except json.JSONDecodeError:
                pass
        d.pop("extra", None)
        out.append(d)
    return list(reversed(out))


def audit_gate_summary(limit: int = 200) -> dict[str, int]:
    with conn() as c:
        rows = c.execute("""
            SELECT gate, COUNT(*) AS n FROM (
              SELECT gate FROM audit WHERE action='skip' ORDER BY id DESC LIMIT ?
            ) GROUP BY gate ORDER BY n DESC
        """, (limit,)).fetchall()
    return {r["gate"] or "?": r["n"] for r in rows}


# ---------- closed_trades ----------

def closed_trade_insert(row: dict) -> None:
    with conn() as c:
        c.execute("""
            INSERT INTO closed_trades
            (ts, symbol, qty, entry, stop, exit, exit_reason,
             pnl, pnl_pct, r_multiple, opened_at,
             mfe_pct, mae_pct, ml_proba_entry, strategy, extra)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            row.get("ts") or datetime.utcnow().isoformat(),
            row["symbol"], int(row["qty"]),
            row["entry"], row["stop"], row["exit"],
            row.get("exit_reason", ""),
            row.get("pnl", 0), row.get("pnl_pct", 0),
            row.get("r_multiple", 0),
            row.get("opened_at", ""),
            row.get("mfe_pct"),
            row.get("mae_pct"),
            row.get("ml_proba_entry"),
            row.get("strategy"),
            row.get("extra"),
        ))


def closed_trades(limit: int = 200) -> list[dict]:
    """MOST RECENT `limit` closed trades, in chronological (oldest→newest) order.

    P1 fix 2026-07-07: was `ORDER BY id ASC LIMIT ?` — the OLDEST N rows.
    Every caller (trade_stats(50), autopilot rollback window, AI param check,
    web /api/closed) treats this as "recent trades", so once the table grew
    past `limit` the stats/autopilot would have been frozen on the earliest
    trades forever. Inner DESC picks the newest N; outer ASC restores the
    chronological order callers iterate in."""
    with conn() as c:
        rows = c.execute(
            """
            SELECT * FROM (
                SELECT * FROM closed_trades ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def last_sl_close_for_symbol(symbol: str) -> dict | None:
    """Return the most recent SL-class close (SL or SL_BRACKET) for `symbol`.

    Used by `risk_manager.in_sl_cooldown` to refuse re-entry on a name that
    just stopped out — the 142-day audit found 29 rebleed losses worth -$425
    when the bot bought the same ticker the very next bar after a stop.

    2026-07-18: losing BREAKEVEN closes count too. A "breakeven" exit that
    booked a loss IS a stop-out (fill slipped below the raised stop — 07-15
    HPE re-entered 9 min after a -1.36R "BREAKEVEN"); executor now relabels
    the bad ones SL at write time, this clause covers legacy rows and small
    scratch losses.
    """
    with conn() as c:
        row = c.execute(
            """
            SELECT * FROM closed_trades
            WHERE symbol = ?
              AND (exit_reason IN ('SL', 'SL_BRACKET')
                   OR (exit_reason = 'BREAKEVEN' AND pnl < 0))
            ORDER BY id DESC LIMIT 1
            """,
            (symbol,),
        ).fetchone()
    return dict(row) if row else None


# ---------- history ----------

def history_insert(row: dict) -> None:
    with conn() as c:
        c.execute("""
            INSERT INTO history
            (ts, week, day, invested, budget, unrealized_pnl,
             realized_pnl_total, total_pnl, positions_count, symbols, timeframe)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            row.get("ts") or datetime.utcnow().isoformat(),
            row.get("week", ""), row.get("day", ""),
            row.get("invested", 0), row.get("budget", 0),
            row.get("unrealized_pnl", 0),
            row.get("realized_pnl_total", 0),
            row.get("total_pnl", 0),
            row.get("positions_count", 0),
            json.dumps(row.get("symbols", [])),
            row.get("timeframe", ""),
        ))


def history_rows(limit: int = 500) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["symbols"] = json.loads(d.get("symbols", "[]"))
        except (json.JSONDecodeError, TypeError):
            d["symbols"] = []
        out.append(d)
    return list(reversed(out))
