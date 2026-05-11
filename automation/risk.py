"""Configurable risk-limit evaluator.

`gates.py` holds the hard-coded blocks (kill switch, familiarity, index
strike rule). This module evaluates the user-tunable Risk-limit caps from
`/settings/risk` and returns a per-gate result so the UI can render
ok / caution / decline state with utilization.

Each check returns a `RiskCheck` dict:
    {
        "id": str,                # stable identifier
        "name": str,              # human label
        "state": "ok"|"caution"|"decline",
        "enforcement": "caution"|"decline",  # configured mode (only when breached)
        "headline": str,          # one-line summary (used as banner)
        "utilization": str,       # "$320 of $1,000 used (32%)"
        "breached": bool,
    }

A trade is BLOCKED iff any check has state == "decline".
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from . import state
from .rules import Rules


def _enf(raw: dict, *path: str, default: str = "decline") -> str:
    """Pluck rules.yaml[a][b][c]... safely with a default."""
    cur: Any = raw
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    if cur in ("caution", "decline"):
        return cur
    return default


def _state(breached: bool, enforcement: str) -> str:
    if not breached:
        return "ok"
    return "decline" if enforcement == "decline" else "caution"


def check_daily_loss(intent: dict, rules: Rules) -> dict:
    raw = rules.raw or {}
    cap = float(rules.daily_loss_cap())  # already absolute
    enforcement = _enf(raw, "daily_caps", "loss_enforcement", default="decline")
    metric = state.daily_metric() or {}
    realized = float(metric.get("realized_pnl") or 0)
    used = max(0.0, -realized)  # only the loss side counts
    pct = (used / cap * 100) if cap > 0 else 0.0
    breached = used >= cap and cap > 0
    return {
        "id": "daily_loss",
        "name": "Max daily loss",
        "state": _state(breached, enforcement),
        "enforcement": enforcement,
        "headline": (f"Daily loss cap breached: ${used:.0f} ≥ ${cap:.0f}"
                     if breached else f"${used:.0f} of ${cap:.0f} used"),
        "utilization": f"${used:.0f} / ${cap:.0f}  ({pct:.0f}%)",
        "breached": breached,
    }


def check_daily_count(intent: dict, rules: Rules) -> dict:
    raw = rules.raw or {}
    enforcement = _enf(raw, "daily_caps", "count_enforcement", default="decline")
    regime = intent.get("regime_tag", "NORMAL")
    cap = int(rules.daily_count_cap(regime))
    today_count = int(state.count_today_entries())
    chain_role = (intent.get("chain_role") or "solo")
    is_roll = chain_role in ("R2", "R3")
    if is_roll:
        return {
            "id": "daily_count",
            "name": "Daily entries cap",
            "state": "ok",
            "enforcement": enforcement,
            "headline": "Roll (R2/R3) — exempt from daily cap",
            "utilization": f"{today_count} entries today (rolls don't count)",
            "breached": False,
        }
    breached = today_count >= cap
    return {
        "id": "daily_count",
        "name": "Daily entries cap",
        "state": _state(breached, enforcement),
        "enforcement": enforcement,
        "headline": (f"{regime} cap breached: {today_count} ≥ {cap}"
                     if breached else f"{today_count} of {cap} {regime} entries today"),
        "utilization": f"{today_count} / {cap}  ({regime})",
        "breached": breached,
    }


def check_max_open_positions(intent: dict, rules: Rules) -> dict:
    raw = rules.raw or {}
    enforcement = _enf(raw, "risk_limits", "size_enforcement", default="decline")
    cap = int((raw.get("risk_limits") or {}).get("max_open_positions", 5))
    open_n = len(state.open_positions())
    breached = open_n >= cap
    return {
        "id": "max_open_positions",
        "name": "Max open positions",
        "state": _state(breached, enforcement),
        "enforcement": enforcement,
        "headline": (f"Open positions at cap: {open_n} ≥ {cap}"
                     if breached else f"{open_n} of {cap} open"),
        "utilization": f"{open_n} / {cap}",
        "breached": breached,
    }


def check_per_trade_size(intent: dict, rules: Rules) -> dict:
    raw = rules.raw or {}
    enforcement = _enf(raw, "risk_limits", "size_enforcement", default="decline")
    cap = float((raw.get("risk_limits") or {}).get("per_trade_max_dollars", 1500))
    contracts = int(intent.get("contracts") or 0)
    price = float(intent.get("limit_price") or 0)
    cost = contracts * price * 100  # $ premium at risk
    pct = (cost / cap * 100) if cap > 0 else 0.0
    breached = cost > cap and cap > 0
    if cost <= 0:
        # No price — skip with informational ok
        return {
            "id": "per_trade_size",
            "name": "Per-trade size cap",
            "state": "ok",
            "enforcement": enforcement,
            "headline": "Size unknown (no limit price)",
            "utilization": f"max ${cap:.0f} per trade",
            "breached": False,
        }
    return {
        "id": "per_trade_size",
        "name": "Per-trade size cap",
        "state": _state(breached, enforcement),
        "enforcement": enforcement,
        "headline": (f"Trade size ${cost:.0f} exceeds ${cap:.0f} cap"
                     if breached else f"${cost:.0f} of ${cap:.0f} cap"),
        "utilization": f"${cost:.0f} / ${cap:.0f}  ({pct:.0f}%)",
        "breached": breached,
    }


def check_dte_bounds(intent: dict, rules: Rules) -> dict:
    raw = rules.raw or {}
    enforcement = _enf(raw, "risk_limits", "dte_enforcement", default="caution")
    rl = raw.get("risk_limits") or {}
    min_dte = int(rl.get("min_dte", 0))
    max_dte = int(rl.get("max_dte", 21))
    expiry_str = intent.get("expiry")
    try:
        dte = (date.fromisoformat(expiry_str) - date.today()).days
    except (TypeError, ValueError):
        return {
            "id": "dte_bounds",
            "name": "DTE bounds",
            "state": "ok",
            "enforcement": enforcement,
            "headline": "DTE unknown",
            "utilization": f"window {min_dte}-{max_dte}",
            "breached": False,
        }
    breached = dte < min_dte or dte > max_dte
    return {
        "id": "dte_bounds",
        "name": "DTE bounds",
        "state": _state(breached, enforcement),
        "enforcement": enforcement,
        "headline": (f"DTE {dte} outside {min_dte}-{max_dte} window"
                     if breached else f"DTE {dte} (window {min_dte}-{max_dte})"),
        "utilization": f"{dte} DTE  (window {min_dte}-{max_dte})",
        "breached": breached,
    }


def check_sector_concentration(intent: dict, rules: Rules) -> dict:
    sector = intent.get("sector") or "—"
    warn_at = int(rules.sector_warn_at())
    reject_at = int(rules.sector_reject_at())
    open_n = int(state.open_sector_count(sector)) if sector and sector != "—" else 0
    if open_n + 1 >= reject_at:
        result_state = "decline"
        headline = f"Sector cap reached: {open_n} open in {sector} (reject ≥ {reject_at})"
    elif open_n + 1 >= warn_at:
        result_state = "caution"
        headline = f"Sector heavy: {open_n} open in {sector} (warn ≥ {warn_at})"
    else:
        result_state = "ok"
        headline = f"{open_n} open in {sector}"
    # Two-tier system — enforcement is implicit. Report whichever tier is binding.
    return {
        "id": "sector_concentration",
        "name": "Sector concentration",
        "state": result_state,
        "enforcement": "decline" if result_state == "decline" else "caution",
        "headline": headline,
        "utilization": f"{open_n} open in {sector}  (warn {warn_at} · reject {reject_at})",
        "breached": result_state != "ok",
    }


# ─── Blackout windows — calendar-driven ────────────────────────────────────
#
# Sources read from rules.yaml event_calendar block. Dates are user-editable
# at /settings/risk so they can be refreshed when new schedules drop.

def _parse_dates(values) -> list[date]:
    out = []
    for v in (values or []):
        if isinstance(v, date):
            out.append(v)
            continue
        try:
            out.append(date.fromisoformat(str(v)))
        except (TypeError, ValueError):
            continue
    return out


def _calendar(rules: Rules) -> dict:
    cal = (rules.raw or {}).get("event_calendar") or {}
    earnings: dict[str, date] = {}
    for k, v in (cal.get("megacap_earnings") or {}).items():
        if not v:
            continue
        try:
            earnings[k.upper()] = (v if isinstance(v, date)
                                    else date.fromisoformat(str(v)))
        except (TypeError, ValueError):
            continue
    return {
        "fomc": _parse_dates(cal.get("fomc_meetings")),
        "cpi": _parse_dates(cal.get("cpi_releases")),
        "earnings": earnings,
    }


def _fomc_blackout(meetings: list[date], today: Optional[date] = None) -> tuple[bool, Optional[date]]:
    """Returns (active, next_meeting_date). Blackout = Tuesday 09:30 ET
    through Wednesday 14:30 ET of the meeting week, where the date in the
    YAML is the Wednesday (statement release).

    Operationally: today is in blackout if the upcoming Wednesday meeting
    is exactly tomorrow (we're on Tue) OR today (we're on Wed before 14:30).
    For the purposes of an entry gate, we treat the whole Tue + Wed as off.
    """
    today = today or date.today()
    for m in sorted(meetings):
        if m < today:
            continue
        # Tuesday before Wednesday meeting
        from datetime import timedelta
        tuesday = m - timedelta(days=1)
        if today == tuesday or today == m:
            return True, m
        return False, m  # next future meeting; not active
    return False, None


def _cpi_blackout(releases: list[date], today: Optional[date] = None) -> tuple[bool, Optional[date]]:
    today = today or date.today()
    for r in sorted(releases):
        if r < today:
            continue
        return today == r, r
    return False, None


def _earnings_blackout(ticker: str, earnings: dict, today: Optional[date] = None,
                        max_dte: int = 14) -> tuple[bool, Optional[date]]:
    """Returns active=True if ticker has earnings inside max_dte days."""
    today = today or date.today()
    d = earnings.get(ticker.upper()) if ticker else None
    if not d:
        return False, None
    delta = (d - today).days
    return (0 <= delta <= max_dte), d


def _check_blackout_pre_fomc(intent: dict, rules: Rules) -> dict:
    bw = ((rules.raw or {}).get("blackout_windows") or {}).get("pre_fomc") or {}
    enabled = bw.get("enabled", True)
    enforcement = bw.get("enforcement", "decline")
    if not enabled:
        return {"id": "blackout_pre_fomc", "name": "Pre-FOMC",
                "state": "ok", "enforcement": enforcement,
                "headline": "Disabled", "utilization": "off", "breached": False}
    cal = _calendar(rules)
    active, next_date = _fomc_blackout(cal["fomc"])
    if next_date is None:
        return {"id": "blackout_pre_fomc", "name": "Pre-FOMC",
                "state": "ok", "enforcement": enforcement,
                "headline": "No upcoming FOMC dates in calendar",
                "utilization": "calendar empty", "breached": False}
    days = (next_date - date.today()).days
    headline = ("In pre-FOMC blackout (Tue–Wed of meeting week)" if active
                else f"Next FOMC: {next_date.isoformat()} ({days}d)")
    util = "active" if active else f"next {next_date.isoformat()}"
    return {"id": "blackout_pre_fomc", "name": "Pre-FOMC",
            "state": _state(active, enforcement),
            "enforcement": enforcement,
            "headline": headline, "utilization": util, "breached": active}


def _check_blackout_cpi(intent: dict, rules: Rules) -> dict:
    bw = ((rules.raw or {}).get("blackout_windows") or {}).get("cpi_day") or {}
    enabled = bw.get("enabled", True)
    enforcement = bw.get("enforcement", "decline")
    if not enabled:
        return {"id": "blackout_cpi_day", "name": "CPI day",
                "state": "ok", "enforcement": enforcement,
                "headline": "Disabled", "utilization": "off", "breached": False}
    cal = _calendar(rules)
    active, next_date = _cpi_blackout(cal["cpi"])
    if next_date is None:
        return {"id": "blackout_cpi_day", "name": "CPI day",
                "state": "ok", "enforcement": enforcement,
                "headline": "No upcoming CPI prints in calendar",
                "utilization": "calendar empty", "breached": False}
    days = (next_date - date.today()).days
    headline = ("CPI release today — entries blocked" if active
                else f"Next CPI: {next_date.isoformat()} ({days}d)")
    util = "active" if active else f"next {next_date.isoformat()}"
    return {"id": "blackout_cpi_day", "name": "CPI day",
            "state": _state(active, enforcement),
            "enforcement": enforcement,
            "headline": headline, "utilization": util, "breached": active}


def _check_blackout_earnings(intent: dict, rules: Rules) -> dict:
    bw = ((rules.raw or {}).get("blackout_windows") or {}).get("pre_megacap_earnings") or {}
    enabled = bw.get("enabled", True)
    enforcement = bw.get("enforcement", "caution")
    ticker = (intent.get("ticker") or "").upper()
    if not enabled:
        return {"id": "blackout_pre_megacap_earnings", "name": "Pre-megacap earnings",
                "state": "ok", "enforcement": enforcement,
                "headline": "Disabled", "utilization": "off", "breached": False}
    cal = _calendar(rules)
    active, next_date = _earnings_blackout(ticker, cal["earnings"])
    if next_date is None:
        return {"id": "blackout_pre_megacap_earnings", "name": "Pre-megacap earnings",
                "state": "ok", "enforcement": enforcement,
                "headline": (f"No earnings date for {ticker} on calendar"
                             if ticker else "No ticker"),
                "utilization": "n/a", "breached": False}
    days = (next_date - date.today()).days
    headline = (f"{ticker} earnings in {days}d ({next_date.isoformat()})"
                if active else
                f"{ticker} earnings {next_date.isoformat()} ({days}d) — outside blackout")
    util = f"earnings {next_date.isoformat()}"
    return {"id": "blackout_pre_megacap_earnings", "name": "Pre-megacap earnings",
            "state": _state(active, enforcement),
            "enforcement": enforcement,
            "headline": headline, "utilization": util, "breached": active}


def evaluate(intent: dict, rules: Rules) -> dict:
    """Run every configurable risk check. Returns:
        {
            "checks": [RiskCheck, ...],
            "blocked": bool,         # any state == 'decline'
            "warnings": int,         # count of state == 'caution'
            "decline_reasons": [str, ...],
        }
    """
    checks = [
        check_daily_loss(intent, rules),
        check_daily_count(intent, rules),
        check_max_open_positions(intent, rules),
        check_per_trade_size(intent, rules),
        check_dte_bounds(intent, rules),
        check_sector_concentration(intent, rules),
        _check_blackout_pre_fomc(intent, rules),
        _check_blackout_cpi(intent, rules),
        _check_blackout_earnings(intent, rules),
    ]
    decline_reasons = [c["headline"] for c in checks if c["state"] == "decline"]
    warnings = sum(1 for c in checks if c["state"] == "caution")
    return {
        "checks": checks,
        "blocked": bool(decline_reasons),
        "warnings": warnings,
        "decline_reasons": decline_reasons,
    }
