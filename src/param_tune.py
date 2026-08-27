"""Backtest-driven parameter tuning — weekly on its own, or only when asked.

Every automated path that PROPOSES a parameter change off a backtest asks
`auto_enabled()` first. The answer lives in config/parameters.json under
PARAM_TUNE_MODE and is re-read from the file on every call, so flipping it in
the panel reaches the already-running scheduler at its next job instead of at
the next restart.

The default is "manual", and the reason is not caution for its own sake: the
proposals land in the approval queue, and a queue that fills itself between
visits trains the owner to clear it rather than read it. Manual means the
analysis happens while the owner is looking at it.

`run()` is the on-demand version of the weekly chain, and it is the SAME chain:
real fills → AI candidates → honest-engine backtest of each → survivors. Two
differences, both deliberate:

  • it reports progress as it goes, because a four-minute wait with no output
    is indistinguishable from a hang; and
  • it RETURNS the survivors instead of enqueuing them. The owner confirms each
    one in the dialog that ran it, which is the same choke point the approval
    queue is — minus the wait, and with the measured numbers still on screen.
"""
from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)

MODES = ("manual", "weekly")
DEFAULT_MODE = "manual"

Progress = Callable[[str], None] | None


def mode() -> str:
    """"manual" (nothing tunes on its own) or "weekly" (the Monday chain runs).

    Read off the file, not `settings` — settings is evaluated once per process,
    and the whole point of this switch is that the scheduler, which has been up
    for days, obeys what the panel says now.
    """
    from . import runtime_config
    raw = str(runtime_config._param("param_tune_mode") or "").strip().lower()
    return raw if raw in MODES else DEFAULT_MODE


def auto_enabled() -> bool:
    return mode() == "weekly"


def skip_note(job: str) -> str:
    """One line for the log when an automated tuner stands down."""
    return (f"{job}: PARAM_TUNE_MODE=manual — 不自动提参数建议"
            f"（在参数面板点「立即回测调参」手动跑）")


# ── the run itself ───────────────────────────────────────────────────────────

def _band(key: str):
    from . import runtime_config
    b = runtime_config.ALLOWED_PARAMS.get(key)
    return [b[0], b[1]] if b else None


def _ai_candidate(p: dict) -> dict:
    """One survivor of validate_proposals, in the shape the panel renders.

    The measured numbers travel WITH the proposal. A row that says "raise TP to
    9" and nothing else is asking to be approved on the strength of the phrase
    "backtested", which is exactly the thing this repo keeps having to unlearn.
    """
    from . import runtime_config
    key = p["key"]
    deltas, dd = p.get("_deltas", {}), p.get("_dd", {})
    return {
        "key": key,
        "current": runtime_config.current(key),
        "value": float(p["value"]),
        "source": "ai",
        "rationale": str(p.get("rationale", "")).strip(),
        "per_day_delta": {str(w): round(v, 2) for w, v in deltas.items()},
        "dd": {str(w): round(v, 1) for w, v in dd.items()},
        "n_trades": p.get("_n_trades"),
        "thin": bool(p.get("_thin")),
        "band": _band(key),
    }


def run(on_progress: Progress = None, should_cancel=None,
        lang: str = "zh") -> dict:
    """Run the tuning chain now.

    Returns {"candidates", "review", "notes", "cancelled"}.

    Never raises for an ordinary empty outcome — "the AI proposed nothing" and
    "nothing survived the backtest" are results, not failures, and the panel
    has to be able to tell them apart. Only a genuine breakage propagates.

    `lang` is the panel's language. The progress lines are the run's only
    output while it is going, so they are the last place it would be acceptable
    to hand an English UI a Chinese log.

    `should_cancel()` is polled between stages and between candidates. It is
    COOPERATIVE, and it has to be: a Python thread cannot be killed, and one
    engine pass over the prefetched window is a single uninterruptible call. So
    stopping lands at the next checkpoint, not instantly — and whatever was
    already measured comes back with cancelled=True rather than being thrown
    away, because those numbers cost the same minutes either way.
    """
    from . import ai, optimizer_ai, runtime_config, self_improve, self_review

    en = lang == "en"

    def L(zh: str, en_: str) -> str:
        return en_ if en else zh

    def say(msg: str) -> None:
        log.info("param-tune: %s", msg)
        if on_progress:
            try:
                on_progress(msg)
            except Exception:      # a broken listener must not kill the run
                pass

    def stopping() -> bool:
        try:
            return bool(should_cancel and should_cancel())
        except Exception:
            return False

    notes: list[str] = []
    candidates: list[dict] = []

    def done(cancelled: bool) -> dict:
        if cancelled:
            notes.append(L("你中途停止了这一轮。下面是停止前已经测完的部分 —— "
                           "还没测到的候选这一轮就不会出现了。",
                           "You stopped this run. Below is what had finished "
                           "measuring; candidates it never reached will not "
                           "appear this round."))
            say("■ " + L("已停止 —— ", "Stopped — ") + (
                L(f"保留了停止前测完的 {len(candidates)} 条改动。",
                  f"kept the {len(candidates)} change(s) measured before it stopped.")
                if candidates else
                L("停止前还没有任何改动测完。",
                  "nothing had finished measuring yet.")))
        return {"candidates": candidates, "notes": notes, "cancelled": cancelled,
                "review": review_out}

    review_out: dict = {}

    if runtime_config.frozen():
        notes.append(L("⚠ PARAMS_FROZEN=true — 参数冻结中，跑出来的建议无法应用。"
                       "先在参数面板关掉冻结。",
                       "⚠ PARAMS_FROZEN=true — parameters are frozen, so nothing "
                       "this run finds can be applied. Turn the freeze off on "
                       "the parameter page first."))
        say(notes[-1])

    say(L("读取最近 7 天的真实成交…", "Reading the last 7 days of real fills…"))
    review = self_review.weekly_review(days=7)
    n_tr = int(review.get("n_trades", 0) or 0)
    review_out = {"n_trades": n_tr, "per_day": review.get("per_day", 0),
                  "win_rate": review.get("win_rate", 0)}
    say(L(f"真实成交 {n_tr} 笔，${review.get('per_day', 0):.2f}/天，"
          f"胜率 {review.get('win_rate', 0)}%",
          f"{n_tr} real fills, ${review.get('per_day', 0):.2f}/day, "
          f"{review.get('win_rate', 0)}% win rate"))
    if stopping():
        return done(True)

    # ── AI candidates, then the honest engine on each one ────────────────────
    proposals: list[dict] = []
    if not ai.has_key():
        notes.append(L("没有配置 AI key —— 这一轮只做 half-Kelly 风险核对，"
                       "不会有 AI 参数建议。",
                       "No AI key configured — this run does the half-Kelly risk "
                       "check only, with no AI parameter proposals."))
        say(notes[-1])
    else:
        say(L(f"询问 {ai.active_provider()} / {ai.active_model()}：哪些参数值得动？",
              f"Asking {ai.active_provider()} / {ai.active_model()}: which "
              f"parameters are worth moving?"))
        proposals = optimizer_ai._call_ai(review)
        if proposals:
            names = ", ".join(f"{p.get('key')}→{p.get('value')}" for p in proposals)
            say(L(f"AI 候选 {len(proposals)} 条：{names}",
                  f"{len(proposals)} AI candidate(s): {names}"))
        else:
            notes.append(L("AI 认为当前参数不需要调整（没有提出候选）。",
                           "The AI sees no change worth making (it proposed none)."))
            say(notes[-1])

    if stopping():
        return done(True)

    if proposals:
        say(L("在 honest 引擎上逐条回测（最慢的一步，约 2–4 分钟）…",
              "Backtesting each one on the honest engine — the slow step, "
              "about 2–4 minutes…"))
        try:
            validated = optimizer_ai.validate_proposals(
                proposals, on_progress=say, should_cancel=should_cancel,
                lang=lang)
        except Exception as e:
            log.exception("param-tune: validation failed")
            notes.append(L(f"回测验证失败：{e}", f"Backtest validation failed: {e}"))
            say("✗ " + notes[-1])
            validated = []
        for p in validated:
            try:
                candidates.append(_ai_candidate(p))
            except Exception as e:      # unknown key slipped the gate
                log.warning("param-tune: cannot render %s: %s", p.get("key"), e)
        if stopping():
            return done(True)
        dropped = len(proposals) - len(validated)
        if dropped > 0:
            # Deliberately vague about WHICH reason, because there are three
            # (lost to the baseline / out of bounds / in post-rollback cooldown)
            # and the progress log above names the one that applied to each. A
            # summary that picked one of them would be wrong for the others.
            notes.append(L(
                f"{dropped} 条候选未被采纳（未胜过当前参数、越界、或刚回滚过还在冷却）"
                f"—— 上面的运行日志里逐条写了是哪一种。",
                f"{dropped} candidate(s) were not taken — they lost to your "
                f"current settings, fell outside the allowed range, or are in "
                f"post-rollback cooldown. The log above says which, per "
                f"candidate."))
            say(notes[-1])

    # ── half-Kelly risk sizing, from the same real fills ─────────────────────
    # Checked BEFORE the call, not inside it: backtest_risk_change is one
    # uninterruptible engine pass, so entering it commits to finishing it.
    if stopping():
        return done(True)
    say(L("half-Kelly：用真实成交估算这个账户的胜率与赔率…",
          "half-Kelly: estimating this account's win rate and payoff from the "
          "real fills…"))
    try:
        kelly, why = self_improve.half_kelly_candidate(lang=lang)
    except Exception as e:
        log.exception("param-tune: half-Kelly failed")
        kelly, why = None, L(f"计算失败：{e}", f"calculation failed: {e}")
    if kelly:
        candidates.append(kelly)
        say(L(f"half-Kelly 建议 risk_per_trade {kelly['current']:.1%} → "
              f"{kelly['value']:.1%}",
              f"half-Kelly proposes risk_per_trade {kelly['current']:.1%} → "
              f"{kelly['value']:.1%}"))
    else:
        notes.append(f"half-Kelly: {why}")
        say(notes[-1])

    if stopping():
        return done(True)
    say(L(f"完成 —— {len(candidates)} 条参数改动通过验证，等你逐条确认。",
          f"Done — {len(candidates)} change(s) passed and are waiting for you "
          f"to confirm them one by one.")
        if candidates else
        L("完成 —— 没有任何参数改动通过验证，当前参数保持不变。",
          "Done — nothing passed. Your parameters stay as they are."))
    return done(False)


def apply_confirmed(accepted: list[dict], source: str = "param-tune(手动确认)") -> dict:
    """Write the changes the owner ticked. Returns {"applied", "failed"}.

    Goes through runtime_config.set_param — the same call the approval executor
    makes — so a confirmation here is journalled, bounds-checked, and visible to
    the autopilot's rollback watcher exactly like an approved one.
    """
    from . import runtime_config
    applied, failed = [], {}
    for item in accepted:
        key = str(item.get("key", ""))
        try:
            rec = runtime_config.set_param(key, float(item.get("value")),
                                           source=source)
            applied.append({"key": key, "old": rec.get("old"),
                            "new": rec.get("new")})
            log.info("param-tune: applied %s %s → %s", key, rec.get("old"),
                     rec.get("new"))
        except Exception as e:
            failed[key] = f"{type(e).__name__}: {e}"
            log.warning("param-tune: %s rejected: %s", key, e)
    return {"applied": applied, "failed": failed}
