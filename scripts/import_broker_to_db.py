"""Load the master trade log into the automation SQLite as trade_intents + fills.

Called by the broker-CSV import pipeline after append_td_exports has updated
the master CSV. Idempotent: each row maps to a stable intent_id derived from
its entry timestamp + symbol so re-runs only add NEW trades.

Usage:
    python3 scripts/import_broker_to_db.py [--master data/master_trade_log.csv]
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


def _f(v):
    try:
        return float(v) if v not in (None, "", "nan") else None
    except (ValueError, TypeError):
        return None


def _i(v):
    f = _f(v)
    return int(f) if f is not None else None


def _stable_intent_id(date_str: str, symbol: str, time_str: str) -> str:
    """Deterministic ID so re-runs match existing rows."""
    raw = f"{date_str}|{symbol}|{time_str}"
    h = hashlib.sha256(raw.encode()).hexdigest()
    return f"trade_{h[:16]}"


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--master", default="data/master_trade_log.csv",
                   help="Path to the master log CSV maintained by append_td_exports.py")
    args = p.parse_args(argv)

    csv = ROOT / args.master
    if not csv.exists():
        print(f"master log not found at {csv} — nothing to import")
        return 0

    df = pd.read_csv(csv, dtype=str, keep_default_na=False)
    print(f"loaded {len(df)} rows from {csv.name}")

    state.init_db()
    inserted = 0
    skipped = 0
    skipped_invalid = 0

    for _, r in df.iterrows():
        # Expected columns from append_td_exports's master format
        date_str = (r.get("Date") or "").strip()
        sym = (r.get("Ticker/option strike") or "").strip()
        time_str = (r.get("Time of alert (EST)") or "").strip()
        entry_price = _f(r.get("Entry"))
        contracts = _i(r.get("Contracts"))
        if not (date_str and sym and entry_price and contracts):
            skipped_invalid += 1
            continue

        intent_id = _stable_intent_id(date_str, sym, time_str)
        if state.get_intent(intent_id):
            skipped += 1
            continue

        # Parse symbol "AAPL C 15MAY26 300.00"
        parts = sym.split()
        if len(parts) < 4:
            skipped_invalid += 1
            continue
        ticker, right, expiry_raw, strike_str = parts[0], parts[1][:1], parts[2], parts[3]

        # expiry_raw like "15MAY26" → ISO "2026-05-15"
        months = {"JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05","JUN":"06",
                  "JUL":"07","AUG":"08","SEP":"09","OCT":"10","NOV":"11","DEC":"12"}
        if len(expiry_raw) == 7:
            dd, mmm, yy = expiry_raw[:2], expiry_raw[2:5], expiry_raw[5:]
            expiry_iso = f"20{yy}-{months.get(mmm, '01')}-{dd}"
        else:
            expiry_iso = expiry_raw

        try:
            strike = float(strike_str)
        except ValueError:
            skipped_invalid += 1
            continue

        # Status / ROI from the master log
        weighted_roi = (r.get("Weighted ROI") or "").strip()
        is_open = weighted_roi == "OPEN" or not weighted_roi
        status = "pending" if is_open else "filled"

        # Combined entry timestamp (date + time-of-alert)
        entry_ts = f"{date_str} {time_str}" if time_str else date_str

        intent_row = {
            "intent_id": intent_id,
            "created_at": entry_ts,
            "ticker": ticker,
            "expiry": expiry_iso,
            "strike": strike,
            "right": right,
            "contracts": contracts,
            "order_type": "MKT",
            "limit_price": entry_price,
            "regime_tag": "NORMAL",
            "chain_role": "solo",
            "sector": "—",
            "status": status,
            "notes": None,
            "tp_ladder_choice": "auto",
            "tp_split_choice": "50_25_25",
            "roll_plan": "default",
            "stop_discipline": "be_stop",
            "stop_initial_pct": 0.0,
            "stop_after_tp1_pct": 0.0,
            "stop_after_tp2_pct": 0.05,
            "current_option_price": entry_price,
        }

        with state.connect() as conn:
            cols = ", ".join(intent_row.keys())
            placeholders = ", ".join("?" * len(intent_row))
            conn.execute(
                f"INSERT INTO trade_intents ({cols}) VALUES ({placeholders})",
                tuple(intent_row.values()))

            # BUY fill
            conn.execute(
                "INSERT INTO fills (fill_id, intent_id, ts, side, contracts, price, is_entry) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"{intent_id}_buy", intent_id, entry_ts, "BUY",
                 contracts, entry_price, 1))

            # TP fills from columns TP1 Exit / TP1 % / TP1 ROI etc.
            for tier in (1, 2, 3, 4):
                exit_price = _f(r.get(f"TP{tier} Exit"))
                if not exit_price:
                    continue
                # tp{n} % is fraction of position closed
                units_pct = _f(r.get(f"TP{tier}%"))
                qty = max(1, round((units_pct or 0) / 100 * contracts)) if units_pct else 1
                qty = min(qty, contracts)
                conn.execute(
                    "INSERT INTO fills (fill_id, intent_id, ts, side, contracts, price, is_entry, tp_tier) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"{intent_id}_tp{tier}", intent_id, entry_ts, "SELL",
                     qty, exit_price, 0, tier))

        inserted += 1

    print(f"\ninserted: {inserted}  skipped (already present): {skipped}  invalid: {skipped_invalid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
