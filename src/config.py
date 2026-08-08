import logging
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

# ── app home ───────────────────────────────────────────────────────────────────
# Dev (running from the repo): everything lives next to the code, as always.
# Frozen (PyInstaller .app): the bundle is read-only, so all mutable state
# (.env, data/, logs/, config/) moves to ~/Library/Application Support/…, and
# read-only bundled resources (web/static, config templates) come from the
# bundle via BUNDLE_DIR. MMT_HOME overrides for tests / secondary instances.
IS_FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def _app_home() -> Path:
    env = os.getenv("MMT_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    if IS_FROZEN:
        return Path.home() / "Library" / "Application Support" / "MooMooTrader"
    return Path(__file__).resolve().parent.parent


ROOT = _app_home()

if IS_FROZEN:
    # First run: create the writable home and seed config templates from the
    # bundle (never overwrite the user's existing files on later runs).
    for _d in ("data", "logs", "config", "cron"):
        (ROOT / _d).mkdir(parents=True, exist_ok=True)
    # Deterministic CWD no matter how the .app was launched (Finder, terminal,
    # re-exec'd worker) — anything that resolves relative paths sees the home.
    os.chdir(ROOT)
    _tpl_dir = BUNDLE_DIR / "config"
    if _tpl_dir.is_dir():
        for _tpl in _tpl_dir.iterdir():
            _dst = ROOT / "config" / _tpl.name
            if _tpl.is_file() and not _dst.exists():
                shutil.copy2(_tpl, _dst)

load_dotenv(ROOT / ".env")


def app_version() -> str:
    """The build's version string, from the ONE place it is declared —
    macos/Resources/Info.plist (frozen: the copy at MooTrader.app/Contents).

    Read at runtime rather than duplicated into a Python constant, because two
    declarations drift and the whole point of showing a version in the UI is to
    be able to trust it. Unknown -> "dev": a repo checkout with no bundle is a
    real, normal case, and pretending it has a release number would be worse
    than admitting it doesn't.
    """
    import plistlib
    candidates = []
    if IS_FROZEN:
        # BUNDLE_DIR is .../MooTrader.app/Contents/Resources/backend/_internal
        # (or similar); walk up to the Contents that owns Info.plist.
        for parent in BUNDLE_DIR.resolve().parents:
            if parent.name == "Contents":
                candidates.append(parent / "Info.plist")
                break
    candidates.append(Path(__file__).resolve().parent.parent
                      / "macos" / "Resources" / "Info.plist")
    for p in candidates:
        try:
            with open(p, "rb") as fh:
                v = plistlib.load(fh).get("CFBundleShortVersionString")
            if v:
                return str(v)
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
    return "dev"


def running_from() -> str:
    """Which build is actually executing — the .app bundle path when frozen,
    else the repo checkout.

    Worth surfacing next to the version: on 2026-08-05 a bot was running out of
    macos/dist (a build-output directory that gets wiped on every rebuild) while
    everyone assumed it was the copy in /Applications. A version number alone
    would not have shown that; the path does.
    """
    if IS_FROZEN:
        for parent in BUNDLE_DIR.resolve().parents:
            if parent.suffix == ".app":
                return str(parent)
        return str(BUNDLE_DIR)
    return str(Path(__file__).resolve().parent.parent)


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _timeframe(default: str = "HOUR_1") -> str:
    # DAILY trading mode removed 2026-06-07 — coerce any legacy value to HOUR_1
    # so no string-keyed branch (MTF/gap/scoring) ever sees a stale "DAILY".
    tf = os.getenv("TIMEFRAME", default).upper()
    if tf == "DAILY":
        import logging
        logging.getLogger(__name__).warning(
            "TIMEFRAME=DAILY is no longer supported (removed 2026-06-07) — "
            "coerced to HOUR_1. Update your .env to avoid this warning.")
    return "HOUR_1" if tf == "DAILY" else tf


def _extension_mode(default: str = "shadow") -> str:
    """ENTRY_EXTENSION_MODE guard. A typo here would silently pick a behaviour
    the owner did not choose, and this gate decides whether entries fire — so an
    unrecognised value falls back to the safe one (legacy behaviour + shadow
    logging) and says so loudly."""
    m = os.getenv("ENTRY_EXTENSION_MODE", default).strip().lower()
    if m not in ("legacy", "shadow", "atr"):
        import logging
        logging.getLogger(__name__).warning(
            "ENTRY_EXTENSION_MODE=%r is not one of legacy/shadow/atr — "
            "falling back to 'shadow' (no behaviour change).", m)
        return "shadow"
    return m


@dataclass(frozen=True)
class Settings:
    moo_host: str = os.getenv("MOO_HOST", "127.0.0.1")
    moo_port: int = _int("MOO_PORT", 11111)
    moo_trade_pwd: str = os.getenv("MOO_TRADE_PWD", "")
    moo_trade_env: str = os.getenv("MOO_TRADE_ENV", "SIMULATE")
    moo_market: str = os.getenv("MOO_MARKET", "US")
    moo_security_firm: str = os.getenv("MOO_SECURITY_FIRM", "FUTUMY")

    gemini_keys: tuple = tuple(
        k.strip() for k in os.getenv("GEMINI_API_KEYS", "").split(",") if k.strip()
    )
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

    tavily_key: str = os.getenv("TAVILY_API_KEY", "")

    # AI provider (2026-06-23): the whole system can run on Gemini OR DeepSeek,
    # flipped live from the web panel (db-state override, no restart — see
    # src/ai.py). These are the .env DEFAULTS; the runtime override wins. The
    # owner reaches for DeepSeek when Gemini is quota'd/down. DeepSeek is
    # OpenAI-compatible REST (text only — no vision), reached via `requests`.
    ai_provider: str = os.getenv("AI_PROVIDER", "gemini").strip().lower()
    deepseek_keys: tuple = tuple(
        k.strip()
        for k in (
            os.getenv("DEEPSEEK_API_KEYS") or os.getenv("DEEPSEEK_API_KEY") or ""
        ).split(",")
        if k.strip()
    )
    # 2026-08-07: was "deepseek-chat", which DeepSeek retired on 2026-07-24 —
    # the name stopped resolving, with no soft-redirect. ai.migrate_deepseek_model()
    # rewrites the old names at call time so an un-edited .env or a stale
    # db-state override still works, but the default here is now current.
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

    telegram_token: str = os.getenv("TELEGRAM_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Signal reporter watchlist — tickers to analyse and push as signal cards.
    # Separate from the trading watchlist; can overlap. Set via SIGNAL_WATCHLIST env var.
    signal_watchlist: tuple = tuple(
        s.strip().upper()
        for s in os.getenv("SIGNAL_WATCHLIST", "").split(",")
        if s.strip()
    )

    account_usd: float = _float("ACCOUNT_USD", 4500)
    risk_per_trade: float = _float("RISK_PER_TRADE", 0.02)
    max_positions: int = _int("MAX_POSITIONS", 5)
    max_position_pct: float = _float("MAX_POSITION_PCT", 0.20)
    # Position-slot cap scales with capital (req#1: change budget → recompute
    # params). max_positions acts as a FLOOR; larger accounts open more slots up
    # to max_positions_cap (≈ universe size). At the $4.5k default round(4500/1000)
    # = 4 (banker's rounding), which the max_positions FLOOR of 5 rescues — so the
    # derived value == max_positions and behaviour is unchanged until capital grows
    # past ~$5.5k. The cash wall (sizing_capital budget cap) stays the real
    # constraint at small budgets.
    slot_target_usd: float = _float("SLOT_TARGET_USD", 1000.0)
    max_positions_cap: int = _int("MAX_POSITIONS_CAP", 10)
    daily_drawdown_stop: float = _float("DAILY_DRAWDOWN_STOP", 0.03)

    # Concentration mode — open fewer new names per scan, pyramid into winners.
    # Per-scan cap on brand-new tickers (does NOT limit add-ons to existing names).
    max_new_names_per_scan: int = _int("MAX_NEW_NAMES_PER_SCAN", 2)
    # How many entries (incl. initial) one symbol may stack. 1 = stacking off.
    max_stacks_per_symbol: int = _int("MAX_STACKS_PER_SYMBOL", 5)
    # Add-on gate: existing position must be at least this many R in profit
    # before a new stack entry is allowed. Prevents averaging-down on losers.
    stack_min_r_multiple: float = _float("STACK_MIN_R_MULTIPLE", 0.5)

    entry_threshold: float = _float("ENTRY_SCORE_THRESHOLD", 70)
    scan_interval_min: int = _int("SCAN_INTERVAL_MIN", 15)

    # Fast protective-stop loop (2026-06-21): seconds between lightweight, stop-ONLY
    # checks (src/executor.manage_stops_only). SIMULATE has no native STOP order, so
    # a soft stop otherwise waits for the 5-min manage tick — the live audit measured
    # a ~−1.38R late-fill overshoot from that lag. This independent loop checks ONLY
    # the breakeven ratchet + soft stop-loss (soft positions) and broker bracket
    # fills (REAL), so it's cheap enough to run every minute. It runs in BOTH
    # SIMULATE and REAL by design — it fixes the simulate soft-stop lag NOW (so it's
    # battle-tested before go-live) and doubles as a fast OCO-fill detector live.
    # No-op when outside market hours or flat (no broker connection). 0 disables it
    # (falls back to the 5-min tick). A dedicated host handles 60s comfortably; 30s
    # is also fine. Stall-out / max-hold / partials / TP stay on the 5-min tick.
    fast_stop_seconds: int = _int("FAST_STOP_SECONDS", 60)

    max_hold_days: int = _int("MAX_HOLD_DAYS", 10)
    timeframe: str = _timeframe()   # DAILY trading mode removed 2026-06-07 (coerced → HOUR_1)

    # Optuna-tuned exit knobs — moved out of code so .env can override.
    # TP = entry + TP_ATR_MULT × ATR ; SL = entry - SL_ATR_MULT × ATR.
    tp_atr_mult: float = _float("TP_ATR_MULT", 1.5)
    sl_atr_mult: float = _float("SL_ATR_MULT", 2.0)
    max_gap_pct: float = _float("MAX_GAP_PCT", 3.0)   # overnight gap filter

    # ── Entry-extension gate (2026-08-05) — replaces the fixed chase tolerance ──
    # The legacy gate skips when the live quote sits > ENTRY_CHASE_TOL (20 bps)
    # above the signal bar close. That threshold is SCALE-DEPENDENT in a way that
    # has no meaning: 20 bps is $0.10 on a $50 name and $2.80 on a $1,400 name,
    # while what actually decides whether the setup is intact is the move
    # measured in the name's OWN volatility. On 2026-08-04 it blocked HPE (80
    # score) six scans running across a $51.88 → $52.50 range.
    # The replacement asks two scale-invariant questions instead:
    #   1. extension = (live - signal) / ATR   — how far past the setup are we?
    #   2. reward/risk at the LIVE price, using the structural stop when the
    #      strategy supplied one (the ATR-only stop makes R:R constant, so the
    #      structural level is the part that actually varies with entry price).
    # MODE: "legacy" = old gate only. "shadow" = old gate still decides, new gate
    # logs what it WOULD have done. "atr" = new gate decides.
    # DEFAULT "shadow" — this gate exists only in the live path (the backtest
    # fills at the next bar open and has no live-quote concept), so it CANNOT be
    # validated by a backtest run. Shadow mode is how it earns its evidence:
    # zero behaviour change, and the log records both verdicts for comparison.
    entry_extension_mode: str = _extension_mode()
    entry_max_extension_atr: float = _float("ENTRY_MAX_EXTENSION_ATR", 0.5)
    entry_min_rr: float = _float("ENTRY_MIN_RR", 1.5)

    # Regime up-scaling (2026-06-08): press more size ONLY in a confirmed strong
    # bull (regime.bullish, i.e. SPY > 50MA > 200MA) AND a calm tape (VIX <
    # regime_vix_calm). Mirrors the honest engine's use_regime_scaling so live ↔
    # backtest stay in parity. DEFAULT 1.0 = INERT (no behaviour change); set
    # REGIME_BULL_MULT=1.4 in .env to activate the owner-approved tailwind boost.
    # It only scales the risk-based qty and still obeys the max_position_pct cap,
    # so it can never push a single name past its concentration limit. Backtests
    # (140d, $5k) showed 1.35-1.5× lifts $/day a few % with DD flat; higher
    # multipliers (2×, 2.5×) were over-fit noise on ~40 trades — do not chase them.
    regime_bull_mult: float = _float("REGIME_BULL_MULT", 1.0)
    regime_vix_calm: float = _float("REGIME_VIX_CALM", 20.0)

    # Gap-risk sentinel (2026-06-08): for HELD positions, exit DURING regular hours
    # before a likely overnight gap (a stop can't catch a gap — it fills past the
    # stop). Two layers: (1) deterministic — exit if earnings is within
    # GAP_EXIT_EARNINGS_DAYS; (2) AI — Gemini judges fresh PUBLIC bad news and only
    # sells on a high-confidence verdict (≥ GAP_SENTINEL_AI_MIN_CONF), fail-safe to
    # HOLD on any error. Default OFF (inert) — set GAP_SENTINEL_ENABLED=true to arm.
    # The AI decision overrides the strategy's hold; every exit fires a notification.
    gap_sentinel_enabled: bool = os.getenv("GAP_SENTINEL_ENABLED", "false").lower() in ("1", "true", "yes")
    gap_exit_earnings_days: int = _int("GAP_EXIT_EARNINGS_DAYS", 1)
    gap_sentinel_ai: bool = os.getenv("GAP_SENTINEL_AI", "true").lower() in ("1", "true", "yes")
    gap_sentinel_ai_min_conf: int = _int("GAP_SENTINEL_AI_MIN_CONF", 70)
    # Cost control: the AI gap layer uses ONE fixed model (not the entry cascade),
    # runs at PRE-MARKET only by default (gap_sentinel_ai_intraday=False skips the
    # ~11 per-scan AI calls/day — the deterministic earnings layer still runs every
    # scan for free), and skips the Gemini call entirely when there's no fresh news.
    # Owner wants Gemini ≥ 3.5-flash everywhere (no lite tiers) — default bumped
    # from gemini-2.5-flash-lite (2026-06-22). Gap sentinel is OFF by default, so
    # this only costs anything once GAP_SENTINEL_ENABLED is armed.
    gap_sentinel_model: str = os.getenv("GAP_SENTINEL_MODEL", "gemini-3.5-flash")
    gap_sentinel_ai_intraday: bool = os.getenv("GAP_SENTINEL_AI_INTRADAY", "false").lower() in ("1", "true", "yes")

    # ── Smart exit (Phase 2A, 2026-06-23): AI/algo intraday early-exit ──
    # Broader than the gap sentinel: exit a HELD long DURING the day when the
    # picture turns bearish — concrete bad news / analyst downgrade / sector roll
    # (AI), OR a clear technical break-down (deterministic). Two intents:
    #   • LOCK PROFIT: on a technical break-down while in profit ≥ min_profit_r R,
    #     bank the gain instead of giving it back waiting for the price TP.
    #   • DEFENSIVE: on concrete bearish news the AI exits at any P&L.
    # Runs on the 5-min manage tick via executor.manage_open_trades (reuses the
    # gap-sentinel _force_close path). DEFAULT OFF; FAIL-SAFE (AI down → no exit).
    smart_exit_enabled: bool = os.getenv("SMART_EXIT_ENABLED", "false").lower() in ("1", "true", "yes")
    # AI layer on/off. Off ⇒ only the deterministic technical-breakdown lock-profit
    # fires (no Gemini cost), so smart exit is still useful without a key.
    smart_exit_ai: bool = os.getenv("SMART_EXIT_AI", "true").lower() in ("1", "true", "yes")
    smart_exit_min_conf: int = _int("SMART_EXIT_MIN_CONF", 70)
    # The algo lock-profit path only fires once unrealized profit ≥ this many R
    # (R = entry − initial stop), so a routine wobble in a barely-green trade
    # doesn't cut a position that hasn't earned anything. The AI news path ignores
    # this (concrete bad news should exit even at a loss).
    smart_exit_min_profit_r: float = _float("SMART_EXIT_MIN_PROFIT_R", 1.0)
    smart_exit_model: str = os.getenv("SMART_EXIT_MODEL", "gemini-3.5-flash")

    # ── Sentiment scoring (Phase 2B, 2026-06-23): broker-style 看好/看空 ──
    # For each buy candidate, Gemini fuses news + analyst-target direction + the
    # technical reasons (sig.reasons) into a 0-100 bullishness score (50=neutral),
    # like the broker analysis card. DEFAULT OFF, ADVISORY (recorded + shown, does
    # NOT change which trades fire → live↔backtest parity preserved). FAIL-SAFE →
    # neutral 50 on any error. Optional SENTIMENT_SIZING folds the score into the
    # existing conviction → position-size channel (still never changes selection).
    sentiment_scoring_enabled: bool = os.getenv("SENTIMENT_SCORING_ENABLED", "false").lower() in ("1", "true", "yes")
    sentiment_sizing: bool = os.getenv("SENTIMENT_SIZING", "false").lower() in ("1", "true", "yes")
    sentiment_model: str = os.getenv("SENTIMENT_MODEL", "gemini-3.5-flash")
    sentiment_budget: int = _int("SENTIMENT_BUDGET", 8)

    # ── News search quality (2026-08-07, src/news_fetcher.py) ─────────────
    # Tavily's news topic searches the open web, which returns SEO listicles,
    # aggregator rewrites and price-move recaps alongside real reporting — all
    # of which read as catalysts to a model and are none. The domain whitelist
    # is the single highest-leverage knob on news quality. Empty (the default,
    # so existing installs are unchanged) = search the open web. Set it to
    # `recommended` for news_fetcher.RECOMMENDED_DOMAINS — the major financial
    # wires plus sec.gov, where an 8-K IS the material event, timestamped,
    # before the press writes it up — or give your own comma-separated list.
    news_include_domains: str = os.getenv("NEWS_INCLUDE_DOMAINS", "")
    # "basic" | "advanced" — advanced costs more Tavily credits per call and
    # returns better-extracted content. Left at basic (unchanged default).
    news_search_depth: str = os.getenv("NEWS_SEARCH_DEPTH", "basic").strip().lower()
    # Per-ticker lookback in days. 3 is the historical default and is why the
    # input is a narrative rather than an event; 1 suits a same-session bet.
    news_ticker_days: int = _int("NEWS_TICKER_DAYS", 3)

    # ── Finnhub company news (2026-08-07, src/finnhub_news.py) ────────────
    # Ticker-TAGGED news (by the provider, not by a search string) that can also
    # answer for a PAST date range — the missing half of a backtestable news
    # strategy, since Tavily has no point-in-time mode and you cannot replay a
    # decision whose input you cannot reconstruct. Free tier: 60 calls/min,
    # ~1 year of history. Merged with Tavily and de-duplicated in news_fetcher.
    # DEFAULT OFF; needs a key from finnhub.io.
    finnhub_enabled: bool = os.getenv("FINNHUB_ENABLED", "false").lower() in ("1", "true", "yes")
    finnhub_key: str = os.getenv("FINNHUB_API_KEY", "")
    finnhub_days: int = _int("FINNHUB_DAYS", 3)
    finnhub_max_results: int = _int("FINNHUB_MAX_RESULTS", 5)

    # ── filings + analyst actions via moomoo (2026-08-08, src/moo_notices.py) ──
    # Replaced SEC EDGAR, which was dropped on the owner's instruction: this bot
    # is not to talk to a US government service. OpenD already relays SEC
    # filings (NewsSubType.NOTICE) and analyst actions (RATING), so the same
    # gateway we trade through answers the "is there a concrete, fresh catalyst"
    # question and this process only ever connects to 127.0.0.1.
    # The trade is real and is documented in moo_notices.py: no item codes, so a
    # notice says something was filed but not what; dates only, no clock time.
    # ADDS to Tavily, does not replace it. DEFAULT OFF.
    moo_notices_enabled: bool = os.getenv("MOO_NOTICES_ENABLED", "false").lower() in ("1", "true", "yes")
    moo_notices_days: int = _int("MOO_NOTICES_DAYS", 3)

    # ── FinBERT local scorer (2026-08-07, src/news_score_local.py) ────────
    # Deterministic, local, and — the actual point — trained on a corpus that
    # predates any window you would test on, so unlike a frontier LLM it does
    # not know what happened next. ADVISORY: logged next to the LLM's read so
    # disagreement becomes measurable, never gates a trade.
    # Runs from the .app AND from a source checkout, off ONE shared copy of the
    # weights (news_score_local.model_home() — a per-OS user path, deliberately
    # not config.ROOT, which differs between the two and would download twice).
    # Runtime is onnxruntime + tokenizers (~25 MB, bundleable); a source install
    # that already has torch + transformers uses those instead.
    #
    # Enabling this does NOT start a download — the weights (~120 MB) are only
    # fetched when the web panel's confirm dialog asks for them, or when
    # FINBERT_AUTO_DOWNLOAD is set for a headless box. Spending a few hundred MB
    # of someone's disk without asking is not a decision this code gets to make.
    finbert_enabled: bool = os.getenv("FINBERT_ENABLED", "false").lower() in ("1", "true", "yes")
    finbert_model: str = os.getenv("FINBERT_MODEL", "ProsusAI/finbert")
    finbert_threads: int = _int("FINBERT_THREADS", 2)
    finbert_auto_download: bool = os.getenv("FINBERT_AUTO_DOWNLOAD", "false").lower() in ("1", "true", "yes")

    # ── News-driven mode (2026-08-07): news as the PRIMARY signal ──────────
    # Everything above treats news as advisory: it annotates, it can veto, it
    # can nudge size, but the technical score decides WHICH trade fires. This
    # switch inverts that — the rule score becomes a cheap prefilter and the
    # news read becomes the thing that selects and sizes. Owner-requested
    # (2026-08-07) after being shown the risks below; DEFAULT OFF.
    #
    # WHAT IT COSTS, stated plainly so nobody rediscovers it via a drawdown:
    #  1. BACKTEST PARITY IS GONE while this is on. Every other AI layer is
    #     advisory precisely so the backtest still describes the live system.
    #     This one changes selection, and sandbox.py deliberately skips AI to
    #     avoid LLM look-ahead, so the backtest models a DIFFERENT strategy.
    #     preflight.check_news_driven() says so on every start.
    #  2. The news is 3 days old (news_fetcher uses days=3, 30-min cache), so
    #     this trades a narrative, not an event. It is not a fast-news edge.
    #  3. No measured factor study backs it — unlike the options-volume factor
    #     (scripts/options_factor_study.py, 5,980 name-days). Treat live
    #     results as the experiment.
    #
    # FAIL-SAFE DIRECTION FLIPS HERE. In advisory mode "AI unavailable" →
    # neutral 50 → trade anyway (correct: the technicals were the thesis). In
    # news-driven mode the news IS the thesis, so no news / no key / API error
    # → NO TRADE. news_driven.gate() enforces that; it never falls back to a
    # technical-only entry, because that would silently be a different system.
    news_driven_enabled: bool = os.getenv("NEWS_DRIVEN_ENABLED", "false").lower() in ("1", "true", "yes")
    # SHADOW: run the entire gate for real — news read, catalyst requirement,
    # risk, sizing — then stop one line before the order and write down what it
    # would have done (data/news_shadow.jsonl). The offline factor study can
    # test whether FinBERT sentiment sorts returns; it CANNOT test this gate,
    # which is an LLM's judgement plus a named-catalyst rule. The only way to
    # learn whether the live pipeline has an edge is to collect its decisions
    # with outcomes attached, and that is either done with money or without.
    # Strongly recommended for the first few weeks. Score it with:
    #   python -m scripts.news_factor_study --shadow
    news_driven_shadow: bool = os.getenv("NEWS_DRIVEN_SHADOW", "false").lower() in ("1", "true", "yes")
    # Minimum 0-100 bullishness for an entry. 50 = neutral, so 65 asks for a
    # clearly positive read rather than "nothing bad in the headlines".
    news_driven_min_score: int = _int("NEWS_DRIVEN_MIN_SCORE", 65)
    # Require a NAMED concrete catalyst (guidance raise, contract, upgrade…),
    # not just a warm tone. Without this the mode degrades into letting an LLM
    # mood score pick trades, which is the failure everyone means by "AI slop".
    news_driven_require_catalyst: bool = os.getenv("NEWS_DRIVEN_REQUIRE_CATALYST", "true").lower() in ("1", "true", "yes")
    # The rule score stops being the decider, so the bar it has to clear drops
    # — it is now only "is this tradeable at all" (liquidity, not-broken tape).
    # Negative = looser. The floor never goes below 50 (see news_driven.py).
    news_driven_threshold_delta: float = _float("NEWS_DRIVEN_THRESHOLD_DELTA", -10.0)
    # Score → size. min_score maps to 1.0x, 100 maps to this. Deliberately
    # wider than the advisory channel's 1.25 cap, because here the news is the
    # conviction, but still bounded by max_position_pct downstream.
    news_driven_max_mult: float = _float("NEWS_DRIVEN_MAX_MULT", 1.5)
    # Per-scan AI call budget for the news read. Higher than sentiment_budget
    # (8) because in this mode a name that doesn't get read cannot trade.
    news_driven_budget: int = _int("NEWS_DRIVEN_BUDGET", 12)
    # (No NEWS_DRIVEN_MODEL knob: ai.generate() has no model= parameter — every
    # layer runs the provider cascade in ai.model_cascade(). The *_MODEL
    # settings above are read by nobody today; adding a fourth dead one would
    # only tell the user they had control they don't have.)
    # 收盘平仓: flatten every bot-managed position before the close, so a
    # news-driven bet never carries overnight gap risk. Owner-requested and ON
    # by default WITH the mode (it is half of what was asked for), but separable
    # — set false to let the normal TP/SL bracket run its course instead.
    # NOTE: this caps the trade at one session, while the documented
    # post-news drift (PEAD) runs for days. That tradeoff is deliberate.
    news_driven_eod_flatten: bool = os.getenv("NEWS_DRIVEN_EOD_FLATTEN", "true").lower() in ("1", "true", "yes")
    # ET wall-clock time to flatten, HH:MM. Default 15:45 — inside RTH so the
    # order is a normal marketable exit, and past 15:30 only by enough to still
    # get out before the close auction (clock.py warns about the MOC zone).
    news_driven_flatten_et: str = os.getenv("NEWS_DRIVEN_FLATTEN_ET", "15:45")
    # Stop opening NEW positions this many minutes before the flatten. Without
    # it the scanner happily opens at 15:40 a position the flatten kills at
    # 15:45 — a round trip's cost and slippage for five minutes of exposure,
    # every day. Default 30 ⇒ last new entry 15:15 ET.
    news_driven_min_hold_min: int = _int("NEWS_DRIVEN_MIN_HOLD_MIN", 30)

    # ── Options flow (Phase 2D, 2026-06-23): unusual options activity ──
    # broker-style 期权异动: volume ≫ open-interest, put/call skew, OI-concentration
    # support/resistance (src/options_flow.py). BLOCKED until the account has US
    # options quote permission — the API denies the chain/snapshot otherwise. The
    # module degrades to neutral and is NOT wired into live paths yet; flip this on
    # only after subscribing, then 2A/2B can consume it. DEFAULT OFF.
    options_flow_enabled: bool = os.getenv("OPTIONS_FLOW_ENABLED", "false").lower() in ("1", "true", "yes")

    # ── Options AGGREGATE stats (2026-08-05, src/options_stats.py) ──
    # The chain above is blocked, but three underlying-level endpoints answer
    # without the options quote subscription — including one with 1y of daily
    # call/put volume. Measured over 5,980 name-days: call volume vs its own 20d
    # mean is MONOTONIC against forward return (rvol>=3 → 61.6% win / +3.058%
    # next-3-day vs a 53.8% / +1.240% baseline), while the put/call ratio is
    # FLAT — the skew carries no direction, only the volume does. Earnings days
    # must be excluded (39% of spikes sit in one, at 2x the volatility); doing so
    # IMPROVES the signal to 63.8% win / +2.559% on n=105.
    # DEFAULT OFF, and ADVISORY even when on: it is logged and shown but never
    # changes which trade fires, so live↔backtest parity holds. Only
    # OPTIONS_STATS_SIZING folds it into the conviction → position-size channel,
    # and even then it can only size UP a spike, never veto a quiet name.
    # CAVEAT the owner must weigh before arming: the broker caps history at 251
    # rows, so the whole sample is one bull market. This has never been observed
    # in a drawdown. See the module docstring for the full numbers and limits.
    options_stats_enabled: bool = os.getenv("OPTIONS_STATS_ENABLED", "false").lower() in ("1", "true", "yes")
    options_stats_sizing: bool = os.getenv("OPTIONS_STATS_SIZING", "false").lower() in ("1", "true", "yes")
    options_stats_min_rvol: float = _float("OPTIONS_STATS_MIN_RVOL", 2.0)
    options_stats_max_mult: float = _float("OPTIONS_STATS_MAX_MULT", 1.15)

    # ── API/subscription health watchdog (2026-06-23) ──
    # Edge-triggered Telegram alerts when a silent dependency lapses: the broker
    # options data subscription (can't fetch chains/snapshots) or the Gemini API
    # balance/quota (AI layers go blind). Owner-requested safety net → DEFAULT ON
    # (set HEALTH_CHECK_ENABLED=false to silence). Runs every interval minutes +
    # once at startup; only alerts on a state CHANGE, so it never spams.
    health_check_enabled: bool = os.getenv("HEALTH_CHECK_ENABLED", "true").lower() in ("1", "true", "yes")
    health_check_interval_min: int = _int("HEALTH_CHECK_INTERVAL_MIN", 30)

    # 3-tranche scale-out (2026-05-30 cash-frontier finding: banking partials and
    # recycling the cash is the best NO-LEVERAGE lever for a real $5k account —
    # it lifts cash-on $/day on both 180d & 360d while keeping MTM-DD < the halt).
    # OFF by default so live behaviour is identical until explicitly switched on.
    # When on, the soft manager closes 1/3 of the ORIGINAL qty at +TP1_R, another
    # 1/3 at +TP2_R, and trails the final ~1/3. R = entry − initial_stop
    # (= SL_ATR_MULT × ATR). NOTE: like the legacy TP_HALF, this runs on the
    # soft-management path (SIMULATE, or REAL when the OCO bracket attach failed);
    # in REAL with a live bracket the broker owns the single TP.
    use_scale_out: bool = os.getenv("USE_SCALE_OUT", "false").lower() in ("1", "true", "yes")
    tp1_r: float = _float("TP1_R", 3.0)
    tp2_r: float = _float("TP2_R", 6.0)

    # Breakeven stop (2026-06-11 exit audit): once price has been
    # +BREAKEVEN_TRIGGER_R × R in profit (R = entry − initial stop), ratchet
    # the stop up to entry — a former winner can no longer turn into a loser.
    # Engine-measured (dynamic top-15, honest lens): PnL-neutral on 140d
    # ($18.0 vs $18.8/day) but PF 3.15→3.44, MTM-DD 11.3%→9.8%, and the choppy
    # recent window flips −$8.5→+$37.8/day. 72% of trades reach +1R.
    use_breakeven_stop: bool = os.getenv("USE_BREAKEVEN_STOP", "true").lower() in ("1", "true", "yes")
    breakeven_trigger_r: float = _float("BREAKEVEN_TRIGGER_R", 1.0)

    # Stall-out (close positions that go nowhere for 3 business days).
    # 2026-06-11: DEFAULT OFF for exit parity — the validated engine has no
    # stall-out and its MAX_HOLD bucket is net positive; live stall-outs were
    # 2-for-2 losers. Turn on only after the engine models it and it passes
    # the dual-window gate.
    stall_out_enabled: bool = os.getenv("STALL_OUT_ENABLED", "false").lower() in ("1", "true", "yes")

    # ── Autopilot (2026-06-11): bounded autonomy for parameter changes ──
    # When ON, an optimizer proposal that PASSED the dual-window honest-engine
    # gate AND sits inside runtime_config.ALLOWED_PARAMS bounds is applied
    # immediately (db-state override, no restart) with a Telegram notification;
    # an auto-rollback watcher reverts it if live results degrade. When OFF
    # (default), every change still queues for owner approval.
    auto_apply_params: bool = os.getenv("AUTO_APPLY_PARAMS", "false").lower() in ("1", "true", "yes")

    # ── Phase 1 (2026-06-11): rule-based dynamic universe ──
    # Weekly: watchlist := top N of the liquidity pool (config/universe_pool.json)
    # by 6-1 momentum (src/universe.py). OFF by default — flip
    # DYNAMIC_UNIVERSE_ENABLED in .env to activate; every refresh that changes
    # the list is Telegram-notified (no silent universe drift).
    dynamic_universe_enabled: bool = os.getenv("DYNAMIC_UNIVERSE_ENABLED",
                                               "false").lower() in ("1", "true", "yes")
    universe_top_n: int = int(os.getenv("UNIVERSE_TOP_N", "15"))
    # Max names per sector bucket in the momentum-ranked universe (0 = off).
    # 2026-07-18: pure 6-1 momentum concentrated the whole watchlist into one
    # correlation cluster (15/15 semis/enterprise-hardware) and the book
    # crashed as one trade on 07-15. Diversification rule, not performance
    # curation — the pool stays liquidity-only.
    universe_sector_cap: int = int(os.getenv("UNIVERSE_SECTOR_CAP", "5"))
    # Reserve this many top-N slots for the pool's ETFs (0 = off, default).
    # 2026-08-05: momentum ranking is blind to whether the account can size what
    # it picks. At $10k with a 20% cap, one share of SNDK ($1,434) is 14.3% of
    # the book and the ATR share count floors to 1 or 0 — position size becomes
    # all-or-nothing and the risk model stops working. The sector funds carry
    # the same exposure at a share price the account can actually divide. This
    # fixes GRANULARITY, not signal: no claim is made that ETFs predict better.
    # Same replayable-rule status as universe_sector_cap — live and backtest
    # must pass the same value or they diverge.
    universe_etf_slots: int = int(os.getenv("UNIVERSE_ETF_SLOTS", "0"))
    # Hysteresis band for the universe: enter at rank<=UNIVERSE_TOP_N, leave only
    # once rank>UNIVERSE_EXIT_RANK. <=top_n disables it (pure top-N, the old
    # behaviour). 2026-08-06: measured over 40 sessions, refreshing DAILY with no
    # hysteresis cost 45 name-changes for 8 net rotations — 5.6x churn, with
    # names ping-ponging on consecutive days (BA/MS, CVX/MS, INTC/WDC) purely
    # because ranks 15 and 16 swapped on a day's move. Weekly got the same 8
    # rotations in 16 changes. The band buys daily responsiveness without buying
    # the boundary noise.
    # WARNING: non-zero makes selection PATH-DEPENDENT — live and any backtest
    # must thread the same previous set forward or they silently diverge.
    universe_exit_rank: int = int(os.getenv("UNIVERSE_EXIT_RANK", "0"))
    # How often the live watchlist is recomputed: "weekly" (Mon) or "daily".
    universe_refresh_freq: str = os.getenv("UNIVERSE_REFRESH_FREQ", "weekly").strip().lower()

    # Drawdown circuit breaker — discovered from the 142-day backtest where
    # Nov 2025 alone lost -$761 (17% of account) before the strategy recovered.
    # When account-level DD breaches these thresholds, we either halve qty or
    # halt new entries entirely until equity recovers above the soft-cut line.
    dd_size_cut_pct: float = _float("DD_SIZE_CUT_PCT", 10.0)   # half qty
    dd_halt_pct: float = _float("DD_HALT_PCT", 15.0)           # no new entries

    # ── Auto-compounding budget (2026-06-26): "money making more money" ──
    # Grow the deployable budget as the bot's realized equity rises above the
    # line where compounding was armed, and shrink it in drawdown — so a
    # validated daily edge compounds GEOMETRICALLY instead of staying linear.
    # The DD breaker is unaffected: while armed, risk_manager.equity_baseline()
    # anchors equity to the FROZEN seed, never the compounding budget (see
    # src/auto_budget.py). DEFAULT OFF — flip on (or use the web toggle) to arm;
    # arming is neutral (target == seed until new profit is earned). The runtime
    # db-state toggle 'auto_budget_enabled' wins over this .env default.
    auto_budget_enabled: bool = os.getenv("AUTO_BUDGET_ENABLED", "false").lower() in ("1", "true", "yes")
    # Fraction of bot-earned profit folded into the base. 1.0 = full reinvest
    # (symmetric: +$1 earned grows the base $1, −$1 lost shrinks it $1 — "press
    # winners, cut size when losing"). <1.0 banks the rest as a safety cushion.
    auto_budget_reinvest_frac: float = _float("AUTO_BUDGET_REINVEST_FRAC", 1.0)
    # Guardrails relative to the seed: never de-risk below seed×MIN_FRAC (the
    # give-back floor), never run away above seed×MAX_MULT. Live account equity
    # is an additional hard cap (can't deploy money that isn't there).
    auto_budget_min_frac: float = _float("AUTO_BUDGET_MIN_FRAC", 0.5)
    auto_budget_max_mult: float = _float("AUTO_BUDGET_MAX_MULT", 5.0)
    # Hysteresis: only move the budget when the change clears max($STEP, PCT×cur)
    # — stops daily churn (and Telegram spam) on noise.
    auto_budget_min_step_usd: float = _float("AUTO_BUDGET_MIN_STEP_USD", 250.0)
    auto_budget_min_step_pct: float = _float("AUTO_BUDGET_MIN_STEP_PCT", 0.05)

    # ── Bear-market cash-yield sweep (2026-06-26): "petty money in bad markets" ──
    # When the regime is BEAR (strategy paused), park idle cash in a T-bill ETF
    # (SGOV ~4-5%/yr, near-zero risk) instead of earning 0%, and unwind back to
    # cash when the regime is tradeable again. Conservative by default: ONLY
    # sweeps in BEAR (so it can't starve a real entry of cash) and keeps a liquid
    # buffer. DEFAULT OFF. Runtime db-state 'cash_yield_enabled' wins over .env.
    cash_yield_enabled: bool = os.getenv("CASH_YIELD_ENABLED", "false").lower() in ("1", "true", "yes")
    cash_yield_symbol: str = os.getenv("CASH_YIELD_SYMBOL", "SGOV").strip().upper()
    cash_yield_only_bear: bool = os.getenv("CASH_YIELD_ONLY_BEAR", "true").lower() in ("1", "true", "yes")
    cash_yield_buffer_usd: float = _float("CASH_YIELD_BUFFER_USD", 500.0)     # keep this much liquid
    cash_yield_min_trade_usd: float = _float("CASH_YIELD_MIN_TRADE_USD", 500.0)  # ignore dust sweeps

    # ── Inverse-ETF sleeve (2026-06-26): profit from confirmed downtrends ──
    # Cash account can't short → buy an inverse ETF (SH −1x, or SQQQ −3x) when a
    # downtrend is CONFIRMED (BEAR regime + inverse ETF above its own SMA), exit
    # when trend/regime ends or stop/TP/max-hold. A small SLEEVE, capped at
    # MAX_PCT of budget. ⚠ NOT VALIDATED — DEFAULT OFF; only the owner enables it
    # after scripts/inverse_sleeve_backtest.py clears the dual-window gate.
    inverse_sleeve_enabled: bool = os.getenv("INVERSE_SLEEVE_ENABLED", "false").lower() in ("1", "true", "yes")
    inverse_sleeve_symbol: str = os.getenv("INVERSE_SLEEVE_SYMBOL", "SH").strip().upper()
    inverse_sleeve_max_pct: float = _float("INVERSE_SLEEVE_MAX_PCT", 0.25)   # ≤ this fraction of budget
    inverse_sleeve_sl_atr: float = _float("INVERSE_SLEEVE_SL_ATR", 2.5)
    inverse_sleeve_tp_atr: float = _float("INVERSE_SLEEVE_TP_ATR", 6.0)
    inverse_sleeve_sma_len: int = _int("INVERSE_SLEEVE_SMA_LEN", 20)
    inverse_sleeve_atr_len: int = _int("INVERSE_SLEEVE_ATR_LEN", 14)
    inverse_sleeve_max_hold_days: int = _int("INVERSE_SLEEVE_MAX_HOLD_DAYS", 30)

    # ── Smart regime sensing (2026-06-26) ──
    # The richer regime telemetry (vix/strength/sub_label/confirmed_label) is
    # ALWAYS computed (harmless). This flag controls the one BEHAVIOR change:
    # when true, entry-block + cash-yield + inverse-sleeve gate on the HYSTERESIS-
    # smoothed `confirmed` label instead of the raw label, cutting 1-day whipsaw
    # around the 200-MA. DEFAULT OFF — validate with scripts/regime_diagnose.py
    # first. (The advisory risk_mult is never auto-applied — VIX sizing already
    # de-risks, so applying it too would double-count.)
    smart_regime_enabled: bool = os.getenv("SMART_REGIME_ENABLED", "false").lower() in ("1", "true", "yes")

    # ── Market breadth filter (P0-3, 2026-06-26) ──
    # When true, an unhealthy breadth reading BLOCKS new entries (advisory
    # otherwise — logs + notifies, but doesn't block). DEFAULT OFF (advisory
    # only) while the owner validates the Spy-proxy fallback against real
    # market snaps. Set true to arm the hard filter.
    breadth_blocking: bool = os.getenv("BREADTH_BLOCKING", "false").lower() in ("1", "true", "yes")

    # ── AI ensemble voting (P1-1, 2026-06-26) ──
    # When true AND both Gemini + DeepSeek keys are configured, the AI validator
    # calls BOTH providers in parallel. Consensus = high-confidence pass/veto;
    # conflict = pass with reduced confidence. Off by default (uses single
    # provider) — turn on after confirming both keys work.
    ai_ensemble_enabled: bool = os.getenv("AI_ENSEMBLE_ENABLED", "false").lower() in ("1", "true", "yes")

    # (ML alpha engine removed 2026-06-03 — proven inert, AUC ~0.5.)

    # 2026-05-29 combo-sweep finding: mean-revert strategy was net-negative in
    # current bull-skewed watchlist (cost ~$4/day vs trend+momentum only).
    # Disable by default. Set MR_ENABLED=true to re-enable for sideways regimes.
    mr_enabled: bool = os.getenv("MR_ENABLED", "false").lower() in ("1", "true", "yes")

    # ── Pattern strategy (2026-06-22): chart-pattern recognition ──
    # Fourth strategy (src/strategy_pattern.py) — geometric + candlestick
    # patterns (double bottom, triangles, inverse H&S, wedges, flags, breakout)
    # scored on the same 0-100 scale and fed through the same funnel. DEFAULT OFF
    # (inert) — same discipline as mr_enabled / gap_sentinel: flip on ONLY after
    # backtest_v3 validates an edge on the dual-window gate.
    pattern_enabled: bool = os.getenv("PATTERN_ENABLED", "false").lower() in ("1", "true", "yes")
    # Admission filters (2026-06-23): the unfiltered set was net-negative on the
    # 180d/10-semis backtest ($16.4→$8.7/day, maxDD 5.8%→17%), dominated by weak
    # breakouts catching false tops. These narrow WHICH detections may become
    # entries. Defaults are inert (all types, no min, triggered-optional) so the
    # raw behaviour is unchanged until set. PATTERN_ALLOWED_TYPES is a csv of
    # detector type names (e.g. "double_bottom,ascending_triangle"); empty = all.
    pattern_min_confidence: float = _float("PATTERN_MIN_CONFIDENCE", 0)
    pattern_require_triggered: bool = os.getenv("PATTERN_REQUIRE_TRIGGERED", "false").lower() in ("1", "true", "yes")
    pattern_allowed_types: tuple = tuple(
        t.strip() for t in os.getenv("PATTERN_ALLOWED_TYPES", "").split(",") if t.strip()
    )
    # AI vision confirmation (src/pattern_vision.py) — render the candle chart and
    # ask Gemini to confirm the algo-detected pattern. The "AI" half of the
    # algorithm+vision design. OFF by default; live-only (skipped in backtest) and
    # FAIL-SAFE (vision unavailable → pass), so it never silently kills a signal.
    pattern_vision_enabled: bool = os.getenv("PATTERN_VISION_ENABLED", "false").lower() in ("1", "true", "yes")
    # When true, a high-confidence vision 'reject' BLOCKS the entry. Default false
    # = advisory (logged + shown in the buy card) so live ↔ backtest signal sets
    # stay aligned (same reasoning as ai_veto_blocking below).
    pattern_vision_blocking: bool = os.getenv("PATTERN_VISION_BLOCKING", "false").lower() in ("1", "true", "yes")
    # A vision 'reject' only blocks if its confidence ≥ this (avoids killing
    # entries on a low-conviction maybe). Only used when pattern_vision_blocking.
    pattern_vision_reject_conf: int = _int("PATTERN_VISION_REJECT_CONF", 60)
    # Vision model — owner wants Gemini ≥ 3.5-flash everywhere (no lite tiers), so
    # this defaults to gemini-3.5-flash (multimodal; verified to read the rendered
    # candle chart). A per-scan call budget keeps cost bounded.
    pattern_vision_model: str = os.getenv("PATTERN_VISION_MODEL", "gemini-3.5-flash")
    pattern_vision_budget: int = _int("PATTERN_VISION_BUDGET", 8)

    # Fidelity fix (2026-06-03): when REAL, use the SAME soft-managed exits as
    # SIMULATE (scale-out + trailing + soft stop) instead of a broker OCO
    # bracket. This closes the backtest↔live gap (the honest engine models
    # scale-out, which the REAL bracket path didn't do). Tradeoff: no broker-side
    # hard stop if the process dies (same as SIMULATE today). Default OFF —
    # verify on a small REAL position before enabling.
    real_use_soft_exits: bool = os.getenv("REAL_USE_SOFT_EXITS", "false").lower() in ("1", "true", "yes")

    # Live↔backtest parity (2026-06-03): the honest backtest does NOT model the
    # Gemini AI veto, so leaving it BLOCKING makes live take different trades than
    # the backtest that the $/day figure is based on. Default False = AI runs as
    # advisory (logged + shown in the buy card) but never blocks an entry, so the
    # set of trades matches the backtest. Set true to let AI veto block again.
    ai_veto_blocking: bool = os.getenv("AI_VETO_BLOCKING", "false").lower() in ("1", "true", "yes")

    root: Path = ROOT


settings = Settings()

# ── Scale-out guard (extracted 2026-07-06) ──────────────────
# With R = sl_atr_mult × ATR, the first scale-out partial sits at
# tp1_r × sl_atr_mult ATR above entry. If that is at/beyond the full TP
# (tp_atr_mult × ATR), the whole position always closes first and
# USE_SCALE_OUT=true is a silent no-op. This function is callable from
# config.py (module load, static values) AND from runtime_config.set_param()
# (after a DB override changes sl/tp — handles the circular-import problem).
# Returns True if scale-out was disabled by this check.

def _check_scale_out(sl: float, tp: float, tp1=None) -> bool:
    """Return True if scale-out was silently disabled due to TP1≥TP."""
    if not settings.use_scale_out:
        return False
    _tp1 = tp1 if tp1 is not None else settings.tp1_r
    if _tp1 * sl >= tp:
        logging.getLogger(__name__).warning(
            "USE_SCALE_OUT=true but TP1 (%.1fR × %.1f ATR/R = +%.1f ATR) is at/"
            "beyond the full TP (+%.1f ATR) — scale-out can never fire; treating "
            "as DISABLED. Lower TP1_R/TP2_R or raise TP_ATR_MULT to activate it.",
            _tp1, sl, _tp1 * sl, tp)
        object.__setattr__(settings, "use_scale_out", False)
        return True
    return False


def recheck_scale_out(sl: float, tp: float) -> bool:
    """Runtime re-check of the scale-out guard — call after any SL/TP change.
    Uses current runtime values (not static .env), solving the circular-import
    problem where the module-level guard in config.py can't import runtime_config.
    Returns True if scale-out was disabled."""
    return _check_scale_out(float(sl), float(tp))


# Module-load check with static .env values (first line of defense)
_check_scale_out(settings.sl_atr_mult, settings.tp_atr_mult)


def derive_max_positions(capital: float) -> int:
    """Position-slot cap derived from allocated capital, so changing the budget
    recomputes how many concurrent names the bot may hold (req#1) instead of a
    hardcoded number. `max_positions` is the floor (never fewer slots than today);
    slots scale by capital / SLOT_TARGET_USD up to `max_positions_cap` (≈ the
    watchlist size, to avoid over-diversification). At the $4.5k default the raw
    slot math gives round(4500/1000)=4, which the max_positions floor clamps back
    up to 5 — so this returns exactly settings.max_positions and live + backtest
    behaviour is unchanged until capital grows past ~$5.5k. The cash wall stays
    binding at small budgets."""
    slot = settings.slot_target_usd
    if slot <= 0:
        return settings.max_positions
    n = round(capital / slot)
    return max(settings.max_positions, min(settings.max_positions_cap, n))

# Model cascade: GEMINI_MODEL is tried first; on 429/quota it retries across all
# keys. Owner preference (2026-06-22): use Gemini 3.5-flash or HIGHER everywhere —
# NO lite-tier fallback — so the cascade floor is gemini-3.5-flash. Override the
# starting model via the GEMINI_MODEL env var (set it to a higher tier if one
# exists; do not point it at a *-lite model).
GEMINI_FREE_CASCADE = [
    "gemini-3.5-flash",
]


def gemini_cascade() -> list[str]:
    """Full cascade starting from the configured model, deduped."""
    primary = settings.gemini_model or "gemini-3.5-flash"
    seen: set[str] = set()
    result = []
    for m in [primary] + GEMINI_FREE_CASCADE:
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result
