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

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
