"""Pre-trade gates — run server-side on POST /entries.

Each gate returns (passed: bool, reason: str | None).
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from . import state
from .rules import Rules


KILL_FILE = Path.home() / ".gamma" / "automation" / "KILL"
PAUSE_FILE = Path.home() / ".gamma" / "automation" / "PAUSE"


def kill_active() -> bool:
    return KILL_FILE.exists()


def pause_active() -> bool:
    return PAUSE_FILE.exists()


def toggle_kill(on: bool) -> None:
    KILL_FILE.parent.mkdir(parents=True, exist_ok=True)
    if on:
        KILL_FILE.touch()
    elif KILL_FILE.exists():
        KILL_FILE.unlink()


def toggle_pause(on: bool) -> None:
    PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if on:
        PAUSE_FILE.touch()
    elif PAUSE_FILE.exists():
        PAUSE_FILE.unlink()


def run_gates(intent: dict, rules: Rules) -> tuple[bool, str | None]:
    """Returns (ok, reason). reason is None on pass, string on fail."""

    if kill_active():
        return False, "KILL switch active. Toggle off in /kill to resume."
    if pause_active():
        return False, "PAUSE switch active. Toggle off in /kill to resume."

    # Familiarity gate
    ticker = intent["ticker"].upper()
    if not rules.is_familiar(ticker):
        return False, (
            f"WATCH-ONLY: {ticker} is not in familiar tickers list. "
            "Per data-derived, observe at named levels for several "
            "sessions before trading. Add to rules.yaml if you want to whitelist."
        )

    # Index strike rule — auto-derive OTM distance from current underlying
    # price when available. If price is unknown, skip this check (the
    # trigger system will catch outliers when prices come in).
    if ticker in {"QQQ", "SPX", "SPY"}:
        try:
            from . import triggers as _triggers
            price_row = _triggers.get_price(ticker)
            if price_row:
                strike = float(intent.get("strike", 0))
                # Strike-interval estimate: SPX = $5, QQQ/SPY = $1
                interval = 5.0 if ticker == "SPX" else 1.0
                otm = max(0, int(round(abs(strike - price_row["price"]) / interval)))
                if not rules.index_strike_ok(ticker, otm):
                    return False, (
                        f"{ticker}: strike {strike:g} is {otm} strikes from current "
                        f"{price_row['price']:.2f} (max 1 OTM). "
                        "Per data-derived."
                    )
        except Exception:
            pass  # fail-open if price lookup fails

    # Daily count, loss, and sector caps are evaluated in risk.evaluate()
    # with configurable caution/decline enforcement (/settings/risk).
    # Hard blocks here are reserved for KILL/PAUSE/familiarity/index-strike.

    return True, None
