#!/usr/bin/env python3
"""The macOS app and the web panel must be talking to the SAME backend.

Run from repo root: .venv/bin/python scripts/test_macos_parity.py
Temp home, empty password, no OpenD required. Places nothing.

WHY THIS EXISTS

  0f6b502 replaced the watch station with the forecast desk and deleted five
  endpoints: /api/signal-monitor, -alerts, -status, -scheduler/<a>, -run/<m>.
  The SwiftUI app went on calling all five. Its Signals tab had been showing an
  empty watchlist and a dead alert feed ever since — every request a 404 that
  `try?` swallowed into "no data yet", which looks exactly like a quiet market.

  Thirty-one route tests were green. None of them had asked whether the OTHER
  client's calls still resolved, because nothing in the Python suite knows the
  Swift app exists.

  Part 1 is the contract: every path APIClient.swift builds must resolve to a
  route this server serves, with a method it accepts. Part 2 is the payload:
  the app's own Codable types must decode what the server actually returns —
  a renamed JSON key is the same silent failure one layer down.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SWIFT = ROOT / "macos" / "Sources" / "MooTraderApp"
_TMP = tempfile.mkdtemp(prefix="mmt-parity-")
os.environ["MMT_HOME"] = _TMP
for _d in ("data", "logs", "config"):
    (Path(_TMP) / _d).mkdir(parents=True, exist_ok=True)
(Path(_TMP) / ".env").write_text(
    "MOO_TRADE_ENV=SIMULATE\nWEB_PASSWORD=\nDEEPSEEK_API_KEY=synthetic-fixture-value\n")
(Path(_TMP) / "config" / "parameters.json").write_text(json.dumps(
    {"version": 1, "params": {"ENTRY_SCORE_THRESHOLD": "70.0",
                              "SL_ATR_MULT": "3.5",
                              "USE_SCALE_OUT": "false",
                              "PARAM_TUNE_MODE": "manual",
                              "STRATEGY_MODE": "technical"}}, indent=2))
(Path(_TMP) / "config" / "signal_watchlist.json").write_text(
    json.dumps({"tickers": ["NVDA", "MU"]}))

from src import db                                             # noqa: E402
db._ensure_initialised()
from web.server import app                                     # noqa: E402

app.config["TESTING"] = True
client = app.test_client()

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name + (f"   [{detail}]" if detail else ""))
    if cond:
        PASS += 1
    else:
        FAIL += 1


# ── the routes this server serves, as "path -> {methods}" ────────────────────
def server_routes():
    out = {}
    for rule in app.url_map.iter_rules():
        # Flask writes "<converter:name>"; the shape is what matters here, not
        # the name, so every placeholder collapses to one token.
        path = re.sub(r"<[^>]+>", "<>", rule.rule).lstrip("/")
        out.setdefault(path, set()).update(rule.methods)
    return out


# ── the paths the Swift client builds ────────────────────────────────────────
def swift_calls():
    """Every api/… literal in APIClient.swift, with its HTTP method.

    Swift interpolation (\\(sym)) and query strings are stripped so a call
    compares against a Flask rule by shape. The method comes from the helper
    the literal is passed to — post() and getJSON()/get() are the only two.
    """
    src = (SWIFT / "Backend" / "APIClient.swift").read_text()
    calls = []
    for m in re.finditer(r'(getJSON|post|get)\(\s*"(api/[^"]*)"', src):
        helper, path = m.group(1), m.group(2)
        path = _strip_interpolation(path)
        path = path.split("?")[0].rstrip("/")
        calls.append((path, "POST" if helper == "post" else "GET"))
    return sorted(set(calls))


def _strip_interpolation(path):
    """Replace each \(…) with "<>", counting nested parens.

    A non-greedy regex stopped at the first ")" of \(esc(symbol)) and left a
    stray one behind, so three live routes were reported as deleted.
    """
    out, i = [], 0
    while i < len(path):
        if path.startswith("\\(", i):
            depth, i = 1, i + 2
            while i < len(path) and depth:
                if path[i] == "(":
                    depth += 1
                elif path[i] == ")":
                    depth -= 1
                i += 1
            out.append("<>")
        else:
            out.append(path[i])
            i += 1
    return "".join(out)


print("\n1  every path the macOS app builds resolves to a route")
routes = server_routes()
calls = swift_calls()
check("APIClient declares endpoints", len(calls) >= 25, f"{len(calls)} calls")
unknown = [(p, v) for p, v in calls if p not in routes]
check("no call to a deleted endpoint", not unknown,
      "; ".join(f"{v} /{p}" for p, v in unknown) or "none")
wrong = [(p, v) for p, v in calls if p in routes and v not in routes[p]]
check("every call uses a method the route accepts", not wrong,
      "; ".join(f"{v} /{p}" for p, v in wrong) or "none")

print("\n2  the endpoints the app polls answer without raising")
POLLED = ["api/status", "api/approvals", "api/closed?n=5", "api/log?n=5",
          "api/sectors", "api/caffeinate", "api/settings", "api/settings/toggles",
          "api/strategy-mode", "api/trade-env", "api/auto-budget", "api/ai-provider",
          "api/finbert", "api/web-access", "api/params?lang=zh", "api/param-tune",
          "api/self-review", "api/signal-watchlist", "api/signal-forecast-health",
          "api/halt", "api/cash-yield", "api/inverse-sleeve"]
for path in POLLED:
    r = client.get("/" + path)
    check(f"GET /{path}", r.status_code == 200, f"HTTP {r.status_code}")

print("\n3  the payloads decode into the app's own Codable types")
FIX = Path(_TMP) / "fixtures"
FIX.mkdir(exist_ok=True)
# Each fixture is (file stem, path, the Swift type that must decode it). The
# forecast/scan/optimizer trio needs a live broker session, so they are covered
# by the contract above and by hand-built payloads here rather than a fetch.
DECODE = [
    ("status", "api/status", "TraderStatus"),
    ("approvals", "api/approvals", "[Approval]"),
    ("closed", "api/closed?n=5", "[ClosedTrade]"),
    ("log", "api/log?n=5", "[String]"),
    ("sectors", "api/sectors", "SectorOverview"),
    ("caffeinate", "api/caffeinate", "CaffeinateStatus"),
    ("settings", "api/settings", "SettingsKeys"),
    ("toggles", "api/settings/toggles", "SettingsToggles"),
    ("strategy_mode", "api/strategy-mode", "StrategyMode"),
    ("trade_env", "api/trade-env", "TradeEnvState"),
    ("auto_budget", "api/auto-budget", "AutoBudgetState"),
    ("ai_provider", "api/ai-provider", "AIProviderState"),
    ("finbert", "api/finbert", "FinbertState"),
    ("web_access", "api/web-access", "WebAccessState"),
    ("params", "api/params?lang=zh", "ParamConsole"),
    ("param_tune", "api/param-tune", "TuneState"),
    ("self_review", "api/self-review", "SelfReview"),
    ("forecast_health", "api/signal-forecast-health", "ForecastHealth"),
    ("preflight", "api/preflight", "PreflightResult"),
    ("halt", "api/halt", "HaltState"),
    ("cash_yield", "api/cash-yield", "SleeveState"),
    ("inverse_sleeve", "api/inverse-sleeve", "SleeveState"),
]
manifest = []
for stem, path, swift_type in DECODE:
    r = client.get("/" + path)
    if r.status_code != 200:
        check(f"capture /{path}", False, f"HTTP {r.status_code}")
        continue
    (FIX / f"{stem}.json").write_bytes(r.data)
    manifest.append({"file": f"{stem}.json", "type": swift_type, "path": path})

# The three broker-backed shapes, as the server documents them. Decoding a
# real forecast needs OpenD; decoding its SHAPE does not, and the shape is
# what a renamed key breaks.
(FIX / "forecast.json").write_text(json.dumps({
    "ok": True, "symbol": "NVDA", "market_as_of": "2026-08-31T14:30:00",
    "model": {"version": "v3", "training_sessions": 249},
    "quality": {"status": "no_opinion", "reasons": ["no direction skill"],
                "skill": {"margin_percentage_points": -1.2}},
    "probabilities": {"up_percent": 41.0, "flat_percent": 22.5, "down_percent": 36.5,
                      "confidence_percent": 47.0, "abstained": True},
    "backtest": {"samples": 42, "direction_accuracy_percent": 47.6,
                 "baseline_accuracy_percent": 52.4, "interval_coverage_percent": 78.0,
                 "mae_percent": 2.31, "brier_score": 0.2417,
                 "calibration": {"source": "out_of_sample_walk_forward"}},
    "recent_30m": [{"low": 1.0, "high": 2.0, "close": 1.5, "time": "2026-08-29T14:00:00"},
                   {"low": 1.1, "high": 2.1, "close": 1.6, "time": "2026-08-29T14:30:00"}],
    "forecast_30m": [{"q10": 1.4, "q50": 1.7, "q90": 2.0, "time": "2026-09-01T14:00:00"},
                     {"q10": 1.3, "q50": 1.8, "q90": 2.2, "time": "2026-09-03T20:00:00"}],
    "context": {"scores": {"five_minute_score": 0.4, "news_score": None},
                "contributions_pp": {"five_minute_score": 0.12},
                "terminal_adjustment_pp": 0.31, "model_version": "p7",
                "user_approved_version": True},
}))
manifest.append({"file": "forecast.json", "type": "Forecast", "path": "api/signal-forecast/<>"})

(FIX / "scan.json").write_text(json.dumps({
    "generated_at": "2026-08-31T13:00:00", "daily_cache": {"hit": True},
    "starred_results": [{"symbol": "NVDA", "origin": "starred", "status": "CANDIDATE",
                         "score": 71.5, "plan": {"entry_low": 170.2, "entry_high": 172.8},
                         "plan_model": {"abstained": False, "stop_percent": -4.1,
                                        "target_percent": 5.3,
                                        "realized": {"stop_hit_percent": 15.1}},
                         "plan_comparison": {"atr_stop_distance_percent": -7.7,
                                             "atr_target_distance_percent": 9.4}}],
    "market_results": [],
    "market_universe": {"outcome": {"by_status": {"NO_TRADE": 12},
                                    "top_blockers": [["spread", 7], ["regime", 5]]}},
    "warnings": ["discovery pool unreadable"],
}))
manifest.append({"file": "scan.json", "type": "ScanResult", "path": "api/signal-scan"})

(FIX / "optimizer.json").write_text(json.dumps({
    "ok": True,
    "proposal": {"proposal_id": "p-42", "status": "pending",
                 "current_version": "v6", "suggested_version": "v7",
                 "current_parameters": {"a": 1}, "suggested_parameters": {"a": 2},
                 "reasoning": [{"parameter": "news_weight", "from": 0.15,
                                "to": 0.22, "sample_count": 31}],
                 "metrics": {"estimated_improvement_percent": 4.2,
                             "raw_mae_pp": 1.884, "estimated_mae_pp": 1.805}},
}))
manifest.append({"file": "optimizer.json", "type": "OptimizerState",
                 "path": "api/signal-optimizer/<>"})

(FIX / "forecast_history.json").write_text(json.dumps({
    "ok": True, "symbol": "NVDA",
    "history": [{"forecast_day": "2026-08-27",
                 "learning": {"direction_hit": True,
                              "signed_error_percentage_points": 1.4,
                              "actual_path": [{"predicted_q50": 1.0, "actual_close": 1.1},
                                              {"predicted_q50": 1.2, "actual_close": 1.15},
                                              {"predicted_q50": 1.3, "actual_close": 1.32},
                                              {"predicted_q50": 1.4, "actual_close": 1.38}]}}],
    "learning": {"samples": 24, "direction_accuracy_percent": 54.2,
                 "interval_coverage_percent": 79.1, "mae_percentage_points": 1.912,
                 "status": "learning", "activation_rule": "20 settled + MAE -2%"},
}))
manifest.append({"file": "forecast_history.json", "type": "ForecastHistory",
                 "path": "api/signal-forecast-history/<>"})

(FIX / "manifest.json").write_text(json.dumps(manifest, indent=2))
check("captured every payload", len(manifest) == len(DECODE) + 4,
      f"{len(manifest)} fixtures")

# ── hand the fixtures to a Swift harness built from the app's own sources ────
harness = FIX / "main.swift"
harness.write_text(r'''
import Foundation

struct Case: Decodable { let file: String; let type: String; let path: String }

let dir = URL(fileURLWithPath: CommandLine.arguments[1])
let cases = try JSONDecoder().decode(
    [Case].self, from: Data(contentsOf: dir.appendingPathComponent("manifest.json")))

var failed = 0
/// Decode one fixture with the app's own type. A throw here is the app
/// silently showing an empty panel in production.
func attempt(_ c: Case, _ body: (Data) throws -> Void) {
    do {
        try body(try Data(contentsOf: dir.appendingPathComponent(c.file)))
        print("  ok  \(c.type) decodes /\(c.path)")
    } catch {
        print(" FAIL \(c.type) decodes /\(c.path)   [\(error)]")
        failed += 1
    }
}

let d = JSONDecoder()
for c in cases {
    switch c.type {
    case "TraderStatus":     attempt(c) { _ = try d.decode(TraderStatus.self, from: $0) }
    case "[Approval]":       attempt(c) { _ = try d.decode([Approval].self, from: $0) }
    case "[ClosedTrade]":    attempt(c) { _ = try d.decode([ClosedTrade].self, from: $0) }
    case "[String]":         attempt(c) { _ = try d.decode([String].self, from: $0) }
    case "SectorOverview":   attempt(c) { _ = try d.decode(SectorOverview.self, from: $0) }
    case "CaffeinateStatus": attempt(c) { _ = try d.decode(CaffeinateStatus.self, from: $0) }
    case "SettingsKeys":     attempt(c) { _ = try d.decode(SettingsKeys.self, from: $0) }
    case "SettingsToggles":  attempt(c) { _ = try d.decode(SettingsToggles.self, from: $0) }
    case "StrategyMode":     attempt(c) { _ = try d.decode(StrategyMode.self, from: $0) }
    case "TradeEnvState":    attempt(c) { _ = try d.decode(TradeEnvState.self, from: $0) }
    case "AutoBudgetState":  attempt(c) { _ = try d.decode(AutoBudgetState.self, from: $0) }
    case "AIProviderState":  attempt(c) { _ = try d.decode(AIProviderState.self, from: $0) }
    case "FinbertState":     attempt(c) { _ = try d.decode(FinbertState.self, from: $0) }
    case "WebAccessState":   attempt(c) { _ = try d.decode(WebAccessState.self, from: $0) }
    case "PreflightResult":  attempt(c) { _ = try d.decode(PreflightResult.self, from: $0) }
    case "ParamConsole":     attempt(c) { _ = try d.decode(ParamConsole.self, from: $0) }
    case "TuneState":        attempt(c) { _ = try d.decode(TuneState.self, from: $0) }
    case "SelfReview":       attempt(c) { _ = try d.decode(SelfReview.self, from: $0) }
    case "ForecastHealth":   attempt(c) { _ = try d.decode(ForecastHealth.self, from: $0) }
    case "HaltState":        attempt(c) { _ = try d.decode(HaltState.self, from: $0) }
    case "SleeveState":      attempt(c) { _ = try d.decode(SleeveState.self, from: $0) }
    case "Forecast":         attempt(c) { _ = try d.decode(Forecast.self, from: $0) }
    case "ScanResult":       attempt(c) { _ = try d.decode(ScanResult.self, from: $0) }
    case "OptimizerState":   attempt(c) { _ = try d.decode(OptimizerState.self, from: $0) }
    case "ForecastHistory":  attempt(c) { _ = try d.decode(ForecastHistory.self, from: $0) }
    default:
        print(" FAIL no harness case for \(c.type)"); failed += 1
    }
}

/// A decode that "succeeds" into all-nil is the failure this test exists to
/// catch, so one payload is read back field by field.
let fc = try d.decode(Forecast.self,
                      from: Data(contentsOf: dir.appendingPathComponent("forecast.json")))
func want(_ name: String, _ cond: Bool) {
    print((cond ? "  ok  " : " FAIL ") + name)
    if !cond { failed += 1 }
}
want("forecast.ok", fc.ok)
want("forecast.symbol", fc.symbol == "NVDA")
want("forecast.quality.status", fc.quality?.status == "no_opinion")
want("forecast.quality.reasons", fc.quality?.reasons.count == 1)
want("forecast.probabilities.abstained", fc.probabilities?.abstained == true)
want("forecast.probabilities.up_percent", fc.probabilities?.upPercent == 41.0)
want("forecast.backtest.brier_score", fc.backtest?.brierScore == 0.2417)
want("forecast.backtest.calibration out-of-sample", fc.backtest?.calibratedOutOfSample == true)
want("forecast.model.training_sessions", fc.model?.trainingSessions == 249)
want("forecast.recent_30m", fc.recent30m.count == 2)
want("forecast.forecast_30m q50", fc.forecast30m.last?.q50 == 1.8)
want("forecast.context.terminal_adjustment_pp", fc.context?.terminalAdjustmentPp == 0.31)
want("forecast.context null factor stays nil", (fc.context?.scores["news_score"] ?? nil) == nil)

let sc = try d.decode(ScanResult.self,
                      from: Data(contentsOf: dir.appendingPathComponent("scan.json")))
want("scan rows", sc.rows.count == 1)
want("scan starred", sc.rows.first?.isStarred == true)
want("scan stop pair", sc.rows.first?.planComparison?.atrStopDistancePercent == -7.7
                    && sc.rows.first?.planModel?.stopPercent == -4.1)
want("scan realized hit", sc.rows.first?.planModel?.realized?.stopHitPercent == 15.1)
want("scan blockers", sc.marketUniverse?.outcome?.topBlockers.count == 2)

let op = try d.decode(OptimizerState.self,
                      from: Data(contentsOf: dir.appendingPathComponent("optimizer.json")))
want("proposal pending", op.proposal?.isPending == true)
want("proposal delta", op.proposal?.reasoning.first?.to == 0.22)
want("proposal improvement", op.proposal?.metrics?.estimatedImprovementPercent == 4.2)

let sleeve = try d.decode(SleeveState.self, from: Data(
    #"{"enabled":true,"symbol":"SQQQ","position":{"qty":10},"history":[{"a":1},{"a":2}]}"#
        .utf8))
want("sleeve enabled", sleeve.enabled)
want("sleeve symbol", sleeve.symbol == "SQQQ")
want("sleeve object position is seen as held", sleeve.hasPosition)
want("sleeve history counted", sleeve.historyCount == 2)
let flat = try d.decode(SleeveState.self,
                        from: Data(#"{"enabled":false,"position":null}"#.utf8))
want("sleeve null position is not held", !flat.hasPosition)

let hi = try d.decode(ForecastHistory.self,
                      from: Data(contentsOf: dir.appendingPathComponent("forecast_history.json")))
want("history settled", hi.settled.count == 1)
want("history learning", hi.learning?.samples == 24)

exit(failed == 0 ? 0 : 1)
''')

if shutil.which("swiftc") is None:
    # Part 1 and 2 are the contract and already ran. The decode half needs a
    # Swift toolchain, which a Linux runner does not have; saying so is better
    # than a red that means "wrong machine".
    print("  --  no swiftc on this machine — decode checks skipped")
    shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)

sources = [SWIFT / "L10n.swift", SWIFT / "Backend" / "Models.swift",
           SWIFT / "Backend" / "ForecastModels.swift",
           SWIFT / "Backend" / "ParamModels.swift", harness]
binary = FIX / "decode-harness"
build = subprocess.run(
    ["swiftc", "-O", "-o", str(binary), *[str(s) for s in sources]],
    capture_output=True, text=True)
if build.returncode != 0:
    check("the decode harness compiles", False, build.stderr.strip()[-400:])
else:
    check("the decode harness compiles", True)
    run = subprocess.run([str(binary), str(FIX)], capture_output=True, text=True)
    print(run.stdout.rstrip())
    for line in run.stdout.splitlines():
        if line.startswith("  ok  "):
            PASS += 1
        elif line.startswith(" FAIL "):
            FAIL += 1
    if run.stderr.strip():
        print(run.stderr.strip()[-400:])

shutil.rmtree(_TMP, ignore_errors=True)
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
