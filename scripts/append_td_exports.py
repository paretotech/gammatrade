"""Read every TD daily export in data/td_exports/ and append new trades
to the user's master log.

Idempotent: trades already in the master log (matched by symbol + date +
entry-minute within 5 min) are skipped.

Usage:
  .venv/bin/python -u scripts/append_td_exports.py \\
      --master data/master_trade_log.csv \\
      --exports data/td_exports/

If the exports dir doesn't exist or is empty, the script exits cleanly
without modifying anything (this is the steady state when no new TD
files have been dropped yet).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.td_reader import merge_td_into_master, read_td_directory


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--master", default="data/master_trade_log.csv")
    p.add_argument("--exports", default="data/td_exports/")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be appended without writing")
    args = p.parse_args(argv)

    master_path = ROOT / args.master
    exports_dir = ROOT / args.exports
    if not exports_dir.exists():
        print(f"no exports dir at {exports_dir} — nothing to append", flush=True)
        return 0

    fills = read_td_directory(exports_dir)
    if fills.empty:
        print(f"no TD CSVs in {exports_dir} — nothing to append", flush=True)
        return 0
    print(f"loaded {len(fills)} fills from {exports_dir}", flush=True)

    if not master_path.exists():
        # First-ever import: no master log yet. Start with an empty frame so
        # merge_td_into_master treats every TD fill as a new append.
        print(f"no existing master log at {master_path} — initializing empty", flush=True)
        master_path.parent.mkdir(parents=True, exist_ok=True)
        master_df = pd.DataFrame()
    else:
        master_df = pd.read_csv(master_path, dtype=str, keep_default_na=False)
        print(f"existing master log: {len(master_df)} rows", flush=True)

    merged, appended, updated = merge_td_into_master(master_df, fills)
    if not appended and not updated:
        print("no changes (all TD trades already present and closed)", flush=True)
        return 0

    if appended:
        print(f"\nwould append {len(appended)} new trades:", flush=True)
        for r in appended:
            print(f"  + {r['Date']}  {r['Ticker/option strike']}  @ {r['Time of alert (EST)']}  entry ${r['Entry']}  status={r.get('Weighted ROI')}", flush=True)

    if updated:
        print(f"\nwould update {len(updated)} OPEN rows with closing fills:", flush=True)
        for r in updated:
            print(f"  ~ {r['Date']}  {r['Ticker/option strike']}  →  {r['new Weighted ROI']}", flush=True)

    if args.dry_run:
        print("\n(dry-run — not writing)", flush=True)
        return 0

    merged.to_csv(master_path, index=False)
    print(f"\nwrote {master_path} with {len(merged)} total rows", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
