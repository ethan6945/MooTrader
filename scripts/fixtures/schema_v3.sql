CREATE TABLE open_trades (
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
    extra           TEXT
);
CREATE TABLE kv_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL                -- JSON-encoded
);
CREATE TABLE audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    action  TEXT NOT NULL,             -- skip | buy | error | scan_start | scan_end
    symbol  TEXT,
    gate    TEXT,
    reason  TEXT,
    score   REAL,
    extra   TEXT                       -- JSON
);
CREATE INDEX idx_audit_ts     ON audit(ts);
CREATE INDEX idx_audit_action ON audit(action);
CREATE INDEX idx_audit_symbol ON audit(symbol);
CREATE TABLE closed_trades (
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
    extra           TEXT          -- JSON blob for forward-compat fields (ml_features, etc.)
);
CREATE INDEX idx_closed_ts     ON closed_trades(ts);
CREATE INDEX idx_closed_symbol ON closed_trades(symbol);
CREATE TABLE history (
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
    timeframe          TEXT
);
CREATE INDEX idx_history_ts   ON history(ts);
CREATE INDEX idx_history_week ON history(week);
CREATE INDEX idx_closed_strategy ON closed_trades(strategy);
-- captured from the authoritative database on 2026-08-18, DDL only, no rows.
PRAGMA user_version = 3;
