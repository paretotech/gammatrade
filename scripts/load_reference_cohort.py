"""Load the bundled anonymized reference cohort into the user's SQLite.

Idempotent — re-running skips rows already loaded by ref_id. Runs once
automatically on post-install; can be re-run anytime to refresh after a
plugin update.

Usage:
    python3 scripts/load_reference_cohort.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from automation import state


def _f(v):
    try:
        return float(v) if v not in (None, "", "nan") else None
    except (ValueError, TypeError):
        return None


def _i(v):
    f = _f(v)
    return int(f) if f is not None else None


def _b(v) -> int:
    s = str(v).strip().lower() if v is not None else ""
    return 1 if s in ("true", "1", "yes") else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", default="data/reference/reference_cohort.csv")
    p.add_argument("--reset", action="store_true")
    args = p.parse_args(argv)

    csv = ROOT / args.csv
    if not csv.exists():
        print(f"reference cohort not found at {csv} — skipping (clean install OK)")
        return 0

    # Ensure DB exists + schema is current
    state.init_db()

    if args.reset:
        with state.connect() as conn:
            n = conn.execute("DELETE FROM reference_trades").rowcount
            print(f"reset: deleted {n} existing reference rows")

    df = pd.read_csv(csv, dtype=str, keep_default_na=False)
    print(f"loaded {len(df)} rows · {len(df.columns)} cols from {csv.name}")

    inserted = 0
    skipped = 0

    with state.connect() as conn:
        for _, r in df.iterrows():
            ref_id = str(r.get("entry_id") or "").strip()
            if not ref_id:
                continue
            existing = conn.execute(
                "SELECT 1 FROM reference_trades WHERE ref_id = ?", (ref_id,)
            ).fetchone()
            if existing:
                skipped += 1
                continue

            # Map fractions → percentages where appropriate
            mfe_in = _f(r.get("mfe_to_exit"))
            mae_in = _f(r.get("mae_to_exit"))
            mfe_exp = _f(r.get("mfe_to_expiry"))
            mae_exp = _f(r.get("mae_to_expiry"))
            roi = _f(r.get("adjusted_roi")) or _f(r.get("reported_roi"))

            conn.execute(
                """INSERT INTO reference_trades
                (ref_id, trader_handle, ticker, expiry, strike, right,
                 entry_ts, entry_price, exit_ts, exit_price, contracts,
                 realized_roi, status, regime, sector,
                 mfe_in_pct, mae_in_pct, mfe_to_expiry_pct, mae_to_expiry_pct,
                 dte, lotto)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ref_id,
                    str(r.get("trader_handle") or "reference_trader_01"),
                    str(r.get("ticker") or "").upper(),
                    str(r.get("expiry") or "")[:10],
                    _f(r.get("strike")),
                    str(r.get("type") or "C")[:1],
                    str(r.get("entry_ts") or ""),
                    _f(r.get("entry_price")),
                    str(r.get("last_exit_ts") or ""),
                    _f(r.get("wavg_exit_price")),
                    _i(r.get("units_closed")) or _i(r.get("n_exits")) or 1,
                    round(roi * 100, 2) if roi is not None else None,
                    str(r.get("status") or ""),
                    str(r.get("regime") or "") or str(r.get("qqq_regime") or ""),
                    str(r.get("sector_etf") or ""),
                    round(mfe_in * 100, 2) if mfe_in is not None else None,
                    round(mae_in * 100, 2) if mae_in is not None else None,
                    round(mfe_exp * 100, 2) if mfe_exp is not None else None,
                    round(mae_exp * 100, 2) if mae_exp is not None else None,
                    _i(r.get("dte")),
                    _b(r.get("lotto")),
                ),
            )
            inserted += 1

    print(f"inserted: {inserted}  skipped (already present): {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
