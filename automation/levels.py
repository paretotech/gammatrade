"""Support/resistance level management.

Also see `parse_pasted_levels()` at the bottom — turns the chartist's
copy-paste format into LevelSnapshot rows ready for upsert. The
Settings → Levels paste box is the primary daily-update surface.

Stores per-ticker key price levels (sourced from a chartist's snapshots
or hand-edited) in the `ticker_levels` SQLite table. Multiple snapshots
per ticker are retained so we can see how levels evolved over time;
application code normally reads via `latest_for_ticker` which collapses
to the most-recent row.

Levels are inserted via:
  - bulk CSV import (one-shot history backfill from Discord)
  - manual upsert from the Settings → Levels UI

And consumed by:
  - the pregame analysis prompt (inject active levels for each candidate)
  - the per-trade detail page (overlay on the underlying-price context)
  - Analytics → Leakage (group stops by distance-to-nearest-level)
"""
from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from . import state


# ─── Data model ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LevelSnapshot:
    ticker:        str
    asof_ts:       str           # ISO timestamp
    current_price: Optional[float]
    levels_below:  list[float]   # sorted ascending toward current_price
    levels_above:  list[float]   # sorted ascending away from current_price
    source:        str
    note:          str = ""

    def to_dict(self) -> dict:
        return {
            "ticker":        self.ticker,
            "asof_ts":       self.asof_ts,
            "current_price": self.current_price,
            "levels_below":  list(self.levels_below),
            "levels_above":  list(self.levels_above),
            "source":        self.source,
            "note":          self.note,
        }


def _parse_pipe_levels(s: Optional[str]) -> list[float]:
    if not s:
        return []
    out = []
    for tok in str(s).split("|"):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.append(float(tok))
        except ValueError:
            continue
    return out


def _serialize_levels(vals: Iterable[float]) -> str:
    return "|".join(f"{v:g}" for v in vals)


# ─── Read paths ──────────────────────────────────────────────────────────

def latest_for_ticker(ticker: str,
                      path: Path = state.DB_PATH) -> Optional[LevelSnapshot]:
    """Return the most recent level snapshot for `ticker`, or None."""
    with state.connect(path) as conn:
        r = conn.execute(
            "SELECT * FROM ticker_levels WHERE ticker = ? "
            "ORDER BY asof_ts DESC LIMIT 1",
            (ticker.upper(),),
        ).fetchone()
    if r is None:
        return None
    return LevelSnapshot(
        ticker=r["ticker"],
        asof_ts=r["asof_ts"],
        current_price=r["current_price"],
        levels_below=_parse_pipe_levels(r["levels_below"]),
        levels_above=_parse_pipe_levels(r["levels_above"]),
        source=r["source"] or "",
        note=r["note"] or "",
    )


def latest_for_all(path: Path = state.DB_PATH) -> list[LevelSnapshot]:
    """Most recent snapshot per ticker. Sorted by ticker ascending."""
    with state.connect(path) as conn:
        rows = conn.execute("""
            SELECT t1.* FROM ticker_levels t1
            INNER JOIN (
                SELECT ticker, MAX(asof_ts) AS max_ts
                FROM ticker_levels GROUP BY ticker
            ) t2 ON t1.ticker = t2.ticker AND t1.asof_ts = t2.max_ts
            ORDER BY t1.ticker ASC
        """).fetchall()
    return [
        LevelSnapshot(
            ticker=r["ticker"],
            asof_ts=r["asof_ts"],
            current_price=r["current_price"],
            levels_below=_parse_pipe_levels(r["levels_below"]),
            levels_above=_parse_pipe_levels(r["levels_above"]),
            source=r["source"] or "",
            note=r["note"] or "",
        )
        for r in rows
    ]


def needs_refresh(snap: LevelSnapshot,
                  current_price: float,
                  ath_threshold_pct: float = 1.0) -> dict:
    """Decide whether a snapshot needs refreshing.

    Returns a dict with `stale` (bool) and `reasons` (list of human strings).
    Triggers:
      - current_price is above the highest known `levels_above` (near ATH;
        new resistance levels haven't been mapped yet)
      - current_price is below the lowest known `levels_below`
      - current_price is within `ath_threshold_pct` % of the top level
        (about to break out — refresh recommended)
    """
    reasons = []
    above = snap.levels_above
    below = snap.levels_below

    if above and current_price > max(above):
        reasons.append(
            f"price ${current_price:.2f} above all mapped resistance "
            f"(top ${max(above):.2f})"
        )
    elif above:
        top = max(above)
        dist_pct = (top - current_price) / top * 100
        if 0 <= dist_pct <= ath_threshold_pct:
            reasons.append(
                f"within {dist_pct:.1f}% of top resistance ${top:.2f}"
            )

    if below and current_price < min(below):
        reasons.append(
            f"price ${current_price:.2f} below all mapped support "
            f"(bottom ${min(below):.2f})"
        )

    return {"stale": bool(reasons), "reasons": reasons}


# ─── Write paths ─────────────────────────────────────────────────────────

def upsert(snap: LevelSnapshot, path: Path = state.DB_PATH) -> None:
    """Insert or replace one snapshot."""
    with state.connect(path) as conn:
        conn.execute("""
            INSERT INTO ticker_levels
                (ticker, asof_ts, current_price, levels_below, levels_above,
                 source, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, asof_ts) DO UPDATE SET
                current_price = excluded.current_price,
                levels_below  = excluded.levels_below,
                levels_above  = excluded.levels_above,
                source        = excluded.source,
                note          = excluded.note
        """, (
            snap.ticker.upper(),
            snap.asof_ts,
            snap.current_price,
            _serialize_levels(snap.levels_below),
            _serialize_levels(snap.levels_above),
            snap.source,
            snap.note,
        ))
        conn.commit()


def delete_ticker(ticker: str, path: Path = state.DB_PATH) -> int:
    """Drop ALL snapshots for a ticker. Returns rows affected."""
    with state.connect(path) as conn:
        cur = conn.execute(
            "DELETE FROM ticker_levels WHERE ticker = ?",
            (ticker.upper(),),
        )
        conn.commit()
        return cur.rowcount


def import_csv(csv_path: Path, path: Path = state.DB_PATH) -> dict:
    """Bulk-import a Discord-export CSV with one snapshot per row.

    Expected columns:
        timestamp, ticker, current_price, levels_below, levels_above,
        n_below, n_above, message_id

    Returns counts: {rows_in, inserted, skipped}.
    """
    rows_in = 0
    inserted = 0
    skipped = 0
    with state.connect(path) as conn, open(csv_path, newline="") as fp:
        rdr = csv.DictReader(fp)
        for row in rdr:
            rows_in += 1
            ticker = (row.get("ticker") or "").strip().upper()
            ts     = (row.get("timestamp") or "").strip()
            if not ticker or not ts:
                skipped += 1
                continue
            try:
                price = float(row.get("current_price") or 0) or None
            except ValueError:
                price = None
            below = _parse_pipe_levels(row.get("levels_below"))
            above = _parse_pipe_levels(row.get("levels_above"))
            note  = (row.get("message_id") or "").strip()
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO ticker_levels
                        (ticker, asof_ts, current_price, levels_below,
                         levels_above, source, note)
                    VALUES (?, ?, ?, ?, ?, 'discord_import', ?)
                """, (
                    ticker, ts, price,
                    _serialize_levels(below),
                    _serialize_levels(above),
                    note,
                ))
                inserted += 1
            except sqlite3.Error:
                skipped += 1
        conn.commit()
    return {"rows_in": rows_in, "inserted": inserted, "skipped": skipped}


# ─── Paste-format parser ─────────────────────────────────────────────────
import re as _re

# Match one ticker block. Tolerant to:
#   - Any whitespace between ticker and "(Current Price: ..."
#   - Optional "$" before the price
#   - "Levels Below:" / "Levels Above:" labels (case-insensitive)
#   - Brackets [] OR plain comma-separated list with no brackets
#   - Trailing or leading whitespace on each line
_TICKER_LINE = _re.compile(
    r"^\s*([A-Z][A-Z0-9.]{0,9})\s*\(\s*Current Price\s*:?\s*\$?\s*([\d.]+)\s*\)",
    _re.IGNORECASE,
)
_BELOW_LINE = _re.compile(
    r"^\s*Levels?\s*Below\s*:?\s*\[?([0-9.,\s]+?)\]?\s*$",
    _re.IGNORECASE,
)
_ABOVE_LINE = _re.compile(
    r"^\s*Levels?\s*Above\s*:?\s*\[?([0-9.,\s]+?)\]?\s*$",
    _re.IGNORECASE,
)


def _split_levels(raw: str) -> list[float]:
    """Parse '270.02, 274.33, 277.33' (with optional brackets) → [float, ...]."""
    out = []
    for tok in raw.replace(";", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.append(float(tok))
        except ValueError:
            continue
    return out


def parse_pasted_levels(text: str,
                        asof_ts: Optional[str] = None,
                        source: str = "manual") -> list[LevelSnapshot]:
    """Parse one OR many ticker blocks from pasted text.

    Format per block (3 lines, in any order for Below/Above):
        AAPL  (Current Price: $291.10)
        Levels Below: [270.02, 274.33, 277.33, 282.54, 288.72]
        Levels Above: [293.86, 300.01, 310.58, 315.16, 318.09]

    Multiple blocks may be pasted at once, separated by blank lines or
    just back-to-back. Each ticker line starts a new block; if a later
    block omits Levels Below or Above the missing side is left empty.
    Returns the parsed LevelSnapshot list (NOT persisted — caller upserts).
    """
    asof_ts = asof_ts or datetime.now().isoformat(timespec="seconds")

    blocks: list[dict] = []
    current: Optional[dict] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m = _TICKER_LINE.match(line)
        if m:
            # Start a new block. Push the prior one if it has any data.
            if current is not None:
                blocks.append(current)
            current = {
                "ticker":        m.group(1).upper(),
                "current_price": float(m.group(2)),
                "below":         [],
                "above":         [],
            }
            continue

        if current is None:
            continue

        mb = _BELOW_LINE.match(line)
        if mb:
            current["below"] = _split_levels(mb.group(1))
            continue
        ma = _ABOVE_LINE.match(line)
        if ma:
            current["above"] = _split_levels(ma.group(1))
            continue
        # Any other line — ignore (allows pasting around extra commentary).

    if current is not None:
        blocks.append(current)

    out: list[LevelSnapshot] = []
    for b in blocks:
        out.append(LevelSnapshot(
            ticker=b["ticker"],
            asof_ts=asof_ts,
            current_price=b["current_price"],
            levels_below=b["below"],
            levels_above=b["above"],
            source=source,
            note="",
        ))
    return out


# ─── Setup evaluation ────────────────────────────────────────────────────
# Two questions every pregame pick should answer deterministically:
#   1. Does the entry level make sense — i.e. is it actually on a published
#      support/resistance, or did the trader pick a number that doesn't
#      correspond to any mapped level?
#   2. From that level to the next-level boundary on each side, what's the
#      reward-to-risk ratio in the underlying?
# Both go into the pregame analysis so Claude (and the trader) judge
# setup quality before sizing — not just structure quality.

def evaluate_pick_level(ticker: str,
                        entry_level: float,
                        direction: str,
                        on_tolerance_pct: float = 0.25,
                        near_tolerance_pct: float = 0.5) -> Optional[dict]:
    """Score a proposed pregame entry against the ticker's published levels.

    Args:
        direction: 'above' (bullish, call-style break) or 'below' (bearish).

    Returns None if no level snapshot exists for the ticker. Otherwise
    returns a dict with:
      - level_status:     'on' | 'near' | 'off'
      - matched_level:    the published level the entry snaps to (or None)
      - matched_dist_pct: % distance from entry to matched_level
      - stop_ref_level:   nearest published level on the stop side
      - target_ref_level: nearest published level on the target side
      - reward_pct:       % move from entry to target_ref (signed positive
                          when the trade direction agrees with the target)
      - risk_pct:         % move from entry to stop_ref (always positive
                          magnitude — the distance you'd give up to stop)
      - rr_ratio:         reward/risk (None when either side is missing)
      - rr_verdict:       'good' (≥2.0) | 'fair' (1.0-2.0) | 'poor' (<1.0)
                          | 'incomplete' (one side missing)
    """
    snap = latest_for_ticker(ticker)
    if snap is None:
        return None

    all_levels  = sorted(set((snap.levels_below or []) + (snap.levels_above or [])))
    if not all_levels:
        return None

    # ── Question 1: does the entry level snap to a published one? ────
    closest = min(all_levels, key=lambda v: abs(v - entry_level))
    dist_pct = abs(closest - entry_level) / entry_level * 100 if entry_level else None

    if dist_pct is None:
        status = "off"
    elif dist_pct <= on_tolerance_pct:
        status = "on"
    elif dist_pct <= near_tolerance_pct:
        status = "near"
    else:
        status = "off"

    matched_level = closest if status in ("on", "near") else None

    # ── Question 2: R/R to the next published level ──────────────────
    # For a bullish "above" pick, the trader is buying a break:
    #   reward = next published level ABOVE entry
    #   risk   = nearest published level BELOW entry  (= where you'd cover)
    # For a bearish "below" pick, inverse.
    above_levels = [v for v in all_levels if v > entry_level]
    below_levels = [v for v in all_levels if v < entry_level]
    next_up   = min(above_levels) if above_levels else None
    next_down = max(below_levels) if below_levels else None

    if direction == "below":
        target_ref = next_down
        stop_ref   = next_up
    else:    # default to bullish ('above' or unknown)
        target_ref = next_up
        stop_ref   = next_down

    reward_pct = None
    risk_pct   = None
    rr_ratio   = None
    if target_ref is not None and entry_level > 0:
        reward_pct = abs(target_ref - entry_level) / entry_level * 100
    if stop_ref is not None and entry_level > 0:
        risk_pct = abs(entry_level - stop_ref) / entry_level * 100
    if reward_pct is not None and risk_pct is not None and risk_pct > 0:
        rr_ratio = reward_pct / risk_pct

    if rr_ratio is None:
        rr_verdict = "incomplete"
    elif rr_ratio >= 2.0:
        rr_verdict = "good"
    elif rr_ratio >= 1.0:
        rr_verdict = "fair"
    else:
        rr_verdict = "poor"

    return {
        "level_status":     status,
        "matched_level":    matched_level,
        "matched_dist_pct": round(dist_pct, 2) if dist_pct is not None else None,
        "stop_ref_level":   stop_ref,
        "target_ref_level": target_ref,
        "reward_pct":       round(reward_pct, 2) if reward_pct is not None else None,
        "risk_pct":         round(risk_pct, 2) if risk_pct is not None else None,
        "rr_ratio":         round(rr_ratio, 2) if rr_ratio is not None else None,
        "rr_verdict":       rr_verdict,
    }
