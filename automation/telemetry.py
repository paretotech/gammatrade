"""Append-only event log + tail helpers for the /logs view."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from collections import deque

LOG_PATH = Path.home() / ".gamma" / "automation" / "events.jsonl"


def log_event(kind: str, payload: dict[str, Any]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": datetime.utcnow().isoformat(), "kind": kind, **payload}
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


def tail_events(n: int = 200) -> list[dict]:
    if not LOG_PATH.exists():
        return []
    with open(LOG_PATH) as f:
        last = deque(f, maxlen=n)
    out = []
    for line in last:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(out))


def seed_demo_events() -> None:
    if LOG_PATH.exists() and LOG_PATH.stat().st_size > 0:
        return
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    demo = [
        ("intent_created", {"ticker": "NVDA", "strike": 950, "right": "C", "contracts": 1}),
        ("entry_submitted", {"ticker": "NVDA", "order_type": "MKT"}),
        ("entry_filled", {"ticker": "NVDA", "price": 8.20, "contracts": 1}),
        ("be_stop_placed", {"ticker": "NVDA", "stop_price": 8.20, "latency_ms": 312}),
        ("tp_ladder_placed", {"ticker": "NVDA", "tp1": 11.81, "tp2": 13.06, "tp3": 17.34}),
        ("tp_filled", {"ticker": "NVDA", "tier": 1, "price": 10.13, "realized": 193.0}),
        ("intent_created", {"ticker": "MU", "strike": 600, "right": "C", "contracts": 1}),
        ("entry_submitted", {"ticker": "MU", "order_type": "MKT"}),
        ("entry_filled", {"ticker": "MU", "price": 3.50, "contracts": 1}),
        ("be_stop_placed", {"ticker": "MU", "stop_price": 3.50, "latency_ms": 287}),
        ("tp_ladder_placed", {"ticker": "MU", "tp1": 4.32, "tp2": 4.56, "tp3": 5.24}),
        ("reconciliation_ok", {"orders_checked": 4, "drift": 0}),
    ]
    for kind, payload in demo:
        log_event(kind, payload)
