"""Build the anonymized reference cohort that ships with the plugin.

INPUT  : the user's private data/brando_enriched.csv (paths configurable)
OUTPUT : data/reference/reference_cohort.csv (shipped in the plugin)

Anonymization treatment:
  - Drop any free-text columns (enrichment_note, anything *note*/*comment*)
  - Replace entry_id with a one-way hashed token: ref_<hash[:12]>
  - Round all timestamps to 5-minute boundaries
  - Drop any column whose name contains 'shane' or 'brando' (safety net —
    none currently match but defends against future schema drift)
  - Add trader_handle column = 'reference_trader_01'
  - Keep ALL numerical context (Polygon-derived market data, MFE/MAE,
    regime tags, level-break features, etc.) — that's the analytical value

Run once before publishing the plugin:
    python3 scripts/build_reference_cohort.py \\
        --input ../gamma/data/brando_enriched.csv \\
        --output data/reference/reference_cohort.csv
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import pandas as pd

# Columns dropped outright — anything that could carry narrative text
DROP_COLUMNS = {
    "enrichment_note",
    "enrichment_status",
    "sanity_flag",
}

# Substring filters — drop any column whose name matches (safety net)
DROP_SUBSTRINGS = ("note", "comment", "comm_", "reason", "lesson", "emotion",
                   "shane", "brando", "alert", "discord", "message")

# Columns that hold timestamps — rounded to 5min
TS_COLUMNS = [
    "entry_ts", "last_exit_ts",
    "tp1_ts", "tp2_ts", "tp3_ts", "tp4_ts",
    "resolved_exit_ts_utc",
]

SECRET_SALT = "gammatrade-public-v1"  # fixed so repeat runs yield same IDs


def _hash_id(raw: str) -> str:
    h = hashlib.sha256(f"{SECRET_SALT}:{raw}".encode()).hexdigest()
    return f"ref_{h[:12]}"


def _round_5min(s: pd.Series) -> pd.Series:
    """Round ISO-format datetime strings to nearest 5-minute mark."""
    dt = pd.to_datetime(s, errors="coerce", utc=True)
    rounded = dt.dt.round("5min")
    return rounded.dt.strftime("%Y-%m-%dT%H:%M:%S%z").fillna("")


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input", default="../gamma/data/brando_enriched.csv",
                   help="Path to the private source CSV")
    p.add_argument("--output", default="data/reference/reference_cohort.csv")
    args = p.parse_args(argv)

    in_path = Path(args.input).expanduser().resolve()
    out_path = Path(args.output).expanduser().resolve()
    if not in_path.exists():
        print(f"error: {in_path} not found", file=sys.stderr)
        return 1

    df = pd.read_csv(in_path, dtype=str, keep_default_na=False)
    print(f"loaded {len(df)} rows · {len(df.columns)} cols from {in_path.name}")

    # Drop narrative / PII-risk columns
    drop_cols = set()
    for c in df.columns:
        if c in DROP_COLUMNS:
            drop_cols.add(c)
            continue
        lc = c.lower()
        if any(sub in lc for sub in DROP_SUBSTRINGS):
            drop_cols.add(c)
    if drop_cols:
        df = df.drop(columns=list(drop_cols))
        print(f"dropped {len(drop_cols)} narrative/PII columns: {sorted(drop_cols)}")

    # Round timestamps
    rounded = []
    for col in TS_COLUMNS:
        if col in df.columns:
            df[col] = _round_5min(df[col])
            rounded.append(col)
    if rounded:
        print(f"rounded {len(rounded)} timestamp cols to 5-min: {rounded}")

    # Hash entry_id
    if "entry_id" in df.columns:
        df["entry_id"] = df["entry_id"].apply(_hash_id)
        print(f"hashed entry_id → ref_<12char>")

    # Tag the trader handle
    df.insert(1, "trader_handle", "reference_trader_01")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    print(f"\nwrote {out_path}")
    print(f"  rows: {len(df)}  cols: {len(df.columns)}  size: {out_path.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
