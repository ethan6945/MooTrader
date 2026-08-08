"""Gemini-based contextual check, augmented with real Tavily news.

The technical scorer can be fooled by:
  - Earnings announcements 24h away (volatility trap)
  - Sector-wide bad news the indicators haven't priced yet
  - Macro events (FOMC, CPI release)

We fetch live news (ticker-specific + macro) via Tavily, feed it to Gemini,
and parse a JSON verdict. A "veto" zeros the AI sub-score, a "pass" awards
the remaining 10 points.
"""
from __future__ import annotations

import json
import logging
import re

from .indicators import Signal
from . import ai, news_fetcher

log = logging.getLogger(__name__)


def _extract_json(text: str) -> dict:
    """First JSON object in an AI reply, tolerant of the three malformations
    deepseek produces in the wild (all seen 2026-07-15, each one a fail-open):
    markdown fences, trailing text/second object after the verdict ("Extra
    data"), and raw control chars inside strings ("Invalid control character").
    """
    start = text.find("{")
    if start < 0:
        raise ValueError(f"no JSON object in AI reply: {text[:80]!r}")
    obj, _ = json.JSONDecoder(strict=False).raw_decode(text[start:])
    if not isinstance(obj, dict):
        raise ValueError("AI reply JSON is not an object")
    return obj


PROMPT = """You are a risk gate for an automated short-term US-stock trading bot.

The technical scorer wants to BUY {symbol} at ${price}. Indicator breakdown:
{reasons}

Recent news (last 3 days):
{ticker_news}

Macro / market context:
{macro_news}

Using the news above, check ONLY context the indicators cannot see:
1. Is there an earnings release within the next 2 trading days?
2. Is there scheduled macro news (FOMC, CPI, jobs) that would dominate price?
3. Is the sector under fresh negative pressure (tariffs, regulation, downgrades)?
4. Is there a known overnight gap risk (lawsuits, guidance cut, analyst downgrade)?

Respond with STRICT JSON, no markdown fence:
{{"verdict": "pass" | "veto", "confidence": 0-100, "reason": "one sentence citing the specific news headline if any"}}

If you have no concerning info, default to "pass".
"""


def validate(signal: Signal) -> tuple[bool, int, str]:
    """Returns (pass, ai_sub_score_0_100, reason). On error, defaults to pass.

    P1-1 (2026-06-26): when AI_ENSEMBLE_ENABLED and both providers have keys,
    uses ensemble voting (Gemini + DeepSeek in parallel). Consensus = high
    confidence pass/veto; conflict = pass with 30% confidence (conservative).
    Single-provider fallback when only one has keys.
    """
    from .config import settings as _s

    if not ai.has_key():
        return True, 50, "no AI key configured — neutral"

    ticker_news = news_fetcher.format_news(
        news_fetcher.fetch_ticker_news(signal.symbol), "Ticker news"
    )
    macro_news = news_fetcher.format_news(
        news_fetcher.fetch_macro_news(), "Macro"
    )
    prompt = PROMPT.format(
        symbol=signal.symbol,
        price=signal.price,
        reasons="\n".join(f"- {r}" for r in signal.reasons),
        ticker_news=ticker_news,
        macro_news=macro_news,
    )

    try:
        # P1-1: ensemble mode when both providers have keys and flag is on
        if _s.ai_ensemble_enabled and ai.has_key("gemini") and ai.has_key("deepseek"):
            result = ai.generate_ensemble(prompt)
            ens_verdict = result["verdict"]
            if ens_verdict == "consensus":
                text = result["text"]
                model_name = f"ensemble({result['gemini']['model']}+{result['deepseek']['model']})"
            elif ens_verdict in ("single", "conflict"):
                text = result["text"]
                src = result["gemini"] or result["deepseek"] or {}
                model_name = f"ensemble-{ens_verdict}({src.get('model','?')})"
            else:  # unavailable
                return True, 50, "ensemble unavailable — neutral"
        else:
            text, model_name = ai.generate(prompt)

        payload = _extract_json(text)
        verdict = payload.get("verdict", "pass")
        confidence = int(payload.get("confidence", 50))
        reason = payload.get("reason", "")
        is_pass = verdict == "pass"
        sub_score = confidence if is_pass else 0
        log.info("AI validator [%s] %s → %s (%d)",
                 model_name, signal.symbol, verdict, confidence)
        return is_pass, sub_score, reason
    except Exception as e:
        log.warning("AI validator unavailable for %s (%s) — defaulting to pass",
                    signal.symbol, e)
        return True, 50, "AI unavailable — neutral"


GAP_PROMPT = """You are an EXIT risk gate for an automated US-stock trading bot.

The bot currently HOLDS {symbol}. We want to avoid carrying it into a likely
GAP-DOWN at the next session (an overnight gap fills past our stop-loss, so the
stop can't protect us — the only defense is to exit NOW, during regular hours,
while there is still liquidity).

Recent news (last 3 days):
{ticker_news}

Macro / market context:
{macro_news}

Judge ONLY concrete, already-PUBLIC catalysts that raise overnight gap-DOWN risk:
- a fresh analyst downgrade, price-target cut, or sell rating
- a lawsuit, SEC action, fraud/accounting probe, or guidance cut
- sector-wide bad news (tariffs, regulation, a peer's blow-up) hitting tonight
- a scheduled binding event tonight/tomorrow that reads clearly negative
Do NOT try to predict an earnings NUMBER — you cannot, and an unknown coin-flip
is NOT a reason to sell. Sell ONLY on concrete negative news above.

Respond with STRICT JSON, no markdown fence:
{{"action": "sell" | "hold", "confidence": 0-100, "reason": "one sentence citing the specific headline; if holding, say why", "evidence_ticker": "ticker of the company whose price move your reason relies on, or null", "evidence_move_pct": claimed % price move of that company (negative = drop), or null}}

If your reason relies on a specific price move (e.g. "XYZ plunged 23%"), you MUST
fill evidence_ticker and evidence_move_pct — the bot verifies the claim against
real price data and IGNORES sell verdicts whose claimed move did not happen.
If you have no concrete negative catalyst, default to "hold".
"""

# A sell verdict claiming a peer/self price move ≥ this magnitude gets checked
# against actual price data before we act on it. (2026-07-15: deepseek closed
# DELL twice citing "IBM's 23% plunge" — IBM's biggest move that week was -2.6%.)
CLAIM_VERIFY_MIN_PCT = 5.0


def _verify_price_claim(ticker: str, claimed_pct: float) -> tuple[bool, str]:
    """Check a claimed % move against real daily data (last ~4 trading days).

    Returns (verified, detail). Fabricated OR unverifiable (bad ticker, no
    data, fetch error) → (False, why) — caller holds, per the sentinel's
    fail-safe policy: we never sell a real position on an unverifiable claim.
    """
    ticker = str(ticker).strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker):
        return False, f"un-checkable ticker {ticker!r}"
    try:
        import yfinance as yf
        h = yf.Ticker(ticker).history(period="7d", interval="1d", auto_adjust=False)
    except Exception as e:
        return False, f"price fetch for {ticker} failed ({e})"
    if h is None or len(h) < 2:
        return False, f"no recent price data for {ticker}"
    closes = h["Close"].tail(5)
    day_moves = (closes.pct_change().dropna().abs() * 100).tolist()
    intraday = ((h["Close"] - h["Open"]) / h["Open"]).tail(4).abs() * 100
    max_actual = max(day_moves + intraday.tolist(), default=0.0)
    # Generous bar on purpose: the move only has to be half the claimed size —
    # we're catching fabrications, not grading the model's precision.
    if max_actual >= abs(claimed_pct) / 2:
        return True, f"{ticker} actual max move {max_actual:.1f}% supports claim"
    return False, (f"{ticker} claimed {claimed_pct:+.1f}% but actual max move "
                   f"over recent sessions is {max_actual:.1f}%")


def _coerce_pct(value) -> float | None:
    """'−23%', '-23', -23.0 → -23.0; garbage → None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        s = str(value).strip().replace("%", "").replace("−", "-")
        return float(s)
    except (ValueError, TypeError):
        return None


def _check_sell_evidence(symbol: str, payload: dict, model_name: str) -> tuple[bool, str]:
    """Gate a SELL verdict on its own cited evidence. Only quantitative price
    claims are checkable; a sell citing one that fails (or can't) verify is
    downgraded to hold — fail-safe, same policy as every other doubt path."""
    ticker = payload.get("evidence_ticker")
    claimed = _coerce_pct(payload.get("evidence_move_pct"))
    if not ticker or claimed is None or abs(claimed) < CLAIM_VERIFY_MIN_PCT:
        return True, ""   # no checkable quantitative claim — let the verdict stand
    ok, detail = _verify_price_claim(ticker, claimed)
    if ok:
        log.info("gap sentinel evidence verified for %s: %s", symbol, detail)
        return True, ""
    log.warning("gap sentinel [%s] SELL on %s REJECTED — claim failed "
                "verification: %s", model_name, symbol, detail)
    return False, f"sell verdict rejected (unverified claim: {detail}) — hold"


def assess_gap_risk(symbol: str) -> tuple[bool, int, str]:
    """EXIT-side check for a HELD name: is there concrete public bad news raising
    overnight gap-DOWN risk? Returns (should_sell, confidence_0_100, reason).

    FAIL-SAFE: any missing key / quota / error / malformed reply → (False, 0, ...)
    i.e. HOLD. We never liquidate a real position on AI doubt — only on a clear,
    high-confidence 'sell' verdict citing concrete negative news.
    """
    if not ai.has_key():
        return False, 0, "no AI key — sentinel AI layer inert (hold)"

    # Cost control: skip the AI call entirely when there is no fresh ticker
    # news — no news ⇒ no concrete catalyst ⇒ hold, for free.
    try:
        news_items = news_fetcher.fetch_ticker_news(symbol)
    except Exception as e:
        return False, 0, f"news fetch failed ({e}) — hold"
    if not news_items:
        return False, 0, "no fresh ticker news — hold (no AI call)"

    try:
        ticker_news = news_fetcher.format_news(news_items, "Ticker news")
        macro_news = news_fetcher.format_news(news_fetcher.fetch_macro_news(), "Macro")
    except Exception as e:
        return False, 0, f"news format failed ({e}) — hold"
    prompt = GAP_PROMPT.format(symbol=symbol, ticker_news=ticker_news, macro_news=macro_news)

    try:
        text, model_name = ai.generate(prompt)
        payload = _extract_json(text)
        action = str(payload.get("action", "hold")).lower()
        confidence = int(payload.get("confidence", 0))
        reason = payload.get("reason", "")
        should_sell = action == "sell"
        log.info("gap sentinel [%s] %s → %s (%d)", model_name, symbol, action, confidence)
        if should_sell:
            ok, detail = _check_sell_evidence(symbol, payload, model_name)
            if not ok:
                return False, 0, detail
        return should_sell, confidence, reason
    except Exception as e:
        # FAIL-SAFE hold (never sell on AI unavailability).
        log.warning("gap sentinel: AI unavailable for %s (%s) — holding (fail-safe)", symbol, e)
        return False, 0, "AI unavailable — hold (fail-safe)"


EXIT_PROMPT = """You are an EXIT risk gate for an automated, LONG-ONLY US-stock
trading bot. The bot HOLDS {symbol} (entry ${entry}, now ${last}, unrealized
{r_now:+.1f}R). It wants to decide whether to EXIT NOW (during regular hours)
instead of waiting for the price target — to LOCK IN a gain that is about to
reverse, or to cut before a known bearish catalyst plays out.

Current technical picture (computed, not opinion):
{tech_summary}

Recent news (last 3 days):
{ticker_news}

Macro / market context:
{macro_news}

Decide SELL only on a CONCRETE, already-PUBLIC bearish development the indicators
can't fully price, e.g.:
- a fresh analyst downgrade, price-target CUT, or sell rating
- bearish research, a guidance cut, lawsuit, SEC action, or accounting probe
- sector-wide bad news (a peer's blow-up, tariffs, regulation) hitting this name
- a clear distribution/reversal signature confirmed by the technicals above
Do NOT sell on vague unease, normal volatility, or to "take profit early" without
a real catalyst. If the position is in profit AND a concrete catalyst threatens
it, SELL to bank the gain. If nothing concrete, HOLD.

Respond with STRICT JSON, no markdown fence:
{{"action": "sell" | "hold", "confidence": 0-100, "reason": "one sentence citing the specific catalyst/technical; if holding, say why", "evidence_ticker": "ticker of the company whose price move your reason relies on, or null", "evidence_move_pct": claimed % price move of that company (negative = drop), or null}}

If your reason relies on a specific price move (e.g. "XYZ plunged 23%"), you MUST
fill evidence_ticker and evidence_move_pct — the bot verifies the claim against
real price data and IGNORES sell verdicts whose claimed move did not happen.
If you have no concrete reason, default to "hold".
"""


def assess_exit(symbol: str, tech_summary: str, entry: float = 0.0,
                last: float = 0.0, r_now: float = 0.0) -> tuple[bool, int, str]:
    """Phase 2A smart-exit AI judge for a HELD long. Broader than assess_gap_risk:
    weighs concrete bearish news + the supplied technical state to decide whether
    to exit now (lock profit / cut on catalyst). Returns (should_sell, conf, reason).

    Same machinery + FAIL-SAFE policy as assess_gap_risk: no key / no fresh news /
    quota / error → (False, 0, ...) i.e. HOLD. We never liquidate on AI doubt.
    Uses the dedicated smart_exit_model (≥3.5-flash), one fixed model, key-cycled.
    """
    if not ai.has_key():
        return False, 0, "no AI key — smart-exit AI inert (hold)"

    # Cost control: no fresh ticker news ⇒ no concrete catalyst ⇒ hold, for free.
    try:
        news_items = news_fetcher.fetch_ticker_news(symbol)
    except Exception as e:
        return False, 0, f"news fetch failed ({e}) — hold"
    if not news_items:
        return False, 0, "no fresh ticker news — hold (no AI call)"

    try:
        ticker_news = news_fetcher.format_news(news_items, "Ticker news")
        macro_news = news_fetcher.format_news(news_fetcher.fetch_macro_news(), "Macro")
    except Exception as e:
        return False, 0, f"news format failed ({e}) — hold"
    prompt = EXIT_PROMPT.format(symbol=symbol, entry=entry, last=last, r_now=r_now,
                                tech_summary=tech_summary,
                                ticker_news=ticker_news, macro_news=macro_news)

    try:
        text, model_name = ai.generate(prompt)
        payload = _extract_json(text)
        action = str(payload.get("action", "hold")).lower()
        confidence = int(payload.get("confidence", 0))
        reason = payload.get("reason", "")
        should_sell = action == "sell"
        log.info("smart-exit [%s] %s → %s (%d)", model_name, symbol, action, confidence)
        if should_sell:
            ok, detail = _check_sell_evidence(symbol, payload, model_name)
            if not ok:
                return False, 0, detail
        return should_sell, confidence, reason
    except Exception as e:
        log.warning("smart-exit: AI unavailable for %s (%s) — holding (fail-safe)", symbol, e)
        return False, 0, "AI unavailable — hold (fail-safe)"


SENTIMENT_PROMPT = """You are a multi-factor analyst for a LONG-ONLY US-stock
trading bot — produce a broker-style 看好/中性/看空 read on BUY candidate {symbol}
(now ${price}).

Technical signals the bot already computed:
{reasons}

Recent news (last 3 days):
{ticker_news}

Macro / market context:
{macro_news}

Options flow (期权异动 — empty if unavailable):
{options_flow}

Weigh the factors and give an overall 0-100 BULLISHNESS score (50 = neutral,
>50 = bullish, <50 = bearish):
- news: fresh catalysts, partnerships, guidance
- analyst_target: direction of analyst rating / price-target changes
- technical: do the computed signals above confirm or contradict a long?
- options: does the options positioning (puts vs calls, unusual volume) lean
  bullish or bearish?

Respond with STRICT JSON, no markdown fence:
{{"verdict": "bullish" | "neutral" | "bearish", "score": 0-100,
  "factors": {{"news": 0-100, "analyst_target": 0-100, "technical": 0-100}},
  "reason": "one short sentence"}}

If you have no strong view, return neutral with score ~50.
"""


NEWS_DRIVEN_PROMPT = """You are a NEWS analyst for a LONG-ONLY US-stock trading
bot that is running in NEWS-DRIVEN mode: the news is the thesis, not a
tiebreaker. The bot will hold {symbol} (now ${price}) for ONE session only and
flatten before the close, so only catalysts that can move the stock TODAY
count.

Right now it is {now_et} US/Eastern. Each headline below is prefixed with its
publication timestamp — use them, they are the difference between a catalyst
and a recap.

Recent news:
{ticker_news}

Macro / market context:
{macro_news}

SEC filings by this company (PRIMARY SOURCE — the filing IS the event, filed by
the issuer with an exact timestamp. Trust these over any press write-up that
disagrees with them. "(none in the window)" means no material filing, which is
normal and is NOT evidence against a catalyst reported elsewhere; "(disabled)"
means we did not look):
{filings}

Technical signals the bot computed (CONTEXT ONLY — do not let a pretty chart
raise the score; this mode is betting on the news):
{reasons}

Score 0-100 how bullish the NEWS is for a same-session long (50 = neutral).

A "concrete catalyst" means a specific, named, dated corporate or macro event:
raised guidance, an earnings beat, a signed contract or partnership, an analyst
upgrade or price-target raise, a product launch, a regulatory approval, an
index inclusion. It is NOT: generic optimism, a "stock to watch" listicle, a
price-move recap ("shares rose 4%"), an analyst merely reiterating, or your own
inference from the chart. If the only item is a recap of a move that ALREADY
happened, has_catalyst is false — the move is already in the price.

Be strict. Returning has_catalyst=false with a neutral score is the correct
answer most days, and this bot is built to sit out.

Respond with STRICT JSON, no markdown fence:
{{"verdict": "bullish" | "neutral" | "bearish", "score": 0-100,
  "has_catalyst": true | false,
  "catalyst": "the specific event in <=10 words, or empty string",
  "stale": true | false,
  "reason": "one short sentence"}}

Set stale=true if the catalyst is real but its timestamp is old enough that the
stock has plainly already reacted to it.
"""


def finbert_crosscheck(symbol: str) -> tuple[int | None, str]:
    """Deterministic second opinion on the SAME headlines the LLM just read.

    None when FinBERT is off or unavailable — deliberately not a neutral 50, so
    "FinBERT saw nothing bullish" stays distinguishable from "FinBERT did not
    run". Advisory only; nothing consumes this to gate a trade.
    """
    try:
        from . import news_score_local
        if not news_score_local.enabled():
            return None, "off"
        # Hits news_fetcher's 30-min cache, so this costs no extra API call.
        return news_score_local.score_news(news_fetcher.fetch_ticker_news(symbol))
    except Exception as e:
        log.warning("FinBERT cross-check failed for %s: %s", symbol, e)
        return None, f"error: {e}"


def assess_news(signal) -> tuple[str, int, bool, str, bool]:
    """News-driven read for a BUY candidate. Returns
    (verdict, score_0_100, has_catalyst, reason, ok).

    `ok` is False whenever the verdict is NOT a real AI read — no key, no news,
    quota, parse error. The caller (news_driven.gate) refuses to trade on
    ok=False. This is the opposite of assess_sentiment's fail-safe, and it is
    deliberate: in news-driven mode the news IS the thesis, so "we couldn't
    read the news" must mean "no trade", never "fall back to technicals".

    A catalyst the model marks `stale` is downgraded to has_catalyst=False —
    a move the market already made is not a reason to enter now.
    """
    if not ai.has_key():
        return "neutral", 50, False, "no AI key — news-driven cannot trade", False

    try:
        raw_ticker = news_fetcher.fetch_ticker_news(signal.symbol)
        ticker_news = news_fetcher.format_news(raw_ticker, "Ticker news")
        macro_news = news_fetcher.format_news(news_fetcher.fetch_macro_news(), "Macro")
    except Exception as e:
        return "neutral", 50, False, f"news fetch failed ({e})", False

    # Filings and analyst actions are additive and best-effort: a failure here
    # degrades the read back to Tavily-only, which is what this did before the
    # source existed. It must never be able to block a news read.
    raw_filings: list = []
    filings = "(disabled)"
    try:
        from . import moo_notices
        if moo_notices.enabled():
            found = moo_notices.fetch_filings(signal.symbol)
            ratings = moo_notices.fetch_ratings(signal.symbol)
            # Ratings count as material for the "is there anything at all" test
            # below: an upgrade with no press write-up yet is exactly the early
            # catalyst this mode exists to catch.
            raw_filings = found + ratings
            filings = moo_notices.format_block(found, ratings)
    except Exception as e:
        log.warning("moomoo notices lookup failed for %s: %s — continuing without it",
                    signal.symbol, e)
        filings = "(lookup failed)"

    if not raw_ticker and not raw_filings:
        # Nothing from either source: no TAVILY_API_KEY, or genuinely nothing
        # published and nothing filed. Either way there is no thesis to act on,
        # and asking the model anyway invites it to invent one from the ticker.
        # Note the AND — an 8-K with no press coverage yet is the BEST case this
        # mode can see (the primary source, before the write-ups), so a silent
        # Tavily must not veto it.
        return "neutral", 50, False, "no ticker news and no material filings", False

    try:
        from . import clock
        now_et = f"{clock.ny_now():%Y-%m-%d %H:%M}"
    except Exception:
        now_et = "(unknown)"
    prompt = NEWS_DRIVEN_PROMPT.format(
        symbol=signal.symbol, price=signal.price, now_et=now_et,
        ticker_news=ticker_news, macro_news=macro_news, filings=filings,
        reasons="\n".join(f"- {r}" for r in signal.reasons))

    try:
        text, model_name = ai.generate(prompt)
        payload = _extract_json(text)
        verdict = str(payload.get("verdict", "neutral")).lower()
        score = int(payload.get("score", 50))
        has_catalyst = bool(payload.get("has_catalyst", False))
        catalyst = str(payload.get("catalyst", "")).strip()
        stale = bool(payload.get("stale", False))
        reason = str(payload.get("reason", ""))
        if has_catalyst and stale:
            has_catalyst = False
            reason = f"catalyst already priced in ({catalyst or 'stale'}) — {reason}"
        elif has_catalyst and catalyst:
            reason = f"{catalyst} — {reason}"
        log.info("news-driven [%s] %s → %s (%d) catalyst=%s",
                 model_name, signal.symbol, verdict, score, has_catalyst or "none")
        return verdict, score, has_catalyst, reason, True
    except Exception as e:
        log.warning("news-driven: AI unavailable for %s (%s) — no trade (fail-safe)",
                    signal.symbol, e)
        return "neutral", 50, False, f"AI unavailable ({e})", False


def assess_sentiment(signal, options_summary: str = "") -> tuple[str, int, str]:
    """Phase 2B broker-style sentiment read for a BUY candidate. Returns
    (verdict, score_0_100, reason). ADVISORY — never vetoes. FAIL-SAFE → neutral
    ('neutral', 50, ...) on no key / quota / error. Uses sentiment_model (≥3.5).
    options_summary (optional) injects the options-flow read as a 4th factor."""
    if not ai.has_key():
        return "neutral", 50, "no AI key — sentiment inert (neutral)"

    try:
        ticker_news = news_fetcher.format_news(
            news_fetcher.fetch_ticker_news(signal.symbol), "Ticker news")
        macro_news = news_fetcher.format_news(news_fetcher.fetch_macro_news(), "Macro")
    except Exception as e:
        return "neutral", 50, f"news fetch failed ({e}) — neutral"
    prompt = SENTIMENT_PROMPT.format(
        symbol=signal.symbol, price=signal.price,
        reasons="\n".join(f"- {r}" for r in signal.reasons),
        ticker_news=ticker_news, macro_news=macro_news,
        options_flow=options_summary or "(none)")

    try:
        text, model_name = ai.generate(prompt)
        payload = _extract_json(text)
        verdict = str(payload.get("verdict", "neutral")).lower()
        score = int(payload.get("score", 50))
        reason = payload.get("reason", "")
        log.info("sentiment [%s] %s → %s (%d)", model_name, signal.symbol, verdict, score)
        return verdict, score, reason
    except Exception as e:
        log.warning("sentiment: AI unavailable for %s (%s) — neutral (fail-safe)",
                    signal.symbol, e)
    return "neutral", 50, "AI unavailable — neutral (fail-safe)"
