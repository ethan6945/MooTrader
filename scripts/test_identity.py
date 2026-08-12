"""Account + execution-session identity, against a real SQLite database.

Run from repo root: .venv/bin/python scripts/test_identity.py
Builds its own database in a temp dir — never touches data/trader.db.

WHY THIS EXISTS
  Until schema v4 there was one namespace for everything: one kv_state, one
  open_trades, one closed_trades. `trade_env` appeared zero times in db.py,
  portfolio.py, risk_manager.py and audit.py, so flipping SIMULATE to REAL would
  have pointed the live account at the paper account's records — its -$625 of
  realized loss, its $10,000 drawdown baseline, its 8 legacy naked shorts, its
  open positions. Not a reporting problem: risk_manager sizes orders from
  budget_usd and halts on peak_equity, so the first live order would have been
  sized against a fiction.

  These tests are mostly about that one property — that the two accounts cannot
  see each other — plus the session rules: trade_env is decided once per run and
  cannot move, positions may outlive a run, orders and fills may not.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# A private MMT_HOME so this builds a fresh db and cannot reach the real one.
_TMP = tempfile.mkdtemp(prefix="mmt-identity-")
os.environ["MMT_HOME"] = _TMP
(Path(_TMP) / "data").mkdir(parents=True, exist_ok=True)
(Path(_TMP) / "logs").mkdir(parents=True, exist_ok=True)

from src import db, identity            # noqa: E402

PASS = 0
FAIL = 0
def check(name, cond):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name)
    if cond: PASS += 1
    else: FAIL += 1


def use_account(env):
    """Point the process at an account the way a fresh process would.

    settings is a frozen dataclass, so this goes through object.__setattr__ —
    the point is to exercise the REAL resolution path (env -> account) rather
    than stubbing active_account_id out, which would test nothing.
    """
    identity._session = None
    identity.reset_cache()
    from src.config import settings
    object.__setattr__(settings, "moo_trade_env", env)


db._ensure_initialised()

# ── 1. SIMULATE and REAL are two accounts ───────────────────────────────────
print("accounts")
sim = identity.get_or_create_account("SIMULATE")
real = identity.get_or_create_account("REAL")
check("SIMULATE and REAL get different ids", sim != real)
check("re-asking returns the same id (idempotent)",
      identity.get_or_create_account("SIMULATE") == sim)
check("ids are internal uuids, not broker ids", len(sim) == 36 and "-" in sim)

# The broker id is a locator that arrives later; it must attach to the account
# the migration already created rather than minting a second one.
adopted = identity.get_or_create_account("SIMULATE", broker_acc_id="283812345")
check("a broker id attaches to the existing account", adopted == sim)
check("broker id is recorded",
      identity.account_info(sim)["broker_acc_id"] == "283812345")
check("the SAME broker id under REAL is a different account",
      identity.get_or_create_account("REAL", broker_acc_id="283812345") != sim)

try:
    identity.get_or_create_account("PAPER")
    check("an unknown trade_env is rejected", False)
except ValueError:
    check("an unknown trade_env is rejected", True)


# ── 2. account-scoped state cannot cross ────────────────────────────────────
# This is the property the whole migration exists for.
print("\nstate isolation")
use_account("SIMULATE")
db.update_state({"budget_usd": 10000.0, "peak_equity": 10000.0,
                 "realized_pnl_total": -625.47, "halted": False,
                 "ai_provider": "deepseek"})          # ai_provider is global
sim_state = db.get_state()
check("paper account sees its own budget", sim_state["budget_usd"] == 10000.0)
check("paper account sees its own realized PnL",
      sim_state["realized_pnl_total"] == -625.47)

use_account("REAL")
real_state = db.get_state()
check("live account does NOT inherit the paper budget",
      "budget_usd" not in real_state)
check("live account does NOT inherit the paper drawdown peak",
      "peak_equity" not in real_state)
check("live account does NOT inherit -$625 of realized loss",
      "realized_pnl_total" not in real_state)
check("global keys ARE shared", real_state.get("ai_provider") == "deepseek")

# Writing on the live account must not disturb the paper account.
db.update_state({"budget_usd": 500.0, "realized_pnl_total": 0.0})
check("live account keeps its own budget", db.get_state()["budget_usd"] == 500.0)
use_account("SIMULATE")
check("paper budget survived the live write",
      db.get_state()["budget_usd"] == 10000.0)
check("paper realized PnL survived the live write",
      db.get_state()["realized_pnl_total"] == -625.47)

# A global write from one side is visible to the other.
db.update_state({"ai_model": "deepseek-v4-pro"})
use_account("REAL")
check("a global write crosses accounts",
      db.get_state().get("ai_model") == "deepseek-v4-pro")

# atomic_state must respect the same scoping.
use_account("SIMULATE")
db.atomic_state(lambda s: {"realized_pnl_total": s.get("realized_pnl_total", 0) - 100})
check("atomic_state writes to the right account",
      abs(db.get_state()["realized_pnl_total"] - (-725.47)) < 0.01)
use_account("REAL")
check("atomic_state did not touch the other account",
      db.get_state()["realized_pnl_total"] == 0.0)


# ── 3. sessions pin the environment ─────────────────────────────────────────
print("\nexecution sessions")
use_account("SIMULATE")
check("no session before start", identity.current_session_id() is None)

s = identity.start_session("SIMULATE", broker_acc_id="283812345",
                           auth_mode="opend_locked")
check("session has an id", len(s["session_id"]) == 36)
check("session binds the internal account", s["account_id"] == sim)
check("session records the real worker", s["pid"] == os.getpid() and s["host"])
check("session pins trade_env", identity.current_trade_env() == "SIMULATE")
check("auth_mode is recorded for later audit", s["auth_mode"] == "opend_locked")

# The environment must not move underneath a running session — that is the
# whole reason it is copied onto the session row.
from src.config import settings                       # noqa: E402
object.__setattr__(settings, "moo_trade_env", "REAL")
check("flipping the setting does NOT move a running session",
      identity.current_trade_env() == "SIMULATE")
check("the session's account is unchanged too",
      identity.active_account_id() == sim)
object.__setattr__(settings, "moo_trade_env", "SIMULATE")

try:
    identity.start_session("SIMULATE")
    check("a second session in one process is refused", False)
except RuntimeError:
    check("a second session in one process is refused", True)

check("the session is listed as open",
      s["session_id"] in [x["session_id"] for x in identity.open_sessions()])

sid = identity.require_session_id()
check("require_session_id returns it inside a session", sid == s["session_id"])

identity.end_session("test complete")
check("session closed", identity.current_session_id() is None)
check("no sessions left open", identity.open_sessions() == [])

# Orders and fills must be attributable; refusing is better than a NULL that
# looks identical to migrated history.
try:
    identity.require_session_id()
    check("require_session_id raises with no session", False)
except RuntimeError:
    check("require_session_id raises with no session", True)


# ── 4. what may and may not span sessions ───────────────────────────────────
print("\nsession attribution rules")
with db.conn() as c:
    cols_open = {r[1] for r in c.execute("PRAGMA table_info(open_trades)")}
    cols_closed = {r[1] for r in c.execute("PRAGMA table_info(closed_trades)")}
check("positions carry an account", "account_id" in cols_open)
check("positions carry an OPENING session (nullable, they outlive a run)",
      "opened_session_id" in cols_open)
check("closes carry an account", "account_id" in cols_closed)
check("closes carry a session", "session_id" in cols_closed)
check("pre-identity rows can say so explicitly", "migrated_from" in cols_closed)

with db.conn() as c:
    env_col = {r[1] for r in c.execute("PRAGMA table_info(execution_sessions)")}
check("session stores an immutable trade_env", "trade_env" in env_col)
check("session has authorization columns ready for the OpenD step",
      {"auth_mode", "authorized_at", "authorized_by"} <= env_col)

# The CHECK constraint must actually reject a bad env at the database level.
try:
    with db.transaction() as c:
        c.execute("INSERT INTO accounts (account_id, trade_env, created_at) "
                  "VALUES ('x', 'PAPER', 'now')")
    check("db rejects an invalid trade_env", False)
except Exception:
    check("db rejects an invalid trade_env", True)


# ── 5. the ledger itself cannot cross ───────────────────────────────────────
#
# Everything above this point tests that the COLUMNS exist. They existed in v4
# too, and the isolation still did not, because open_trades was keyed on symbol
# alone and every read was unfiltered. A schema-shape assertion cannot tell the
# difference between "there is an account_id column" and "the account_id column
# is load-bearing", and only the second one keeps a live account from being
# sized against paper history.
print("\nledger isolation (the columns are load-bearing)")

use_account("SIMULATE")
sim_acct = identity.active_account_id()
db.upsert_open_trade({"symbol": "AAPL", "qty": 100, "entry_price": 190.0,
                      "stop_loss": 185.0, "take_profit": 200.0})
db.closed_trade_insert({"symbol": "MSFT", "qty": 5, "entry": 400.0,
                        "stop": 390.0, "exit": 380.0, "pnl": -100.0,
                        "ts": "2026-08-01T10:00:00"})
db.audit_insert("buy", symbol="AAPL", reason="paper")
db.history_insert({"ts": "2026-08-01T10:00:00", "budget": 10000,
                   "realized_pnl_total": -100.0})

use_account("REAL")
real_acct = identity.active_account_id()
check("SIMULATE and REAL resolve to different accounts", sim_acct != real_acct)
check("REAL sees no paper positions", db.load_open_trades() == {})
check("REAL sees no paper closes", db.closed_trades() == [])
check("REAL sees no paper audit rows", db.audit_recent() == [])
check("REAL sees no paper equity history", db.history_rows() == [])
check("REAL cannot fetch a paper position by name",
      db.get_open_trade("AAPL") is None)

# The v5 key. On v4 this next line did not raise and did not create a row — it
# UPDATEd the paper AAPL in place, at the live account's quantity, and stamped
# the live account_id onto it. Verified against a v4-shaped table: the result is
# ONE row, ('AAPL', 3, 191.0, real). The paper account's 100 shares are not
# mislabelled, they are gone — while still held at the broker, with no stop, no
# take-profit and nothing left to drive an exit. A position the software no
# longer knows it owns is the worst outcome available here, and it needed no
# concurrency and no error to happen: just two accounts and the same ticker.
db.upsert_open_trade({"symbol": "AAPL", "qty": 3, "entry_price": 191.0,
                      "stop_loss": 186.0, "take_profit": 201.0})
real_aapl = db.load_open_trades().get("AAPL")
check("REAL can hold the same ticker as SIMULATE", real_aapl is not None)
check("REAL's AAPL is REAL's quantity", real_aapl and real_aapl["qty"] == 3)

use_account("SIMULATE")
sim_aapl = db.load_open_trades().get("AAPL")
check("SIMULATE's AAPL survived untouched", sim_aapl and sim_aapl["qty"] == 100)
check("SIMULATE's AAPL kept its own entry",
      sim_aapl and abs(sim_aapl["entry_price"] - 190.0) < 1e-9)

# Deleting one account's position must not reach into the other's.
use_account("REAL")
db.delete_open_trade("AAPL")
check("deleting REAL's AAPL removed it", db.load_open_trades() == {})
use_account("SIMULATE")
check("deleting REAL's AAPL left SIMULATE's alone",
      "AAPL" in db.load_open_trades())

# And the paper side still sees exactly its own rows, not a merged view.
check("SIMULATE still sees its close", len(db.closed_trades()) == 1)
check("SIMULATE still sees its equity row", len(db.history_rows()) == 1)


# ── 6. an unresolvable account raises instead of reading as empty ───────────
print("\nan unresolvable account is an error, not an empty result")
_saved = db._active_account_id
db._active_account_id = lambda: ""
try:
    try:
        db.load_open_trades()
        check("load_open_trades refuses rather than returning {}", False)
    except RuntimeError:
        # This is the failure mode worth paying for. Returning {} here is a
        # perfectly ordinary answer — "no open positions" — and the caller acts
        # on it by buying everything it already holds.
        check("load_open_trades refuses rather than returning {}", True)
    try:
        db.closed_trades()
        check("closed_trades refuses rather than returning []", False)
    except RuntimeError:
        check("closed_trades refuses rather than returning []", True)
    try:
        db.upsert_open_trade({"symbol": "X", "qty": 1, "entry_price": 1.0,
                              "stop_loss": 1.0, "take_profit": 1.0})
        check("upsert refuses to write an unattributed position", False)
    except RuntimeError:
        check("upsert refuses to write an unattributed position", True)
finally:
    db._active_account_id = _saved


# ── 7. attribution is stamped, never left to be guessed ─────────────────────
print("\nattribution")
use_account("SIMULATE")

# Outside a session: the row is still written — losing a real fill to a missing
# session would be the worse bug — but it says so, rather than leaving a NULL
# that is indistinguishable from migrated history.
identity._session = None
db.closed_trade_insert({"symbol": "NVDA", "qty": 1, "entry": 100.0,
                        "stop": 95.0, "exit": 90.0, "pnl": -10.0,
                        "ts": "2026-08-02T10:00:00"})
with db.conn() as c:
    row = c.execute("SELECT session_id, migrated_from FROM closed_trades "
                    "WHERE symbol='NVDA'").fetchone()
check("a close outside a session is still recorded", row is not None)
check("...and is marked unattributed, not left NULL",
      row and (row["migrated_from"] or "").startswith("unattributed:"))

# Inside a session: the session is on the row.
sess = identity.start_session("SIMULATE")
db.closed_trade_insert({"symbol": "TSLA", "qty": 1, "entry": 100.0,
                        "stop": 95.0, "exit": 110.0, "pnl": 10.0,
                        "ts": "2026-08-03T10:00:00"})
db.upsert_open_trade({"symbol": "AMD", "qty": 2, "entry_price": 150.0,
                      "stop_loss": 145.0, "take_profit": 160.0})
with db.conn() as c:
    closed = c.execute("SELECT session_id FROM closed_trades "
                       "WHERE symbol='TSLA'").fetchone()
    opened = c.execute("SELECT opened_session_id FROM open_trades "
                       "WHERE symbol='AMD'").fetchone()
    aud = c.execute("SELECT session_id FROM audit ORDER BY id DESC "
                    "LIMIT 1").fetchone()
check("a close inside a session carries it",
      closed and closed["session_id"] == sess["session_id"])
check("a new position records the session that opened it",
      opened and opened["opened_session_id"] == sess["session_id"])

# A position outlives the run that opened it, so a later run must not restamp
# it — otherwise "which run opened this?" quietly becomes "which run last
# touched it?", and the first question has no other source.
identity.end_session("test")
sess2 = identity.start_session("SIMULATE")
db.upsert_open_trade({"symbol": "AMD", "qty": 4, "entry_price": 150.0,
                      "stop_loss": 148.0, "take_profit": 160.0})
with db.conn() as c:
    again = c.execute("SELECT qty, opened_session_id FROM open_trades "
                      "WHERE symbol='AMD'").fetchone()
check("updating a position does not restamp its opening session",
      again and again["opened_session_id"] == sess["session_id"])
check("...but the update itself applied", again and again["qty"] == 4)
identity.end_session("test")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
