"""Analytics over the local trade store.

All functions read from the same SQLite that the engine writes to. No external
calls. Numbers are computed on demand — no caching layer yet.

Conventions:
- Per-trade ROI uses MEDIAN, not mean (per communication_prefs).
- Book-level $ P&L uses SUM.
- 'Closed' position = entry_qty == exit_qty AND entry_qty > 0.
- 'Win' = realized $ P&L > 0.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import median
from typing import Any, Optional
from zoneinfo import ZoneInfo

from . import state

_ET = ZoneInfo("America/New_York")


def _connect():
    return state.connect(state.DB_PATH)


# ─── Date range filtering ──────────────────────────────────────────────────

RANGE_LABELS = {
    "today": "Today",
    "week": "This week",
    "month": "This month",
    "90d": "Last 90 days",
    "all": "All time",
}


def _range_start(range_key: str) -> Optional[date]:
    today = date.today()
    if range_key == "today":
        return today
    if range_key == "week":
        return today - timedelta(days=today.weekday())
    if range_key == "month":
        return today.replace(day=1)
    if range_key == "90d":
        return today - timedelta(days=90)
    return None  # all time


def _filter_by_range(trades: list[dict], range_key: str) -> list[dict]:
    start = _range_start(range_key)
    if start is None:
        return trades
    out = []
    for t in trades:
        ts = t.get("last_exit_ts") or t.get("first_entry_ts")
        if not ts:
            continue
        try:
            d = date.fromisoformat(ts[:10])
        except (TypeError, ValueError):
            continue
        if d >= start:
            out.append(t)
    return out


# ─── Trade log ─────────────────────────────────────────────────────────────

def closed_trades(limit: int = 200, range_key: str = "all") -> list[dict]:
    """Closed positions with computed entry/exit/P&L. Newest first."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT i.*,
                   (SELECT MIN(ts)  FROM fills f WHERE f.intent_id = i.intent_id AND f.is_entry = 1) AS first_entry_ts,
                   (SELECT MAX(ts)  FROM fills f WHERE f.intent_id = i.intent_id AND f.is_entry = 0) AS last_exit_ts,
                   (SELECT SUM(contracts) FROM fills f WHERE f.intent_id = i.intent_id AND f.is_entry = 1) AS entry_qty,
                   (SELECT SUM(contracts) FROM fills f WHERE f.intent_id = i.intent_id AND f.is_entry = 0) AS exit_qty
            FROM trade_intents i
            WHERE i.status IN ('filled', 'closed')
            ORDER BY first_entry_ts DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    out: list[dict] = []
    for r in rows:
        d = dict(r)
        eq = int(d.get("entry_qty") or 0)
        xq = int(d.get("exit_qty") or 0)
        if eq <= 0 or xq < eq:
            continue  # still open or never filled
        with _connect() as conn:
            entry_fills = conn.execute(
                "SELECT contracts, price, ts FROM fills WHERE intent_id=? AND is_entry=1 ORDER BY ts ASC",
                (d["intent_id"],)).fetchall()
            exit_fills = conn.execute(
                "SELECT contracts, price, tp_tier, ts FROM fills WHERE intent_id=? AND is_entry=0 ORDER BY ts ASC",
                (d["intent_id"],)).fetchall()
        if not entry_fills:
            continue
        avg_entry = sum(f["price"] * f["contracts"] for f in entry_fills) / eq
        gross_exit = sum(f["price"] * f["contracts"] for f in exit_fills)
        gross_entry = sum(f["price"] * f["contracts"] for f in entry_fills)
        realized = (gross_exit - gross_entry) * 100  # contract multiplier
        roi = ((gross_exit / gross_entry) - 1) if gross_entry > 0 else 0.0

        # First exit price > entry price means the trade was *managed at TP*,
        # not stopped out — that's the real "TP1 hit" definition.
        first_exit_profitable = bool(exit_fills) and exit_fills[0]["price"] > avg_entry

        # MFE/MAE percentages computed off avg_entry. Used by the trade-log
        # expandable detail panel.
        def _pct(p):
            return round(((p - avg_entry) / avg_entry * 100), 1) if (p and avg_entry > 0) else None
        mfe_in_pct = _pct(d.get("mfe_in_trade_price"))
        mfe_exp_pct = _pct(d.get("mfe_to_expiry_price"))
        mae_in_pct = _pct(d.get("mae_in_trade_price"))
        mae_exp_pct = _pct(d.get("mae_to_expiry_price"))
        actual_pct = round(roi * 100, 1)
        capture_pct = (round(max(0.0, min(100.0, (actual_pct / mfe_in_pct) * 100)), 1)
                       if mfe_in_pct and mfe_in_pct > 0 else None)
        capture_exp_pct = (round(max(0.0, min(100.0, (actual_pct / mfe_exp_pct) * 100)), 1)
                            if mfe_exp_pct and mfe_exp_pct > 0 else None)

        # Per-TP details (price / % from entry / minutes from entry).
        # Keep the earliest fill per tier in case of duplicates.
        first_entry_ts = entry_fills[0]["ts"] if entry_fills else None
        tp_details: dict[int, dict] = {}
        for f in exit_fills:
            tier_raw = f["tp_tier"]
            if tier_raw is None:
                continue
            tier = int(tier_raw)
            if tier in tp_details:
                continue
            pct = round(((f["price"] - avg_entry) / avg_entry * 100), 1) if avg_entry > 0 else None
            mins = None
            if first_entry_ts and f["ts"]:
                try:
                    t0 = datetime.fromisoformat(first_entry_ts[:19])
                    t1 = datetime.fromisoformat(f["ts"][:19])
                    mins = round((t1 - t0).total_seconds() / 60.0, 1)
                except (TypeError, ValueError):
                    pass
            tp_details[tier] = {
                "tier": tier,
                "price": round(f["price"], 2),
                "qty": int(f["contracts"]),
                "pct": pct,
                "mins": mins,
            }
        tp_fills = [tp_details[k] for k in sorted(tp_details.keys())]

        out.append({
            **d,
            "avg_entry": round(avg_entry, 2),
            "avg_exit": round(gross_exit / xq, 2) if xq > 0 else None,
            "realized_pnl": round(realized, 2),
            "roi": roi,
            "tp_tiers_hit": sorted({int(f["tp_tier"]) for f in exit_fills if f["tp_tier"]}),
            "first_exit_profitable": first_exit_profitable,
            "exit_qty": xq,
            "entry_qty": eq,
            "mfe_in_pct": mfe_in_pct,
            "mfe_exp_pct": mfe_exp_pct,
            "mae_in_pct": mae_in_pct,
            "mae_exp_pct": mae_exp_pct,
            "actual_pct": actual_pct,
            "capture_pct": capture_pct,
            "capture_exp_pct": capture_exp_pct,
            "tp_fills": tp_fills,
        })
    return _filter_by_range(out, range_key)


# ─── PnL ───────────────────────────────────────────────────────────────────

def monthly_calendar(year: int, month: int) -> dict:
    """Calendar-grid view of the given month. Returns weeks of 7 days (Sun-Sat),
    each day populated with daily P&L stats. Days outside the month are None.

    Output:
        {
            "year": int, "month": int, "label": "May 2026",
            "weeks": [
                [day_or_None, day_or_None, ...],  # 7 entries per week
                ...
            ],
            "week_totals": [
                {"label": "Week 1", "pnl": float, "n_days": int, "n_trades": int}, ...
            ],
            "month_total": float, "month_trades": int, "month_win_rate": float,
            "prev_month": "YYYY-MM", "next_month": "YYYY-MM",
        }
    """
    from calendar import monthcalendar

    # All closed trades, then bucket by entry date in the requested month.
    trades = closed_trades(limit=10000)
    month_start = date(year, month, 1)
    if month == 12:
        next_start = date(year + 1, 1, 1)
    else:
        next_start = date(year, month + 1, 1)

    by_day: dict[date, list[dict]] = defaultdict(list)
    for t in trades:
        ts = t.get("first_entry_ts")
        if not ts:
            continue
        try:
            d = date.fromisoformat(ts[:10])
        except (TypeError, ValueError):
            continue
        if month_start <= d < next_start:
            by_day[d].append(t)

    # monthcalendar returns weeks as lists of day-numbers (Mon-Sun by default
    # if calendar.setfirstweekday is called; we want Sun-Sat).
    import calendar as _cal
    _cal.setfirstweekday(_cal.SUNDAY)
    raw_weeks = _cal.monthcalendar(year, month)

    weeks_out: list[list] = []
    week_totals: list[dict] = []
    for i, w in enumerate(raw_weeks):
        days_row = []
        wk_pnl = 0.0
        wk_days = 0
        wk_trades = 0
        for day_num in w:
            if day_num == 0:
                days_row.append(None)
                continue
            d = date(year, month, day_num)
            ts = by_day.get(d, [])
            n = len(ts)
            if n == 0:
                days_row.append({
                    "day": day_num, "date": d.isoformat(),
                    "n_trades": 0, "pnl": 0.0, "win_rate": None,
                    "has_data": False,
                })
                continue
            wins = sum(1 for t in ts if t["realized_pnl"] > 0)
            pnl = sum(t["realized_pnl"] for t in ts)
            wk_pnl += pnl
            wk_days += 1
            wk_trades += n
            days_row.append({
                "day": day_num, "date": d.isoformat(),
                "n_trades": n,
                "pnl": round(pnl, 2),
                "win_rate": round(wins / n * 100, 1),
                "has_data": True,
            })
        weeks_out.append(days_row)
        week_totals.append({
            "label": f"Week {i + 1}",
            "pnl": round(wk_pnl, 2),
            "n_days": wk_days,
            "n_trades": wk_trades,
        })

    # Month rollup
    month_trades = sum(len(v) for v in by_day.values())
    month_pnl = sum(t["realized_pnl"] for ts in by_day.values() for t in ts)
    month_wins = sum(1 for ts in by_day.values() for t in ts if t["realized_pnl"] > 0)
    month_win_rate = round(month_wins / month_trades * 100, 1) if month_trades else 0.0

    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)

    return {
        "year": year, "month": month,
        "label": month_start.strftime("%B %Y"),
        "weeks": weeks_out,
        "week_totals": week_totals,
        "month_total": round(month_pnl, 2),
        "month_trades": month_trades,
        "month_win_rate": month_win_rate,
        "prev_month": f"{prev_y:04d}-{prev_m:02d}",
        "next_month": f"{next_y:04d}-{next_m:02d}",
    }


def daily_pnl_series(days: int = 30) -> list[dict]:
    """Realized P&L attributed to the day the trade was ENTERED (not exited).
    Days with no entries show 0. Oldest → newest, with running cumulative."""
    today = date.today()
    start = today - timedelta(days=days - 1)
    trades = closed_trades(limit=10000)

    by_day: dict[str, float] = defaultdict(float)
    for t in trades:
        ts = t.get("first_entry_ts")
        if not ts:
            continue
        try:
            d = date.fromisoformat(ts[:10])
        except (TypeError, ValueError):
            continue
        if d < start or d > today:
            continue
        by_day[d.isoformat()] += t["realized_pnl"]

    series: list[dict] = []
    cumulative = 0.0
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        day_pnl = round(by_day.get(d, 0.0), 2)
        cumulative += day_pnl
        series.append({"date": d, "pnl": day_pnl, "cumulative": round(cumulative, 2)})
    return series


def entry_period_series(period: str = "week", count: int | None = None) -> list[dict]:
    """Per-bucket stats attributed to ENTRY date for the trade.

    period:
      - 'day'   → last 30 calendar days
      - 'week'  → last 12 ISO weeks (Monday-start)
      - 'month' → last 12 calendar months

    Each row: label, bucket_start, n_trades, wins, losses, win_rate,
    median_roi_pct, total_pnl, cumulative. Oldest → newest. Leading
    empty buckets trimmed; trailing kept.
    """
    today = date.today()
    trades = closed_trades(limit=10000)

    def _bucket_for(d: date) -> tuple[date, str]:
        if period == "day":
            # "Mon May 4" — day-of-week + month-day
            return d, d.strftime("%a %b %-d")
        if period == "month":
            first = d.replace(day=1)
            return first, first.strftime("%b %Y")
        # default = week
        wk = d - timedelta(days=d.weekday())
        return wk, wk.strftime("%b %-d")

    # Event calendar (FOMC / CPI / earnings) — only used for day period.
    event_tags_by_date: dict[date, list[str]] = defaultdict(list)
    if period == "day":
        try:
            from .rules import Rules
            cal = (Rules.load().raw or {}).get("event_calendar") or {}
            for d_str in (cal.get("fomc_meetings") or []):
                try:
                    fd = date.fromisoformat(str(d_str))
                    # FOMC blackout = Tuesday (day before Wed release) + Wed
                    event_tags_by_date[fd - timedelta(days=1)].append("FOMC eve")
                    event_tags_by_date[fd].append("FOMC")
                except (TypeError, ValueError):
                    pass
            for d_str in (cal.get("cpi_releases") or []):
                try:
                    event_tags_by_date[date.fromisoformat(str(d_str))].append("CPI")
                except (TypeError, ValueError):
                    pass
            for ticker, d_str in (cal.get("megacap_earnings") or {}).items():
                try:
                    event_tags_by_date[date.fromisoformat(str(d_str))].append(f"{ticker.upper()} earnings")
                except (TypeError, ValueError):
                    pass
        except Exception:
            pass

    # Build expected bucket sequence over the lookback window
    if period == "day":
        n = count or 30
        # Walk back over calendar days, keeping only weekdays (Mon-Fri).
        starts = []
        d = today
        while len(starts) < n:
            if d.weekday() < 5:  # 0-4 = Mon-Fri
                starts.append(d)
            d -= timedelta(days=1)
        starts.reverse()
    elif period == "month":
        n = count or 12
        cur = today.replace(day=1)
        starts = []
        for _ in range(n):
            starts.append(cur)
            # step back one month
            cur = (cur - timedelta(days=1)).replace(day=1)
        starts.reverse()
    else:  # week
        n = count or 12
        this_monday = today - timedelta(days=today.weekday())
        starts = [this_monday - timedelta(weeks=n - 1 - i) for i in range(n)]

    starts_set = set(starts)
    bucket_label = {s: _bucket_for(s)[1] for s in starts}

    by_bucket: dict[date, list[dict]] = defaultdict(list)
    for t in trades:
        ts = t.get("first_entry_ts")
        if not ts:
            continue
        try:
            d = date.fromisoformat(ts[:10])
        except (TypeError, ValueError):
            continue
        bstart, _ = _bucket_for(d)
        if bstart not in starts_set:
            continue
        by_bucket[bstart].append(t)

    series: list[dict] = []
    for s in starts:
        ts = by_bucket.get(s, [])
        events = event_tags_by_date.get(s, []) if period == "day" else []
        if ts:
            rois = [t["roi"] for t in ts]
            wins = sum(1 for t in ts if t["realized_pnl"] > 0)
            pnl = sum(t["realized_pnl"] for t in ts)
            series.append({
                "bucket_start": s.isoformat(),
                "label": bucket_label[s],
                "n_trades": len(ts),
                "wins": wins,
                "losses": len(ts) - wins,
                "win_rate": round(wins / len(ts) * 100, 1),
                "median_roi_pct": round(median(rois) * 100, 1),
                "total_pnl": round(pnl, 2),
                "events": events,
            })
        else:
            series.append({
                "bucket_start": s.isoformat(),
                "label": bucket_label[s],
                "n_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "median_roi_pct": 0.0,
                "total_pnl": 0.0,
                "events": events,
            })

    # Trim leading empty buckets
    while series and series[0]["n_trades"] == 0:
        series.pop(0)

    cum = 0.0
    for r in series:
        cum += r["total_pnl"]
        r["cumulative"] = round(cum, 2)
    return series


def weekly_pnl_series(weeks: int = 12) -> list[dict]:
    """Per-week stats attributed to ENTRY week (ISO week starting Monday).
    Returns rows with: label, week_start, n_trades, wins, win_rate,
    median_roi_pct, total_pnl, cumulative. Oldest → newest. Leading empty
    weeks trimmed; trailing kept (current quiet week still shows)."""
    today = date.today()
    monday_this = today - timedelta(days=today.weekday())
    start_monday = monday_this - timedelta(weeks=weeks - 1)
    trades = closed_trades(limit=10000)

    by_week: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        ts = t.get("first_entry_ts")
        if not ts:
            continue
        try:
            d = date.fromisoformat(ts[:10])
        except (TypeError, ValueError):
            continue
        wk_monday = d - timedelta(days=d.weekday())
        if wk_monday < start_monday or wk_monday > monday_this:
            continue
        by_week[wk_monday.isoformat()].append(t)

    series: list[dict] = []
    for i in range(weeks):
        wk = start_monday + timedelta(weeks=i)
        key = wk.isoformat()
        ts = by_week.get(key, [])
        if ts:
            rois = [t["roi"] for t in ts]
            wins = sum(1 for t in ts if t["realized_pnl"] > 0)
            pnl = sum(t["realized_pnl"] for t in ts)
            series.append({
                "week_start": key,
                "label": wk.strftime("%b %-d"),
                "n_trades": len(ts),
                "wins": wins,
                "losses": len(ts) - wins,
                "win_rate": round(wins / len(ts) * 100, 1),
                "median_roi_pct": round(median(rois) * 100, 1),
                "total_pnl": round(pnl, 2),
            })
        else:
            series.append({
                "week_start": key,
                "label": wk.strftime("%b %-d"),
                "n_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "median_roi_pct": 0.0,
                "total_pnl": 0.0,
            })

    # Trim leading empty weeks; keep trailing empties (so the current quiet
    # week still surfaces).
    while series and series[0]["n_trades"] == 0:
        series.pop(0)

    # Running cumulative
    cum = 0.0
    for r in series:
        cum += r["total_pnl"]
        r["cumulative"] = round(cum, 2)
    return series


def advanced_metrics(range_key: str = "all") -> dict:
    """A pack of operational metrics for the P&L tab — designed around the
    questions the user actually asks themselves at end-of-day:
      - Trade quality: avg/median winner & loser, profit factor, expectancy
      - Extremes: largest win, largest loss, top-trade $ concentration
      - Streaks: current run, longest win run, longest loss run
      - TP/chain: TP1/TP2/TP3 hit rates (profitable fills only),
                  chain rate (% of trades that became R2/R3)
      - Discipline: days within daily cap, kill-switch close-call days

    Per communication_prefs: median for per-trade outcomes, sum for $.
    """
    trades = closed_trades(limit=10000, range_key=range_key)
    if not trades:
        return {"empty": True}

    winners = [t for t in trades if t["realized_pnl"] > 0]
    losers = [t for t in trades if t["realized_pnl"] < 0]
    scratches = [t for t in trades if t["realized_pnl"] == 0]

    sum_wins = sum(t["realized_pnl"] for t in winners)
    sum_losses = abs(sum(t["realized_pnl"] for t in losers))  # absolute
    profit_factor = (sum_wins / sum_losses) if sum_losses > 0 else (float("inf") if sum_wins > 0 else 0.0)

    avg_winner = sum_wins / len(winners) if winners else 0.0
    avg_loser = -sum_losses / len(losers) if losers else 0.0  # negative
    med_winner = median([t["realized_pnl"] for t in winners]) if winners else 0.0
    med_loser = median([t["realized_pnl"] for t in losers]) if losers else 0.0
    reward_risk = (avg_winner / abs(avg_loser)) if avg_loser != 0 else None

    # Expectancy per trade = (P_win × avg_win) + (P_loss × avg_loss)
    n = len(trades)
    p_win = len(winners) / n
    p_loss = len(losers) / n
    expectancy = (p_win * avg_winner) + (p_loss * avg_loser)

    # Extremes
    largest_win = max((t["realized_pnl"] for t in winners), default=0.0)
    largest_loss = min((t["realized_pnl"] for t in losers), default=0.0)
    total_pnl = sum(t["realized_pnl"] for t in trades)
    # Top-10 trade concentration — what % of total $ came from top 10 by absolute size
    top10 = sorted(trades, key=lambda t: t["realized_pnl"], reverse=True)[:10]
    top10_sum = sum(t["realized_pnl"] for t in top10)
    top10_pct = (top10_sum / total_pnl * 100) if total_pnl > 0 else 0.0

    # Streaks — walk chronologically by entry date
    by_date = sorted(trades, key=lambda t: t.get("first_entry_ts") or "")
    cur_streak_type = None
    cur_streak_len = 0
    max_win_streak = 0
    max_loss_streak = 0
    win_run = 0
    loss_run = 0
    for t in by_date:
        if t["realized_pnl"] > 0:
            win_run += 1
            loss_run = 0
            cur_streak_type, cur_streak_len = "W", win_run
            max_win_streak = max(max_win_streak, win_run)
        elif t["realized_pnl"] < 0:
            loss_run += 1
            win_run = 0
            cur_streak_type, cur_streak_len = "L", loss_run
            max_loss_streak = max(max_loss_streak, loss_run)
        # scratch trade doesn't break either streak — skip

    # TP hit rates — only PROFITABLE TP fills count as a tier hit
    # (matches the corrected TP1 rate logic on Trends).
    # Pull all profitable exit fills with tier ≤ 3.
    intent_ids = [t["intent_id"] for t in trades]
    tp_hit_count = {1: 0, 2: 0, 3: 0}
    with _connect() as conn:
        if intent_ids:
            placeholders = ",".join("?" * len(intent_ids))
            rows = conn.execute(
                f"SELECT intent_id, tp_tier, price FROM fills "
                f"WHERE is_entry = 0 AND tp_tier IN (1,2,3) "
                f"  AND intent_id IN ({placeholders})",
                intent_ids,
            ).fetchall()
    avg_entry_by_intent = {t["intent_id"]: t["avg_entry"] for t in trades}
    seen: set[tuple[str, int]] = set()
    for r in rows:
        ae = avg_entry_by_intent.get(r["intent_id"])
        if not ae or r["price"] <= ae:
            continue
        key = (r["intent_id"], int(r["tp_tier"]))
        if key in seen:
            continue
        seen.add(key)
        tp_hit_count[int(r["tp_tier"])] += 1
    tp1_rate = round(tp_hit_count[1] / n * 100, 1)
    tp2_rate = round(tp_hit_count[2] / n * 100, 1)
    tp3_rate = round(tp_hit_count[3] / n * 100, 1)

    # Chain metrics — % of trades opened as R2/R3
    chain_legs = sum(1 for t in trades if (t.get("chain_role") or "").upper() in ("R2", "R3"))
    chain_rate = round(chain_legs / n * 100, 1)
    chain_pnl = sum(t["realized_pnl"] for t in trades if (t.get("chain_role") or "").upper() in ("R2", "R3"))
    solo_pnl = sum(t["realized_pnl"] for t in trades if (t.get("chain_role") or "").upper() not in ("R2", "R3"))

    # Days analysis
    days: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        ts = t.get("first_entry_ts")
        if ts:
            days[ts[:10]].append(t)
    n_days = len(days)
    days_with_2plus_losses = sum(1 for ts in days.values() if sum(1 for t in ts if t["realized_pnl"] < 0) >= 2)
    best_day = max(days.values(), key=lambda ts: sum(t["realized_pnl"] for t in ts), default=None)
    worst_day = min(days.values(), key=lambda ts: sum(t["realized_pnl"] for t in ts), default=None)
    best_day_pnl = sum(t["realized_pnl"] for t in best_day) if best_day else 0.0
    worst_day_pnl = sum(t["realized_pnl"] for t in worst_day) if worst_day else 0.0
    best_day_str = best_day[0]["first_entry_ts"][:10] if best_day else "—"
    worst_day_str = worst_day[0]["first_entry_ts"][:10] if worst_day else "—"
    trades_per_day = round(n / n_days, 1) if n_days else 0

    return {
        "empty": False,
        # Trade quality
        "avg_winner": round(avg_winner, 2),
        "avg_loser": round(avg_loser, 2),
        "med_winner": round(med_winner, 2),
        "med_loser": round(med_loser, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
        "reward_risk": round(reward_risk, 2) if reward_risk is not None else None,
        "expectancy": round(expectancy, 2),
        # Extremes
        "largest_win": round(largest_win, 2),
        "largest_loss": round(largest_loss, 2),
        "top10_pct": round(top10_pct, 1),
        # Streaks
        "current_streak_type": cur_streak_type,
        "current_streak_len": cur_streak_len,
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        # TP / chain
        "tp1_rate": tp1_rate,
        "tp2_rate": tp2_rate,
        "tp3_rate": tp3_rate,
        "chain_rate": chain_rate,
        "chain_legs": chain_legs,
        "chain_pnl": round(chain_pnl, 2),
        "solo_pnl": round(solo_pnl, 2),
        # Days
        "n_days": n_days,
        "trades_per_day": trades_per_day,
        "best_day": best_day_str,
        "best_day_pnl": round(best_day_pnl, 2),
        "worst_day": worst_day_str,
        "worst_day_pnl": round(worst_day_pnl, 2),
        "days_with_2plus_losses": days_with_2plus_losses,
        # Counts
        "n_winners": len(winners),
        "n_losers": len(losers),
        "n_scratches": len(scratches),
    }


def pnl_summary(range_key: str = "all") -> dict:
    """Headline totals: today / week / month / all-time + win count.
    Note: range_key narrows the win/loss/median-ROI cohort but the today/week/
    month/all-time card values stay calendar-anchored."""
    trades = closed_trades(limit=10000, range_key=range_key)
    today = date.today()
    week_start = today - timedelta(days=today.weekday())  # Monday
    month_start = today.replace(day=1)

    def _sum(window_start: date) -> float:
        # Attribute P&L to the day the trade was ENTERED so today/week/month
        # reflect entry-cohort performance, not exit-side cash settlement.
        total = 0.0
        for t in trades:
            try:
                entry_d = date.fromisoformat(t["first_entry_ts"][:10])
            except (TypeError, ValueError):
                continue
            if entry_d >= window_start:
                total += t["realized_pnl"]
        return round(total, 2)

    wins = sum(1 for t in trades if t["realized_pnl"] > 0)
    losses = sum(1 for t in trades if t["realized_pnl"] < 0)
    n = len(trades)
    return {
        "trades_total": n,
        "wins": wins,
        "losses": losses,
        "win_rate": round((wins / n * 100), 1) if n > 0 else 0.0,
        "today": _sum(today),
        "week": _sum(week_start),
        "month": _sum(month_start),
        "all_time": round(sum(t["realized_pnl"] for t in trades), 2),
        "median_roi_pct": round(median([t["roi"] for t in trades]) * 100, 1) if trades else 0.0,
    }


# ─── Best / worst tickers ─────────────────────────────────────────────────

def ticker_leaderboard(min_trades: int = 2, range_key: str = "all") -> list[dict]:
    """Per-ticker stats. Sorted by total $ P&L desc."""
    trades = closed_trades(limit=10000, range_key=range_key)
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_ticker[t["ticker"]].append(t)

    rows: list[dict] = []
    for ticker, ts in by_ticker.items():
        if len(ts) < min_trades:
            continue
        rois = [t["roi"] for t in ts]
        pnls = [t["realized_pnl"] for t in ts]
        wins = sum(1 for p in pnls if p > 0)
        rows.append({
            "ticker": ticker,
            "n": len(ts),
            "wins": wins,
            "losses": len(ts) - wins,
            "win_rate": round(wins / len(ts) * 100, 1),
            "total_pnl": round(sum(pnls), 2),
            "median_roi_pct": round(median(rois) * 100, 1),
            "best_pnl": round(max(pnls), 2),
            "worst_pnl": round(min(pnls), 2),
        })
    rows.sort(key=lambda r: r["total_pnl"], reverse=True)
    return rows


def mfe_mae_by_ticker(range_key: str = "all") -> dict:
    """Median MFE-in-trade, MFE-to-expiry, and MAE-in-trade per ticker.
    All values are % from entry (e.g. +45 means the option ran to +45%).
    Total row aggregates across all closed trades.

    Definitions:
      - MFE in trade  = peak option price between entry and exit
                        (best moment to have exited)
      - MFE to expiry = peak price between entry and option expiry
                        (counterfactual: would-have-been peak if held)
      - MAE in trade  = trough price between entry and exit
                        (max adverse — drawdown sat through)
    """
    trades = closed_trades(limit=10000, range_key=range_key)
    if not trades:
        return {"rows": [], "total": None}

    # Pull MFE/MAE columns straight from the intent rows
    with _connect() as conn:
        intent_ids = [t["intent_id"] for t in trades]
        placeholders = ",".join("?" * len(intent_ids))
        meta = {r["intent_id"]: r for r in conn.execute(
            f"SELECT intent_id, mfe_in_trade_price, mae_in_trade_price, "
            f"mfe_to_expiry_price, mae_to_expiry_price "
            f"FROM trade_intents WHERE intent_id IN ({placeholders})",
            intent_ids,
        ).fetchall()}

    by_ticker: dict[str, dict] = defaultdict(lambda: {
        "mfe_in": [], "mfe_exp": [], "mae_in": [], "mae_exp": [],
        "actual": [], "capture": [], "capture_exp": []
    })
    all_buckets = {"mfe_in": [], "mfe_exp": [], "mae_in": [], "mae_exp": [],
                    "actual": [], "capture": [], "capture_exp": []}

    for t in trades:
        m = meta.get(t["intent_id"])
        if not m:
            continue
        ae = t["avg_entry"]
        if not ae or ae <= 0:
            continue
        def _pct(p):
            return ((p - ae) / ae * 100) if p else None
        mfe_in = _pct(m["mfe_in_trade_price"])
        mfe_exp = _pct(m["mfe_to_expiry_price"])
        mae_in = _pct(m["mae_in_trade_price"])
        mae_exp = _pct(m["mae_to_expiry_price"])
        actual = (t["roi"] * 100) if t.get("roi") is not None else None

        # Capture = realized / peak. Only meaningful when peak > 0
        # (can't "capture" a negative peak). Capped 0–100% — slight
        # over/undershoots from rounding get clamped.
        capture = None
        if actual is not None and mfe_in is not None and mfe_in > 0:
            capture = max(0.0, min(100.0, (actual / mfe_in) * 100))
        capture_exp = None
        if actual is not None and mfe_exp is not None and mfe_exp > 0:
            capture_exp = max(0.0, min(100.0, (actual / mfe_exp) * 100))

        bucket = by_ticker[t["ticker"]]
        if mfe_in is not None:
            bucket["mfe_in"].append(mfe_in); all_buckets["mfe_in"].append(mfe_in)
        if mfe_exp is not None:
            bucket["mfe_exp"].append(mfe_exp); all_buckets["mfe_exp"].append(mfe_exp)
        if mae_in is not None:
            bucket["mae_in"].append(mae_in); all_buckets["mae_in"].append(mae_in)
        if mae_exp is not None:
            bucket["mae_exp"].append(mae_exp); all_buckets["mae_exp"].append(mae_exp)
        if actual is not None:
            bucket["actual"].append(actual); all_buckets["actual"].append(actual)
        if capture is not None:
            bucket["capture"].append(capture); all_buckets["capture"].append(capture)
        if capture_exp is not None:
            bucket["capture_exp"].append(capture_exp); all_buckets["capture_exp"].append(capture_exp)

    def _med(arr):
        return round(median(arr), 1) if arr else None

    rows_out: list[dict] = []
    for ticker, bucket in by_ticker.items():
        n = sum(1 for t in trades if t["ticker"] == ticker)
        rows_out.append({
            "ticker": ticker,
            "n": n,
            "mfe_in_pct": _med(bucket["mfe_in"]),
            "actual_pct": _med(bucket["actual"]),
            "capture_pct": _med(bucket["capture"]),
            "mfe_exp_pct": _med(bucket["mfe_exp"]),
            "capture_exp_pct": _med(bucket["capture_exp"]),
            "mae_in_pct": _med(bucket["mae_in"]),
            "mae_exp_pct": _med(bucket["mae_exp"]),
        })
    rows_out.sort(key=lambda r: r["n"], reverse=True)

    total = {
        "ticker": "TOTAL",
        "n": len(trades),
        "mfe_in_pct": _med(all_buckets["mfe_in"]),
        "actual_pct": _med(all_buckets["actual"]),
        "capture_pct": _med(all_buckets["capture"]),
        "mfe_exp_pct": _med(all_buckets["mfe_exp"]),
        "capture_exp_pct": _med(all_buckets["capture_exp"]),
        "mae_in_pct": _med(all_buckets["mae_in"]),
        "mae_exp_pct": _med(all_buckets["mae_exp"]),
    }
    return {"rows": rows_out, "total": total}


def time_to_tp_by_ticker(range_key: str = "all") -> dict:
    """Median time-to-TP1 / TP2 / TP3 per ticker. Time measured from the
    first entry fill to the TP fill, in minutes. Returns:
        {
            "rows":  [{ticker, n, t1_min, t2_min, t3_min, hits1, hits2, hits3}, ...],
            "total": {ticker:"TOTAL", n, t1_min, ...}
        }
    Tickers ordered by trade count desc. Total row aggregates across all
    closed trades (not the mean of per-ticker medians)."""
    trades = closed_trades(limit=10000, range_key=range_key)
    if not trades:
        return {"rows": [], "total": None}

    by_ticker: dict[str, dict] = defaultdict(lambda: {"t1": [], "t2": [], "t3": []})
    all_minutes: dict[str, list[float]] = {"t1": [], "t2": [], "t3": []}

    # Pull TP fill (ts, price) per intent. Only PROFITABLE fills count as
    # TP hits — a "TP1" fill at a price below entry is a stage exit on a
    # losing trade, not a real take-profit.
    avg_entry_by_intent = {t["intent_id"]: t["avg_entry"] for t in trades}
    with _connect() as conn:
        intent_ids = list(avg_entry_by_intent.keys())
        if not intent_ids:
            return {"rows": [], "total": None}
        placeholders = ",".join("?" * len(intent_ids))
        rows = conn.execute(
            f"SELECT intent_id, ts, price, tp_tier FROM fills "
            f"WHERE is_entry = 0 AND tp_tier IS NOT NULL AND intent_id IN ({placeholders}) "
            f"ORDER BY ts ASC",
            intent_ids,
        ).fetchall()

    tp_ts_by_intent: dict[str, dict[int, str]] = defaultdict(dict)
    for r in rows:
        # Skip if fill was at or below entry (not a real TP, just a stage exit)
        avg_entry = avg_entry_by_intent.get(r["intent_id"])
        if avg_entry is None or r["price"] <= avg_entry:
            continue
        tier = int(r["tp_tier"])
        # Keep the earliest profitable fill per tier
        if tier not in tp_ts_by_intent[r["intent_id"]]:
            tp_ts_by_intent[r["intent_id"]][tier] = r["ts"]

    def _minutes_between(a: str, b: str) -> float | None:
        try:
            ta = datetime.fromisoformat(a[:19])
            tb = datetime.fromisoformat(b[:19])
            return (tb - ta).total_seconds() / 60.0
        except (TypeError, ValueError):
            return None

    for t in trades:
        ticker = t["ticker"]
        entry_ts = t.get("first_entry_ts")
        if not entry_ts:
            continue
        tp_ts = tp_ts_by_intent.get(t["intent_id"], {})
        for tier_key, tier in [("t1", 1), ("t2", 2), ("t3", 3)]:
            ts = tp_ts.get(tier)
            if not ts:
                continue
            m = _minutes_between(entry_ts, ts)
            if m is None or m < 0:
                continue
            by_ticker[ticker][tier_key].append(m)
            all_minutes[tier_key].append(m)

    rows_out: list[dict] = []
    for ticker, buckets in by_ticker.items():
        n = sum(1 for t in trades if t["ticker"] == ticker)
        rows_out.append({
            "ticker": ticker,
            "n": n,
            "t1_min": round(median(buckets["t1"]), 1) if buckets["t1"] else None,
            "t2_min": round(median(buckets["t2"]), 1) if buckets["t2"] else None,
            "t3_min": round(median(buckets["t3"]), 1) if buckets["t3"] else None,
            "hits1": len(buckets["t1"]),
            "hits2": len(buckets["t2"]),
            "hits3": len(buckets["t3"]),
        })
    rows_out.sort(key=lambda r: r["n"], reverse=True)

    total = {
        "ticker": "TOTAL",
        "n": len(trades),
        "t1_min": round(median(all_minutes["t1"]), 1) if all_minutes["t1"] else None,
        "t2_min": round(median(all_minutes["t2"]), 1) if all_minutes["t2"] else None,
        "t3_min": round(median(all_minutes["t3"]), 1) if all_minutes["t3"] else None,
        "hits1": len(all_minutes["t1"]),
        "hits2": len(all_minutes["t2"]),
        "hits3": len(all_minutes["t3"]),
    }
    return {"rows": rows_out, "total": total}


def sector_leaderboard(range_key: str = "all") -> list[dict]:
    """Per-sector stats. Sorted by total $ P&L desc. No min-trades cutoff —
    sectors usually have enough volume that even 1 trade is meaningful for
    concentration awareness."""
    trades = closed_trades(limit=10000, range_key=range_key)
    by_sector: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_sector[t.get("sector") or "—"].append(t)

    rows: list[dict] = []
    for sector, ts in by_sector.items():
        rois = [t["roi"] for t in ts]
        pnls = [t["realized_pnl"] for t in ts]
        wins = sum(1 for p in pnls if p > 0)
        unique_tickers = len({t["ticker"] for t in ts})
        rows.append({
            "sector": sector,
            "n": len(ts),
            "tickers": unique_tickers,
            "wins": wins,
            "losses": len(ts) - wins,
            "win_rate": round(wins / len(ts) * 100, 1),
            "total_pnl": round(sum(pnls), 2),
            "median_roi_pct": round(median(rois) * 100, 1),
        })
    rows.sort(key=lambda r: r["total_pnl"], reverse=True)
    return rows


# ─── Trends ────────────────────────────────────────────────────────────────

def _dte_bucket(dte: int) -> str:
    if dte <= 1:
        return "0-1"
    if dte <= 4:
        return "2-4"
    if dte <= 9:
        return "5-9"
    if dte <= 21:
        return "10-21"
    return "22+"


def trends_summary(range_key: str = "all", bucket_minutes: int = 5) -> dict:
    """Operational trend metrics derived from closed trades:
       - TP1 hit rate
       - Median capture % (realized / MFE-to-expiry — proxy via max_option_price)
       - Per-DTE-bucket median ROI + win rate
       - Per-regime median ROI + win rate
       - Day-of-week win rate
    """
    trades = closed_trades(limit=10000, range_key=range_key)
    if not trades:
        return {"empty": True}

    # TP1 hit rate
    # "TP1 hit" = the first exit fill was at a profit. Denominator = all closed
    # trades (winners + losers). A trade stopped out before any profit doesn't
    # count as TP1 hit even if it has multiple sell fills.
    tp1_hits = sum(1 for t in trades if t.get("first_exit_profitable"))
    tp1_rate = round(tp1_hits / len(trades) * 100, 1)

    # Capture proxy: realized / (max_price - avg_entry)*qty*100
    captures = []
    for t in trades:
        mp = t.get("max_option_price")
        if not mp or not t["avg_entry"] or mp <= t["avg_entry"]:
            continue
        peak_pnl = (mp - t["avg_entry"]) * t["entry_qty"] * 100
        if peak_pnl > 0:
            captures.append(t["realized_pnl"] / peak_pnl)
    capture_med = round(median(captures) * 100, 1) if captures else None

    # Per-DTE bucket
    by_dte: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        try:
            entry_d = date.fromisoformat(t["first_entry_ts"][:10])
            exp_d = date.fromisoformat(t["expiry"])
            dte = (exp_d - entry_d).days
            by_dte[_dte_bucket(dte)].append(t)
        except (TypeError, ValueError):
            continue
    dte_rows = []
    for bucket in ["0-1", "2-4", "5-9", "10-21", "22+"]:
        ts = by_dte.get(bucket, [])
        if not ts:
            continue
        rois = [t["roi"] for t in ts]
        wins = sum(1 for t in ts if t["realized_pnl"] > 0)
        dte_rows.append({
            "bucket": bucket,
            "n": len(ts),
            "win_rate": round(wins / len(ts) * 100, 1),
            "median_roi_pct": round(median(rois) * 100, 1),
            "total_pnl": round(sum(t["realized_pnl"] for t in ts), 2),
        })

    # Per-regime
    by_regime: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_regime[t.get("regime_tag") or "—"].append(t)
    regime_rows = []
    for regime, ts in by_regime.items():
        rois = [t["roi"] for t in ts]
        wins = sum(1 for t in ts if t["realized_pnl"] > 0)
        regime_rows.append({
            "regime": regime,
            "n": len(ts),
            "win_rate": round(wins / len(ts) * 100, 1),
            "median_roi_pct": round(median(rois) * 100, 1),
            "total_pnl": round(sum(t["realized_pnl"] for t in ts), 2),
        })
    # Order: HOT > WARM > NORMAL > TRAP > COLD > others
    order = {"HOT": 0, "WARM": 1, "NORMAL": 2, "TRAP": 3, "COLD": 4}
    regime_rows.sort(key=lambda r: order.get(r["regime"], 99))

    # Day of week
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    by_dow: dict[int, list[dict]] = defaultdict(list)
    for t in trades:
        try:
            d = date.fromisoformat(t["first_entry_ts"][:10])
            by_dow[d.weekday()].append(t)
        except (TypeError, ValueError):
            continue
    dow_rows = []
    for i, name in enumerate(dow_names):
        ts = by_dow.get(i, [])
        if not ts:
            continue
        wins = sum(1 for t in ts if t["realized_pnl"] > 0)
        dow_rows.append({
            "day": name,
            "n": len(ts),
            "win_rate": round(wins / len(ts) * 100, 1),
            "median_roi_pct": round(median([t["roi"] for t in ts]) * 100, 1),
            "total_pnl": round(sum(t["realized_pnl"] for t in ts), 2),
        })

    # Intraday entry-time bucket — 10-minute increments, ET (handles DST via
    # zoneinfo). Stored timestamps are tz-naive UTC; we attach UTC then convert
    # to America/New_York. Only market-hours buckets surface
    # (09:30–16:00 ET); pre-market / after-hours entries are skipped.
    try:
        from zoneinfo import ZoneInfo
        UTC = ZoneInfo("UTC")
        ET = ZoneInfo("America/New_York")
    except Exception:
        UTC = ET = None

    intraday_rows = []
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        try:
            raw = t["first_entry_ts"]
            if not raw or UTC is None:
                continue
            dt = datetime.fromisoformat(raw[:19]).replace(tzinfo=UTC).astimezone(ET)
            mins_from_open = (dt.hour - 9) * 60 + (dt.minute - 30)
            if mins_from_open < 0 or mins_from_open >= 390:  # 09:30–16:00
                continue
            # Normalize bucket size: 5 / 10 / 60. Default to 5 if invalid.
            bw = bucket_minutes if bucket_minutes in (5, 10, 60) else 5
            if bw == 60:
                bucket = f"{dt.hour:02d}:00"
            else:
                bucket_min = (dt.minute // bw) * bw
                bucket = f"{dt.hour:02d}:{bucket_min:02d}"
            by_bucket[bucket].append(t)
        except (TypeError, ValueError, KeyError):
            continue
    bw = bucket_minutes if bucket_minutes in (5, 10, 60) else 5
    for bucket in sorted(by_bucket.keys()):
        ts = by_bucket[bucket]
        wins = sum(1 for t in ts if t["realized_pnl"] > 0)
        # Label as a range: "09:30–09:34 ET" (5m), "09:30–09:39" (10m), "10:00–10:59" (60m)
        h, m = int(bucket[:2]), int(bucket[3:])
        end_m = m + (bw - 1)
        end_h = h
        if end_m >= 60:
            end_m -= 60
            end_h += 1
        intraday_rows.append({
            "hour": f"{bucket}–{end_h:02d}:{end_m:02d}",
            "n": len(ts),
            "win_rate": round(wins / len(ts) * 100, 1),
            "median_roi_pct": round(median([t["roi"] for t in ts]) * 100, 1),
            "total_pnl": round(sum(t["realized_pnl"] for t in ts), 2),
        })

    # MFE-vs-realized scatter — each point: (mfe_pct, realized_pct, ticker, win)
    # mfe_pct = (max_option_price - avg_entry) / avg_entry  (peak unrealized %)
    # realized_pct = roi  (actual exit %)
    # The y=x diagonal is "perfect capture"; below it = money left on table.
    scatter = []
    for t in trades:
        mp = t.get("max_option_price")
        ae = t.get("avg_entry")
        if not mp or not ae or ae <= 0:
            continue
        mfe_pct = (mp - ae) / ae * 100
        realized_pct = t["roi"] * 100
        scatter.append({
            "ticker": t["ticker"],
            "mfe": round(mfe_pct, 1),
            "realized": round(realized_pct, 1),
            "win": t["realized_pnl"] > 0,
        })

    return {
        "empty": False,
        "tp1_rate": tp1_rate,
        "capture_pct": capture_med,
        "n_trades": len(trades),
        "dte_rows": dte_rows,
        "regime_rows": regime_rows,
        "dow_rows": dow_rows,
        "intraday_rows": intraday_rows,
        "scatter": scatter,
    }


# ─── Leakage analysis ─────────────────────────────────────────────────────
# Two rollups that answer "where am I leaving money on the table?":
#   1. leakage_scatter — one dot per closed trade plotting in-trade MFE %
#      against realized %. Below-diagonal cluster = systematic leakage.
#   2. stop_discipline — for every Stop fill (price < max(entry, prev TP)),
#      look at the next N trading minutes in the cached Polygon bars and
#      measure how often the option recovered to entry or to the floor it
#      breached. Quantifies "I always stop right before the rally" feels
#      with an actual percentage.

def leakage_scatter(range_key: str = "all") -> list[dict]:
    """One dot per closed trade for the MFE-vs-realized scatter plot."""
    trades = closed_trades(limit=5000, range_key=range_key)
    out = []
    for t in trades:
        if t.get("mfe_in_pct") is None or t.get("actual_pct") is None:
            continue
        out.append({
            "intent_id":    t["intent_id"],
            "ticker":       t["ticker"],
            "mfe":          t["mfe_in_pct"],
            "realized":     t["actual_pct"],
            "realized_pnl": t["realized_pnl"],
            "win":          t["realized_pnl"] > 0,
            "first_entry":  t.get("first_entry_ts"),
        })
    return out


def _parse_et_ts(ts: str) -> Optional[datetime]:
    """Naive fill timestamps are broker-local (ET). Localize explicitly."""
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts[:19])
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_ET)
    return d


def stop_discipline(range_key: str = "all",
                    lookback_minutes: int = 60) -> dict:
    """For every stop fill across closed trades, look at the next
    `lookback_minutes` trading minutes in the cached bars and report:
      - how often the option recovered to the entry price
      - how often it recovered to the floor it breached (max of entry
        and prior TP) — the "ideal" exit reference
      - the median minutes from stop to the post-stop high
      - the median peak-vs-stop give-back, expressed as a % of entry
    """
    from src import bars_store
    from src.contract_symbols import build_occ_symbol

    trades = closed_trades(limit=5000, range_key=range_key)

    # Phase 1 — identify stop fills using the same rule the chart uses
    # (an exit whose price is below max(entry, last classified TP price)).
    stops: list[dict] = []
    for t in trades:
        with _connect() as conn:
            fills = conn.execute(
                "SELECT ts, contracts, price, is_entry, tp_tier "
                "FROM fills WHERE intent_id=? ORDER BY ts ASC, rowid ASC",
                (t["intent_id"],),
            ).fetchall()

        entry_price   = None
        prev_tp_price = None
        for f in fills:
            price = float(f["price"])
            if f["is_entry"] == 1:
                if entry_price is None:
                    entry_price = price
                continue
            refs  = [v for v in (entry_price, prev_tp_price) if v is not None]
            floor = max(refs) if refs else None
            if floor is not None and price < floor:
                # DTE at stop = days from entry day to expiry. We bucket
                # it later into 0 / 1–2 / 3–7 / 8+ for the rollup.
                dte = None
                try:
                    fe = t.get("first_entry_ts")
                    if fe and t.get("expiry"):
                        d_entry  = date.fromisoformat(fe[:10])
                        d_expiry = date.fromisoformat(str(t["expiry"])[:10])
                        dte = (d_expiry - d_entry).days
                except (TypeError, ValueError):
                    pass
                stops.append({
                    "intent_id":   t["intent_id"],
                    "ticker":      t["ticker"],
                    "expiry":      t.get("expiry"),
                    "strike":      t.get("strike"),
                    "right":       t.get("right"),
                    "stop_ts":     f["ts"],
                    "stop_price":  price,
                    "entry_price": entry_price,
                    "floor":       floor,
                    "dte":         dte,
                })
            elif f["tp_tier"]:
                prev_tp_price = price

    # Phase 2 — for each stop, look up bars and compute recovery stats.
    bars_cache: dict[str, Any] = {}
    results: list[dict] = []
    for s in stops:
        try:
            occ = build_occ_symbol(
                s["ticker"], date.fromisoformat(str(s["expiry"])[:10]),
                s["right"], float(s["strike"]),
            )
        except (ValueError, TypeError):
            continue
        df = bars_cache.get(occ)
        if df is None:
            try:
                df = bars_store.load_option_bars(occ)
            except FileNotFoundError:
                continue
            bars_cache[occ] = df
        if df is None or df.empty:
            continue

        stop_dt = _parse_et_ts(s["stop_ts"])
        if stop_dt is None:
            continue
        stop_ms = int(stop_dt.timestamp() * 1000)
        end_ms  = stop_ms + lookback_minutes * 60_000

        post = df[(df["t"] > stop_ms) & (df["t"] <= end_ms)].dropna(subset=["h"])
        if post.empty:
            continue
        idx_high = post["h"].idxmax()
        max_high = float(post.loc[idx_high, "h"])
        peak_ms  = int(post.loc[idx_high, "t"])
        mins_to_peak = (peak_ms - stop_ms) / 60_000

        results.append({
            **s,
            "post_max":           round(max_high, 2),
            "recovered_to_entry": max_high >= s["entry_price"],
            "recovered_to_floor": max_high >= s["floor"],
            "mins_to_peak":       round(mins_to_peak, 1),
            # Give-back as % of entry: how much higher the peak got vs the
            # price you stopped at. Positive = you stopped before the rally.
            "giveback_pct":       round(
                (max_high - s["stop_price"]) / s["entry_price"] * 100, 1
            ),
        })

    n = len(results)
    if n == 0:
        return {
            "n":                   0,
            "lookback_minutes":    lookback_minutes,
            "recent":              [],
            "by_ticker":           [],
            "by_hour":             [],
            "by_dte":              [],
        }

    def _bucket_stats(rs: list[dict]) -> dict:
        nb = len(rs)
        return {
            "n":                      nb,
            "pct_recovered_to_entry": round(
                sum(1 for r in rs if r["recovered_to_entry"]) / nb * 100, 1),
            "pct_recovered_to_floor": round(
                sum(1 for r in rs if r["recovered_to_floor"]) / nb * 100, 1),
            "median_giveback_pct":    round(
                median(r["giveback_pct"] for r in rs), 1),
            "median_mins_to_peak":    round(
                median(r["mins_to_peak"] for r in rs), 1),
        }

    # By ticker — only show tickers with ≥2 stops so single noisy samples
    # don't dominate. Sorted worst-first (highest recovered-to-entry %).
    by_ticker_map: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_ticker_map[r["ticker"]].append(r)
    by_ticker = [
        {"key": k, **_bucket_stats(v)}
        for k, v in by_ticker_map.items() if len(v) >= 2
    ]
    by_ticker.sort(key=lambda x: (-x["pct_recovered_to_entry"], -x["n"]))

    # By hour-of-day (ET). 9-15 cover RTH. Sort by hour for natural order.
    by_hour_map: dict[int, list[dict]] = defaultdict(list)
    for r in results:
        dt = _parse_et_ts(r["stop_ts"])
        if dt is None:
            continue
        by_hour_map[dt.hour].append(r)
    by_hour = [
        {"key": h, "label": f"{h:02d}:00", **_bucket_stats(v)}
        for h, v in sorted(by_hour_map.items())
    ]

    # By DTE bucket — 0 / 1–2 / 3–7 / 8+. Useful for finding the DTE
    # bucket where your stops most often fire too early (typically 0DTE).
    def _dte_label(d):
        if d is None:    return "Unknown"
        if d == 0:       return "0DTE"
        if d <= 2:       return "1–2"
        if d <= 7:       return "3–7"
        return "8+"
    by_dte_map: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_dte_map[_dte_label(r.get("dte"))].append(r)
    # Keep a stable order regardless of which buckets are non-empty.
    _dte_order = ["0DTE", "1–2", "3–7", "8+", "Unknown"]
    by_dte = [
        {"key": k, **_bucket_stats(by_dte_map[k])}
        for k in _dte_order if k in by_dte_map
    ]

    return {
        "n":                       n,
        "n_stops_total":           len(stops),
        "lookback_minutes":        lookback_minutes,
        "pct_recovered_to_entry":  round(
            sum(1 for r in results if r["recovered_to_entry"]) / n * 100, 1),
        "pct_recovered_to_floor":  round(
            sum(1 for r in results if r["recovered_to_floor"]) / n * 100, 1),
        "median_mins_to_peak":     round(
            median(r["mins_to_peak"] for r in results), 1),
        "median_giveback_pct":     round(
            median(r["giveback_pct"] for r in results), 1),
        "avg_giveback_pct":        round(
            sum(r["giveback_pct"] for r in results) / n, 1),
        "by_ticker": by_ticker,
        "by_hour":   by_hour,
        "by_dte":    by_dte,
        # 20 most recent stops for the detail table.
        "recent": sorted(results, key=lambda r: r["stop_ts"], reverse=True)[:20],
    }
