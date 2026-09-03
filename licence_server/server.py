"""The activation service — what the issuer hosts, and the only thing they host.

  MMT_LICENCE_ADMIN_TOKEN=... .venv/bin/python licence_server/server.py
  (behind a real HTTPS terminator; this speaks plain HTTP)

WHAT IT IS FOR

  Offline verification already proves a licence was issued by the holder of the
  private key. It cannot answer the two questions that need a memory: how many
  machines a licence has been activated on, and whether it should still work at
  all. Refunds, chargebacks and a key posted on a forum are all revocation, and
  revocation is not expressible in a signature that has already been handed out.

WHAT IT DELIBERATELY DOES NOT HOLD

  The signing private key. It verifies with the PUBLIC half, exactly as the app
  does, so a compromise of this host leaks the activation table and nothing
  else — no ability to mint licences. A service that signs is a service whose
  breach ends the product.

FAILURE DIRECTION

  The client fails OPEN when this host is unreachable and closed only on an
  explicit refusal, so an outage here does not stop customers trading. That is
  a deliberate asymmetry: the cost of this box being down must fall on the
  issuer, not on someone holding a position.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

from flask import Flask, jsonify, request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.licence import verify_signature_only                      # noqa: E402

log = logging.getLogger("licence-server")
app = Flask(__name__)

DB = Path(os.getenv("MMT_LICENCE_DB",
                    Path.home() / ".mootrader-licence" / "activations.db"))
ADMIN_TOKEN = os.getenv("MMT_LICENCE_ADMIN_TOKEN", "").strip()
# 0 disables the cap. A perpetual licence for one customer with two Macs is a
# normal thing to want; a licence seen on forty machines is a leaked key.
MAX_MACHINES = int(os.getenv("MMT_LICENCE_MAX_MACHINES", "2"))


def _db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=10)
    c.execute("""CREATE TABLE IF NOT EXISTS activations (
                   licence_id TEXT NOT NULL,
                   machine    TEXT NOT NULL,
                   first_seen INTEGER NOT NULL,
                   last_seen  INTEGER NOT NULL,
                   PRIMARY KEY (licence_id, machine))""")
    c.execute("""CREATE TABLE IF NOT EXISTS revoked (
                   licence_id TEXT PRIMARY KEY,
                   reason     TEXT,
                   at         INTEGER NOT NULL)""")
    return c


def _is_revoked(c: sqlite3.Connection, lic_id: str) -> str | None:
    row = c.execute("SELECT reason FROM revoked WHERE licence_id=?",
                    (lic_id,)).fetchone()
    return (row[0] or "revoked") if row else None


@app.post("/activate")
def activate():
    body = request.get_json(silent=True) or {}
    key = (body.get("licence") or "").strip()
    machine = (body.get("machine") or "").strip().lower()

    # Verify here too. The client already did, but the client is the thing an
    # attacker controls; an unsigned string must never earn a row in this table.
    payload = verify_signature_only(key)
    if payload is None:
        return jsonify({"ok": False,
                        "error": "That licence key is not valid."}), 400

    lic_id = payload.get("id") or "?"
    bound = (payload.get("machine") or "").lower()
    if bound and machine and bound != machine:
        return jsonify({"ok": False,
                        "error": "This licence was issued for a different "
                                 "computer."}), 403

    now = int(time.time())
    with _db() as c:
        reason = _is_revoked(c, lic_id)
        if reason:
            return jsonify({"ok": False,
                            "error": f"This licence is no longer active "
                                     f"({reason})."}), 403

        seen = {r[0] for r in c.execute(
            "SELECT machine FROM activations WHERE licence_id=?", (lic_id,))}
        if MAX_MACHINES and machine not in seen and len(seen) >= MAX_MACHINES:
            return jsonify({"ok": False,
                            "error": f"This licence is already active on "
                                     f"{len(seen)} computers, which is its "
                                     f"limit. Contact support to move it."}), 403

        c.execute("""INSERT INTO activations (licence_id, machine, first_seen,
                                              last_seen)
                     VALUES (?,?,?,?)
                     ON CONFLICT(licence_id, machine)
                     DO UPDATE SET last_seen=excluded.last_seen""",
                  (lic_id, machine, now, now))
    log.info("activated %s on %s", lic_id, machine or "unbound")
    return jsonify({"ok": True, "id": lic_id})


@app.post("/check")
def check():
    """Is this licence still good? Answers only about state this host knows."""
    body = request.get_json(silent=True) or {}
    lic_id = (body.get("id") or "").strip()
    machine = (body.get("machine") or "").strip().lower()
    now = int(time.time())
    with _db() as c:
        reason = _is_revoked(c, lic_id)
        if reason:
            return jsonify({"ok": False, "error": reason}), 403
        c.execute("""UPDATE activations SET last_seen=?
                     WHERE licence_id=? AND machine=?""", (now, lic_id, machine))
    return jsonify({"ok": True})


# ── admin ────────────────────────────────────────────────────────────────────

def _admin_ok() -> bool:
    if not ADMIN_TOKEN:
        return False              # no token configured -> admin is CLOSED
    got = request.headers.get("X-Admin-Token", "")
    return hmac.compare_digest(got, ADMIN_TOKEN)


@app.post("/admin/revoke")
def admin_revoke():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin token required"}), 401
    body = request.get_json(silent=True) or {}
    lic_id = (body.get("id") or "").strip()
    if not lic_id:
        return jsonify({"ok": False, "error": "id required"}), 400
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO revoked VALUES (?,?,?)",
                  (lic_id, (body.get("reason") or "revoked"), int(time.time())))
    log.warning("revoked %s", lic_id)
    return jsonify({"ok": True})


@app.post("/admin/unrevoke")
def admin_unrevoke():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin token required"}), 401
    lic_id = ((request.get_json(silent=True) or {}).get("id") or "").strip()
    with _db() as c:
        c.execute("DELETE FROM revoked WHERE licence_id=?", (lic_id,))
    return jsonify({"ok": True})


@app.post("/admin/list")
def admin_list():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin token required"}), 401
    with _db() as c:
        rows = [{"id": r[0], "machine": r[1], "first_seen": r[2],
                 "last_seen": r[3]}
                for r in c.execute("""SELECT licence_id, machine, first_seen,
                                             last_seen FROM activations
                                      ORDER BY last_seen DESC LIMIT 500""")]
        rev = [{"id": r[0], "reason": r[1], "at": r[2]}
               for r in c.execute("SELECT licence_id, reason, at FROM revoked")]
    return jsonify({"ok": True, "activations": rows, "revoked": rev})


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "mootrader-licence"})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not ADMIN_TOKEN:
        log.warning("MMT_LICENCE_ADMIN_TOKEN is not set — /admin/* is closed")
    app.run(host=os.getenv("MMT_LICENCE_HOST", "127.0.0.1"),
            port=int(os.getenv("MMT_LICENCE_PORT", "8787")))
