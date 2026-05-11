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


# ─── Levels analysis ──────────────────────────────────────────────────────
# Connects published support/resistance levels to actual trade outcomes:
#   - per-(ticker, level) win rate / median ROI / median capture
#   - "position class" buckets (ATH break, near-resistance, near-support,
#     off-level) and their aggregate win rates
#   - ATH-break detail list
#
# An "active levels" lookup uses the most recent ticker_levels snapshot
# whose asof_ts is on-or-before the trade's entry timestamp — so we don't
# leak future-published levels into historical analyses.

def _underlying_price_at(ticker: str, ts_iso: str) -> Optional[float]:
    """Closest cached underlying bar close to the given naive-ET timestamp.

    Returns None if no bars are cached for the ticker. When the timestamp
    is outside the cached window we snap to the nearest edge (which is
    accurate enough for "what was the price when I entered this trade"
    within a few minutes).
    """
    import bisect as _bisect
    try:
        from src import bars_store
    except ImportError:
        return None
    try:
        df = bars_store.load_underlying_bars(ticker)
    except FileNotFoundError:
        return None
    if df is None or df.empty:
        return None

    try:
        dt = datetime.fromisoformat(ts_iso[:19])
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ET)
    target_ms = int(dt.timestamp() * 1000)

    df_sorted = df.dropna(subset=["t", "c"]).sort_values("t").reset_index(drop=True)
    if df_sorted.empty:
        return None
    times = df_sorted["t"].astype("int64").tolist()
    j = _bisect.bisect_left(times, target_ms)
    if j == 0:
        return float(df_sorted["c"].iloc[0])
    if j >= len(times):
        return float(df_sorted["c"].iloc[-1])
    if abs(times[j] - target_ms) < abs(times[j-1] - target_ms):
        return float(df_sorted["c"].iloc[j])
    return float(df_sorted["c"].iloc[j-1])


def _active_levels_at(ticker: str, ts_iso: str):
    """Most recent ticker_levels snapshot at or before `ts_iso`. Returns
    a LevelSnapshot or None."""
    from . import levels as _lv
    with _connect() as conn:
        r = conn.execute(
            "SELECT * FROM ticker_levels "
            "WHERE ticker = ? AND asof_ts <= ? "
            "ORDER BY asof_ts DESC LIMIT 1",
            (ticker.upper(), ts_iso),
        ).fetchone()
    if r is None:
        return None
    return _lv.LevelSnapshot(
        ticker=r["ticker"],
        asof_ts=r["asof_ts"],
        current_price=r["current_price"],
        levels_below=_lv._parse_pipe_levels(r["levels_below"]),
        levels_above=_lv._parse_pipe_levels(r["levels_above"]),
        source=r["source"] or "",
        note=r["note"] or "",
    )


def _classify_position(underlying: float, snap, proximity_pct: float) -> dict:
    """Classify where the underlying sat relative to published levels.

    Returns a dict with `bucket` (one of: ath_break, below_all_support,
    near_resistance, near_support, mid_range, off_level) plus the
    nearest support/resistance and their distance percentages.
    """
    below = sorted(snap.levels_below or [])
    above = sorted(snap.levels_above or [])

    nearest_above = min((lvl for lvl in above if lvl >= underlying), default=None)
    nearest_below = max((lvl for lvl in below if lvl <= underlying), default=None)

    dist_above_pct = ((nearest_above - underlying) / underlying * 100
                      if nearest_above is not None else None)
    dist_below_pct = ((underlying - nearest_below) / underlying * 100
                      if nearest_below is not None else None)

    # ATH break: underlying above all mapped resistance
    if above and underlying > max(above):
        bucket = "ath_break"
    # Below all support — capitulation / break of structure
    elif below and underlying < min(below):
        bucket = "below_all_support"
    elif dist_above_pct is not None and dist_above_pct <= proximity_pct:
        bucket = "near_resistance"
    elif dist_below_pct is not None and dist_below_pct <= proximity_pct:
        bucket = "near_support"
    elif nearest_above is None and nearest_below is None:
        bucket = "off_level"
    else:
        bucket = "mid_range"

    return {
        "bucket":         bucket,
        "nearest_above":  nearest_above,
        "nearest_below":  nearest_below,
        "dist_above_pct": dist_above_pct,
        "dist_below_pct": dist_below_pct,
    }


def _trade_outcome_stats(trades: list[dict]) -> dict:
    """Aggregate (n, win_rate, median_realized, median_capture) from a
    list of closed_trades() rows."""
    n = len(trades)
    if n == 0:
        return {"n": 0, "win_rate": None, "median_roi": None,
                "median_capture": None, "median_mfe": None, "total_pnl": 0}
    wins = sum(1 for t in trades if (t.get("realized_pnl") or 0) > 0)
    rois = [t["actual_pct"] for t in trades if t.get("actual_pct") is not None]
    caps = [t["capture_pct"] for t in trades if t.get("capture_pct") is not None]
    mfes = [t["mfe_in_pct"]  for t in trades if t.get("mfe_in_pct")  is not None]
    total_pnl = sum(t.get("realized_pnl") or 0 for t in trades)
    return {
        "n":              n,
        "win_rate":       round(wins / n * 100, 1),
        "median_roi":     round(median(rois), 1) if rois else None,
        "median_capture": round(median(caps), 1) if caps else None,
        "median_mfe":     round(median(mfes), 1) if mfes else None,
        "total_pnl":      round(total_pnl, 0),
    }


def levels_analysis(range_key: str = "all",
                    proximity_pct: float = 0.5) -> dict:
    """Connect published levels to trade outcomes.

    For each closed trade in `range_key`:
      1. Look up the underlying price at entry (closest cached bar).
      2. Look up the levels snapshot active at entry time (latest snapshot
         on or before the entry timestamp).
      3. Classify the position (ATH break / near-resistance / near-support /
         mid-range / etc.).
      4. Record the trade against the nearest published level.

    Then roll up by position-class and by (ticker, level).
    """
    trades = closed_trades(limit=5000, range_key=range_key)

    classified: list[dict] = []
    no_bars   = 0
    no_levels = 0

    # For each trade, find its underlying price + active levels + nearest level
    for t in trades:
        entry_ts = t.get("first_entry_ts")
        if not entry_ts:
            continue
        ticker = t["ticker"]
        under  = _underlying_price_at(ticker, entry_ts)
        if under is None:
            no_bars += 1
            continue
        snap = _active_levels_at(ticker, entry_ts)
        if snap is None or (not snap.levels_above and not snap.levels_below):
            no_levels += 1
            continue
        cls = _classify_position(under, snap, proximity_pct)

        # Nearest level overall — break ties toward the one above (more
        # interesting for typical bullish call trades)
        nearest_level = None
        nearest_dist  = None
        if cls["nearest_above"] is not None:
            nearest_level = cls["nearest_above"]
            nearest_dist  = cls["dist_above_pct"]
        if cls["nearest_below"] is not None:
            d_below = cls["dist_below_pct"]
            if nearest_dist is None or d_below < nearest_dist:
                nearest_level = cls["nearest_below"]
                nearest_dist  = d_below

        classified.append({
            **t,
            "underlying_at_entry": under,
            "bucket":              cls["bucket"],
            "nearest_above":       cls["nearest_above"],
            "nearest_below":       cls["nearest_below"],
            "dist_above_pct":      cls["dist_above_pct"],
            "dist_below_pct":      cls["dist_below_pct"],
            "nearest_level":       nearest_level,
            "nearest_dist_pct":    nearest_dist,
        })

    # ── Position-class rollup ─────────────────────────────────────────
    bucket_labels = {
        "ath_break":          "ATH break",
        "near_resistance":    "Near resistance",
        "near_support":       "Near support",
        "below_all_support":  "Below all support",
        "mid_range":          "Mid-range",
        "off_level":          "Off-level (no published levels)",
    }
    by_bucket_map: dict[str, list[dict]] = defaultdict(list)
    for c in classified:
        by_bucket_map[c["bucket"]].append(c)
    bucket_order = ["ath_break", "near_resistance", "near_support",
                    "mid_range", "below_all_support", "off_level"]
    by_bucket = []
    for k in bucket_order:
        if k not in by_bucket_map:
            continue
        s = _trade_outcome_stats(by_bucket_map[k])
        by_bucket.append({"key": k, "label": bucket_labels[k], **s})

    # ── Per-(ticker, level) rollup ────────────────────────────────────
    # A trade "hits" a level if its underlying at entry was within
    # `proximity_pct` of that level. We use the nearest level (above or
    # below) to avoid double-counting.
    per_level_map: dict[tuple, list[dict]] = defaultdict(list)
    for c in classified:
        if c["nearest_dist_pct"] is None or c["nearest_dist_pct"] > proximity_pct:
            continue
        per_level_map[(c["ticker"], c["nearest_level"])].append(c)

    per_level = []
    for (tkr, lvl), trades_at in per_level_map.items():
        if len(trades_at) < 2:   # skip single-trade levels (noisy)
            continue
        s = _trade_outcome_stats(trades_at)
        # Resistance vs support hint — was this level above or below at entry?
        side = "resistance" if any(c["nearest_above"] == lvl for c in trades_at) else "support"
        # Trades behind the row, in chronological order, with just enough
        # fields to render an inline expandable detail panel without
        # requiring a second fetch.
        details = []
        for c in sorted(trades_at, key=lambda x: x.get("first_entry_ts") or ""):
            details.append({
                "intent_id":     c["intent_id"],
                "ticker":        c["ticker"],
                "strike":        c.get("strike"),
                "right":         c.get("right"),
                "expiry":        c.get("expiry"),
                "entry_ts":      c.get("first_entry_ts"),
                "exit_ts":       c.get("last_exit_ts"),
                "underlying":    c.get("underlying_at_entry"),
                "actual_pct":    c.get("actual_pct"),
                "realized_pnl":  c.get("realized_pnl"),
                "capture_pct":   c.get("capture_pct"),
                "mfe_in_pct":    c.get("mfe_in_pct"),
            })
        per_level.append({
            "ticker":   tkr,
            "level":    lvl,
            "side":     side,
            **s,
            "trades":   details,
        })
    per_level.sort(key=lambda r: (-r["n"], -(r["win_rate"] or 0)))

    # ── ATH break detail ───────────────────────────────────────────────
    ath = by_bucket_map.get("ath_break", [])
    ath_summary = _trade_outcome_stats(ath)
    ath_recent = sorted(
        ath, key=lambda c: c.get("first_entry_ts") or "", reverse=True
    )[:20]

    return {
        "range_key":      range_key,
        "proximity_pct":  proximity_pct,
        "n_total":        len(trades),
        "n_analyzed":     len(classified),
        "n_no_bars":      no_bars,
        "n_no_levels":    no_levels,
        "by_bucket":      by_bucket,
        "per_level":      per_level,
        "ath_break":      ath_summary,
        "ath_recent":     ath_recent,
    }


# ─── Winner profile ───────────────────────────────────────────────────────
# A single rollup that compares winning vs losing trades (and top-quartile
# vs bottom-quartile by realized %) across every feature we have:
# entry time, hold time, DTE, distance to nearest level, level bucket,
# pregame coverage/verdict, 0DTE flag, TP1 hit rate, first-trade-of-day.

def _winner_extract_entry_hour(t) -> Optional[float]:
    ts = t.get("first_entry_ts")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts[:19])
    except ValueError:
        return None
    return dt.hour + dt.minute / 60.0


def _winner_hold_minutes(t) -> Optional[float]:
    e, x = t.get("first_entry_ts"), t.get("last_exit_ts")
    if not e or not x:
        return None
    try:
        return (datetime.fromisoformat(x[:19])
                - datetime.fromisoformat(e[:19])).total_seconds() / 60
    except ValueError:
        return None


def _winner_dte(t) -> Optional[int]:
    e, exp = t.get("first_entry_ts"), t.get("expiry")
    if not e or not exp:
        return None
    try:
        return (date.fromisoformat(str(exp)[:10])
                - date.fromisoformat(e[:10])).days
    except (ValueError, TypeError):
        return None


def _winner_pregame_lookup(date_str: str, _cache: dict) -> Optional[dict]:
    if date_str in _cache:
        return _cache[date_str]
    from . import analysis as _anl
    res = _anl.get_cached(date_str)
    if res and res.get("status") == "ok":
        _cache[date_str] = res.get("analysis") or {}
    else:
        _cache[date_str] = None
    return _cache[date_str]


def _agg(values: list, kind: str) -> Optional[float]:
    """median (numeric values) or pct_true (booleans) — None when empty."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    if kind == "median":
        return float(median(vals))
    if kind == "pct_true":
        n = len(vals)
        true_n = sum(1 for v in vals if bool(v))
        return round(true_n / n * 100, 1) if n else None
    raise ValueError(f"unknown agg kind {kind!r}")


def _fmt_winner_cell(value: Optional[float], unit: str) -> str:
    if value is None:
        return "—"
    if unit == "hour":   # e.g. 10.5 → "10:30"
        h = int(value); m = int(round((value - h) * 60))
        if m == 60:
            h += 1; m = 0
        return f"{h:02d}:{m:02d}"
    if unit == "min":
        return f"{int(round(value))}m"
    if unit == "day":
        return f"{int(round(value))}d"
    if unit == "pct":
        return f"{value:.1f}%"
    if unit == "pct_pt":
        return f"{value:.0f}%"
    return f"{value:.1f}"


def _fmt_delta(d: Optional[float], unit: str) -> str:
    if d is None:
        return "—"
    sign = "+" if d > 0 else ""   # negative numbers carry their own minus
    if unit == "hour":
        # delta in hours → show in mins
        mins = round(d * 60)
        return f"{'+' if mins > 0 else ''}{mins}m"
    if unit == "min":
        return f"{sign}{int(round(d))}m"
    if unit == "day":
        return f"{sign}{int(round(d))}d"
    if unit == "pct":
        return f"{sign}{d:.1f}pt"
    if unit == "pct_pt":
        return f"{sign}{d:.0f}pt"
    return f"{sign}{d:.1f}"


def winner_profile(range_key: str = "all",
                   proximity_pct: float = 0.5) -> dict:
    """Compare winners vs losers (and top-Q vs bottom-Q by realized %)
    across every per-trade feature we have."""
    trades = closed_trades(limit=5000, range_key=range_key)

    # Enrich each trade with level classification + computed features.
    pregame_cache: dict = {}
    classified: list[dict] = []
    for t in trades:
        entry_ts = t.get("first_entry_ts")
        ticker   = t.get("ticker")

        # Level bucket + nearest distance (re-using the same logic as
        # levels_analysis; cheap to redo)
        under = _underlying_price_at(ticker, entry_ts) if entry_ts else None
        snap  = _active_levels_at(ticker, entry_ts)   if entry_ts else None
        if under is not None and snap is not None and (snap.levels_above or snap.levels_below):
            cls = _classify_position(under, snap, proximity_pct)
            bucket    = cls["bucket"]
            nearest_d = None
            if cls["dist_above_pct"] is not None:
                nearest_d = cls["dist_above_pct"]
            if cls["dist_below_pct"] is not None and (
                nearest_d is None or cls["dist_below_pct"] < nearest_d
            ):
                nearest_d = cls["dist_below_pct"]
        else:
            bucket    = None
            nearest_d = None

        # Pregame coverage — has a pregame analysis on entry day that names
        # this ticker? What's the verdict if yes?
        pregame_named  = False
        pregame_like   = False
        if entry_ts:
            pg = _winner_pregame_lookup(entry_ts[:10], pregame_cache)
            if pg:
                picks = pg.get("picks") or []
                for p in picks:
                    if (p.get("ticker") or "").upper() == (ticker or "").upper():
                        pregame_named = True
                        pregame_like  = (p.get("verdict") == "LIKE")
                        break

        classified.append({
            **t,
            "_entry_hour":     _winner_extract_entry_hour(t),
            "_hold_minutes":   _winner_hold_minutes(t),
            "_dte":            _winner_dte(t),
            "_bucket":         bucket,
            "_dist_pct":       nearest_d,
            "_pregame_named":  pregame_named,
            "_pregame_like":   pregame_like,
            "_is_0dte":        (_winner_dte(t) == 0) if _winner_dte(t) is not None else None,
            # Price-based, not broker-tag-based — the broker tags some stops
            # as TP1 even when they print below entry, which inflates the
            # tag-based TP1-hit measure. Use first_exit_profitable instead
            # (first exit fill at a higher price than avg entry).
            "_first_exit_win": bool(t.get("first_exit_profitable")),
            "_is_index":       (t.get("ticker") or "").upper() in ("QQQ", "SPY", "SPX", "IWM"),
        })

    # First-trade-of-day flag — needs the full set in hand.
    by_date: dict[str, list[dict]] = defaultdict(list)
    for c in classified:
        ts = c.get("first_entry_ts")
        if ts:
            by_date[ts[:10]].append(c)
    for d, day_trades in by_date.items():
        if not day_trades:
            continue
        earliest = min(day_trades, key=lambda x: x.get("first_entry_ts") or "")
        for c in day_trades:
            c["_first_of_day"] = (c["intent_id"] == earliest["intent_id"])
    for c in classified:
        c.setdefault("_first_of_day", None)

    # ── Cohort splits ─────────────────────────────────────────────────
    winners = [c for c in classified if (c.get("realized_pnl") or 0) > 0]
    losers  = [c for c in classified if (c.get("realized_pnl") or 0) <= 0]

    # Top/bottom quartile by realized %
    have_pct = [c for c in classified if c.get("actual_pct") is not None]
    have_pct.sort(key=lambda c: c["actual_pct"])
    q = max(1, len(have_pct) // 4)
    bottom_q = have_pct[:q]
    top_q    = have_pct[-q:]

    # ── Dimensions ────────────────────────────────────────────────────
    # Each row pulls a value from each cohort and formats it.
    DIMENSIONS = [
        # (label, field, agg, unit)
        ("Entry hour (median)",            "_entry_hour",    "median",   "hour"),
        ("Time in trade (median)",         "_hold_minutes",  "median",   "min"),
        ("DTE at entry (median)",          "_dte",           "median",   "day"),
        ("Distance to nearest level",      "_dist_pct",      "median",   "pct"),
        ("ATH-break entries",              "_bucket",        "ath",      "pct_pt"),
        ("Near-resistance entries",        "_bucket",        "near_res", "pct_pt"),
        ("Near-support entries",           "_bucket",        "near_sup", "pct_pt"),
        ("Mid-range entries",              "_bucket",        "mid",      "pct_pt"),
        ("Index name (QQQ/SPY/SPX/IWM)",   "_is_index",      "pct_true", "pct_pt"),
        ("0DTE entries",                   "_is_0dte",       "pct_true", "pct_pt"),
        ("First exit profitable",          "_first_exit_win","pct_true", "pct_pt"),
        ("First trade of the day",         "_first_of_day",  "pct_true", "pct_pt"),
        ("Pregame mention",                "_pregame_named", "pct_true", "pct_pt"),
        ("Pregame verdict LIKE",           "_pregame_like",  "pct_true", "pct_pt"),
    ]

    def cohort_val(cohort, field, agg):
        # Bucket-derived agg keys roll up boolean membership of a class.
        if agg.startswith("ath") or agg.startswith("near") or agg.startswith("mid"):
            wanted = {"ath": "ath_break", "near_res": "near_resistance",
                      "near_sup": "near_support", "mid": "mid_range"}[agg]
            flags = [c.get(field) == wanted for c in cohort if c.get(field) is not None]
            return _agg(flags, "pct_true")
        return _agg([c.get(field) for c in cohort], agg)

    rows = []
    for label, field, agg, unit in DIMENSIONS:
        w  = cohort_val(winners, field, agg)
        l  = cohort_val(losers,  field, agg)
        tq = cohort_val(top_q,   field, agg)
        bq = cohort_val(bottom_q,field, agg)
        delta_wl = None if (w is None or l is None) else (w - l)
        delta_tb = None if (tq is None or bq is None) else (tq - bq)
        rows.append({
            "label":         label,
            "winners":       _fmt_winner_cell(w,  unit),
            "losers":        _fmt_winner_cell(l,  unit),
            "delta_wl":      _fmt_delta(delta_wl, unit),
            "delta_wl_raw":  delta_wl,
            "top_q":         _fmt_winner_cell(tq, unit),
            "bottom_q":      _fmt_winner_cell(bq, unit),
            "delta_tb":      _fmt_delta(delta_tb, unit),
            "delta_tb_raw":  delta_tb,
            "unit":          unit,
        })

    return {
        "range_key":   range_key,
        "n_total":     len(classified),
        "n_winners":   len(winners),
        "n_losers":    len(losers),
        "n_top_q":     len(top_q),
        "n_bottom_q":  len(bottom_q),
        "total_pnl":   round(sum(c.get("realized_pnl") or 0 for c in classified), 0),
        "winners_pnl": round(sum(c.get("realized_pnl") or 0 for c in winners), 0),
        "losers_pnl":  round(sum(c.get("realized_pnl") or 0 for c in losers), 0),
        "rows":        rows,
    }


# ─── Losing trades vs levels ──────────────────────────────────────────────
# Pivot the levels enrichment around losing trades. Surfaces:
#   - median distance to nearest support / resistance for losers vs winners
#   - bucketed WR by distance-to-nearest-level
#   - sortable per-loser detail table with both directions' distances

def loser_levels_analysis(range_key: str = "all",
                          proximity_pct: float = 0.5) -> dict:
    """For each closed trade, compute distance to nearest support and
    nearest resistance at entry. Then pivot to surface the losing
    trades and how they compare to winners on those metrics.

    A "loser" here is realized_pnl <= 0 (binary). For more granular
    analysis (top vs bottom quartile by realized %) see winner_profile.
    """
    trades = closed_trades(limit=5000, range_key=range_key)

    enriched = []
    for t in trades:
        entry_ts = t.get("first_entry_ts")
        if not entry_ts:
            continue
        ticker = t["ticker"]
        under = _underlying_price_at(ticker, entry_ts)
        snap  = _active_levels_at(ticker, entry_ts)
        if under is None or snap is None:
            continue

        cls = _classify_position(under, snap, proximity_pct)
        nearest_above = cls["nearest_above"]
        nearest_below = cls["nearest_below"]
        dist_above    = cls["dist_above_pct"]
        dist_below    = cls["dist_below_pct"]

        # "Nearest overall" is the minimum of the two — direction-agnostic
        nearest_dist = None
        for d in (dist_above, dist_below):
            if d is not None and (nearest_dist is None or d < nearest_dist):
                nearest_dist = d

        is_winner = (t.get("realized_pnl") or 0) > 0
        enriched.append({
            "intent_id":          t["intent_id"],
            "ticker":             t["ticker"],
            "strike":             t.get("strike"),
            "right":              t.get("right"),
            "entry_ts":           entry_ts,
            "exit_ts":            t.get("last_exit_ts"),
            "underlying":         under,
            "nearest_above":      nearest_above,
            "nearest_below":      nearest_below,
            "dist_above_pct":     dist_above,
            "dist_below_pct":     dist_below,
            "nearest_dist_pct":   nearest_dist,
            "bucket":             cls["bucket"],
            "actual_pct":         t.get("actual_pct"),
            "mfe_in_pct":         t.get("mfe_in_pct"),
            "capture_pct":        t.get("capture_pct"),
            "realized_pnl":       t.get("realized_pnl") or 0,
            "is_winner":          is_winner,
        })

    losers  = sorted(
        (c for c in enriched if not c["is_winner"]),
        key=lambda c: c["realized_pnl"],            # worst losses first
    )
    winners = sorted(
        (c for c in enriched if c["is_winner"]),
        key=lambda c: -c["realized_pnl"],           # biggest wins first
    )

    # ── Distance buckets ──────────────────────────────────────────────
    # "How close was this trade to ANY level at entry?" Both winners and
    # losers go through the same bucketing so we can read the win-rate
    # column to see whether tight-to-level entries are actually better.
    def _bucket(d):
        if d is None:    return "no level"
        if d <= 0.25:    return "<0.25%"
        if d <= 0.5:     return "0.25–0.5%"
        if d <= 1.0:     return "0.5–1%"
        if d <= 2.0:     return "1–2%"
        if d <= 5.0:     return "2–5%"
        return ">5%"

    bucket_order = ["<0.25%", "0.25–0.5%", "0.5–1%", "1–2%", "2–5%", ">5%", "no level"]
    bucket_data: dict[str, dict] = defaultdict(
        lambda: {"n_w": 0, "n_l": 0, "pnl_w": 0.0, "pnl_l": 0.0, "rois": []}
    )
    for c in enriched:
        key = _bucket(c["nearest_dist_pct"])
        if c["is_winner"]:
            bucket_data[key]["n_w"] += 1
            bucket_data[key]["pnl_w"] += c["realized_pnl"]
        else:
            bucket_data[key]["n_l"] += 1
            bucket_data[key]["pnl_l"] += c["realized_pnl"]
        if c.get("actual_pct") is not None:
            bucket_data[key]["rois"].append(c["actual_pct"])

    buckets = []
    for b in bucket_order:
        if b not in bucket_data:
            continue
        d = bucket_data[b]
        total = d["n_w"] + d["n_l"]
        net = round(d["pnl_w"] + d["pnl_l"], 0)
        buckets.append({
            "label":      b,
            "n_winners":  d["n_w"],
            "n_losers":   d["n_l"],
            "total":      total,
            "win_rate":   round(d["n_w"] / total * 100, 1) if total else None,
            "median_roi": round(median(d["rois"]), 1) if d["rois"] else None,
            "net_pnl":    net,
        })

    # ── Aggregate medians ─────────────────────────────────────────────
    def _med(seq):
        vals = [x for x in seq if x is not None]
        return round(median(vals), 2) if vals else None

    summary = {
        "median_dist_above_losers":  _med(c["dist_above_pct"] for c in losers),
        "median_dist_below_losers":  _med(c["dist_below_pct"] for c in losers),
        "median_dist_nearest_losers":_med(c["nearest_dist_pct"] for c in losers),
        "median_dist_above_winners": _med(c["dist_above_pct"] for c in winners),
        "median_dist_below_winners": _med(c["dist_below_pct"] for c in winners),
        "median_dist_nearest_winners":_med(c["nearest_dist_pct"] for c in winners),
        "total_loss":  round(sum(c["realized_pnl"] for c in losers), 0),
        "total_win":   round(sum(c["realized_pnl"] for c in winners), 0),
    }

    return {
        "n_total":       len(enriched),
        "n_losers":      len(losers),
        "n_winners":     len(winners),
        "proximity_pct": proximity_pct,
        "summary":       summary,
        "buckets":       buckets,
        "losers":        losers,
        "winners":       winners,
    }


# ─── TP-ladder analysis ──────────────────────────────────────────────────
# How well does the user's TP ladder actually convert?
# Computes per-tier hit rates, per-tier price gains and time-to-fire,
# and the distribution of "ladder completion" (TP1 only / TP1+TP2 /
# full ladder / no TP). For each completion class we roll up the
# realized outcome so the user can answer "is TP2/TP3 worth setting?".

def ladder_analysis(range_key: str = "all") -> dict:
    """Analyze TP-ladder progression and outcomes across closed trades."""
    trades = closed_trades(limit=5000, range_key=range_key)

    # ── Per-tier rollup ──────────────────────────────────────────────
    # Walk each trade's tp_fills (already computed by closed_trades) and
    # collect (price_pct_from_entry, mins_from_entry) for each tier.
    tier_data: dict[int, list[dict]] = {1: [], 2: [], 3: []}
    completion_buckets: dict[str, list[dict]] = {
        "no_tp":       [],
        "tp1_only":    [],
        "tp1_tp2":     [],
        "full_ladder": [],
    }
    ladder_choices = Counter() if False else defaultdict(int)
    split_choices  = defaultdict(int)

    for t in trades:
        tiers_hit = set(t.get("tp_tiers_hit") or [])
        for f in t.get("tp_fills") or []:
            tier = f.get("tier")
            if tier in tier_data:
                tier_data[tier].append({
                    "intent_id": t["intent_id"],
                    "pct":       f.get("pct"),
                    "mins":      f.get("mins"),
                    "qty":       f.get("qty"),
                })
        # Completion class
        if not tiers_hit:
            k = "no_tp"
        elif 3 in tiers_hit:
            k = "full_ladder"
        elif 2 in tiers_hit:
            k = "tp1_tp2"
        else:
            k = "tp1_only"
        completion_buckets[k].append(t)
        # Ladder/split choice
        ladder_choices[t.get("tp_ladder_choice") or "—"] += 1
        split_choices[t.get("tp_split_choice") or "—"] += 1

    # Per-tier rollup metrics
    tier_rows = []
    n_total = len(trades)
    for tier in (1, 2, 3):
        fills = tier_data[tier]
        n_hit = len(fills)
        pcts = [f["pct"] for f in fills if f.get("pct") is not None]
        mins = [f["mins"] for f in fills if f.get("mins") is not None]
        tier_rows.append({
            "tier":             tier,
            "label":            f"TP{tier}",
            "n_hit":            n_hit,
            "hit_pct":          round(n_hit / n_total * 100, 1) if n_total else 0,
            "median_pct":       round(median(pcts), 1) if pcts else None,
            "median_mins":      round(median(mins), 1) if mins else None,
        })

    # Completion-class outcome rollup
    completion_label = {
        "no_tp":       "No TP (stopped/expired)",
        "tp1_only":    "TP1 only (stopped after)",
        "tp1_tp2":     "TP1 + TP2",
        "full_ladder": "Full ladder (TP1+TP2+TP3)",
    }
    completion_rows = []
    for k in ("no_tp", "tp1_only", "tp1_tp2", "full_ladder"):
        bucket = completion_buckets[k]
        stats  = _trade_outcome_stats(bucket)
        completion_rows.append({
            "key":   k,
            "label": completion_label[k],
            "pct_of_total": round(len(bucket) / n_total * 100, 1) if n_total else 0,
            **stats,
        })

    # "Worth-it" comparison — median outcomes by progression class
    # (no_tp / tp1_only / tp1_tp2 / full_ladder)
    progression_comparison = []
    for row in completion_rows:
        if row["n"] == 0:
            continue
        progression_comparison.append({
            "label":         row["label"],
            "n":             row["n"],
            "median_roi":    row["median_roi"],
            "median_capture":row["median_capture"],
            "median_mfe":    row["median_mfe"],
            "total_pnl":     row["total_pnl"],
            "avg_pnl":       round(row["total_pnl"] / row["n"], 0) if row["n"] else 0,
        })

    # ── Per-ticker × per-tier matrix, split by DTE bucket ────────────
    # Two rollups with tickers as rows and TP tiers as columns:
    #   - median minutes from entry to each TP fill
    #   - median % gain over entry at each TP fill
    # Returned as {bucket: matrix}, where bucket ∈ all / 0 / 1-2 / 3-7 / 8+.
    # The frontend uses these for a DTE filter tab without reloading.
    def _dte_for(t: dict) -> Optional[int]:
        fe = t.get("first_entry_ts")
        exp = t.get("expiry")
        if not fe or not exp:
            return None
        try:
            return (date.fromisoformat(str(exp)[:10])
                    - date.fromisoformat(fe[:10])).days
        except (ValueError, TypeError):
            return None

    DTE_BUCKETS = [
        ("all",  "All",  lambda d: True),
        ("0",    "0DTE", lambda d: d == 0),
        ("1-2",  "1–2",  lambda d: d is not None and 1 <= d <= 2),
        ("3-7",  "3–7",  lambda d: d is not None and 3 <= d <= 7),
        ("8+",   "8+",   lambda d: d is not None and d >= 8),
    ]

    matrices_by_bucket: dict[str, list[dict]] = {}
    bucket_counts: dict[str, int] = {}
    for slug, _label, fn in DTE_BUCKETS:
        bucket_trades = [t for t in trades if fn(_dte_for(t))]
        bucket_counts[slug] = len(bucket_trades)

        by_ticker_tier: dict[tuple, list[dict]] = defaultdict(list)
        by_ticker_total: dict[str, int] = defaultdict(int)
        for t in bucket_trades:
            ticker = t["ticker"]
            for f in t.get("tp_fills") or []:
                tier = f.get("tier")
                if tier in (1, 2, 3):
                    by_ticker_tier[(ticker, tier)].append(f)
                    by_ticker_total[ticker] += 1

        matrix = []
        for ticker in sorted(by_ticker_total.keys(),
                             key=lambda k: -by_ticker_total[k]):
            row = {"ticker": ticker, "tiers": {}}
            for tier in (1, 2, 3):
                fills = by_ticker_tier.get((ticker, tier), [])
                pcts = [f["pct"]  for f in fills if f.get("pct")  is not None]
                mins = [f["mins"] for f in fills if f.get("mins") is not None]
                row["tiers"][tier] = {
                    "n":          len(fills),
                    "median_pct": round(median(pcts), 1) if pcts else None,
                    "median_mins":round(median(mins), 1) if mins else None,
                }
            matrix.append(row)
        matrices_by_bucket[slug] = matrix

    # Keep the legacy ticker_matrix field pointing at the all-DTE rollup
    # so any consumer that was reading it stays unchanged.
    ticker_matrix = matrices_by_bucket["all"]
    dte_buckets = [
        {"slug": slug, "label": label, "n_trades": bucket_counts[slug]}
        for slug, label, _ in DTE_BUCKETS
    ]

    return {
        "range_key":      range_key,
        "n_total":        n_total,
        "ladder_choices": dict(ladder_choices),
        "split_choices":  dict(split_choices),
        "tier_rows":      tier_rows,
        "completion_rows": completion_rows,
        "progression":    progression_comparison,
        "ticker_matrix":  ticker_matrix,
        "ticker_matrix_by_dte": matrices_by_bucket,
        "dte_buckets":    dte_buckets,
    }


# ─── TP-ladder what-if simulator ──────────────────────────────────────────
# Replays each trade's cached option bars against a user-supplied ladder
# (TP1/TP2/TP3 %s, splits, optional initial stop, optional trail-to-entry
# after TP1) and reports the simulated aggregate vs the actual outcomes.

def simulate_ladder(range_key: str = "all",
                    tp1_pct: float = 20.0,
                    tp2_pct: float = 30.0,
                    tp3_pct: float = 40.0,
                    split1: float = 0.5,
                    split2: float = 0.25,
                    split3: float = 0.25,
                    init_stop_pct: Optional[float] = None,
                    trail_after_tp1: bool = False) -> dict:
    """Replay cached option bars under a user-defined ladder and return
    actual-vs-simulated aggregate stats.

    TP percentages are expressed in % from entry (e.g. tp1_pct=20 means
    +20% above entry price). Splits are fractions of entry quantity that
    fill at each tier. Remaining position at expiry (or at the last cached
    bar) closes at that bar's close.
    """
    from src import bars_store
    from src.contract_symbols import build_occ_symbol

    trades = closed_trades(limit=5000, range_key=range_key)
    splits = [max(0, split1), max(0, split2), max(0, split3)]
    if sum(splits) <= 0:
        splits = [1.0, 0.0, 0.0]   # fall through to single-target

    bars_cache: dict[str, Any] = {}
    sim_results: list[dict] = []
    skipped = 0

    for t in trades:
        entry_price = float(t.get("avg_entry") or 0)
        entry_qty   = int(t.get("entry_qty") or 0)
        entry_ts    = t.get("first_entry_ts")
        expiry      = t.get("expiry")
        ticker      = t.get("ticker")
        right       = t.get("right")
        strike      = t.get("strike")
        if entry_price <= 0 or entry_qty <= 0 or not entry_ts or not expiry:
            skipped += 1
            continue
        try:
            occ = build_occ_symbol(
                ticker, date.fromisoformat(str(expiry)[:10]),
                right, float(strike),
            )
        except (ValueError, TypeError):
            skipped += 1
            continue

        df = bars_cache.get(occ)
        if df is None:
            try:
                df = bars_store.load_option_bars(occ)
            except FileNotFoundError:
                skipped += 1
                continue
            bars_cache[occ] = df
        if df is None or df.empty:
            skipped += 1
            continue

        # Window: entry time → expiry close (4pm ET on expiry day)
        try:
            entry_dt = datetime.fromisoformat(entry_ts[:19]).replace(tzinfo=_ET)
        except ValueError:
            skipped += 1
            continue
        entry_ms = int(entry_dt.timestamp() * 1000)
        try:
            exp_d = date.fromisoformat(str(expiry)[:10])
        except (ValueError, TypeError):
            skipped += 1
            continue
        expiry_close_dt = datetime.combine(
            exp_d, datetime.min.time(), tzinfo=_ET,
        ) + timedelta(hours=16)
        expiry_ms = int(expiry_close_dt.timestamp() * 1000)

        window = df[(df["t"] >= entry_ms) & (df["t"] <= expiry_ms)].copy()
        if window.empty:
            skipped += 1
            continue

        # TP price targets (entry price × (1 + tp_pct/100))
        tp_prices = [
            entry_price * (1 + tp1_pct / 100),
            entry_price * (1 + tp2_pct / 100),
            entry_price * (1 + tp3_pct / 100),
        ]
        # Allocate split quantities (integer contracts) — distribute rounding
        qty_alloc = [int(round(entry_qty * s)) for s in splits]
        diff = entry_qty - sum(qty_alloc)
        if diff != 0:
            qty_alloc[-1] += diff
        qty_alloc = [max(0, q) for q in qty_alloc]
        # If a tier's quantity is 0 (split=0), we still need it claimable.

        # Stop price — None means no stop, otherwise entry × (1 - stop_pct/100)
        stop_price = (entry_price * (1 - init_stop_pct / 100)
                      if init_stop_pct and init_stop_pct > 0 else None)

        remaining = entry_qty
        filled: list[dict] = []
        tier_hit = [False, False, False]

        for _, bar in window.iterrows():
            if remaining <= 0:
                break
            bar_h = float(bar["h"]) if bar.get("h") is not None else None
            bar_l = float(bar["l"]) if bar.get("l") is not None else None

            # Stop check first (within a single bar we assume the stop
            # would fill before any TP if both intra-bar levels are tagged;
            # this is conservative — favors the simulator showing slightly
            # worse outcomes than a "TP fills first" assumption would).
            if stop_price is not None and bar_l is not None and bar_l <= stop_price:
                filled.append({
                    "tier":  "stop",
                    "price": stop_price,
                    "qty":   remaining,
                    "t":     int(bar["t"]),
                })
                remaining = 0
                break

            # TP checks in order — fire each tier that the high crossed.
            for i in range(3):
                if tier_hit[i]:
                    continue
                if bar_h is None or bar_h < tp_prices[i]:
                    break   # ordered tiers; if this one didn't fire, higher ones can't either
                qty = min(qty_alloc[i], remaining)
                if qty <= 0:
                    tier_hit[i] = True
                    continue
                filled.append({
                    "tier":  i + 1,
                    "price": tp_prices[i],
                    "qty":   qty,
                    "t":     int(bar["t"]),
                })
                remaining -= qty
                tier_hit[i] = True
                # After TP1 fires, optionally trail stop to entry
                if i == 0 and trail_after_tp1 and (stop_price is None or stop_price < entry_price):
                    stop_price = entry_price

        # Whatever's left at the end → close at last bar's close
        if remaining > 0:
            last_close = float(window["c"].iloc[-1])
            filled.append({
                "tier":  "expiry",
                "price": last_close,
                "qty":   remaining,
                "t":     int(window["t"].iloc[-1]),
            })

        # Compute simulated P&L (option-contract multiplier of 100)
        sim_pnl = sum(
            (f["price"] - entry_price) * f["qty"] * 100
            for f in filled
        )
        sim_roi = sim_pnl / (entry_price * entry_qty * 100)
        tiers_hit_set = {f["tier"] for f in filled if isinstance(f["tier"], int)}
        if "stop" in [f["tier"] for f in filled] and not tiers_hit_set:
            progression = "stopped"
        elif 3 in tiers_hit_set:
            progression = "full_ladder"
        elif 2 in tiers_hit_set:
            progression = "tp1_tp2"
        elif 1 in tiers_hit_set:
            progression = "tp1_only"
        else:
            progression = "expiry_close"

        # "Reached" = price crossed the tier threshold; "hit" = contracts
        # were filled. They diverge when a split allocation rounds to 0
        # (e.g. 25% of 2 contracts). Use reached for hit-rate analytics,
        # use hit (filled) for progression-class P&L.
        tiers_reached = [i + 1 for i, h in enumerate(tier_hit) if h]
        sim_results.append({
            "intent_id":    t["intent_id"],
            "ticker":       t["ticker"],
            "entry_price":  entry_price,
            "entry_qty":    entry_qty,
            "actual_pnl":   t.get("realized_pnl") or 0,
            "actual_roi":   round((t.get("roi") or 0) * 100, 1),
            "sim_pnl":      round(sim_pnl, 0),
            "sim_roi":      round(sim_roi * 100, 1),
            "progression":  progression,
            "tiers_hit":    sorted(tiers_hit_set),
            "tiers_reached":tiers_reached,
            "filled":       filled,
        })

    # ── Aggregate comparison ────────────────────────────────────────
    n_sim = len(sim_results)
    actual_total = round(sum(r["actual_pnl"] for r in sim_results), 0)
    sim_total    = round(sum(r["sim_pnl"]    for r in sim_results), 0)

    # Tier-reached counts — "did the bar ever cross the TP threshold?"
    # Independent of split allocation, so it doesn't get fooled by
    # small-quantity trades where a split rounds to zero contracts.
    sim_tier_hits = [
        sum(1 for r in sim_results if (i+1) in r["tiers_reached"])
        for i in range(3)
    ]

    # Per-progression class rollup
    by_prog: dict[str, list[dict]] = defaultdict(list)
    for r in sim_results:
        by_prog[r["progression"]].append(r)

    prog_order = ["full_ladder", "tp1_tp2", "tp1_only", "stopped", "expiry_close"]
    prog_label = {
        "full_ladder":  "Full ladder (TP1+TP2+TP3)",
        "tp1_tp2":      "TP1 + TP2",
        "tp1_only":     "TP1 only",
        "stopped":      "Stopped out",
        "expiry_close": "Held to expiry close",
    }
    progression_rows = []
    for key in prog_order:
        bucket = by_prog.get(key, [])
        if not bucket:
            continue
        wins = sum(1 for r in bucket if r["sim_pnl"] > 0)
        rois = [r["sim_roi"] for r in bucket]
        progression_rows.append({
            "key":       key,
            "label":     prog_label[key],
            "n":         len(bucket),
            "pct":       round(len(bucket) / n_sim * 100, 1) if n_sim else 0,
            "wins":      wins,
            "win_rate":  round(wins / len(bucket) * 100, 1),
            "median_roi": round(median(rois), 1) if rois else None,
            "total_pnl": round(sum(r["sim_pnl"] for r in bucket), 0),
            "avg_pnl":   round(sum(r["sim_pnl"] for r in bucket) / len(bucket), 0),
        })

    # Biggest movers (where simulation differs most from actual)
    movers = sorted(sim_results,
                    key=lambda r: r["sim_pnl"] - r["actual_pnl"],
                    reverse=True)
    top_better = movers[:10]
    top_worse  = movers[-10:][::-1]   # worst first

    return {
        "params": {
            "tp1_pct": tp1_pct, "tp2_pct": tp2_pct, "tp3_pct": tp3_pct,
            "split1": split1, "split2": split2, "split3": split3,
            "init_stop_pct": init_stop_pct,
            "trail_after_tp1": trail_after_tp1,
        },
        "n_total":      len(trades),
        "n_simulated":  n_sim,
        "n_skipped":    skipped,
        "actual_total": actual_total,
        "sim_total":    sim_total,
        "delta_total":  round(sim_total - actual_total, 0),
        "tier_hits":    sim_tier_hits,
        "tier_hit_pct": [round(h / n_sim * 100, 1) if n_sim else 0
                         for h in sim_tier_hits],
        "progression":  progression_rows,
        "top_better":   top_better,
        "top_worse":    top_worse,
    }


# ─── Chain analysis ─────────────────────────────────────────────────────
# A "chain" is a sequence of entry trades on the same ticker + same side
# (calls or puts) where the strike progression is monotonic in the chase
# direction:
#   - calls: each new leg has strike >= last leg's strike (chase up)
#   - puts:  each new leg has strike <= last leg's strike (chase down)
# A strike that breaks the monotonic direction starts a NEW chain.
# A gap of more than `max_gap_days` between consecutive legs also breaks
# the chain.
#
# Examples:
#   META 2026-04-06 585C → ... → 2026-04-16 685C   (9 legs, one chain)
#   META 2026-04-21 675C exp 04-24                  (new chain — strike dropped)
#   TSLA 2026-04-15 375C → ... → 2026-04-17 407.5C (10 legs, one chain)
#   TSLA 2026-05-05 405C exp 05-08                  (new chain — 18-day gap)

def chain_analysis(range_key: str = "all",
                   max_roll_minutes: int = 60) -> dict:
    """Group trades into roll chains per ticker × side.

    A chain is a sequence where each new leg's BUY happens within
    `max_roll_minutes` of an EXIT FILL of an earlier leg in the same
    chain — i.e. you closed (or partial-closed) the prior position and
    rolled the capital into the next strike. Plus the strike direction
    must be monotonic in the chase direction:
      - calls: strikes ≥ prior leg's strike
      - puts:  strikes ≤ prior leg's strike
    Breaks either condition → new chain starts.
    """
    trades = closed_trades(limit=5000, range_key=range_key)

    # Fetch every exit fill timestamp for the trades, keyed by intent_id.
    # One pass over the fills table; used to test roll-timing per leg.
    intent_ids = [t["intent_id"] for t in trades]
    exits_by_intent: dict[str, list[datetime]] = defaultdict(list)
    if intent_ids:
        placeholders = ",".join("?" for _ in intent_ids)
        with _connect() as conn:
            for r in conn.execute(
                f"SELECT intent_id, ts FROM fills "
                f"WHERE is_entry = 0 AND intent_id IN ({placeholders})",
                intent_ids,
            ).fetchall():
                try:
                    exits_by_intent[r["intent_id"]].append(
                        datetime.fromisoformat(r["ts"][:19])
                    )
                except (ValueError, TypeError):
                    continue

    # Bucket by (ticker, side) and sort each bucket chronologically
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for t in trades:
        side = t.get("right")
        if side not in ("C", "P"):
            continue
        by_key[(t["ticker"], side)].append(t)
    for v in by_key.values():
        v.sort(key=lambda x: x.get("first_entry_ts") or "")

    chains: list[dict] = []
    for (ticker, side), legs in by_key.items():
        current: list[dict] = []
        chain_exits: list[datetime] = []   # all exits from current chain's legs
        last_strike: Optional[float] = None

        def _close_chain():
            if current:
                chains.append(_chain_record(ticker, side, list(current)))

        for leg in legs:
            strike = float(leg.get("strike") or 0)
            try:
                entry_dt = datetime.fromisoformat(
                    (leg.get("first_entry_ts") or "")[:19]
                )
            except ValueError:
                entry_dt = None

            # Monotonicity check — only enforced from the 2nd leg onward
            monotonic_ok = True
            if last_strike is not None:
                if side == "C":
                    monotonic_ok = strike >= last_strike
                else:    # P
                    monotonic_ok = strike <= last_strike

            # Roll-timing check — must be within max_roll_minutes of an
            # exit fill of an earlier chain leg. Skipped for the first
            # leg of a chain.
            roll_ok = True
            if current:
                if entry_dt is None or not chain_exits:
                    roll_ok = False
                else:
                    roll_ok = any(
                        0 <= (entry_dt - x).total_seconds() / 60.0 <= max_roll_minutes
                        for x in chain_exits if x <= entry_dt
                    )

            if (not current) or (monotonic_ok and roll_ok):
                current.append(leg)
                chain_exits.extend(exits_by_intent.get(leg["intent_id"], []))
            else:
                _close_chain()
                current = [leg]
                chain_exits = list(exits_by_intent.get(leg["intent_id"], []))

            last_strike = strike

        _close_chain()

    # Sort: most-recent starter first; chains with more legs ranked higher within same date
    chains.sort(key=lambda c: (c["starter_date"], c["n_legs"]), reverse=True)

    # Summary stats across all chains
    n_chains = len(chains)
    multi_leg = [c for c in chains if c["n_legs"] >= 2]
    singletons = [c for c in chains if c["n_legs"] == 1]

    summary = {
        "n_chains":      n_chains,
        "n_multi_leg":   len(multi_leg),
        "n_singletons":  len(singletons),
        "median_legs":   round(median([c["n_legs"] for c in multi_leg]), 1) if multi_leg else None,
        "max_legs":      max((c["n_legs"] for c in chains), default=0),
        "total_pnl_multi": round(sum(c["total_pnl"] for c in multi_leg), 0),
        "total_pnl_solo":  round(sum(c["total_pnl"] for c in singletons), 0),
    }

    return {
        "range_key":   range_key,
        "max_roll_minutes": max_roll_minutes,
        "n_trades":    len(trades),
        "summary":     summary,
        "chains":      chains,
    }


def _chain_record(ticker: str, side: str, legs: list[dict]) -> dict:
    """Build the per-chain rollup dict."""
    strikes  = [float(l.get("strike") or 0) for l in legs]
    entries  = [l.get("first_entry_ts") or "" for l in legs]
    exits    = [l.get("last_exit_ts")  or "" for l in legs]
    starter  = legs[0]
    last_leg = legs[-1]

    total_pnl   = sum(l.get("realized_pnl") or 0 for l in legs)
    total_qty   = sum(int(l.get("entry_qty") or 0) for l in legs)
    total_cost  = sum(float(l.get("avg_entry") or 0) * int(l.get("entry_qty") or 0) * 100
                      for l in legs)
    wins        = sum(1 for l in legs if (l.get("realized_pnl") or 0) > 0)

    # Chain shape — derived from strike progression
    if len(legs) == 1:
        shape = "solo"
    elif all(s == strikes[0] for s in strikes):
        shape = "scale (same strike)"
    elif side == "C" and strikes[-1] > strikes[0]:
        shape = "chase up"
    elif side == "P" and strikes[-1] < strikes[0]:
        shape = "chase down"
    else:
        shape = "drift"

    return {
        "ticker":         ticker,
        "side":           side,
        "n_legs":         len(legs),
        "starter_id":     starter["intent_id"],
        "starter_date":   (starter.get("first_entry_ts") or "")[:10],
        "starter_strike": strikes[0],
        "starter_expiry": starter.get("expiry"),
        "last_date":      (last_leg.get("last_exit_ts") or last_leg.get("first_entry_ts") or "")[:10],
        "last_strike":    strikes[-1],
        "first_entry_ts": starter.get("first_entry_ts"),
        "last_exit_ts":   last_leg.get("last_exit_ts"),
        "min_strike":     min(strikes),
        "max_strike":     max(strikes),
        "n_unique_strikes": len(set(strikes)),
        "total_pnl":      round(total_pnl, 0),
        "total_qty":      total_qty,
        "total_cost":     round(total_cost, 0),
        "roi":            round(total_pnl / total_cost * 100, 1) if total_cost > 0 else None,
        "wins":           wins,
        "losses":         len(legs) - wins,
        "win_rate":       round(wins / len(legs) * 100, 1),
        "shape":          shape,
        "days_span":      (datetime.fromisoformat(entries[-1][:19])
                           - datetime.fromisoformat(entries[0][:19])).days
                          if entries[0] and entries[-1] else None,
        "legs": [
            {
                "intent_id":   l["intent_id"],
                "entry_ts":    l.get("first_entry_ts"),
                "exit_ts":     l.get("last_exit_ts"),
                "strike":      float(l.get("strike") or 0),
                "right":       l.get("right"),
                "expiry":      l.get("expiry"),
                "qty":         int(l.get("entry_qty") or 0),
                "avg_entry":   l.get("avg_entry"),
                "avg_exit":    l.get("avg_exit"),
                "realized_pnl":l.get("realized_pnl") or 0,
                "actual_pct":  l.get("actual_pct"),
                "mfe_in_pct":  l.get("mfe_in_pct"),
                "capture_pct": l.get("capture_pct"),
                "tp_tiers_hit": l.get("tp_tiers_hit") or [],
            }
            for l in legs
        ],
    }


# ─── Pre-trade decision card ─────────────────────────────────────────────
# Composes a single artifact for the entry form / trigger creation flow:
# given a candidate (ticker, strike, right, direction), pulls together
# every dimension we already analyze post-trade and produces a structured
# verdict the trader can glance at before clicking submit.

def pretrade_decision_card(
    ticker: str,
    strike: Optional[float] = None,
    right: str = "C",
    direction: Optional[str] = None,
    daily_loss_cap: float = 1000.0,
) -> dict:
    """Return per-dimension signals + an overall verdict for the candidate.

    All sources are read from data we already have:
      - published levels (level legitimacy + R:R)
      - cached underlying bars (current price → bucket placement)
      - closed_trades (ticker × side history + today's P&L + hour-of-day edge)
    No live quotes required — uses last cached bar as "now".
    """
    direction = direction or ("above" if right == "C" else "below")
    ticker_u  = (ticker or "").upper()

    card: dict[str, Any] = {
        "ticker":    ticker_u,
        "strike":    strike,
        "right":     right,
        "direction": direction,
        "signals":   [],
    }
    if not ticker_u:
        card["error"] = "ticker required"
        return card

    # ── 1. Level legitimacy + R:R ─────────────────────────────────────
    if strike is not None and strike > 0:
        from . import levels as _lv
        ev = _lv.evaluate_pick_level(ticker_u, float(strike), direction)
        if ev is not None:
            card["level_eval"] = ev

    # ── 2. Underlying price + position bucket ─────────────────────────
    try:
        from src import bars_store
        df = bars_store.load_underlying_bars(ticker_u)
        if not df.empty:
            card["underlying"] = round(float(df["c"].dropna().iloc[-1]), 2)
    except (ImportError, FileNotFoundError):
        pass

    snap = None
    try:
        from . import levels as _lv
        snap = _lv.latest_for_ticker(ticker_u)
    except Exception:
        pass

    if card.get("underlying") and snap is not None and (snap.levels_above or snap.levels_below):
        cls = _classify_position(card["underlying"], snap, proximity_pct=0.5)
        card["bucket"] = cls

    # ── 3. User's historical record for ticker × side ─────────────────
    trades = closed_trades(limit=5000)
    same_t = [t for t in trades if (t.get("ticker") or "").upper() == ticker_u]
    same_ts = [t for t in same_t if t.get("right") == right]
    wins_t = sum(1 for t in same_ts if (t.get("realized_pnl") or 0) > 0)

    def _med(seq):
        s = [x for x in seq if x is not None]
        return round(median(s), 1) if s else None

    card["user_history"] = {
        "ticker_n":        len(same_t),
        "ticker_side_n":   len(same_ts),
        "ticker_side_wr":  round(wins_t / len(same_ts) * 100, 1) if same_ts else None,
        "median_roi":      _med(t.get("actual_pct")     for t in same_ts),
        "median_capture":  _med(t.get("capture_pct")    for t in same_ts),
        "median_mfe":      _med(t.get("mfe_in_pct")     for t in same_ts),
    }

    # ── 4. Hour-of-day edge (uses current ET hour) ────────────────────
    now_et = datetime.now(_ET)
    hour = now_et.hour
    same_hour = []
    for t in trades:
        ts = t.get("first_entry_ts")
        if not ts:
            continue
        try:
            h = datetime.fromisoformat(ts[:19]).hour
        except ValueError:
            continue
        if h == hour:
            same_hour.append(t)
    wins_hour = sum(1 for t in same_hour if (t.get("realized_pnl") or 0) > 0)
    card["hour_edge"] = {
        "hour":       hour,
        "n":          len(same_hour),
        "win_rate":   round(wins_hour / len(same_hour) * 100, 1) if same_hour else None,
        "median_roi": _med(t.get("actual_pct") for t in same_hour),
    }

    # ── 5. Daily P&L vs cap ───────────────────────────────────────────
    today = date.today().isoformat()
    today_trades = [
        t for t in trades
        if (t.get("first_entry_ts") or "")[:10] == today
    ]
    today_pnl = round(sum(t.get("realized_pnl") or 0 for t in today_trades), 0)
    card["daily_budget"] = {
        "today_pnl":     today_pnl,
        "today_n":       len(today_trades),
        "loss_cap":      daily_loss_cap,
        "remaining":     round(daily_loss_cap + today_pnl, 0),  # cap is the absolute floor; pnl can be -ve
        "exhausted":     today_pnl <= -daily_loss_cap,
    }

    # ── 6. Compose verdict signals ────────────────────────────────────
    signals = []

    ev = card.get("level_eval")
    if ev:
        s = ev["level_status"]
        if s == "on":
            signals.append({"kind": "level", "tone": "good",
                            "label": f"ON published ${ev['matched_level']:g}"})
        elif s == "near":
            signals.append({"kind": "level", "tone": "warn",
                            "label": f"Near ${ev['matched_level']:g} ({ev['matched_dist_pct']}% off)"})
        else:
            signals.append({"kind": "level", "tone": "bad",
                            "label": "Off-level — entry doesn't match any published level"})

        if ev["rr_verdict"] == "good":
            signals.append({"kind": "rr", "tone": "good",
                            "label": f"R:R {ev['rr_ratio']} — reward {ev['reward_pct']}% / risk {ev['risk_pct']}%"})
        elif ev["rr_verdict"] == "fair":
            signals.append({"kind": "rr", "tone": "warn",
                            "label": f"R:R {ev['rr_ratio']} — fair, not great"})
        elif ev["rr_verdict"] == "poor":
            signals.append({"kind": "rr", "tone": "bad",
                            "label": f"R:R {ev['rr_ratio']} — risk > reward"})
        else:
            signals.append({"kind": "rr", "tone": "warn",
                            "label": "R:R incomplete — one side of the level grid is missing"})

    bk = card.get("bucket")
    if bk:
        if bk["bucket"] == "mid_range":
            signals.append({"kind": "bucket", "tone": "bad",
                            "label": "Mid-range entry — your weakest class (61% WR, +18% ROI median)"})
        elif bk["bucket"] == "ath_break":
            signals.append({"kind": "bucket", "tone": "good",
                            "label": "ATH-break setup — your highest-edge class (87% WR, +25% ROI median)"})
        elif bk["bucket"] in ("near_resistance", "near_support"):
            signals.append({"kind": "bucket", "tone": "good",
                            "label": f"{bk['bucket'].replace('_',' ').title()} — historically your better class"})

    uh = card["user_history"]
    if uh["ticker_side_n"] >= 3:
        if uh["ticker_side_wr"] is not None and uh["ticker_side_wr"] >= 70:
            signals.append({"kind": "ticker", "tone": "good",
                            "label": f"{ticker_u} {right}: {uh['ticker_side_wr']}% WR over {uh['ticker_side_n']} trades"})
        elif uh["ticker_side_wr"] is not None and uh["ticker_side_wr"] < 40:
            signals.append({"kind": "ticker", "tone": "bad",
                            "label": f"{ticker_u} {right}: only {uh['ticker_side_wr']}% WR over {uh['ticker_side_n']} trades"})
    elif uh["ticker_side_n"] == 0:
        signals.append({"kind": "ticker", "tone": "warn",
                        "label": f"No personal history on {ticker_u} {right}"})

    he = card["hour_edge"]
    if he["n"] >= 5 and he["win_rate"] is not None:
        if he["win_rate"] >= 70:
            signals.append({"kind": "hour", "tone": "good",
                            "label": f"{he['hour']:02d}:00 entries: {he['win_rate']}% WR (n={he['n']})"})
        elif he["win_rate"] < 50:
            signals.append({"kind": "hour", "tone": "bad",
                            "label": f"{he['hour']:02d}:00 entries underperform: {he['win_rate']}% WR (n={he['n']})"})

    db = card["daily_budget"]
    if db["exhausted"]:
        signals.append({"kind": "budget", "tone": "bad",
                        "label": f"Daily loss cap hit (${db['today_pnl']:+.0f}/${db['loss_cap']:.0f}) — stand down"})
    elif db["today_pnl"] < -daily_loss_cap * 0.6:
        signals.append({"kind": "budget", "tone": "warn",
                        "label": f"Today's P&L ${db['today_pnl']:+.0f} — ${db['remaining']:.0f} budget remaining"})

    # ── 7. Overall verdict — majority tone wins, with explicit rules ──
    bad_count  = sum(1 for s in signals if s["tone"] == "bad")
    good_count = sum(1 for s in signals if s["tone"] == "good")
    warn_count = sum(1 for s in signals if s["tone"] == "warn")

    if bad_count >= 2 or db["exhausted"]:
        overall = "stand_down"
    elif bad_count >= 1 and good_count <= 1:
        overall = "marginal"
    elif good_count >= 2 and bad_count == 0:
        overall = "strong"
    elif good_count >= 1 and bad_count == 0:
        overall = "ok"
    else:
        overall = "neutral"

    card["signals"]   = signals
    card["verdict"]   = overall
    card["counts"]    = {"good": good_count, "warn": warn_count, "bad": bad_count}
    return card


# ─── Equity curve + drawdown ─────────────────────────────────────────────
# Cumulative realized $ over trade exits, with running peak and drawdown.
# Powers the chart on /analytics/pnl.

def equity_curve(range_key: str = "all") -> dict:
    """Cumulative realized P&L per closed trade, plus drawdown analysis."""
    trades = closed_trades(limit=5000, range_key=range_key)
    # Sort by realization time (last exit), falling back to entry
    trades.sort(key=lambda t: t.get("last_exit_ts") or t.get("first_entry_ts") or "")

    cum: float = 0
    peak: float = 0
    peak_at: Optional[str] = None
    max_dd: float = 0
    max_dd_at: Optional[str] = None
    max_dd_peak_at: Optional[str] = None
    series: list[dict] = []

    for t in trades:
        ts = t.get("last_exit_ts") or t.get("first_entry_ts")
        if not ts:
            continue
        pnl = float(t.get("realized_pnl") or 0)
        cum += pnl
        if cum > peak:
            peak = cum
            peak_at = ts[:10]
        dd = cum - peak    # always <= 0
        if dd < max_dd:
            max_dd = dd
            max_dd_at = ts[:10]
            max_dd_peak_at = peak_at
        series.append({
            "ts":         ts,
            "date":       ts[:10],
            "trade_pnl":  round(pnl, 0),
            "cum_pnl":    round(cum, 0),
            "peak":       round(peak, 0),
            "drawdown":   round(dd, 0),
            "ticker":     t.get("ticker"),
            "intent_id":  t.get("intent_id"),
        })

    # Top 3 drawdowns — each starts at a peak and ends at the trough
    # before the next peak (or end-of-series). Sorted by magnitude.
    drawdowns: list[dict] = []
    if series:
        i = 0
        n = len(series)
        while i < n:
            # Find next peak (cum_pnl equals peak at that point)
            while i < n and series[i]["cum_pnl"] != series[i]["peak"]:
                i += 1
            if i >= n - 1:
                break
            peak_idx = i
            # Walk until cum_pnl reaches peak again or end
            peak_val = series[i]["cum_pnl"]
            trough_idx = i
            trough_val = peak_val
            j = i + 1
            while j < n and series[j]["cum_pnl"] < peak_val:
                if series[j]["cum_pnl"] < trough_val:
                    trough_val = series[j]["cum_pnl"]
                    trough_idx = j
                j += 1
            if trough_val < peak_val:
                drawdowns.append({
                    "peak_date":    series[peak_idx]["date"],
                    "trough_date":  series[trough_idx]["date"],
                    "recovery_date": series[j]["date"] if j < n else None,
                    "peak_val":     series[peak_idx]["cum_pnl"],
                    "trough_val":   trough_val,
                    "drop":         round(trough_val - peak_val, 0),
                    "n_trades":     trough_idx - peak_idx,
                })
            i = j if j > i else i + 1
    drawdowns.sort(key=lambda d: d["drop"])  # most negative first

    return {
        "series":            series,
        "n_trades":          len(series),
        "current_pnl":       round(cum, 0),
        "peak":              round(peak, 0),
        "peak_at":           peak_at,
        "current_drawdown":  round(cum - peak, 0),
        "max_drawdown":      round(max_dd, 0),
        "max_drawdown_at":   max_dd_at,
        "max_dd_peak_at":    max_dd_peak_at,
        "top_drawdowns":     drawdowns[:3],
    }


# ─── Cohort benchmarking ─────────────────────────────────────────────────
# Compares the user's per-trade metrics against the 742-row reference cohort
# (loaded into reference_trades from external trader books). Two sides
# computed identically, then surfaced side-by-side.

def _outcome_stats_for(rows: list[dict],
                       roi_key: str,
                       win_predicate) -> dict:
    """Generic stat block: win rate, median ROI, median MFE, median capture."""
    n = len(rows)
    if n == 0:
        return {"n": 0, "win_rate": None, "median_roi": None,
                "median_mfe": None, "median_mae": None, "median_capture": None}
    wins = sum(1 for r in rows if win_predicate(r))

    def _med(key):
        vals = [r.get(key) for r in rows if r.get(key) is not None]
        return round(median(vals), 1) if vals else None

    rois = [r.get(roi_key) for r in rows if r.get(roi_key) is not None]
    mfes = [r.get("mfe_in_pct") for r in rows if r.get("mfe_in_pct") is not None]
    maes = [r.get("mae_in_pct") for r in rows if r.get("mae_in_pct") is not None]
    # Capture computed per-trade then medianed
    captures = []
    for r in rows:
        m = r.get("mfe_in_pct")
        actual = r.get(roi_key)
        if m is not None and m > 0 and actual is not None:
            captures.append(max(0.0, min(100.0, actual / m * 100.0)))

    return {
        "n":              n,
        "win_rate":       round(wins / n * 100, 1),
        "median_roi":     round(median(rois), 1) if rois else None,
        "median_mfe":     round(median(mfes), 1) if mfes else None,
        "median_mae":     round(median(maes), 1) if maes else None,
        "median_capture": round(median(captures), 1) if captures else None,
    }


def _reference_rows(range_key: str = "all") -> list[dict]:
    """Return reference_trades rows as plain dicts (fully_closed only)."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT * FROM reference_trades
            WHERE status = 'fully_closed'
        """).fetchall()
    return [dict(r) for r in rows]


def cohort_benchmark(range_key: str = "all") -> dict:
    """User vs. cohort metrics, overall + by DTE bucket + by side."""
    user_trades = closed_trades(limit=5000, range_key=range_key)
    cohort_rows = _reference_rows(range_key=range_key)

    # Normalize keys so _outcome_stats_for can read both via the same call.
    # User: actual_pct (already %)  ·  Cohort: realized_roi (also %)
    def _user_win(r):  return (r.get("realized_pnl") or 0) > 0
    def _coh_win(r):   return (r.get("realized_roi") or 0) > 0

    def _dte_bucket(d):
        if d is None:     return None
        try: d = int(d)
        except (TypeError, ValueError): return None
        if d == 0:        return "0DTE"
        if d <= 2:        return "1–2"
        if d <= 7:        return "3–7"
        return "8+"

    # Add dte to user trades for bucketing
    enriched_user = []
    for t in user_trades:
        fe = t.get("first_entry_ts")
        exp = t.get("expiry")
        dte_v = None
        if fe and exp:
            try:
                dte_v = (date.fromisoformat(str(exp)[:10])
                         - date.fromisoformat(fe[:10])).days
            except (TypeError, ValueError):
                pass
        enriched_user.append({**t, "_dte_bucket": _dte_bucket(dte_v)})

    enriched_coh = [
        {**r, "_dte_bucket": _dte_bucket(r.get("dte"))}
        for r in cohort_rows
    ]

    # Overall
    overall = {
        "user":   _outcome_stats_for(enriched_user, "actual_pct",    _user_win),
        "cohort": _outcome_stats_for(enriched_coh,  "realized_roi",  _coh_win),
    }

    def _delta(u, c):
        if u is None or c is None:
            return None
        return round(u - c, 1)
    overall["delta"] = {
        k: _delta(overall["user"].get(k), overall["cohort"].get(k))
        for k in ("win_rate", "median_roi", "median_mfe", "median_mae", "median_capture")
    }

    # By DTE bucket
    by_dte = []
    for b in ("0DTE", "1–2", "3–7", "8+"):
        u_rows = [r for r in enriched_user if r["_dte_bucket"] == b]
        c_rows = [r for r in enriched_coh  if r["_dte_bucket"] == b]
        u = _outcome_stats_for(u_rows, "actual_pct",   _user_win)
        c = _outcome_stats_for(c_rows, "realized_roi", _coh_win)
        delta = {k: _delta(u.get(k), c.get(k))
                 for k in ("win_rate", "median_roi", "median_mfe", "median_capture")}
        by_dte.append({"bucket": b, "user": u, "cohort": c, "delta": delta})

    # By side (Calls / Puts)
    by_side = []
    for side, label in (("C", "Calls"), ("P", "Puts")):
        u_rows = [r for r in enriched_user if r.get("right") == side]
        c_rows = [r for r in enriched_coh  if r.get("right") == side]
        u = _outcome_stats_for(u_rows, "actual_pct",   _user_win)
        c = _outcome_stats_for(c_rows, "realized_roi", _coh_win)
        delta = {k: _delta(u.get(k), c.get(k))
                 for k in ("win_rate", "median_roi", "median_mfe", "median_capture")}
        by_side.append({"side": side, "label": label, "user": u, "cohort": c, "delta": delta})

    # By sector — pick the top sectors by combined headcount
    sector_counts: dict[str, int] = defaultdict(int)
    for r in enriched_user + enriched_coh:
        s = r.get("sector")
        if s:
            sector_counts[s] += 1
    top_sectors = [s for s, _ in sorted(sector_counts.items(), key=lambda x: -x[1])[:6]]

    by_sector = []
    for sec in top_sectors:
        u_rows = [r for r in enriched_user if r.get("sector") == sec]
        c_rows = [r for r in enriched_coh  if r.get("sector") == sec]
        u = _outcome_stats_for(u_rows, "actual_pct",   _user_win)
        c = _outcome_stats_for(c_rows, "realized_roi", _coh_win)
        delta = {k: _delta(u.get(k), c.get(k))
                 for k in ("win_rate", "median_roi", "median_mfe", "median_capture")}
        by_sector.append({"sector": sec, "user": u, "cohort": c, "delta": delta})

    return {
        "range_key":   range_key,
        "n_user":      len(user_trades),
        "n_cohort":    len(cohort_rows),
        "overall":     overall,
        "by_dte":      by_dte,
        "by_side":     by_side,
        "by_sector":   by_sector,
    }


# ─── Pregame accuracy / outcome loop ─────────────────────────────────────
# Connects each historical pregame's picks to the actual closed trades
# that resulted. Answers:
#   - For each pregame's pick (LIKE / WATCH / PASS), did you take it?
#   - When you did, what was the outcome?
#   - Is the analysis "calibrated" — does LIKE win more than WATCH?

def _pregame_dates() -> list[str]:
    """All cached pregame analysis dates (YYYY-MM-DD)."""
    from pathlib import Path
    p = Path.home() / ".gamma" / "automation" / "analyses"
    if not p.exists():
        return []
    return sorted(f.stem for f in p.glob("*.json"))


def _load_pregame(d: str):
    from . import analysis as _anl
    r = _anl.get_cached(d)
    if not r or r.get("status") != "ok":
        return None
    return r.get("analysis") or None


def pregame_accuracy(range_key: str = "all") -> dict:
    """For every cached pregame, walk its picks and attach the outcome
    of any matching trade you took that same day."""
    trades = closed_trades(limit=5000, range_key=range_key)

    # Index trades by (date, ticker) for fast lookup. Multiple trades on the
    # same ticker on the same day collapse into a list (e.g. chain legs).
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for t in trades:
        fe = t.get("first_entry_ts") or ""
        if not fe:
            continue
        by_key[(fe[:10], (t.get("ticker") or "").upper())].append(t)

    pregames = []
    verdict_buckets: dict[str, list[dict]] = defaultdict(list)

    for d in _pregame_dates():
        a = _load_pregame(d)
        if not a or not a.get("picks"):
            continue
        picks_out = []
        for p in a["picks"]:
            tkr = (p.get("ticker") or "").upper()
            matched = by_key.get((d, tkr), [])
            total_pnl = round(sum(t.get("realized_pnl") or 0 for t in matched), 0)
            roi_med = None
            if matched:
                rois = [t["actual_pct"] for t in matched if t.get("actual_pct") is not None]
                if rois:
                    roi_med = round(median(rois), 1)
            row = {
                "ticker":     tkr,
                "verdict":    p.get("verdict"),
                "size":       p.get("suggested_size"),
                "plan":       p.get("plan"),
                "took":       len(matched) > 0,
                "n_trades":   len(matched),
                "trade_ids":  [t["intent_id"] for t in matched],
                "total_pnl":  total_pnl,
                "median_roi": roi_med,
                "winner":     total_pnl > 0,
                "loser":      total_pnl < 0,
            }
            picks_out.append(row)
            verdict_buckets[p.get("verdict") or "—"].append(row)

        pregames.append({
            "date":     d,
            "headline": (a.get("day_read") or {}).get("headline"),
            "conviction": (a.get("day_read") or {}).get("conviction"),
            "picks":    picks_out,
            "n_picks":  len(picks_out),
            "n_taken":  sum(1 for r in picks_out if r["took"]),
            "total_pnl": round(sum(r["total_pnl"] for r in picks_out), 0),
        })

    # Calibration table — for each verdict tier, summarize the trades the
    # user actually took that matched a pick of that verdict.
    calibration = []
    for v in ("LIKE", "WATCH", "PASS"):
        rows = verdict_buckets.get(v, [])
        taken = [r for r in rows if r["took"]]
        wins  = sum(1 for r in taken if r["winner"])
        losses = sum(1 for r in taken if r["loser"])
        roi_pool = [r["median_roi"] for r in taken if r["median_roi"] is not None]
        calibration.append({
            "verdict":      v,
            "n_picks":      len(rows),
            "n_taken":      len(taken),
            "n_wins":       wins,
            "n_losses":     losses,
            "take_rate":    round(len(taken) / len(rows) * 100, 1) if rows else None,
            "win_rate":     round(wins / len(taken) * 100, 1) if taken else None,
            "median_roi":   round(median(roi_pool), 1) if roi_pool else None,
            "total_pnl":    round(sum(r["total_pnl"] for r in taken), 0),
        })

    return {
        "pregames":     sorted(pregames, key=lambda x: x["date"], reverse=True),
        "calibration":  calibration,
        "n_pregames":   len(pregames),
        "n_total_picks": sum(p["n_picks"] for p in pregames),
        "n_taken":      sum(p["n_taken"] for p in pregames),
    }


# ─── Today view ──────────────────────────────────────────────────────────
# Composition of: today's pregame + stale/near-level tickers + recent
# performance + watchlist (tickers recently traded with setups that historically print).

def today_dashboard(loss_cap: float = 1000.0) -> dict:
    """Single payload for the /today page. No new analysis — just lifts
    existing rollups into a daily-routine artifact.
    """
    from . import analysis as _anl
    from . import levels as _lv
    from . import tagging
    today_iso = date.today().isoformat()

    # ── Today's pregame (if cached) ──────────────────────────────────
    pregame = None
    cached = _anl.get_cached(today_iso)
    if cached and cached.get("status") == "ok":
        a = cached.get("analysis") or {}
        pregame = {
            "headline":   (a.get("day_read") or {}).get("headline"),
            "conviction": (a.get("day_read") or {}).get("conviction"),
            "warnings":   (a.get("day_read") or {}).get("blackouts_or_warnings") or [],
            "summary":    a.get("setup_summary") or {},
            "picks":      [
                {
                    "ticker":  p.get("ticker"),
                    "verdict": p.get("verdict"),
                    "size":    p.get("suggested_size"),
                    "plan":    p.get("plan"),
                    "rr":      (p.get("setup_eval") or {}).get("rr_verdict"),
                }
                for p in (a.get("picks") or [])
            ],
        }

    # ── Levels-to-watch: stale snapshots + near-top/bottom ─────────────
    try:
        from src import bars_store
    except Exception:
        bars_store = None
    snaps = _lv.latest_for_all()
    near_levels = []
    for s in snaps:
        live = s.current_price
        if bars_store is not None:
            try:
                df = bars_store.load_underlying_bars(s.ticker)
                if not df.empty:
                    live = float(df["c"].dropna().iloc[-1])
            except (FileNotFoundError, IndexError, ValueError, KeyError):
                pass
        if live is None:
            continue
        info = _lv.needs_refresh(s, live)
        if info["stale"]:
            near_levels.append({
                "ticker": s.ticker,
                "live":   round(live, 2),
                "reasons": info["reasons"],
                "stale":  True,
            })
        else:
            # Also surface near-level (within 1%) when not strictly "stale"
            top_above = min((lv for lv in s.levels_above if lv >= live), default=None)
            top_below = max((lv for lv in s.levels_below if lv <= live), default=None)
            nearest_above = (top_above, (top_above - live) / live * 100) if top_above else None
            nearest_below = (top_below, (live - top_below) / live * 100) if top_below else None
            for lvl, dist in filter(None, (nearest_above, nearest_below)):
                if dist <= 0.5:
                    near_levels.append({
                        "ticker": s.ticker,
                        "live":   round(live, 2),
                        "reasons": [f"within {dist:.2f}% of mapped level ${lvl:g}"],
                        "stale":  False,
                    })
                    break
    # Cap at ~10 entries, prioritize stale
    near_levels.sort(key=lambda r: (not r["stale"], r["ticker"]))
    near_levels = near_levels[:10]

    # ── Recent performance ────────────────────────────────────────────
    trades = closed_trades(limit=5000)
    today_trades = [t for t in trades if (t.get("first_entry_ts") or "")[:10] == today_iso]
    today_pnl = round(sum(t.get("realized_pnl") or 0 for t in today_trades), 0)
    last_5 = trades[:5]    # closed_trades is newest-first

    # Equity curve quick read
    eq = equity_curve()
    perf = {
        "today_pnl":         today_pnl,
        "today_n":           len(today_trades),
        "current_pnl":       eq.get("current_pnl"),
        "current_drawdown":  eq.get("current_drawdown"),
        "max_drawdown":      eq.get("max_drawdown"),
        "loss_cap":          loss_cap,
        "remaining":         round(loss_cap + today_pnl, 0),
        "last_5": [
            {
                "intent_id":   t["intent_id"],
                "ticker":      t["ticker"],
                "exit_date":   (t.get("last_exit_ts") or "")[:10],
                "realized_pnl": round(t.get("realized_pnl") or 0, 0),
                "actual_pct":  t.get("actual_pct"),
                "is_winner":   (t.get("realized_pnl") or 0) > 0,
            }
            for t in last_5
        ],
    }

    # ── Watchlist: tickers recently traded with strong recent setups ──
    # For each ticker traded in the last N days, compute WR + median ROI
    # on its 0DTE / 1-2 DTE setups (since those dominate the user's book).
    from collections import defaultdict, Counter
    recent_tickers = Counter()
    for t in trades[:30]:
        recent_tickers[t["ticker"]] += 1
    watchlist = []
    for tkr, count in recent_tickers.most_common(8):
        same = [t for t in trades if t["ticker"] == tkr]
        wins = sum(1 for t in same if (t.get("realized_pnl") or 0) > 0)
        wr = round(wins / len(same) * 100, 1) if same else None
        med_roi = None
        rois = [t["actual_pct"] for t in same if t.get("actual_pct") is not None]
        if rois:
            med_roi = round(median(rois), 1)
        # Pull the most common tags for this ticker
        tag_counter = Counter()
        for t in same:
            for tag_row in tagging.get_tags(t["intent_id"]):
                tag_counter[tag_row["tag"]] += 1
        top_tags = [t for t, _ in tag_counter.most_common(3) if t in tagging.TAG_INDEX]
        watchlist.append({
            "ticker":     tkr,
            "n":          len(same),
            "wr":         wr,
            "median_roi": med_roi,
            "top_tags":   top_tags,
        })

    return {
        "date":        today_iso,
        "pregame":     pregame,
        "near_levels": near_levels,
        "perf":        perf,
        "watchlist":   watchlist,
    }


# ─── Playbook gallery ────────────────────────────────────────────────────
# Per-tag aggregation across closed trades, with best-example and
# cautionary-tale picks for the gallery card.

def playbook_gallery(range_key: str = "all") -> list[dict]:
    """One card-row per setup tag with N, WR, median ROI, total P&L,
    plus 3 example winners + 1 cautionary loser per tag."""
    from . import tagging
    trades = closed_trades(limit=5000, range_key=range_key)
    by_intent = {t["intent_id"]: t for t in trades}

    # Pull every (intent_id, tag) row
    with _connect() as conn:
        rows = conn.execute(
            "SELECT intent_id, tag FROM trade_tags"
        ).fetchall()
    by_tag: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        t = by_intent.get(r["intent_id"])
        if t is not None:
            by_tag[r["tag"]].append(t)

    out = []
    for key, fam, label, desc in tagging.TAG_CATALOG:
        tag_trades = by_tag.get(key, [])
        n = len(tag_trades)
        if n == 0:
            continue
        wins = sum(1 for t in tag_trades if (t.get("realized_pnl") or 0) > 0)
        rois = [t.get("actual_pct") for t in tag_trades if t.get("actual_pct") is not None]
        caps = [t.get("capture_pct") for t in tag_trades if t.get("capture_pct") is not None]

        # Best examples — top winners by realized P&L
        sorted_by_pnl = sorted(tag_trades, key=lambda t: t.get("realized_pnl") or 0, reverse=True)
        best = sorted_by_pnl[:3]
        worst = sorted_by_pnl[-1:] if sorted_by_pnl[-1].get("realized_pnl", 0) < 0 else []

        def _slim(t):
            return {
                "intent_id":    t["intent_id"],
                "ticker":       t["ticker"],
                "strike":       t.get("strike"),
                "right":        t.get("right"),
                "expiry":       t.get("expiry"),
                "entry_date":   (t.get("first_entry_ts") or "")[:10],
                "realized_pnl": round(t.get("realized_pnl") or 0, 0),
                "actual_pct":   t.get("actual_pct"),
                "capture_pct":  t.get("capture_pct"),
            }

        out.append({
            "key":             key,
            "family":          fam,
            "label":           label,
            "desc":            desc,
            "n":               n,
            "wins":            wins,
            "losses":          n - wins,
            "win_rate":        round(wins / n * 100, 1),
            "median_roi":      round(median(rois), 1) if rois else None,
            "median_capture":  round(median(caps), 1) if caps else None,
            "total_pnl":       round(sum(t.get("realized_pnl") or 0 for t in tag_trades), 0),
            "avg_pnl":         round(sum(t.get("realized_pnl") or 0 for t in tag_trades) / n, 0),
            "best":            [_slim(t) for t in best],
            "worst":           [_slim(t) for t in worst],
        })
    # Sort: families in catalog order, then by N desc within family
    family_order = {"level": 0, "structure": 1, "time": 2, "execution": 3}
    out.sort(key=lambda r: (family_order.get(r["family"], 99), -r["n"]))
    return out


# ─── Time-of-day expectancy heatmap ──────────────────────────────────────
# Per-hour-of-entry win rate + median ROI + total P&L. Hour 9..15 covers
# regular trading hours; extended-hours entries fall in 4..8 or 16..19.

def time_of_day_heatmap(range_key: str = "all") -> dict:
    """Grid: entry hour (rows) × (n, win_rate, median_roi, total_pnl)."""
    trades = closed_trades(limit=5000, range_key=range_key)
    by_hour: dict[int, list[dict]] = defaultdict(list)
    for t in trades:
        ts = t.get("first_entry_ts")
        if not ts:
            continue
        try:
            h = datetime.fromisoformat(ts[:19]).hour
        except ValueError:
            continue
        by_hour[h].append(t)

    rows = []
    # Pre-market 7-9, RTH 9-16, after-hours 16-20 — only show hours we have
    for h in sorted(by_hour.keys()):
        bucket = by_hour[h]
        n = len(bucket)
        wins = sum(1 for t in bucket if (t.get("realized_pnl") or 0) > 0)
        rois = [t.get("actual_pct") for t in bucket if t.get("actual_pct") is not None]
        rows.append({
            "hour":       h,
            "label":      f"{h:02d}:00",
            "n":          n,
            "wins":       wins,
            "win_rate":   round(wins / n * 100, 1),
            "median_roi": round(median(rois), 1) if rois else None,
            "total_pnl":  round(sum(t.get("realized_pnl") or 0 for t in bucket), 0),
            "avg_pnl":    round(sum(t.get("realized_pnl") or 0 for t in bucket) / n, 0),
        })

    # Headline best/worst hour by total P&L (with n≥3 to filter anecdote)
    qualified = [r for r in rows if r["n"] >= 3]
    best_hour  = max(qualified, key=lambda r: r["total_pnl"], default=None)
    worst_hour = min(qualified, key=lambda r: r["total_pnl"], default=None)

    return {
        "rows":       rows,
        "best_hour":  best_hour,
        "worst_hour": worst_hour,
        "n_total":    sum(r["n"] for r in rows),
    }
