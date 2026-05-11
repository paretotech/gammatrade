"""Claude-powered pregame analysis.

Calls Anthropic API to analyze a pregame note against:
  - Per-pick + watchlist historical stats from reference trade books
  - The strategy guide (docs/strategy_rules.md)

Output is structured JSON: day-level read + per-pick verdict + operational notes.

Caches results keyed by pregame date so the same pregame doesn't re-analyze
on every page view. Cache lives at ~/.gamma/automation/analyses/<date>.json.

Graceful degradation: if ANTHROPIC_API_KEY is not set, returns a stub with
a helpful message instead of erroring.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


REPO_ROOT = Path(__file__).parent.parent
STRATEGY_GUIDE = REPO_ROOT / "docs" / "strategy_rules.md"
CACHE_DIR = Path.home() / ".gamma" / "automation" / "analyses"

MODEL = "claude-opus-4-7"


def _strategy_guide_excerpt() -> str:
    """Read the strategy guide. Trimmed to keep tokens reasonable; full file
    is ~30KB which is fine for a cached prefix on Opus 4.7's 1M context."""
    if not STRATEGY_GUIDE.exists():
        return ""
    return STRATEGY_GUIDE.read_text()


SYSTEM_PROMPT = """You are a trading strategy analyst for a retail options \
trader who follows a level-based discretionary framework.

Your job: analyze the trader's pregame plan against historical reference \
trade data and the strategy guide, then output a structured per-pick verdict.

PER-PICK OUTPUT — be RUTHLESSLY concise. Each field has a tight word cap.
- plan         — the actual trade in ONE clause. ≤18 words. Entry condition \
+ structure (e.g. "ATM/1-OTM call on PDH reclaim above 700"). \
No commentary, no rationale, just the trade.
- invalidation — the ONE price or condition that kills it. ≤12 words. \
PREFER the `stop_ref_level` from the Setup eval when one is provided \
(e.g. "falls below 697.30"). No "if X, do Y" — just the trigger.
- edge         — historical stats as inline shorthand. Format: \
"cohort +MED%/WR% (n=N) · you +MED%/WR% (n=N)". Use exact numbers from the \
data block. If a side has no history, write "you n=0".
- risk         — ONE sentence, ≤20 words. The single most important risk. \
WHEN the Setup eval shows R:R verdict POOR or INCOMPLETE, the risk MUST \
lead with that explicitly (e.g. "R:R 0.69 — risking 1.14% to capture \
0.78%; setup gives more than it gets"). When the level is OFF-level (not on \
or near a published support/resistance), call that out too. Otherwise lead \
with personal-record gaps when relevant.
- reasoning    — OPTIONAL single short sentence (≤15 words) only when the \
verdict needs context the four fields above don't carry. Skip otherwise.

DAY READ
- headline — one sentence, ≤15 words.
- regime_assessment — ONE sentence, ≤20 words.

OPERATIONAL NOTES
- Maximum 3 bullets. Each ≤18 words. Bullets that just restate a pick are wasted.

OTHER RULES
- Use MEDIANS not means for per-trade outcomes.
- Never invent rules. Cite named strategy-guide rules only when relevant.
- Pre-FOMC (Tue → Wed 14:30 ET of FOMC week) and CPI release days = HARD SKIP. \
Surface in blackouts_or_warnings.
- Unfamiliar tickers default to WATCH (verdict) and skip (size).
- Index strikes (QQQ/SPX/SPY): ATM or 1-OTM only.

Output a single JSON object matching the schema.

═══════════════════ STRATEGY GUIDE ═══════════════════

{strategy_guide}

═════════════════════════════════════════════════════"""


OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "day_read": {
            "type": "object",
            "properties": {
                "headline": {
                    "type": "string",
                    "description": "One-sentence punchline read for the day."
                },
                "regime_assessment": {
                    "type": "string",
                    "description": "Brief regime context: bullish/bearish/chop, notable macro factors. 1-2 sentences max."
                },
                "conviction": {
                    "type": "string",
                    "enum": ["high", "moderate", "low", "stay-out"],
                    "description": "Overall conviction for trading today."
                },
                "blackouts_or_warnings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Any hard skips (FOMC, CPI, megacap earnings) or warnings (sector concentration, late-week, regime mismatch). Empty if none."
                }
            },
            "required": ["headline", "regime_assessment", "conviction", "blackouts_or_warnings"],
            "additionalProperties": False
        },
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "verdict": {
                        "type": "string",
                        "enum": ["LIKE", "WATCH", "PASS"]
                    },
                    "suggested_size": {
                        "type": "string",
                        "enum": ["full", "half", "quarter", "skip"]
                    },
                    "plan": {
                        "type": "string",
                        "description": "The trade in one clause. ≤18 words. Entry condition + structure only. No rationale."
                    },
                    "invalidation": {
                        "type": "string",
                        "description": "The single price or condition that kills the trade. ≤12 words."
                    },
                    "edge": {
                        "type": "string",
                        "description": "Cohort + personal stats inline, e.g. 'cohort +39%/99% (n=90) · you +23%/70% (n=20)'. Use 'n=0' if no history."
                    },
                    "risk": {
                        "type": "string",
                        "description": "The one risk that matters most. ≤20 words. Lead with personal-record divergence when relevant."
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "OPTIONAL one short sentence (≤15 words) only when plan/edge/risk don't carry the context."
                    }
                },
                "required": ["ticker", "verdict", "suggested_size", "plan", "invalidation", "edge", "risk"],
                "additionalProperties": False
            }
        },
        "operational_notes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3-5 day-level operational bullets: sizing, cap awareness, what to ignore from the watchlist, what to watch for, etc."
        }
    },
    "required": ["day_read", "picks", "operational_notes"],
    "additionalProperties": False
}


def _build_user_prompt(parsed: dict, today: str) -> str:
    """Construct the per-request user prompt from parsed pregame data."""
    # Pull the latest published support/resistance levels for every
    # ticker appearing in this pregame. Levels are sourced from the
    # Settings → Levels tab (manually curated chartist snapshots).
    from . import levels as _lv
    seen_tickers = set()
    for c in parsed.get("candidates", []):
        seen_tickers.add(c.ticker.upper())
    for c in parsed.get("watch_candidates", []):
        seen_tickers.add(c.ticker.upper())
    ticker_levels: dict[str, Any] = {}
    for tkr in seen_tickers:
        snap = _lv.latest_for_ticker(tkr)
        if snap is not None:
            ticker_levels[tkr] = snap

    lines = [f"Today: {today}", ""]
    lines.append("PREGAME RAW TEXT (sections):")
    for name, content in parsed["sections"].items():
        lines.append(f"## {name}")
        lines.append(content)
        lines.append("")

    if parsed.get("regime"):
        lines.append("PARSED REGIME CONTEXT:")
        for k, v in parsed["regime"].items():
            lines.append(f"  {k}: {v}")
        lines.append("")

    if parsed.get("macro_confluence"):
        lines.append("MACRO CONFLUENCE DETECTED:")
        for m in parsed["macro_confluence"]:
            lines.append(f"  {m['ticker']} {m['direction']} {m['level']} (from: '{m.get('raw_line','')[:80]}')")
        lines.append("")

    if ticker_levels:
        lines.append("PUBLISHED LEVELS (chartist snapshots, latest per ticker):")
        for tkr in sorted(ticker_levels.keys()):
            snap = ticker_levels[tkr]
            below = ", ".join(f"{v:g}" for v in snap.levels_below) or "—"
            above = ", ".join(f"{v:g}" for v in snap.levels_above) or "—"
            asof  = snap.asof_ts[:10]
            cp    = f"${snap.current_price:g}" if snap.current_price else "—"
            lines.append(
                f"  {tkr}  (asof {asof}, snap price {cp}):"
                f"  support [{below}]  ·  resistance [{above}]"
            )
        lines.append(
            "Use these as the authoritative level set. When a pick references "
            "a level that's NOT in the published support/resistance, call it "
            "out as 'off-level' in the risk field."
        )
        lines.append("")

    lines.append(f"PRE-GAME PICKS (n={len(parsed['candidates'])}):")
    for c in parsed["candidates"]:
        # Deterministic setup evaluation against published levels.
        # We pre-compute it server-side and inject the facts so Claude
        # can ground its plan/risk fields in real numbers instead of
        # making up R/R from the price tick.
        ev = _lv.evaluate_pick_level(c.ticker, c.level, c.direction)
        if ev is not None:
            level_str = (
                f"ON published ${ev['matched_level']:g}"
                if ev["level_status"] == "on"
                else f"NEAR published ${ev['matched_level']:g} ({ev['matched_dist_pct']}% off)"
                if ev["level_status"] == "near"
                else f"OFF — closest published ${ev['matched_level'] or '?'} is {ev['matched_dist_pct']}% away"
                if ev["matched_level"] is not None
                else "OFF — entry is above ALL mapped levels (ATH territory)"
            )
            stop_t   = ev["stop_ref_level"]
            tgt_t    = ev["target_ref_level"]
            rew      = ev["reward_pct"]
            risk_v   = ev["risk_pct"]
            rr       = ev["rr_ratio"]
            verdict  = ev["rr_verdict"].upper()
            rr_str = (
                f"reward {rew}% (target ${tgt_t:g}) · risk {risk_v}% (stop ${stop_t:g}) "
                f"· R:R {rr:.2f} → {verdict}"
                if rr is not None
                else (
                    f"reward {rew}% (target ${tgt_t:g}) · NO mapped stop level "
                    f"→ {verdict}"
                    if rew is not None and tgt_t is not None
                    else f"risk {risk_v}% (stop ${stop_t:g}) · NO mapped target level "
                         f"(above all resistance) → {verdict}"
                    if risk_v is not None and stop_t is not None
                    else f"→ {verdict} (no mapped levels around entry)"
                )
            )
            try:
                c._setup_eval = ev   # stash for downstream render (frozen=False)
            except Exception:
                pass

        b_all = c.historical["reference"]["all"]
        b_calls = c.historical["reference"]["calls"]
        s_all = c.historical["user"]["all"]
        lines.append(
            f"  {c.ticker} {c.direction} {c.level:g}  score={c.score:.0f}"
        )
        if ev is not None:
            lines.append(f"    Setup eval: level {level_str}")
            lines.append(f"                {rr_str}")

        def _fmt_stats(label: str, st: dict) -> str:
            n = st.get("n", 0)
            if n == 0:
                return f"    {label}: no history"
            med = st.get("median_roi")
            win = st.get("win_rate")
            mean = st.get("mean_roi")
            parts = [f"n={n}"]
            if med is not None:
                parts.append(f"med={med*100:+.0f}%")
            if win is not None:
                parts.append(f"win={win*100:.0f}%")
            if mean is not None:
                parts.append(f"mean={mean*100:+.0f}%")
            return f"    {label}: " + " ".join(parts)

        lines.append(_fmt_stats("reference all", b_all))
        lines.append(_fmt_stats("reference calls", b_calls))
        lines.append(_fmt_stats("the user all", s_all))
        lines.append(f"    Strategy: top5={c.strategy.get('top5')} familiar={c.strategy.get('familiar')} index={c.strategy.get('is_index')}")
        if c.flags:
            lines.append(f"    Flags: {' · '.join(c.flags)}")
        lines.append("")

    if parsed.get("watch_candidates"):
        lines.append(f"WATCHLIST (context only, n={len(parsed['watch_candidates'])}):")
        for c in parsed["watch_candidates"][:15]:
            n = c.historical["reference"]["all"].get("n", 0)
            med = c.historical["reference"]["all"].get("median_roi")
            med_s = f"med={med*100:+.0f}%" if med is not None else "no hist"
            lines.append(f"  {c.ticker} {c.direction} {c.level:g}  reference n={n} {med_s}")
        lines.append("")

    lines.append("Analyze and respond per the schema. Be terse and action-first.")
    return "\n".join(lines)


def _cache_path(pregame_date: str) -> Path:
    return CACHE_DIR / f"{pregame_date}.json"


def get_cached(pregame_date: str) -> Optional[dict]:
    p = _cache_path(pregame_date)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(pregame_date: str, data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(pregame_date).write_text(json.dumps(data, indent=2))


def analyze_pregame(parsed: dict, pregame_date: str,
                     force: bool = False) -> dict:
    """Run Claude analysis on a parsed pregame.

    Returns: {
        "status": "ok" | "no_api_key" | "error",
        "analysis": {...} | None,
        "error": "..." | None,
        "model": str,
        "ts": str (ISO),
        "cached": bool,
    }
    """
    if not force:
        cached = get_cached(pregame_date)
        if cached:
            return {**cached, "cached": True}

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {
            "status": "no_api_key",
            "analysis": None,
            "error": "Set ANTHROPIC_API_KEY in your environment to enable Claude analysis.",
            "model": MODEL,
            "ts": datetime.utcnow().isoformat(),
            "cached": False,
        }

    try:
        import anthropic
    except ImportError:
        return {
            "status": "error",
            "analysis": None,
            "error": "anthropic SDK not installed. Run: pip install anthropic",
            "model": MODEL,
            "ts": datetime.utcnow().isoformat(),
            "cached": False,
        }

    client = anthropic.Anthropic()
    system_prompt = SYSTEM_PROMPT.format(strategy_guide=_strategy_guide_excerpt())
    user_prompt = _build_user_prompt(parsed, pregame_date)

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            thinking={"type": "adaptive"},
            output_config={
                "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
                "effort": "high",
            },
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_prompt}],
        )
    except anthropic.APIError as e:
        return {
            "status": "error",
            "analysis": None,
            "error": f"Anthropic API error: {e}",
            "model": MODEL,
            "ts": datetime.utcnow().isoformat(),
            "cached": False,
        }

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        analysis = json.loads(text)
    except json.JSONDecodeError as e:
        return {
            "status": "error",
            "analysis": None,
            "error": f"Failed to parse Claude response as JSON: {e}",
            "raw": text[:500],
            "model": MODEL,
            "ts": datetime.utcnow().isoformat(),
            "cached": False,
        }

    result = {
        "status": "ok",
        "analysis": analysis,
        "error": None,
        "model": MODEL,
        "ts": datetime.utcnow().isoformat(),
        "cached": False,
        "usage": {
            "input": response.usage.input_tokens,
            "output": response.usage.output_tokens,
            "cache_read": getattr(response.usage, "cache_read_input_tokens", 0),
            "cache_write": getattr(response.usage, "cache_creation_input_tokens", 0),
        },
    }
    _save_cache(pregame_date, result)
    return result
