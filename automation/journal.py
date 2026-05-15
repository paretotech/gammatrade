"""End-of-day journal storage + plan-adherence scoring.

Each journal entry is one JSON file at
    ~/.gamma/automation/journal/<YYYY-MM-DD>.json

with the shape:

    {
        "date":          "2026-05-11",
        "ts_created":    "2026-05-11T16:35:00",
        "ts_updated":    "2026-05-11T16:35:00",
        "plan_adherence": "...",   # free-form: did entries match the plan?
        "wins":           "...",
        "losses":         "...",
        "lessons":        "...",   # one-liner takeaways
        "mfe_gaps":       "...",   # which trades left % on the table
        "notes":          "..."    # anything else
    }

Plan-adherence scoring (computed on read, not stored):
For each pick in the day's pregame analysis (~/.gamma/automation/analyses/<date>.json),
check if the user traded that ticker. Combine verdict + did-they-trade to
classify each pick as one of:

    followed   — verdict was LIKE and they traded it
                 OR verdict was PASS/skip and they didn't
    skipped    — verdict was LIKE and they didn't trade
    violated   — verdict was PASS and they traded it anyway
    neutral    — verdict was WATCH (no expected action)

Plus any traded ticker NOT in any pick is "off_plan".

Score = (followed / total picks). Off-plan trades are reported separately.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

JOURNAL_DIR = Path.home() / ".gamma" / "automation" / "journal"
ANALYSES_DIR = Path.home() / ".gamma" / "automation" / "analyses"
DB_PATH = Path.home() / ".gamma" / "automation" / "state.db"


# ─── Storage ────────────────────────────────────────────────────────────

def _path(date_str: str) -> Path:
    return JOURNAL_DIR / f"{date_str}.json"


def load(date_str: str) -> Optional[dict]:
    p = _path(date_str)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save(entry: dict) -> dict:
    """Save an entry. Sets ts_created on first write, updates ts_updated each time."""
    if not entry.get("date"):
        raise ValueError("entry must have a 'date' field (YYYY-MM-DD)")
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat(timespec="seconds")
    existing = load(entry["date"]) or {}
    merged = {
        "date":           entry["date"],
        "ts_created":     existing.get("ts_created") or now,
        "ts_updated":     now,
        "plan_adherence": entry.get("plan_adherence", existing.get("plan_adherence", "")),
        "wins":           entry.get("wins",           existing.get("wins", "")),
        "losses":         entry.get("losses",         existing.get("losses", "")),
        "lessons":        entry.get("lessons",        existing.get("lessons", "")),
        "mfe_gaps":       entry.get("mfe_gaps",       existing.get("mfe_gaps", "")),
        "notes":          entry.get("notes",          existing.get("notes", "")),
    }
    _path(entry["date"]).write_text(json.dumps(merged, indent=2))
    return merged


def list_entries(limit: int = 100) -> list[dict]:
    """Return entries newest-first as a list of {date, ts_updated, summary}."""
    if not JOURNAL_DIR.exists():
        return []
    out: list[dict] = []
    for p in sorted(JOURNAL_DIR.glob("*.json"), reverse=True)[:limit]:
        try:
            d = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        # Truncate long fields for the list view
        def _snip(s: str, n: int = 110) -> str:
            s = (s or "").strip().replace("\n", " ")
            return s if len(s) <= n else s[: n - 1] + "…"
        out.append({
            "date":         d.get("date"),
            "ts_updated":   d.get("ts_updated"),
            "lessons_snip": _snip(d.get("lessons", "")),
            "has_content":  bool(d.get("plan_adherence") or d.get("wins") or d.get("losses")
                                 or d.get("lessons") or d.get("mfe_gaps") or d.get("notes")),
        })
    return out


# ─── Day's-trades + adherence ──────────────────────────────────────────

def _day_closed_trades(date_str: str) -> list[dict]:
    """Trades whose entry-date matches the given day, with realized P&L."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT i.intent_id, i.ticker, i.strike, i.right,
               (SELECT SUM(contracts) FROM fills f WHERE f.intent_id=i.intent_id AND f.is_entry=1) AS eq,
               (SELECT SUM(contracts) FROM fills f WHERE f.intent_id=i.intent_id AND f.is_entry=0) AS xq
        FROM trade_intents i
        WHERE i.status='filled' AND substr(i.created_at, 1, 10) = ?
        """,
        (date_str,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        eq, xq = r["eq"] or 0, r["xq"] or 0
        if eq <= 0 or xq < eq:
            continue
        ef = conn.execute(
            "SELECT contracts, price FROM fills WHERE intent_id=? AND is_entry=1",
            (r["intent_id"],),
        ).fetchall()
        xf = conn.execute(
            "SELECT contracts, price FROM fills WHERE intent_id=? AND is_entry=0",
            (r["intent_id"],),
        ).fetchall()
        avg_e = sum(f["price"] * f["contracts"] for f in ef) / eq
        gross_e = sum(f["price"] * f["contracts"] for f in ef)
        gross_x = sum(f["price"] * f["contracts"] for f in xf)
        roi = (gross_x / gross_e - 1) if gross_e > 0 else 0
        out.append({
            "intent_id":    r["intent_id"],
            "ticker":       r["ticker"],
            "strike":       r["strike"],
            "right":        r["right"],
            "contracts":    eq,
            "avg_entry":    round(avg_e, 2),
            "roi_pct":      round(roi * 100, 1),
            "realized_pnl": round((gross_x - gross_e) * 100, 2),
        })
    conn.close()
    return out


def _load_day_analysis(date_str: str) -> Optional[dict]:
    """Pull the pregame analysis cache for this date, if any."""
    p = ANALYSES_DIR / f"{date_str}.json"
    if not p.exists():
        return None
    try:
        wrapped = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if wrapped.get("status") != "ok":
        return None
    return wrapped.get("analysis")


def adherence_for_day(date_str: str) -> dict:
    """Compare the day's pregame picks to the day's actual trades.

    Returns:
        {
            "has_pregame":    bool,
            "picks":          [ {ticker, verdict, traded, classification}, ... ],
            "off_plan":       [ {ticker, n_trades, pnl_usd}, ... ],
            "score_pct":      float | None,   # followed / scorable
            "summary":        "3 of 5 picks followed · 1 off-plan trade"
        }
    """
    trades = _day_closed_trades(date_str)
    traded_by_ticker: dict[str, list[dict]] = {}
    for t in trades:
        traded_by_ticker.setdefault(t["ticker"], []).append(t)

    analysis = _load_day_analysis(date_str)
    if analysis is None:
        return {
            "has_pregame": False,
            "picks":       [],
            "off_plan":    [
                {
                    "ticker":   tk,
                    "n_trades": len(ts),
                    "pnl_usd":  round(sum(x["realized_pnl"] for x in ts), 2),
                }
                for tk, ts in sorted(traded_by_ticker.items())
            ],
            "score_pct": None,
            "summary":   f"no pregame analysis for {date_str} · {len(trades)} trades closed",
        }

    pick_tickers: set[str] = set()
    classified: list[dict] = []
    followed = 0
    scorable = 0  # picks where verdict implies an expected action (not WATCH)

    for p in analysis.get("picks", []):
        tk = p.get("ticker", "").upper()
        verdict = (p.get("verdict") or "").upper()
        pick_tickers.add(tk)
        ts = traded_by_ticker.get(tk, [])
        traded = bool(ts)
        if verdict == "LIKE":
            scorable += 1
            classification = "followed" if traded else "skipped"
            if traded:
                followed += 1
        elif verdict == "PASS":
            scorable += 1
            classification = "violated" if traded else "followed"
            if not traded:
                followed += 1
        else:  # WATCH or unknown
            classification = "neutral"
        classified.append({
            "ticker":         tk,
            "verdict":        verdict,
            "traded":         traded,
            "n_trades":       len(ts),
            "pnl_usd":        round(sum(x["realized_pnl"] for x in ts), 2) if ts else 0,
            "classification": classification,
        })

    off_plan = [
        {
            "ticker":   tk,
            "n_trades": len(ts),
            "pnl_usd":  round(sum(x["realized_pnl"] for x in ts), 2),
        }
        for tk, ts in sorted(traded_by_ticker.items()) if tk not in pick_tickers
    ]

    score_pct = round(followed / scorable * 100, 1) if scorable else None
    summary_parts = []
    if scorable:
        summary_parts.append(f"{followed} of {scorable} pick decisions matched")
    if off_plan:
        n_off = sum(o["n_trades"] for o in off_plan)
        summary_parts.append(f"{n_off} off-plan trade{'s' if n_off != 1 else ''}")
    summary = " · ".join(summary_parts) if summary_parts else "no trades today"

    return {
        "has_pregame": True,
        "picks":       classified,
        "off_plan":    off_plan,
        "score_pct":   score_pct,
        "summary":     summary,
    }


def day_summary(date_str: str) -> dict:
    """Day's closed-trade roll-up (count, P&L, win rate, biggest capture gap)."""
    trades = _day_closed_trades(date_str)
    if not trades:
        return {"n_trades": 0, "pnl_usd": 0.0, "win_rate_pct": None, "trades": []}
    wins = sum(1 for t in trades if t["realized_pnl"] > 0)
    return {
        "n_trades":     len(trades),
        "pnl_usd":      round(sum(t["realized_pnl"] for t in trades), 2),
        "win_rate_pct": round(wins / len(trades) * 100, 1),
        "trades":       trades,
    }
