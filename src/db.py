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
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)

DB_FILE = settings.root / "data" / "trader.db"
SCHEMA_VERSION = 5          # v5: open_trades keyed by (account_id, symbol)

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
    # Strategy sleeves are account-scoped rather than global on purpose. They
    # read like software settings, but "I switched this on to see what it does"
    # is a thing done on paper, and inheriting that decision into a live account
    # is the surprise worth not having.
    "cash_yield_enabled", "inverse_sleeve_enabled",
})
# Keys that genuinely belong to the software rather than to an account, listed
# explicitly so that "not account-scoped" is a decision someone made rather than
# the result of a lookup missing.
GLOBAL_SCOPED_KEYS = frozenset({
    "ai_provider", "ai_model", "ai_fail_streak", "ai_last_error", "ai_last_ok_ts",
    "health_ai_calls_ok", "health_ai_calls_ok_streak",
    "health_gemini_ok", "health_gemini_ok_streak",
    "health_options_stats_ok", "health_options_stats_ok_streak",
    "regime_last_label", "telegram_offset",
    # One-time migration markers: facts about this installation's history.
    "baseline_established_at", "ledger_merge", "ledger_quality_migration",
    "phase0a_applied", "poststop_reconcile",
})

# Prefixes that are global by nature. Strategy parameters describe the software's
# behaviour, not one account's money.
_GLOBAL_KEY_PREFIXES = ("param_", "health_", "cron_")

GLOBAL_SCOPE = ""
_init_lock = threading.Lock()
_initialised = False

# Set when init found a database older than this build and was not permitted to
# upgrade it. While true, nothing may change the file FORMAT — see conn().
_schema_frozen = False


# Defined once, used twice: SCHEMA splices it in below, and _migrate_v5 rebuilds
# the table from this exact text. A rebuild that restates the columns is a
# rebuild that will disagree with SCHEMA one release later — this fixture drifted
# twice already, and a table that is nearly right fails somewhere far away from
# the definition that was wrong.
#
# The primary key is (account_id, symbol), not symbol.
#
# v4 gave this table an account_id but left symbol as the sole key, which made
# the column decorative: SIMULATE and REAL cannot both hold AAPL, and the second
# one to write does not fail — ON CONFLICT(symbol) UPDATEs the other account's
# row in place, quantity, stops and all, and stamps its own account_id over the
# top. Adding an account filter to the reads on top of that would have been
# worse than no filter at all, because the row the filter hides is the row that
# was just silently overwritten.
OPEN_TRADES_DDL = """\
CREATE TABLE IF NOT EXISTS open_trades (
    symbol          TEXT NOT NULL,
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
    -- A position outlives the process that opened it, so the session here is
    -- the one that OPENED it and is never required — a restart must not orphan
    -- or duplicate a live position. The account is required: a position always
    -- belongs to exactly one account.
    account_id         TEXT NOT NULL,
    opened_session_id  TEXT,
    PRIMARY KEY (account_id, symbol)
)"""

# The column list, in table order — used by the v5 rebuild to copy rows across.
OPEN_TRADES_COLUMNS = (
    "symbol", "qty", "entry_price", "stop_loss", "take_profit", "atr",
    "half_closed", "buy_order_id", "stop_order_id", "tp_order_id", "opened_at",
    "high_water", "low_water", "ml_proba_entry", "strategy", "extra",
    "account_id", "opened_session_id",
)

SCHEMA = f"""
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

-- open_trades is defined at OPEN_TRADES_DDL above, beside the column list the
-- v5 rebuild copies with. Spliced rather than restated so the two cannot drift.
{OPEN_TRADES_DDL};

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
    # journal_mode is persistent, so it is a format change, not a session
    # setting. On a database this build was told not to upgrade, forcing WAL
    # rewrites the header and leaves -wal/-shm behind — on every read.
    if not _schema_frozen:
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
    if not _schema_frozen:      # see conn()
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


def _upgrade_allowed() -> bool:
    """Is this process permitted to change a database's schema?

    Off by default. A schema upgrade is a decision about a file another program
    may depend on — the packaged app runs a frozen backend that predates v4 and
    reads its kv_state as duplicate keys — and it must never be a side effect of
    reading state.
    """
    return os.getenv("MMT_ALLOW_SCHEMA_UPGRADE", "").strip().lower() in (
        "1", "true", "yes")


def _ensure_initialised() -> None:
    """Run schema + migration exactly once per process."""
    global _initialised, _schema_frozen
    if _initialised:
        return
    with _init_lock:
        if _initialised:
            return
        DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(DB_FILE), timeout=10)
        try:
            # Read the version BEFORE touching journal_mode. Setting WAL is a
            # persistent change: on a rollback-journal database it rewrites the
            # header and leaves -wal/-shm sidecars behind. Doing that during an
            # open we are about to refuse means "no schema changes applied" was
            # false — the file's hash and format both moved.
            user_v = c.execute("PRAGMA user_version").fetchone()[0]
            existing = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'")}
            if existing and user_v < SCHEMA_VERSION and not _upgrade_allowed():
                log.warning(
                    "DB at %s is schema v%d; v%d is available but NOT applied "
                    "— set MMT_ALLOW_SCHEMA_UPGRADE=1 to upgrade deliberately. "
                    "Continuing on v%d, no schema or journal changes made.",
                    DB_FILE, user_v, SCHEMA_VERSION, user_v)
                _schema_frozen = True
                _initialised = True
                return
            c.execute("PRAGMA journal_mode=WAL")

            # The version decision happens BEFORE any schema statement runs.
            #
            # It used to run executescript(SCHEMA) first and decide afterwards.
            # SCHEMA carries the v4 tables, so refusing to upgrade a v3 database
            # still created `accounts` and `execution_sessions` in it: a database
            # left reporting user_version=3 while holding half of v4. The refusal
            # logged correctly and was, on disk, not a refusal.
            if not existing:
                # Fresh database — nothing to preserve, so build it current.
                c.executescript(SCHEMA)
                _migrate_from_json(c)
                c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                log.info("SQLite created at v%d: %s", SCHEMA_VERSION, DB_FILE)

            elif user_v > SCHEMA_VERSION:
                # Written by a newer build. Touching it could destroy structure
                # this code does not know about.
                log.warning("DB at %s is schema v%d but this build knows v%d — "
                            "opening read-only-ish, no schema changes applied.",
                            DB_FILE, user_v, SCHEMA_VERSION)
                _initialised = True
                return

            elif user_v < SCHEMA_VERSION:
                # The refusal already returned above, before journal_mode was
                # touched. Reaching here means the upgrade was permitted.
                #
                # All of it in ONE transaction, explicitly. Python's sqlite3
                # does not begin one for DDL, and executescript() COMMITs before
                # it runs — so the default behaviour is that each migration step
                # lands on disk as it completes. A crash partway then leaves a
                # database that is half of two versions, still reporting the old
                # one, and the next run resumes from a state no migration was
                # written to expect. Either the whole upgrade happened or none
                # of it did; there is no useful state in between.
                c.executescript(SCHEMA)      # idempotent, outside the rebuild
                c.execute("BEGIN IMMEDIATE")
                try:
                    if user_v == 0:
                        _migrate_from_json(c)
                    if user_v < 2:
                        _migrate_v2(c)
                    if user_v < 3:
                        _migrate_v3(c)
                    if user_v < 4:
                        _migrate_v4(c)
                    if user_v < 5:
                        _migrate_v5(c)
                    c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                except BaseException:
                    c.execute("ROLLBACK")
                    log.error("schema upgrade v%d -> v%d failed and was rolled "
                              "back; the database is untouched at v%d",
                              user_v, SCHEMA_VERSION, user_v)
                    raise
                c.execute("COMMIT")
                log.info("SQLite upgraded v%d -> v%d: %s",
                         user_v, SCHEMA_VERSION, DB_FILE)

            else:
                # Already current. SCHEMA is idempotent here and heals an index
                # dropped by hand; it adds nothing this version does not define.
                c.executescript(SCHEMA)
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


def _migrate_v5(c: sqlite3.Connection) -> None:
    """v4 → v5: open_trades keyed by (account_id, symbol).

    v4 added the column but left `symbol TEXT PRIMARY KEY`, so the isolation it
    describes did not exist. Two accounts holding the same ticker was not a
    conflict the database rejected — upsert_open_trade's ON CONFLICT(symbol)
    quietly rewrote the other account's position and relabelled it.

    SQLite cannot ALTER a primary key, so this is a table rebuild. Two things
    are worth stating about it:

    - Any row still carrying a NULL account_id is adopted by the local account
      rather than dropped. A NULL here can only come from code that wrote
      between the v4 migration and this one; the position is real either way,
      and losing a live position to a migration is not a trade-off worth making.
    - If the same symbol somehow exists twice under one account, the rebuild
      would fail on the new key. That is checked FIRST and raised, so the
      migration refuses rather than letting INSERT OR REPLACE silently pick a
      winner between two real positions.
    """
    info = c.execute("PRAGMA table_info(open_trades)").fetchall()
    if not info:
        return
    cols = [r[1] for r in info]
    pk_cols = {r[1] for r in info if r[5]}
    scratch_left = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND "
        "name='open_trades_v4'").fetchone()

    # "Already done" means the new key AND no scratch table. Checking the key
    # alone is what made an interrupted rebuild permanent: a crash after the
    # new table was created but before the rows were copied leaves a correctly
    # keyed, EMPTY open_trades next to a full open_trades_v4 — and a guard that
    # only looks at the key returns early, so the positions stay hidden in the
    # scratch table forever while the bot reports holding nothing.
    if pk_cols == {"account_id", "symbol"} and not scratch_left:
        return

    if scratch_left:
        log.warning("v5: found open_trades_v4 from an interrupted rebuild — "
                    "restoring from it and starting over")
        c.execute("DROP TABLE IF EXISTS open_trades")
        c.execute("ALTER TABLE open_trades_v4 RENAME TO open_trades")
        info = c.execute("PRAGMA table_info(open_trades)").fetchall()
        cols = [r[1] for r in info]

    account_id = _ensure_local_account(c)
    c.execute("UPDATE open_trades SET account_id = ? WHERE account_id IS NULL "
              "OR account_id = ''", (account_id,))

    dupes = c.execute(
        "SELECT account_id, symbol, COUNT(*) n FROM open_trades "
        "GROUP BY account_id, symbol HAVING n > 1").fetchall()
    if dupes:
        raise RuntimeError(
            "cannot key open_trades by (account_id, symbol): duplicate "
            f"positions exist — {[(d[1], d[2]) for d in dupes]}. Resolve them "
            "by hand; picking a winner automatically would discard a real "
            "position.")

    before = c.execute("SELECT COUNT(*) FROM open_trades").fetchone()[0]
    carried = [x for x in OPEN_TRADES_COLUMNS if x in cols]
    collist = ", ".join(carried)

    # No executescript() in here. It issues an implicit COMMIT before running,
    # which would end the transaction in the middle of the rebuild — verified:
    # rename, executescript, then die, and the file is left with an empty new
    # open_trades and a renamed old one, committed. The whole point of v5 is
    # that the software knows which positions it holds; a migration that can
    # lose them is worse than the defect it fixes.
    c.execute("ALTER TABLE open_trades RENAME TO open_trades_v4")
    c.execute(OPEN_TRADES_DDL)
    c.execute(f"INSERT INTO open_trades ({collist}) "
              f"SELECT {collist} FROM open_trades_v4")
    moved = c.execute("SELECT COUNT(*) FROM open_trades").fetchone()[0]
    if moved != before:
        raise RuntimeError(
            f"v5 rebuild would lose positions: {before} before, {moved} after. "
            f"Refusing; the transaction is rolled back.")
    c.execute("DROP TABLE open_trades_v4")
    c.execute("CREATE INDEX IF NOT EXISTS idx_open_account "
              "ON open_trades(account_id)")
    log.info("schema migrated to v5: open_trades rebuilt on (account_id, "
             "symbol), %d position(s) carried over", moved)


# ---------- one-time JSON → SQLite migration ----------

def _ensure_local_account(c: sqlite3.Connection) -> str:
    """The account everything on this install belongs to, created once.

    Shared by the fresh-database path and the v3->v4 migration so both attribute
    rows the same way. Uses the configured trade_env: whatever this install has
    been trading is what its existing records are.
    """
    import uuid as _uuid
    from datetime import timezone as _tz
    env = (settings.moo_trade_env or "SIMULATE").upper()
    if env not in ("SIMULATE", "REAL"):
        env = "SIMULATE"
    row = c.execute("SELECT account_id FROM accounts WHERE trade_env = ? "
                    "ORDER BY created_at LIMIT 1", (env,)).fetchone()
    if row:
        return row[0]
    account_id = str(_uuid.uuid4())
    c.execute("INSERT INTO accounts (account_id, trade_env, broker_acc_id, label, "
              "created_at) VALUES (?, ?, NULL, ?, ?)",
              (account_id, env, f"{env} account",
               datetime.now(_tz.utc).isoformat()))
    log.info("minted %s account %s", env, account_id)
    return account_id


def _migrate_from_json(c: sqlite3.Connection) -> None:
    """Read pre-existing JSON files and load them into the freshly-created tables.
    Idempotent because tables were just created empty.

    Everything imported is attributed to this install's account. Legacy JSON has
    no notion of accounts, and dropping its keys into the GLOBAL scope would put
    a paper budget, a paper realized PnL and a paper drawdown peak where EVERY
    account reads them — so switching to REAL would inherit the paper account's
    losses as its own risk state.
    """
    data_dir = settings.root / "data"
    account_id = _ensure_local_account(c)
    scoped = any(r[1] == "account_id"
                 for r in c.execute("PRAGMA table_info(kv_state)"))

    # open_trades.json
    f = data_dir / "open_trades.json"
    if f.exists():
        try:
            for sym, t in json.loads(f.read_text()).items():
                c.execute("""
                    INSERT OR REPLACE INTO open_trades
                    (symbol, qty, entry_price, stop_loss, take_profit, atr,
                     half_closed, buy_order_id, stop_order_id, tp_order_id,
                     opened_at, extra, account_id)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    account_id,
                ))
            log.info("migrated open_trades.json")
        except Exception as e:
            log.warning("open_trades migration failed: %s", e)

    # state.json
    f = data_dir / "state.json"
    if f.exists():
        try:
            for k, v in json.loads(f.read_text()).items():
                if scoped:
                    c.execute(
                        "INSERT OR REPLACE INTO kv_state (account_id, key, value) "
                        "VALUES (?, ?, ?)",
                        (account_id if k in ACCOUNT_SCOPED_KEYS else GLOBAL_SCOPE,
                         k, json.dumps(v, default=str)))
                    continue
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
                    INSERT INTO audit (ts, action, symbol, gate, reason, score,
                                       extra, account_id)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (
                    r.get("ts", ""), r.get("action", ""), r.get("symbol"),
                    r.get("gate"), r.get("reason"), r.get("score"),
                    json.dumps(extras) if extras else None,
                    account_id,
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
                     pnl, pnl_pct, r_multiple, opened_at, account_id,
                     migrated_from)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    r.get("ts", ""), r.get("symbol", ""),
                    r.get("qty", 0),
                    r.get("entry", 0), r.get("stop", 0), r.get("exit", 0),
                    r.get("exit_reason", ""),
                    r.get("pnl", 0), r.get("pnl_pct", 0),
                    r.get("r_multiple", 0),
                    r.get("opened_at", ""),
                    account_id,
                    "legacy trades.jsonl (pre-identity)",
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
                     symbols, timeframe, account_id)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    r.get("ts", ""), r.get("week", ""), r.get("day", ""),
                    r.get("invested", 0), r.get("budget", 0),
                    r.get("unrealized_pnl", 0),
                    r.get("realized_pnl_total", 0),
                    r.get("total_pnl", 0),
                    r.get("positions_count", 0),
                    json.dumps(r.get("symbols", [])),
                    r.get("timeframe", ""),
                    account_id,
                ))
                n += 1
            log.info("migrated history.jsonl (%d rows)", n)
        except Exception as e:
            log.warning("history migration failed: %s", e)


# ---------- open_trades operations ----------

def load_open_trades() -> dict[str, dict]:
    """This account's open positions, in the dict shape open_trades.json had."""
    acc = _require_account_id("load_open_trades")
    with conn() as c:
        rows = c.execute("SELECT * FROM open_trades WHERE account_id = ?",
                         (acc,)).fetchall()
    return {r["symbol"]: _row_to_trade_dict(r) for r in rows}


def get_open_trade(symbol: str) -> dict | None:
    acc = _require_account_id("get_open_trade")
    with conn() as c:
        r = c.execute("SELECT * FROM open_trades WHERE account_id = ? AND "
                      "symbol = ?", (acc, symbol)).fetchone()
    return _row_to_trade_dict(r) if r else None


_OPEN_TRADE_COLUMNS = {
    "symbol", "qty", "entry_price", "stop_loss", "take_profit", "atr",
    "half_closed", "buy_order_id", "stop_order_id", "tp_order_id",
    "opened_at", "high_water", "low_water", "ml_proba_entry", "strategy",
    "extra",
    # Listed so that a caller round-tripping a row does not get them swept into
    # the `extra` blob as a stale second copy. Identity is stamped from the
    # session below, never taken from the caller's dict.
    "account_id", "opened_session_id",
}


def upsert_open_trade(trade: dict) -> None:
    # Auto-collect any non-column keys (e.g. `stacks`, `last_stack_at`)
    # into the `extra` JSON blob so forward-compat fields survive round-trips.
    extra = dict(trade.get("extra") or {}) if isinstance(trade.get("extra"), dict) else {}
    for k, v in trade.items():
        if k not in _OPEN_TRADE_COLUMNS:
            extra[k] = v
    acc = _require_account_id("upsert_open_trade")
    # The session that OPENED the position, recorded once. It is deliberately
    # absent from the UPDATE clause below: a position outlives the run that
    # opened it, and letting each restart restamp it would turn "which run
    # opened this?" into "which run last touched this?" — the second question is
    # already answerable from the audit trail, and the first would be gone.
    sid = None
    try:
        from . import identity
        sid = identity.current_session_id()
    except Exception:
        pass
    with transaction() as c:
        c.execute("""
            INSERT INTO open_trades
            (symbol, qty, entry_price, stop_loss, take_profit, atr,
             half_closed, buy_order_id, stop_order_id, tp_order_id,
             opened_at, high_water, low_water, ml_proba_entry, strategy, extra,
             account_id, opened_session_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account_id, symbol) DO UPDATE SET
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
            acc, sid,
        ))


def delete_open_trade(symbol: str) -> None:
    acc = _require_account_id("delete_open_trade")
    with transaction() as c:
        c.execute("DELETE FROM open_trades WHERE account_id = ? AND symbol = ?",
                  (acc, symbol))


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


def _require_account_id(what: str) -> str:
    """The account for a trade-data read or write, or raise.

    Deliberately NOT _active_account_id(), which falls back to the global scope
    so that kv_state keeps working during initialisation. Trade data cannot take
    that fallback, because the fallback's failure mode is silence: a positions
    query scoped to an account that does not exist returns [] rather than an
    error, "no open positions" is a perfectly ordinary answer, and the bot acts
    on it by buying everything it already holds.

    Raising is recoverable — the scan fails, nothing trades, someone reads a log.
    An empty result is not.
    """
    acc = _active_account_id()
    if not acc:
        raise RuntimeError(
            f"cannot resolve the active account for {what} — refusing rather "
            f"than returning a result scoped to nothing. This usually means "
            f"the database has no accounts row for the configured trade_env.")
    return acc


_warned_unclassified: set[str] = set()


def assert_single_account(db_path=None, *, what: str = "this tool") -> str:
    """Refuse if the ledger holds rows for more than one account.

    For the one-shot maintenance tools — the ledger merge, the broker
    reconciliation — which were written when there was one account and query
    whole tables. They are not wrong today, because there is one account; they
    become wrong silently the moment there are two, and the symptom is a
    reconciliation report that mixes paper fills with live ones.

    Cheaper and more honest than retrofitting an account argument onto tools
    that have already served their purpose: they either run against an
    unambiguous database or they stop.
    """
    import sqlite3 as _sq
    path = db_path or DB_FILE
    c = _sq.connect(f"file:{path}?mode=ro", uri=True)
    try:
        found = set()
        for table in ("open_trades", "closed_trades", "audit", "history"):
            try:
                found |= {r[0] for r in c.execute(
                    f"SELECT DISTINCT account_id FROM {table}") if r[0]}
            except _sq.Error:
                continue           # pre-v4 table: no account column at all
    finally:
        c.close()
    if len(found) > 1:
        raise SystemExit(
            f"{what} operates on whole tables and this database holds "
            f"{len(found)} accounts. Refusing: its output would mix them. "
            f"Scope it to one account before running it again.")
    return next(iter(found), "")


def _scope_for(key: str, account_id: str) -> str:
    """Where a key is filed. Unclassified keys go to the ACCOUNT, not global.

    The default used to be global: anything not in ACCOUNT_SCOPED_KEYS was
    software-wide. That is the wrong direction to be wrong in. A new key that
    turns out to be account-sensitive — some future realized-PnL variant, a
    per-account cooldown — would be shared between paper and live silently, and
    the symptom is a live order sized off paper state.

    Defaulting to the account is wrong in the harmless direction: a genuinely
    global key filed per-account is stored twice and read back correctly by
    both. So unknown keys land there, and say so once, loudly enough that the
    classification gets made rather than inherited.
    """
    if key in ACCOUNT_SCOPED_KEYS:
        return account_id
    if key in GLOBAL_SCOPED_KEYS or key.startswith(_GLOBAL_KEY_PREFIXES):
        return GLOBAL_SCOPE
    if key not in _warned_unclassified:
        _warned_unclassified.add(key)
        log.warning(
            "kv_state key %r is in neither ACCOUNT_SCOPED_KEYS nor "
            "GLOBAL_SCOPED_KEYS — filing it under the account, which is the "
            "safe guess. Classify it in db.py.", key)
    return account_id


def _kv_is_scoped(c) -> bool:
    """Does this database's kv_state carry an account scope (v4) or not (v3)?

    Both must work. The authoritative production database is still v3 and must
    stay that way until a rebuilt app can read v4, while this code has to be
    able to read it — for the migration rehearsal, for reconciliation, for any
    inspection. Assuming v4 turned a read into a crash the moment the schema
    guard started doing its job.
    """
    return any(r[1] == "account_id"
               for r in c.execute("PRAGMA table_info(kv_state)"))


def _read_scoped(c, account_id: str) -> dict:
    """Global rows overlaid with this account's rows (v4), or every row (v3)."""
    out = {}
    if not _kv_is_scoped(c):
        for r in c.execute("SELECT key, value FROM kv_state").fetchall():
            try:
                out[r["key"]] = json.loads(r["value"])
            except json.JSONDecodeError:
                out[r["key"]] = r["value"]
        return out
    for scope in (GLOBAL_SCOPE, account_id) if account_id else (GLOBAL_SCOPE,):
        for r in c.execute("SELECT key, value FROM kv_state WHERE account_id = ?",
                           (scope,)).fetchall():
            try:
                out[r["key"]] = json.loads(r["value"])
            except json.JSONDecodeError:
                out[r["key"]] = r["value"]
    return out


def _write_scoped(c, updates: dict, account_id: str) -> None:
    scoped = _kv_is_scoped(c)
    for k, v in updates.items():
        if scoped:
            c.execute(
                "INSERT OR REPLACE INTO kv_state (account_id, key, value) "
                "VALUES (?, ?, ?)",
                (_scope_for(k, account_id), k, json.dumps(v, default=str)),
            )
        else:
            c.execute(
                "INSERT OR REPLACE INTO kv_state (key, value) VALUES (?, ?)",
                (k, json.dumps(v, default=str)),
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


# ── legacy JSON mirrors ─────────────────────────────────

_STATE_JSON = settings.root / "data" / "state.json"
_OPEN_TRADES_JSON = settings.root / "data" / "open_trades.json"


def mirror_open_trades_json(trades: dict) -> None:
    """Write data/open_trades.json for this account, stamped.

    One file, two accounts. Both executor and reconcile wrote it directly with
    a bare position dict, so a REAL run replaced the paper account's mirror and
    the file said nothing about which one it described. start_protocol compares
    the mirror against the database at every start; an unstamped file from the
    other account reads as a position conflict, and the obvious fix for a bogus
    conflict is to overwrite the mirror — which destroys the other account's
    record to silence a warning about it.

    The underscore keys are metadata; readers skip anything starting with '_'.
    """
    payload = {k: v for k, v in trades.items() if not str(k).startswith("_")}
    try:
        acct = _active_account_id()
        if acct:
            payload["_account_id"] = acct
            try:
                from . import identity
                payload["_trade_env"] = (identity.account_info(acct) or {}).get(
                    "trade_env", "")
            except Exception:
                pass
        _OPEN_TRADES_JSON.parent.mkdir(parents=True, exist_ok=True)
        tmp = _OPEN_TRADES_JSON.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(payload, indent=2, default=str))
        os.replace(tmp, _OPEN_TRADES_JSON)
    except Exception as e:
        log.warning("open_trades.json mirror write failed: %s", e)


def _mirror_state_json(state: dict) -> None:
    """Write current kv_state to the legacy state.json file so GUI and
    external scripts always see the latest state — even after
    runtime_config.set_param() writes directly to DB without going
    through risk_manager._save_state().

    Atomic (tmp + os.replace): the scheduler and the web server both come
    through here from separate processes — a plain write_text truncates
    first, so a concurrent reader could catch a half-written file.

    Stamped with the account it describes. There is one state.json for the
    whole installation, so a REAL run overwrites the paper account's file with
    live numbers and nothing in it says so. Anyone reading it — the operator,
    an external script, a future version of this code — sees a budget and a
    realized PnL with no way to tell whose they are. The stamp does not make
    the file per-account; it makes the ambiguity detectable.
    """
    try:
        import os as _os
        acct = _active_account_id()
        payload = dict(state)
        payload["_account_id"] = acct
        try:
            from . import identity
            payload["_trade_env"] = (identity.account_info(acct) or {}).get(
                "trade_env", "")
        except Exception:
            payload["_trade_env"] = ""
        _STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_JSON.with_suffix(f".tmp.{_os.getpid()}")
        tmp.write_text(json.dumps(payload, indent=2, default=str))
        _os.replace(tmp, _STATE_JSON)
    except Exception as e:
        log.warning("state.json mirror failed: %s", e)


# ---------- audit ----------

def audit_insert(action: str, symbol: str = "", gate: str = "", reason: str = "",
                 score: float = 0.0, extra: dict | None = None,
                 ts: str | None = None) -> None:
    acc = _require_account_id("audit_insert")
    # NULL session is legitimate here, unlike on a fill: the web panel, the CLI
    # and the reconciler all write audit rows outside any run, and forcing a
    # session on them would mean either refusing to record what happened or
    # inventing a run that did not exist.
    sid = None
    try:
        from . import identity
        sid = identity.current_session_id()
    except Exception:
        pass
    with conn() as c:
        c.execute("""
            INSERT INTO audit (ts, action, symbol, gate, reason, score, extra,
                               account_id, session_id)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            ts or datetime.utcnow().isoformat(),
            action, symbol, gate, reason, score,
            json.dumps(extra, default=str) if extra else None,
            acc, sid,
        ))


def audit_recent(limit: int = 100, action: str | None = None) -> list[dict]:
    acc = _require_account_id("audit_recent")
    q = "SELECT * FROM audit WHERE account_id = ?"
    args: tuple = (acc,)
    if action:
        q += " AND action = ?"
        args = (acc, action)
    # ts, not id — insertion order stopped tracking time at the ledger merge.
    q += " ORDER BY ts DESC, id DESC LIMIT ?"
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
    acc = _require_account_id("audit_gate_summary")
    with conn() as c:
        rows = c.execute("""
            SELECT gate, COUNT(*) AS n FROM (
              SELECT gate FROM audit WHERE action='skip' AND account_id = ?
              ORDER BY ts DESC, id DESC LIMIT ?
            ) GROUP BY gate ORDER BY n DESC
        """, (acc, limit)).fetchall()
    return {r["gate"] or "?": r["n"] for r in rows}


# ---------- closed_trades ----------

def closed_trade_insert(row: dict) -> None:
    """Record a close. Always attributable, never refused.

    A close is a fill, and identity.py says a fill must belong to exactly one
    run. The obvious implementation — require_session_id() and raise otherwise —
    is wrong here, because portfolio.record_trade_close wraps this call in
    `except Exception: log.warning(...)`. Raising would not stop a bad write; it
    would delete a real trade from the ledger and leave a log line.

    So nothing is refused, and nothing is left ambiguous either. Outside a
    session the row is stamped with an explicit provenance string rather than a
    bare NULL, because a NULL meaning "written by a script" and a NULL meaning
    "a bug dropped the session" are indistinguishable later, and one of them is
    a duplicate order nobody can attribute. `migrated_from` already carries that
    meaning for pre-identity rows; this reuses it rather than inventing a
    second convention.
    """
    acc = _require_account_id("closed_trade_insert")
    sid, provenance = None, row.get("migrated_from")
    try:
        from . import identity
        sid = identity.current_session_id()
    except Exception:
        pass
    if not sid and not provenance:
        provenance = f"unattributed:no-session (pid {os.getpid()})"
        log.warning("closed trade for %s recorded outside any execution "
                    "session — stamped %r", row.get("symbol"), provenance)
    with conn() as c:
        c.execute("""
            INSERT INTO closed_trades
            (ts, symbol, qty, entry, stop, exit, exit_reason,
             pnl, pnl_pct, r_multiple, opened_at,
             mfe_pct, mae_pct, ml_proba_entry, strategy, extra,
             account_id, session_id, migrated_from)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            acc, sid, provenance,
        ))


def is_excluded(row: dict) -> bool:
    """Is this row marked as not representing real trading performance?

    Set by the ledger-quality migration on records that are in the ledger but
    are not trades: a synthetic TST test row, and the second copy of an MRK
    close that was booked twice. They stay in the table — deleting evidence to
    make a number look better is how a ledger stops being one — but they must
    not reach anything that reasons about performance.
    """
    extra = row.get("extra")
    if not extra:
        return False
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except (json.JSONDecodeError, TypeError):
            return False
    if not isinstance(extra, dict):
        return False
    return bool((extra.get("ledger_quality") or {}).get("excluded_from_performance"))


def closed_trades(limit: int = 200, include_excluded: bool = False) -> list[dict]:
    """MOST RECENT `limit` closed trades, in chronological (oldest→newest) order.

    Returns the EFFECTIVE ledger by default — quality-marked rows are filtered
    out. Pass include_excluded=True for the raw ledger, which is what a history
    view or an audit wants; every consumer that reasons about performance or
    risk (trade_stats, self_review, autopilot, adaptive_sizing, blacklist,
    strategy_gate, relative_strength) wants the default.

    The filter lives here rather than at the eleven call sites for the same
    reason the account scoping does: one of eleven will eventually be added
    without it, and a duplicate MRK close inside the sizing window is a
    real-money decision made on a trade that never happened.

    P1 fix 2026-07-07: was `ORDER BY id ASC LIMIT ?` — the OLDEST N rows.
    Every caller (trade_stats(50), autopilot rollback window, AI param check,
    web /api/closed) treats this as "recent trades", so once the table grew
    past `limit` the stats/autopilot would have been frozen on the earliest
    trades forever. Inner DESC picks the newest N; outer ASC restores the
    chronological order callers iterate in."""
    # Ordered by ts, not id. id is insertion order, and those stopped agreeing
    # the moment the repo-era ledger was merged in: the app's own six trades
    # hold ids 1-6 (August) while the thirty imported ones hold 7-36 (June and
    # July). "ORDER BY id DESC LIMIT 50" then returns the OLDEST trades under
    # the name of the newest, which silently feeds the wrong window to
    # trade_stats, self_review, the autopilot's learning window and the equity
    # curve. Ties break on id so the order is still total.
    # Over-fetch, then filter, then trim: applying LIMIT before the exclusion
    # would return fewer than `limit` usable rows and quietly shrink every
    # analysis window by however many marked rows happened to fall inside it.
    fetch = limit if include_excluded else limit + 64
    acc = _require_account_id("closed_trades")
    with conn() as c:
        rows = c.execute(
            """
            SELECT * FROM (
                SELECT * FROM closed_trades WHERE account_id = ?
                ORDER BY ts DESC, id DESC LIMIT ?
            ) ORDER BY ts ASC, id ASC
            """,
            (acc, fetch),
        ).fetchall()
    out = [dict(r) for r in rows]
    if not include_excluded:
        out = [r for r in out if not is_excluded(r)]
    return out[-limit:]


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
    acc = _require_account_id("last_sl_close_for_symbol")
    with conn() as c:
        rows = c.execute(
            """
            SELECT * FROM closed_trades
            WHERE account_id = ? AND symbol = ?
              AND (exit_reason IN ('SL', 'SL_BRACKET')
                   OR (exit_reason = 'BREAKEVEN' AND pnl < 0))
            ORDER BY ts DESC, id DESC LIMIT 5
            """,
            (acc, symbol),
        ).fetchall()
    # Effective ledger only. This drives the stop-loss re-entry cooldown, so a
    # quality-marked row here blocks a real entry on the strength of a trade
    # that did not happen — the synthetic TST record is itself an SL close.
    # Take a few and pick the newest that counts, rather than one that might not.
    for row in rows:
        d = dict(row)
        if not is_excluded(d):
            return d
    return None


# ---------- history ----------

def history_insert(row: dict) -> None:
    # No session: an equity curve is a property of the account and spans every
    # run that ever traded it. Stamping the session would invite someone to
    # slice the curve by run and find gaps wherever the bot was restarted.
    acc = _require_account_id("history_insert")
    with conn() as c:
        c.execute("""
            INSERT INTO history
            (ts, week, day, invested, budget, unrealized_pnl,
             realized_pnl_total, total_pnl, positions_count, symbols, timeframe,
             account_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
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
            acc,
        ))


def history_rows(limit: int = 500) -> list[dict]:
    acc = _require_account_id("history_rows")
    with conn() as c:
        rows = c.execute(
            # ts, not id: the equity curve is drawn from these rows and the
            # merge interleaved 582 imported ones among the app's own.
            "SELECT * FROM history WHERE account_id = ? "
            "ORDER BY ts DESC, id DESC LIMIT ?", (acc, limit)
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
