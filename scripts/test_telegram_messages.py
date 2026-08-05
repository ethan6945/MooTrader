"""Regression guard for Telegram message rendering.

Every action type executor.manage_open_trades can emit must have a branch in
notifier.trade_action_msg, and every rendered message must have BALANCED legacy-
Markdown control characters. 2026-08-05: five of eleven types had no branch and
fell through to a `str(action)` fallback whose dict repr carried an odd number of
"_", which Telegram rejects with HTTP 400 "can't parse entities".

    .venv/bin/python -m scripts.test_telegram_messages

Exits non-zero if any type renders unbalanced. Needs no network or broker.
"""
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from src import notifier  # noqa: E402

emitted = sorted(set(re.findall(r'"type":\s*"([a-z_0-9]+)"',
                                (pathlib.Path(__file__).resolve().parent.parent / "src" / "executor.py").read_text())))
samples = dict(symbol="FTNT", qty=5, price=166.59, pnl=12.34, new_stop=167.0,
               stop=162.08, age_days=3, tranche=2, age_min=6.0, old=162.08,
               new=166.59, order_id="3148686")


def unbalanced(s: str):
    """Return the first control char appearing an ODD number of times unescaped —
    that is exactly what makes Telegram answer 400 "can't parse entities"."""
    for ch in ("_", "*", "`"):
        if len(re.findall(r"(?<!\\)\%s" % ch, s)) % 2:
            return ch
    return None


bad = 0
print(f"{'action type':<20} {'renders as':<60} markdown")
print("-" * 92)
for k in emitted + ["some_future_action"]:
    msg = notifier.trade_action_msg(dict(samples, type=k)).replace("\n", " ⏎ ")
    flag = unbalanced(msg)
    bad += bool(flag)
    print(f"{k:<20} {msg[:58]:<60} {'BAD ' + flag if flag else 'ok'}")

print()
print(f"executor emits {len(emitted)} action types; unbalanced renders: {bad}")
sys.exit(1 if bad else 0)
