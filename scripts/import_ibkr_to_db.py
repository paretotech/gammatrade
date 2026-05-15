"""Import IBKR Flex Transaction History CSVs into the automation SQLite.

Reads every .csv in data/ibkr_exports/ (or the directory given via --dir),
parses the multi-section format with src.ibkr_reader, groups fills into
trades per contract, and inserts trade_intents + fills rows. Idempotent
via a stable intent_id derived from (date, ticker, expiry, strike, right).

Unlike the TD pipeline, this importer writes directly to the DB — it
does NOT touch data/master_trade_log.csv, which is TD-shaped and a poor
fit for IBKR data.

Usage:
    python3 scripts/import_ibkr_to_db.py [--dir data/ibkr_exports]
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from automation import state
from src.ibkr_reader import read_ibkr_directory


def _stable_intent_id(trade: dict) -> str:
    """Deterministic ID — re-runs of the same IBKR CSV skip existing rows."""
    raw = "|".join([
        "ibkr",
        trade["entry_date"],
        trade["ticker"],
        trade["expiry"],
        f"{trade['strike']:.4f}",
        trade["right"],
    ])
    h = hashlib.sha256(raw.encode()).hexdigest()
    return f"trade_{h[:16]}"


def _iso_ts(date_str: str) -> str:
    """IBKR statements only carry date granularity. Anchor to midnight so
    analytics that do date.fromisoformat(ts[:10]) still bucket correctly."""
    return f"{date_str}T00:00:00"


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", default="data/ibkr_exports",
                   help="Directory of IBKR Flex Transaction History CSVs")
    args = p.parse_args(argv)

    exports = ROOT / args.dir
    trades = read_ibkr_directory(exports)
    if not trades:
        print(f"no IBKR trades found in {exports}")
        return 0
    print(f"parsed {len(trades)} trades from {exports}")

    state.init_db()
    inserted = 0
    skipped = 0
    for t in trades:
        intent_id = _stable_intent_id(t)
        if state.get_intent(intent_id):
            skipped += 1
            continue

        entry_ts = _iso_ts(t["entry_date"])
        exit_ts = _iso_ts(t["exit_date"]) if t["exit_date"] else entry_ts

        intent_row = {
            "intent_id":    intent_id,
            "created_at":   entry_ts,
            "ticker":       t["ticker"],
            "expiry":       t["expiry"],
            "strike":       t["strike"],
            "right":        t["right"],
            "contracts":    t["contracts"],
            "order_type":   "MKT",
            "limit_price":  t["entry_price"],
            "regime_tag":   "NORMAL",
            "chain_role":   "solo",
            "sector":       "—",
            "status":       t["status"],
            "notes":        f"imported from IBKR ({len(t['buys'])} buy fills, {len(t['sells'])} sell fills)",
            "tp_ladder_choice":   "auto",
            "tp_split_choice":    "100",
            "roll_plan":          "default",
            "stop_discipline":    "be_stop",
            "stop_initial_pct":   0.0,
            "stop_after_tp1_pct": 0.0,
            "stop_after_tp2_pct": 0.05,
            "current_option_price": t["entry_price"],
        }

        with state.connect() as conn:
            cols = ", ".join(intent_row.keys())
            placeholders = ", ".join("?" * len(intent_row))
            conn.execute(
                f"INSERT INTO trade_intents ({cols}) VALUES ({placeholders})",
                tuple(intent_row.values()),
            )
            # One fill per original buy / sell line, preserving the
            # individual fill prices (not just the weighted average).
            for i, (d, q, px) in enumerate(t["buys"]):
                conn.execute(
                    "INSERT INTO fills (fill_id, intent_id, ts, side, contracts, "
                    "price, is_entry) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (f"{intent_id}_buy{i}", intent_id, _iso_ts(d), "BUY",
                     q, px, 1),
                )
            for i, (d, q, px) in enumerate(t["sells"]):
                conn.execute(
                    "INSERT INTO fills (fill_id, intent_id, ts, side, contracts, "
                    "price, is_entry, tp_tier) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"{intent_id}_sell{i}", intent_id, _iso_ts(d), "SELL",
                     q, px, 0, 1),
                )
        inserted += 1

    print(f"inserted: {inserted}  skipped (already present): {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
