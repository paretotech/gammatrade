"""Support/resistance level management.

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
