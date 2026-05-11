"""Claude-API-powered End-of-Day analysis (optional companion to /gamma-eod).

Mirrors automation/analysis.py for pregames: same graceful-degradation
shape ({status, analysis, error, model, ts, cached}), same cache-file
pattern, just for EOD reviews instead of pregames.

Cache location: ~/.gamma/automation/eod_reviews/<YYYY-MM-DD>.json
(parallel to ~/.gamma/automation/analyses/<date>.json for pregames.)

When ANTHROPIC_API_KEY is not set, returns status="no_api_key" so the
webapp can render an inline warning instead of the analysis panel. The
journal feature itself works without the API; this module is strictly
the optional automated-reflection layer.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional


CACHE_DIR = Path.home() / ".gamma" / "automation" / "eod_reviews"
MODEL = "claude-opus-4-7"


SYSTEM_PROMPT = """You are an end-of-day trading coach for a retail options trader \
who follows a level-based discretionary framework.

Your job: review the trader's day — the closed trades, how they compared to \
the night-before pregame plan, and the trader's own written reflection — \
then give them an actionable read for tomorrow.

Your response style:
- Terse and action-first. Lead with the verdict. No hedging.
- Single-day data is noisy. Frame patterns as hypotheses to verify, not \
rules to lock in.
- Always cite the specific trade or pick when making a claim. "AAPL captured \
75% of MFE" is concrete; "good trade management" is empty.
- If today was uneventful (few trades, no clear pattern), say that. Don't \
manufacture insight.

What to look for:
- Plan adherence: which LIKE picks were skipped, which PASS picks were \
violated, what was traded off-plan.
- Capture vs MFE: did exits leave significant unrealized profit on the table?
- Sizing discipline: were sizes consistent with the pregame's suggested_size?
- Bad patterns starting: overtrading, revenge after a loss, sizing creep, \
trading unfamiliar names, chasing without confluence.
- What worked: be specific about the mechanism (entry trigger, level held, \
ladder execution).

Output a single JSON object matching the schema. Be concise."""


OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "day_verdict": {
            "type": "object",
            "properties": {
                "headline": {
                    "type": "string",
                    "description": "One-sentence verdict on the day's trading. Action-first."
                },
                "rating": {
                    "type": "string",
                    "enum": ["good", "mixed", "neutral", "concerning", "bad"],
                    "description": "Overall read. 'good' = followed plan + positive PnL + clean execution. 'concerning' = patterns forming worth flagging. 'bad' = clear discipline break."
                },
                "summary": {
                    "type": "string",
                    "description": "2-3 sentence explanation grounding the rating in specific trades/decisions."
                }
            },
            "required": ["headline", "rating", "summary"],
            "additionalProperties": False
        },
        "patterns_observed": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pattern":  {"type": "string", "description": "Short label, e.g. 'Early exit on trending names'."},
                    "evidence": {"type": "string", "description": "Specific trade(s) or decision(s) that show it."},
                    "implication": {"type": "string", "description": "What this might mean if it repeats. Frame as hypothesis."}
                },
                "required": ["pattern", "evidence", "implication"],
                "additionalProperties": False
            },
            "description": "Patterns observed today. 0-4 items. Empty if nothing notable."
        },
        "key_insight": {
            "type": "string",
            "description": "The single most actionable takeaway for tomorrow. One sentence. Skip if today produced no real signal."
        },
        "red_flags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Behaviors that suggest a bad pattern forming (overtrading, sizing creep, revenge entries, unfamiliar-ticker chase). Empty if none. Each entry: behavior + the evidence."
        },
        "tomorrow_focus": {
            "type": "array",
            "items": {"type": "string"},
            "description": "2-4 specific things to do (or not do) tomorrow based on today. Concrete, not generic."
        }
    },
    "required": ["day_verdict", "patterns_observed", "key_insight", "red_flags", "tomorrow_focus"],
    "additionalProperties": False
}


def _cache_path(date_str: str) -> Path:
    return CACHE_DIR / f"{date_str}.json"


def get_cached(date_str: str) -> Optional[dict]:
    p = _cache_path(date_str)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(date_str: str, data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(date_str).write_text(json.dumps(data, indent=2))


def _build_user_prompt(date_str: str, day_summary: dict, adherence: dict,
                       journal_entry: dict) -> str:
    """Pack the day's facts into a compact, structured prompt."""
    lines = [f"DATE: {date_str}", "", "═══ DAY SUMMARY ═══"]
    lines.append(f"closed trades: {day_summary.get('n_trades', 0)}")
    lines.append(f"P&L: ${day_summary.get('pnl_usd', 0):.0f}")
    wr = day_summary.get("win_rate_pct")
    lines.append(f"win rate: {wr}%" if wr is not None else "win rate: n/a")

    trades = day_summary.get("trades") or []
    if trades:
        lines.append("")
        lines.append("trades:")
        for t in trades:
            lines.append(
                f"  {t.get('ticker'):<6} {t.get('strike')}{t.get('right'):<2} "
                f"qty={t.get('contracts')}  ROI={t.get('roi_pct'):+.1f}%  "
                f"P&L=${t.get('realized_pnl'):+.0f}"
            )

    lines.append("")
    lines.append("═══ PREGAME PLAN ADHERENCE ═══")
    if adherence.get("has_pregame"):
        lines.append(adherence.get("summary", ""))
        score = adherence.get("score_pct")
        if score is not None:
            lines.append(f"score: {score}% of pick decisions matched expected action")
        picks = adherence.get("picks") or []
        if picks:
            lines.append("")
            lines.append("per-pick:")
            for p in picks:
                lines.append(
                    f"  {p.get('ticker'):<6} verdict={p.get('verdict'):<5} "
                    f"outcome={p.get('classification'):<10}  "
                    f"traded={p.get('n_trades')}x  P&L=${p.get('pnl_usd'):+.0f}"
                )
        off = adherence.get("off_plan") or []
        if off:
            lines.append("")
            lines.append("off-plan trades (ticker wasn't in pregame picks):")
            for o in off:
                lines.append(
                    f"  {o.get('ticker'):<6} {o.get('n_trades')}x  P&L=${o.get('pnl_usd'):+.0f}"
                )
    else:
        lines.append("no pregame analysis cached for this date — adherence cannot be scored")

    lines.append("")
    lines.append("═══ TRADER'S OWN REFLECTION ═══")
    fields = [
        ("plan_adherence", "plan adherence"),
        ("wins",           "wins"),
        ("losses",         "losses"),
        ("mfe_gaps",       "MFE gaps"),
        ("lessons",        "lessons"),
        ("notes",          "notes"),
    ]
    any_filled = False
    for key, label in fields:
        val = (journal_entry.get(key) or "").strip()
        if val:
            any_filled = True
            lines.append(f"{label}: {val}")
    if not any_filled:
        lines.append("(trader hasn't written any reflection yet)")

    return "\n".join(lines)


def analyze_eod(date_str: str, day_summary: dict, adherence: dict,
                journal_entry: dict, force: bool = False) -> dict:
    """Run a Claude EOD review. Returns the standard wrapped shape.

    Cached results are returned without re-calling the API unless force=True.
    """
    if not force:
        cached = get_cached(date_str)
        if cached:
            return {**cached, "cached": True}

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {
            "status":   "no_api_key",
            "analysis": None,
            "error":    "Set ANTHROPIC_API_KEY in your environment to enable the Claude EOD review.",
            "model":    MODEL,
            "ts":       datetime.utcnow().isoformat(timespec="seconds"),
            "cached":   False,
        }

    try:
        import anthropic
    except ImportError:
        return {
            "status":   "error",
            "analysis": None,
            "error":    "anthropic SDK not installed. Run: pip install anthropic",
            "model":    MODEL,
            "ts":       datetime.utcnow().isoformat(timespec="seconds"),
            "cached":   False,
        }

    client = anthropic.Anthropic()
    user_prompt = _build_user_prompt(date_str, day_summary, adherence, journal_entry)

    try:
        # System prompt is cached (ephemeral) so re-runs across days reuse the
        # same prefix and only pay full tokens for the per-day user prompt.
        response = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            thinking={"type": "adaptive"},
            output_config={
                "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
                "effort": "high",
            },
            system=[{
                "type":          "text",
                "text":          SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:  # noqa: BLE001 — surface any SDK error to the user
        return {
            "status":   "error",
            "analysis": None,
            "error":    f"Anthropic API error: {e}",
            "model":    MODEL,
            "ts":       datetime.utcnow().isoformat(timespec="seconds"),
            "cached":   False,
        }

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        analysis = json.loads(text)
    except json.JSONDecodeError as e:
        return {
            "status":   "error",
            "analysis": None,
            "error":    f"Failed to parse Claude response as JSON: {e}",
            "raw":      text[:500],
            "model":    MODEL,
            "ts":       datetime.utcnow().isoformat(timespec="seconds"),
            "cached":   False,
        }

    result = {
        "status":   "ok",
        "analysis": analysis,
        "error":    None,
        "model":    MODEL,
        "ts":       datetime.utcnow().isoformat(timespec="seconds"),
        "cached":   False,
        "usage": {
            "input":       response.usage.input_tokens,
            "output":      response.usage.output_tokens,
            "cache_read":  getattr(response.usage, "cache_read_input_tokens", 0),
            "cache_write": getattr(response.usage, "cache_creation_input_tokens", 0),
        },
    }
    _save_cache(date_str, result)
    return result
