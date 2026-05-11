"""Conditional triggers — pre-committed entries that fire on level break.

A trigger watches an underlying ticker price for a level break in a
specified direction. Optionally requires additional conditions on other
tickers (e.g., NVDA above 212 AND QQQ above 700 AND SPY above PDH) — all
must be met simultaneously for the trigger to fire. Maps directly onto
`data-derived` and the macro-confluence rules in
strategy_rules.md.

When evaluation finds all conditions true, the trigger fires: it converts
to a trade intent, runs through pre-trade gates, and either creates the
entry or rejects with a reason.

Built around `data-derived`: eliminate
FOMO-vulnerable real-time deliberation by pre-posting "IF X, THEN Y".

Price source: the `prices` table. Set manually from the UI for now;
swap for broker market-data subscription later.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Any, Optional

from . import state, telemetry


# ─── Trigger CRUD ───────────────────────────────────────────────────────────

def create_trigger(data: dict[str, Any], path: Path = state.DB_PATH) -> str:
    trigger_id = state.new_id()
    row = {
        "trigger_id": trigger_id,
        "created_at": datetime.utcnow().isoformat(),
        "status": "waiting",
        **data,
    }
    cols = ", ".join(row.keys())
    placeholders = ", ".join("?" * len(row))
    with state.connect(path) as conn:
        conn.execute(f"INSERT INTO triggers ({cols}) VALUES ({placeholders})", tuple(row.values()))
    telemetry.log_event("trigger_created", {
        "trigger_id": trigger_id, "ticker": data["ticker"],
        "direction": data["direction"], "level": data["level"],
    })
    return trigger_id


def list_triggers(status: Optional[str] = None, limit: int = 200,
                  path: Path = state.DB_PATH) -> list[dict]:
    with state.connect(path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM triggers WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM triggers ORDER BY created_at DESC LIMIT ?", (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_trigger(trigger_id: str, path: Path = state.DB_PATH) -> Optional[dict]:
    with state.connect(path) as conn:
        row = conn.execute("SELECT * FROM triggers WHERE trigger_id = ?", (trigger_id,)).fetchone()
    return dict(row) if row else None


def update_trigger(trigger_id: str, fields: dict[str, Any],
                   path: Path = state.DB_PATH) -> bool:
    """Update a waiting trigger's fields. Only works on status='waiting'.
    Caller is responsible for validating values."""
    if not fields:
        return False
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [trigger_id]
    with state.connect(path) as conn:
        cur = conn.execute(
            f"UPDATE triggers SET {cols} WHERE trigger_id = ? AND status = 'waiting'",
            values,
        )
        ok = cur.rowcount > 0
    if ok:
        telemetry.log_event("trigger_updated", {
            "trigger_id": trigger_id, "fields": list(fields.keys()),
        })
    return ok


def cancel_trigger(trigger_id: str, path: Path = state.DB_PATH) -> bool:
    with state.connect(path) as conn:
        cur = conn.execute(
            "UPDATE triggers SET status = 'canceled' WHERE trigger_id = ? AND status = 'waiting'",
            (trigger_id,),
        )
        ok = cur.rowcount > 0
    if ok:
        telemetry.log_event("trigger_canceled", {"trigger_id": trigger_id})
    return ok


# ─── Price store ────────────────────────────────────────────────────────────

def set_price(ticker: str, price: float, source: str = "manual",
              path: Path = state.DB_PATH) -> None:
    ticker = ticker.upper()
    ts = datetime.utcnow().isoformat()
    with state.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO prices (ticker, price, updated_at, source)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                price = excluded.price,
                updated_at = excluded.updated_at,
                source = excluded.source
            """,
            (ticker, price, ts, source),
        )
        conn.execute(
            "INSERT INTO price_history (ticker, price, ts, source) VALUES (?, ?, ?, ?)",
            (ticker, price, ts, source),
        )
    telemetry.log_event("price_set", {"ticker": ticker, "price": price, "source": source})


def price_history(ticker: str, since: datetime,
                  path: Path = state.DB_PATH) -> list[dict]:
    """Return price history for a ticker since a given UTC datetime."""
    with state.connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM price_history WHERE ticker = ? AND ts >= ? ORDER BY ts",
            (ticker.upper(), since.isoformat()),
        ).fetchall()
    return [dict(r) for r in rows]


def get_price(ticker: str, path: Path = state.DB_PATH) -> Optional[dict]:
    with state.connect(path) as conn:
        row = conn.execute("SELECT * FROM prices WHERE ticker = ?", (ticker.upper(),)).fetchone()
    return dict(row) if row else None


def list_prices(path: Path = state.DB_PATH) -> list[dict]:
    with state.connect(path) as conn:
        rows = conn.execute("SELECT * FROM prices ORDER BY ticker").fetchall()
    return [dict(r) for r in rows]


# ─── Evaluation ─────────────────────────────────────────────────────────────

# ─── Direction logic ────────────────────────────────────────────────────────

DIRECTIONS = {
    "above":         "Above",
    "below":         "Below",
    "hold_above":    "Hold above",
    "hold_below":    "Hold below",
    "bounce_above":  "Bounce off (up)",
    "bounce_below":  "Bounce off (down)",
}


# Defaults — overridden by rules.yaml `trigger_dynamics` if present
DEFAULT_HOLD_SECONDS = 300       # 5 min continuously past level
DEFAULT_BOUNCE_EPS_PCT = 0.001   # 0.1% past level confirms bounce
DEFAULT_BOUNCE_WINDOW_SEC = 3600 # touch must have happened within 60 min


def _trigger_dynamics(rules) -> dict:
    cfg = (getattr(rules, "raw", {}) or {}).get("trigger_dynamics") or {}
    return {
        "hold_seconds": int(cfg.get("hold_seconds", DEFAULT_HOLD_SECONDS)),
        "bounce_eps_pct": float(cfg.get("bounce_eps_pct", DEFAULT_BOUNCE_EPS_PCT)),
        "bounce_window_sec": int(cfg.get("bounce_window_sec", DEFAULT_BOUNCE_WINDOW_SEC)),
    }


def _condition_met_simple(direction: str, level: float, current: float) -> bool:
    if direction == "above":
        return current >= level
    if direction == "below":
        return current <= level
    return False


def _evaluate_hold(history: list[dict], level: float, direction: str,
                   hold_seconds: int) -> tuple[bool, dict]:
    """Hold above/below: continuously past level for hold_seconds."""
    if not history:
        return False, {"reason": "no history"}
    cmp_op = (lambda p: p >= level) if direction == "hold_above" else (lambda p: p <= level)
    # Walk backwards from latest sample. We need an unbroken streak that
    # spans at least hold_seconds.
    history_sorted = sorted(history, key=lambda r: r["ts"])
    if not cmp_op(history_sorted[-1]["price"]):
        return False, {"reason": "not currently past level",
                       "current": history_sorted[-1]["price"]}
    # Find the most recent sample that violated the condition
    streak_start = None
    for r in reversed(history_sorted):
        if not cmp_op(r["price"]):
            break
        streak_start = r["ts"]
    if streak_start is None:
        return False, {"reason": "no streak"}
    streak_dt = datetime.fromisoformat(streak_start)
    streak_seconds = (datetime.utcnow() - streak_dt).total_seconds()
    if streak_seconds >= hold_seconds:
        return True, {"streak_seconds": streak_seconds, "streak_start": streak_start}
    return False, {"streak_seconds": streak_seconds, "needed": hold_seconds,
                   "streak_start": streak_start}


def _evaluate_bounce(history: list[dict], level: float, direction: str,
                     eps_pct: float, window_sec: int) -> tuple[bool, dict]:
    """Bounce off level: must have TOUCHED the level (or crossed into it) in
    the last window_sec, then moved past by eps_pct.

    bounce_above: touch <= level (came down to it), now > level * (1 + eps)
    bounce_below: touch >= level (came up to it), now < level * (1 - eps)
    """
    if not history:
        return False, {"reason": "no history"}
    cutoff = datetime.utcnow() - __import__("datetime").timedelta(seconds=window_sec)
    in_window = [r for r in history if datetime.fromisoformat(r["ts"]) >= cutoff]
    if not in_window:
        return False, {"reason": f"no samples in last {window_sec}s"}
    in_window.sort(key=lambda r: r["ts"])
    current = in_window[-1]["price"]

    if direction == "bounce_above":
        touched = any(r["price"] <= level for r in in_window)
        eps = level * eps_pct
        confirmed = current > level + eps
        return (touched and confirmed), {
            "touched": touched, "confirmed": confirmed,
            "current": current, "min_seen": min(r["price"] for r in in_window),
        }
    else:  # bounce_below
        touched = any(r["price"] >= level for r in in_window)
        eps = level * eps_pct
        confirmed = current < level - eps
        return (touched and confirmed), {
            "touched": touched, "confirmed": confirmed,
            "current": current, "max_seen": max(r["price"] for r in in_window),
        }


def parse_extra_conditions(raw: Optional[str]) -> list[dict]:
    """Parse the JSON-encoded extra_conditions column."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [c for c in parsed if isinstance(c, dict)
                    and "ticker" in c and "direction" in c and "level" in c]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def condition_status(condition: dict, rules=None) -> dict[str, Any]:
    """Return current state of one condition.

    Handles all directions: above, below, hold_above, hold_below,
    bounce_above, bounce_below. Adds extra detail dict for HOLD/BOUNCE.
    """
    p = get_price(condition["ticker"])
    if not p:
        return {**condition, "current": None, "met": None, "detail": None}
    current = p["price"]
    direction = condition["direction"]
    level = float(condition["level"])

    if direction in ("above", "below"):
        met = _condition_met_simple(direction, level, current)
        return {**condition, "current": current, "met": met, "detail": None}

    # Need history for HOLD and BOUNCE
    dyn = _trigger_dynamics(rules) if rules is not None else {
        "hold_seconds": DEFAULT_HOLD_SECONDS,
        "bounce_eps_pct": DEFAULT_BOUNCE_EPS_PCT,
        "bounce_window_sec": DEFAULT_BOUNCE_WINDOW_SEC,
    }
    if direction in ("hold_above", "hold_below"):
        from datetime import timedelta
        # Look back enough to see whether we have a long-enough streak
        since = datetime.utcnow() - timedelta(seconds=dyn["hold_seconds"] * 3)
        hist = price_history(condition["ticker"], since)
        met, detail = _evaluate_hold(hist, level, direction, dyn["hold_seconds"])
        return {**condition, "current": current, "met": met, "detail": detail}

    if direction in ("bounce_above", "bounce_below"):
        from datetime import timedelta
        since = datetime.utcnow() - timedelta(seconds=dyn["bounce_window_sec"])
        hist = price_history(condition["ticker"], since)
        met, detail = _evaluate_bounce(hist, level, direction,
                                        dyn["bounce_eps_pct"], dyn["bounce_window_sec"])
        return {**condition, "current": current, "met": met, "detail": detail}

    # Unknown direction
    return {**condition, "current": current, "met": False,
            "detail": {"reason": f"unknown direction {direction}"}}


def all_conditions_met(trig: dict, rules=None) -> tuple[bool, list[dict]]:
    """Return (all_met, [status of each condition])."""
    primary = {
        "ticker": trig["ticker"],
        "direction": trig["direction"],
        "level": trig["level"],
        "kind": "primary",
    }
    extras = parse_extra_conditions(trig.get("extra_conditions"))
    statuses = [condition_status(primary, rules)]
    for c in extras:
        statuses.append({**condition_status(c, rules), "kind": "extra"})

    all_met = all(s["met"] is True for s in statuses)
    return all_met, statuses


def evaluate_trigger(trig: dict, gates_module, rules) -> dict[str, Any]:
    """Evaluate a single trigger. If ALL conditions met, fire it (place
    intent through gates). Returns a result dict."""
    ticker = trig["ticker"]
    p = get_price(ticker)
    if not p:
        return {"fired": False, "reason": f"no price for {ticker}"}

    current = p["price"]
    # Update last-seen for the primary ticker
    with state.connect() as conn:
        conn.execute(
            "UPDATE triggers SET last_evaluated_at = ?, last_seen_price = ? WHERE trigger_id = ?",
            (datetime.utcnow().isoformat(), current, trig["trigger_id"]),
        )

    all_met, statuses = all_conditions_met(trig, rules)
    if not all_met:
        unmet = [s for s in statuses if s["met"] is not True]
        return {"fired": False, "reason": "conditions not met",
                "statuses": statuses, "unmet_count": len(unmet)}

    # Fire: build intent dict, run gates, persist intent
    intent_data = {
        "ticker": ticker,
        "expiry": trig["expiry"],
        "strike": trig["strike"],
        "right": trig["right"],
        "contracts": trig["contracts"],
        "order_type": trig["order_type"],
        "limit_price": trig["limit_price"],
        "regime_tag": trig["regime_tag"],
        "chain_role": trig["chain_role"],
        "sector": trig["sector"],
        "notes": trig.get("notes"),
        "brando_alert_id": None,
    }
    ok, reason = gates_module.run_gates({**intent_data, "otm_strikes": 0}, rules)
    if not ok:
        with state.connect() as conn:
            conn.execute(
                "UPDATE triggers SET status = 'rejected', rejection_reason = ?, fired_at = ? WHERE trigger_id = ?",
                (reason, datetime.utcnow().isoformat(), trig["trigger_id"]),
            )
        telemetry.log_event("trigger_rejected", {
            "trigger_id": trig["trigger_id"], "ticker": ticker, "reason": reason,
        })
        return {"fired": False, "rejected": True, "reason": reason}

    intent_id = state.insert_trade_intent(intent_data)
    with state.connect() as conn:
        conn.execute(
            "UPDATE triggers SET status = 'fired', fired_at = ?, fired_intent_id = ? WHERE trigger_id = ?",
            (datetime.utcnow().isoformat(), intent_id, trig["trigger_id"]),
        )
    telemetry.log_event("trigger_fired", {
        "trigger_id": trig["trigger_id"], "intent_id": intent_id,
        "ticker": ticker, "level": trig["level"], "current": current,
    })
    return {"fired": True, "intent_id": intent_id, "current": current, "level": trig["level"]}


def evaluate_all(gates_module, rules) -> list[dict]:
    """Evaluate every waiting trigger. Returns list of result dicts."""
    waiting = list_triggers(status="waiting")
    results = []
    for trig in waiting:
        # expiry check
        if trig.get("expires_at"):
            try:
                exp = datetime.fromisoformat(trig["expires_at"])
                if datetime.utcnow() >= exp:
                    with state.connect() as conn:
                        conn.execute(
                            "UPDATE triggers SET status = 'expired' WHERE trigger_id = ?",
                            (trig["trigger_id"],),
                        )
                    telemetry.log_event("trigger_expired", {"trigger_id": trig["trigger_id"]})
                    continue
            except ValueError:
                pass
        results.append({"trigger": trig, **evaluate_trigger(trig, gates_module, rules)})
    return results


# ─── Background watcher ────────────────────────────────────────────────────

async def watcher_loop(gates_module, rules_provider, interval_seconds: int = 5) -> None:
    """Long-running asyncio task. Polls evaluate_all every N seconds."""
    while True:
        try:
            evaluate_all(gates_module, rules_provider())
        except Exception as e:  # don't let watcher crash silently
            telemetry.log_event("watcher_error", {"error": str(e)})
        await asyncio.sleep(interval_seconds)
