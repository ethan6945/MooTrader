"""Which broker account a call reaches, and what happens when that is unclear.

Run from repo root: .venv/bin/python scripts/test_broker_binding.py
Uses a fake trade context. Contacts no OpenD, places nothing, needs no network.

THE TWO DEFECTS THESE COVER

  _env_enum() was:

      TrdEnv.SIMULATE if settings.moo_trade_env == "SIMULATE" else TrdEnv.REAL

  Every value that is not exactly "SIMULATE" selected real money — a typo, a
  lowercase spelling, an empty string from a .env that failed to load.

  And acc_id was passed on none of the fourteen broker calls, so the SDK chose
  with _get_default_acc_id(): the first account whose environment matches, in
  broker-supplied order. Correct with one account per environment; a silent
  coin-flip with two, and nothing in any log would name the account that traded.
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import os
_TMP = tempfile.mkdtemp(prefix="mmt-binding-")
os.environ["MMT_HOME"] = _TMP
(Path(_TMP) / "data").mkdir(parents=True, exist_ok=True)
(Path(_TMP) / "logs").mkdir(parents=True, exist_ok=True)

from moomoo import RET_OK, RET_ERROR, TrdEnv          # noqa: E402
from src import broker_binding as bb                  # noqa: E402

PASS = 0
FAIL = 0
def check(name, cond):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name)
    if cond: PASS += 1
    else: FAIL += 1


def refuses(code, fn, *a, **kw):
    """fn(...) must raise BrokerBindingRefused with exactly `code`."""
    try:
        fn(*a, **kw)
        return False
    except bb.BrokerBindingRefused as e:
        if e.code != code:
            print(f"        (refused {e.code}, expected {code})")
        return e.code == code


class FakeTrade:
    """Just enough of OpenSecTradeContext to answer get_acc_list()."""
    def __init__(self, rows, ret=RET_OK):
        self.rows, self.ret = rows, ret
        self.closed = False
    def get_acc_list(self):
        return (self.ret, self.rows if self.ret == RET_OK else "boom")
    def close(self):
        self.closed = True


from src.config import settings as _s                # noqa: E402
FIRM = _s.moo_security_firm                          # what this install runs as


def acct(acc_id, env="SIMULATE", firm=None, status="ACTIVE", typ="CASH"):
    firm = FIRM if firm is None else firm
    return {"acc_id": acc_id, "trd_env": env, "security_firm": firm,
            "acc_status": status, "acc_type": typ}


# ── 1. the environment parser is fail-closed ────────────────────────────────
print("1  an unrecognised environment refuses, and does not become REAL")
check("SIMULATE parses", bb.parse_trade_env("SIMULATE") == "SIMULATE")
check("REAL parses", bb.parse_trade_env("REAL") == "REAL")
check("whitespace and case are tolerated",
      bb.parse_trade_env("  simulate\n") == "SIMULATE")
# Each of these selected REAL under the old expression.
for bad in ("", None, "PAPER", "SIM", "REEL", "SIMULATE ONLY", 0, "SIMULATED"):
    check(f"{bad!r} refuses instead of selecting REAL",
          refuses("bad_trade_env", bb.parse_trade_env, bad))
check("the enum follows the parser",
      bb.trd_env_enum("SIMULATE") is TrdEnv.SIMULATE
      and bb.trd_env_enum("REAL") is TrdEnv.REAL)


# ── 2. an unbound call cannot reach the broker ──────────────────────────────
print("\n2  nothing may call the broker before an account is pinned")
bb.reset()
check("require() refuses while unbound",
      refuses("not_bound", bb.require, "a broker call"))
check("current() is None while unbound", bb.current() is None)

from src import moo_client                            # noqa: E402
check("moo_client._env_enum refuses while unbound",
      refuses("not_bound", moo_client._env_enum))
check("moo_client._acc_id refuses while unbound",
      refuses("not_bound", moo_client._acc_id))


# ── 3. resolution picks one account, or refuses to pick ─────────────────────
print("\n3  resolution")
bb.reset()
one = bb.resolve(FakeTrade([acct(111), acct(999, env="REAL")]),
                 trade_env="SIMULATE")
check("the matching account is selected", one.acc_id == 111)
check("the environment is carried on the binding", one.trade_env == "SIMULATE")
check("the account number never appears in the description",
      "111" not in one.describe() and one.account_ref in one.describe())

# The case the SDK silently resolved: two accounts, same environment.
check("two candidates and no record refuses rather than choosing",
      refuses("ambiguous_account", bb.resolve,
              FakeTrade([acct(111), acct(222)]), trade_env="SIMULATE"))

# ...and is decidable once the account is on record.
pinned = bb.resolve(FakeTrade([acct(111), acct(222)]), trade_env="SIMULATE",
                    expected_acc_id="222")
check("a recorded account resolves the ambiguity", pinned.acc_id == 222)
check("a recorded account that OpenD does not offer refuses",
      refuses("acc_id_mismatch", bb.resolve,
              FakeTrade([acct(111), acct(222)]), trade_env="SIMULATE",
              expected_acc_id="333"))

check("no account for the environment refuses",
      refuses("no_account_for_env", bb.resolve,
              FakeTrade([acct(999, env="REAL")]), trade_env="SIMULATE"))
check("a REPORTED firm that differs refuses",
      refuses("security_firm_mismatch", bb.resolve,
              FakeTrade([acct(111, firm="OTHERFIRM")]), trade_env="SIMULATE"))

# What a real OpenD actually returns. The first version of this check compared
# security_firm for equality and refused every start against the live gateway,
# while passing every test here — because the fixture supplied a firm and the
# broker does not. The paper account comes back with 'N/A', the SDK's stand-in
# for a field that was never set.
#
# Unreported is not a mismatch and not a match. The firm is an INPUT to
# OpenSecTradeContext, and this list is that context's answer, so a connection
# for the wrong firm returns a different list rather than a mislabelled one.
for blank in ("N/A", "", "NONE"):
    b = bb.resolve(FakeTrade([acct(111, firm=blank)]), trade_env="SIMULATE")
    check(f"an unreported firm ({blank!r}) binds rather than refusing",
          b.acc_id == 111)
    check("...and the binding says the firm was not verified",
          b.firm_verified is False)
    check("...which the log line makes visible",
          "unverified" in b.describe())
verified = bb.resolve(FakeTrade([acct(111)]), trade_env="SIMULATE")
check("a matching reported firm is marked verified",
      verified.firm_verified is True and "unverified" not in verified.describe())
check("a failed account list refuses",
      refuses("acc_list_failed", bb.resolve,
              FakeTrade([], ret=RET_ERROR), trade_env="SIMULATE"))
check("a disabled account refuses",
      refuses("account_not_active", bb.resolve,
              FakeTrade([acct(111, status="DISABLED")]), trade_env="SIMULATE"))
# REAL resolves on its own merits — this module is not where REAL is gated.
real = bb.resolve(FakeTrade([acct(999, env="REAL")]), trade_env="REAL")
check("REAL resolves when it is genuinely what was asked for",
      real.acc_id == 999 and real.trd_env is TrdEnv.REAL)


# ── 4. one process, one account ─────────────────────────────────────────────
print("\n4  the binding cannot move under a running process")
bb.reset()
b1 = bb.bind(bb.resolve(FakeTrade([acct(111)]), trade_env="SIMULATE"))
check("binding pins", bb.current().acc_id == 111)
check("re-binding the same account is idempotent",
      bb.bind(b1).acc_id == 111)
other = bb.resolve(FakeTrade([acct(222)]), trade_env="SIMULATE")
check("re-binding a different account refuses",
      refuses("rebind_refused", bb.bind, other))
check("the original binding survived the refusal", bb.current().acc_id == 111)

# Once bound, the client's helpers answer from the binding rather than settings.
settings = _s
object.__setattr__(settings, "moo_trade_env", "REAL")
check("_env_enum ignores a settings flip mid-run",
      moo_client._env_enum() is TrdEnv.SIMULATE)
check("_acc_id answers from the binding", moo_client._acc_id() == 111)
object.__setattr__(settings, "moo_trade_env", "SIMULATE")


# ── 5. the binding must agree with the session ──────────────────────────────
print("\n5  the session and the broker must describe the same account")
from src import db, identity                          # noqa: E402
db._ensure_initialised()
bb.reset()
identity._session = None
identity.reset_cache()
sess = identity.start_session("SIMULATE")

sim = bb.resolve(FakeTrade([acct(111)]), trade_env="SIMULATE")
bb.verify_against_session(sim)
check("a matching environment passes verification", True)

real_b = bb.resolve(FakeTrade([acct(999, env="REAL")]), trade_env="REAL")
check("a REAL binding under a SIMULATE session refuses",
      refuses("session_env_mismatch", bb.verify_against_session, real_b))

# Once the account row remembers a broker account, a different one refuses:
# the records were written against the first, and the second would inherit them.
with db.transaction() as c:
    c.execute("UPDATE accounts SET broker_acc_id = '111' WHERE account_id = ?",
              (sess["account_id"],))
bb.verify_against_session(sim)
check("the remembered broker account still matches", True)
mismatch = bb.resolve(FakeTrade([acct(222)]), trade_env="SIMULATE")
check("a different broker account under the same records refuses",
      refuses("account_record_mismatch", bb.verify_against_session, mismatch))
identity.end_session("test")


# ── 6. unlock_trade is not called, ever, by this code ───────────────────────
print("\n6  the gateway is not armed as a side effect of connecting")
src = (ROOT / "src" / "moo_client.py").read_text()
check("moo_client never calls unlock_trade",
      "unlock_trade(" not in src.replace("# ", "").split("\n\n")[0] and
      not any(ln.strip().startswith(("ret, data = self._trade.unlock_trade",
                                     "self._trade.unlock_trade",
                                     "self.trade.unlock_trade"))
              for ln in src.splitlines()))
check("the trade password is no longer read by the broker client",
      "moo_trade_pwd" not in src)

# The unlock state cannot be read back from the protocol, so a successful gated
# op is the only evidence — and it is evidence about an order already sent.
# It must never be usable as permission to send one.
bb.reset()
check("no unlock claim without a REAL binding",
      moo_client.real_unlock_confirmed() is False)
bb.bind(bb.resolve(FakeTrade([acct(111)]), trade_env="SIMULATE"))
moo_client._note_gated_op_ok()
check("a SIMULATE gated op never claims a REAL unlock",
      moo_client.real_unlock_confirmed() is False)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
