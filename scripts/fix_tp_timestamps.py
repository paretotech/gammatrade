"""One-shot migration: backfill correct TP-fill timestamps for trades
imported by an earlier version of import_broker_to_db.py.

Versions prior to v0.3.1 wrote every TP fill with ts = entry_ts, so the
"minutes held" column on the trade-detail page always showed 0. The
data needed to fix this (TP{n} Time In Trade) was already in the master
CSV — the importer just ignored it.

This script walks the master log, finds the matching fill row for each
TP tier (via stable_intent_id), parses "Time In Trade", and updates the
fill's ts to entry_ts + delta. Idempotent: rows whose ts already
differs from entry_ts are left alone.

Usage:
    python3 scripts/fix_tp_timestamps.py [--master data/master_trade_log.csv] [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from automation import state
# Reuse helpers from the importer so we stay in lockstep.
from scripts.import_broker_to_db import (
    _parse_time_in_trade,
    _ts_plus,
    _to_iso_ts,
)


def _stable_intent_id(date_str: str, symbol: str, time_str: str) -> str:
    """Same hash function the importer uses to derive intent_ids."""
    raw = f"{date_str}|{symbol}|{time_str}"
    h = hashlib.sha256(raw.encode()).hexdigest()
    return f"trade_{h[:16]}"


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--master", default="data/master_trade_log.csv")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would change without writing.")
    args = p.parse_args(argv)

    csv_path = ROOT / args.master
    if not csv_path.exists():
        print(f"✗ master log not found at {csv_path}", file=sys.stderr)
        return 1

    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    print(f"loaded {len(df)} master-log rows from {csv_path.name}")

    updated = 0
    skipped_no_match = 0
    skipped_no_tit = 0
    skipped_already_set = 0

    with state.connect() as conn:
        for _, r in df.iterrows():
            date_str = (r.get("Date") or "").strip()
            sym      = (r.get("Ticker/option strike") or "").strip()
            time_str = (r.get("Time of alert (EST)") or "").strip()
            if not (date_str and sym):
                continue

            intent_id = _stable_intent_id(date_str, sym, time_str)
            existing = conn.execute(
                "SELECT 1 FROM trade_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if not existing:
                skipped_no_match += 1
                continue

            entry_ts = _to_iso_ts(date_str, time_str)
            if not entry_ts:
                continue

            for tier in (1, 2, 3, 4):
                tit_raw = r.get(f"TP{tier} Time In Trade", "")
                tit = _parse_time_in_trade(tit_raw)
                if tit is None:
                    skipped_no_tit += 1
                    continue
                new_ts = _ts_plus(entry_ts, tit)

                fill = conn.execute(
                    "SELECT ts FROM fills WHERE intent_id=? AND tp_tier=?",
                    (intent_id, tier),
                ).fetchone()
                if not fill:
                    continue
                current_ts = fill["ts"]
                if current_ts and current_ts != entry_ts:
                    # Already migrated, or otherwise non-default — leave alone.
                    skipped_already_set += 1
                    continue
                if args.dry_run:
                    print(f"  would update {intent_id} TP{tier}: {current_ts} → {new_ts}")
                else:
                    conn.execute(
                        "UPDATE fills SET ts=? WHERE intent_id=? AND tp_tier=?",
                        (new_ts, intent_id, tier),
                    )
                updated += 1
        if not args.dry_run:
            conn.commit()

    print()
    print(f"updated:            {updated}")
    print(f"skipped (no Time):  {skipped_no_tit}")
    print(f"skipped (no match): {skipped_no_match}")
    print(f"skipped (already):  {skipped_already_set}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
