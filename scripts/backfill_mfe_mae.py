"""Backfill MFE / MAE columns on trade_intents using Polygon option bars.

For each closed trade, fetches 1-minute aggregates for the option contract
spanning at minimum entry_ts → expiry, then computes:

  - mfe_in_trade_price  = max(high)  over [entry_ts, exit_ts]
  - mae_in_trade_price  = min(low)   over [entry_ts, exit_ts]
  - mfe_to_expiry_price = max(high)  over [entry_ts, expiry]
  - mae_to_expiry_price = min(low)   over [entry_ts, expiry]

Bulk-pull strategy
------------------
Polygon's options-aggregates endpoint is per-contract (no grouped endpoint
exists for options). What we DO bulk: the date window. One call per
contract covers the full life of the option (minute bars, up to 50k per
call — enough for a multi-week contract). So 96 unique contracts in the
user's data = 96 API calls, not 96 * N-days * N-trades.

Bars are persisted via src.bars_store (parquet), so re-running the
backfill skips contracts whose window is already cached. Within a single
run, src.polygon_client also has an in-memory dedup, but the parquet
cache is what survives across runs.

Requires POLYGON_API_KEY in the env (or saved in the in-app Settings →
API keys tab; the server's startup loads it into os.environ).

Usage:
    python3 scripts/backfill_mfe_mae.py [--limit N] [--force]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from automation import state
from src import bars_store
from src.contract_symbols import build_occ_symbol
from src.polygon_client import PolygonClient, PolygonError


def _parse_ts(s: str) -> datetime | None:
    """Parse 'YYYY-MM-DDTHH:MM:SS' (the importer's canonical format)."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s[:19])
    except ValueError:
        return None


def _expiry_date(s: str) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _closed_trades_needing_backfill(conn, force: bool, limit: int | None):
    """Closed trades, optionally filtered to ones that don't yet have MFE/MAE."""
    where = "i.status IN ('filled','closed')"
    if not force:
        where += " AND (i.mfe_in_trade_price IS NULL OR i.mfe_to_expiry_price IS NULL)"
    q = f"""
        SELECT i.intent_id, i.ticker, i.expiry, i.strike, i.right,
               i.created_at,
               (SELECT MIN(ts) FROM fills f WHERE f.intent_id=i.intent_id AND f.is_entry=1) AS first_entry_ts,
               (SELECT MAX(ts) FROM fills f WHERE f.intent_id=i.intent_id AND f.is_entry=0) AS last_exit_ts
        FROM trade_intents i
        WHERE {where}
    """
    if limit:
        q += f" LIMIT {int(limit)}"
    return conn.execute(q).fetchall()


def _slice_max_min(bars_df, start_ts: datetime, end_ts: datetime):
    """Return (max_high, min_low) within [start_ts, end_ts] inclusive."""
    if bars_df.empty:
        return None, None
    if start_ts is None or end_ts is None:
        return None, None
    if end_ts < start_ts:
        return None, None
    # bars_store writes 't' as UTC ms; convert to naive UTC datetimes for compare.
    mask = (bars_df["t"] >= int(start_ts.replace(tzinfo=timezone.utc).timestamp() * 1000)) & \
           (bars_df["t"] <= int(end_ts.replace(tzinfo=timezone.utc).timestamp() * 1000))
    slc = bars_df[mask]
    if slc.empty:
        return None, None
    return float(slc["h"].max()), float(slc["l"].min())


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None,
                   help="Process at most N trades (for testing).")
    p.add_argument("--force", action="store_true",
                   help="Recompute MFE/MAE even for trades that already have values.")
    args = p.parse_args(argv)

    bars_store.ensure_dirs()

    try:
        client = PolygonClient()
    except RuntimeError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1

    with sqlite3.connect(state.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = _closed_trades_needing_backfill(conn, args.force, args.limit)

    if not rows:
        print("no trades need backfilling — done.")
        return 0
    print(f"trades to process: {len(rows)}")

    # ──────────────────────────────────────────────────────────────
    # Pass 1: group by contract. The window for each contract is the
    # union of (earliest entry) → (latest exit OR expiry, whichever later)
    # across all trades on that contract, so one API call covers all.
    # ──────────────────────────────────────────────────────────────
    contracts: dict[str, dict] = defaultdict(lambda: {"from": None, "to": None, "trade_rows": []})
    skipped_invalid = 0
    for r in rows:
        exp = _expiry_date(r["expiry"])
        if exp is None:
            skipped_invalid += 1
            continue
        try:
            occ = build_occ_symbol(r["ticker"], exp, r["right"], float(r["strike"]))
        except (ValueError, TypeError):
            skipped_invalid += 1
            continue

        first_entry = _parse_ts(r["first_entry_ts"] or r["created_at"])
        last_exit   = _parse_ts(r["last_exit_ts"]) or first_entry
        if first_entry is None:
            skipped_invalid += 1
            continue

        from_d = first_entry.date()
        # Always reach expiry to cover the to-expiry MFE/MAE window.
        to_d = max(last_exit.date() if last_exit else from_d, exp)
        # Don't fetch into the future.
        today = date.today()
        if to_d > today:
            to_d = today

        meta = contracts[occ]
        meta["from"] = from_d if meta["from"] is None else min(meta["from"], from_d)
        meta["to"]   = to_d   if meta["to"]   is None else max(meta["to"],   to_d)
        meta["trade_rows"].append({
            "intent_id":    r["intent_id"],
            "first_entry":  first_entry,
            "last_exit":    last_exit,
            "expiry":       exp,
        })

    if skipped_invalid:
        print(f"  skipped {skipped_invalid} trades with invalid contract / timestamps")
    print(f"unique contracts to fetch: {len(contracts)}")

    # ──────────────────────────────────────────────────────────────
    # Pass 2: fetch bars per contract, skipping any whose window is
    # already cached.
    # ──────────────────────────────────────────────────────────────
    fetched, cached, errors = 0, 0, 0
    for i, (occ, meta) in enumerate(contracts.items(), 1):
        if bars_store.manifest_covers("option", occ, meta["from"], meta["to"]):
            cached += 1
            continue
        try:
            bars = client.get_aggregates(occ, 1, "minute", meta["from"], meta["to"])
        except (PolygonError, Exception) as e:  # noqa: BLE001
            errors += 1
            print(f"  [{i}/{len(contracts)}] {occ}  ERROR: {e}", flush=True)
            bars_store.append_manifest(bars_store.ManifestRow(
                kind="option", symbol=occ,
                from_date=meta["from"], to_date=meta["to"],
                n_bars=0, fetched_at=datetime.now(timezone.utc),
                status="error", note=str(e)[:200],
            ))
            continue
        bars_store.write_option_bars(occ, bars)
        bars_store.append_manifest(bars_store.ManifestRow(
            kind="option", symbol=occ,
            from_date=meta["from"], to_date=meta["to"],
            n_bars=len(bars), fetched_at=datetime.now(timezone.utc),
            status="ok" if bars else "empty", note="",
        ))
        fetched += 1
        if i % 10 == 0 or i == len(contracts):
            print(f"  [{i}/{len(contracts)}] fetched={fetched}  cached={cached}  errors={errors}",
                  flush=True)

    # ──────────────────────────────────────────────────────────────
    # Pass 3: compute MFE/MAE per trade from cached bars, write back.
    # ──────────────────────────────────────────────────────────────
    updated = 0
    no_bars = 0
    with sqlite3.connect(state.DB_PATH) as conn:
        for occ, meta in contracts.items():
            df = bars_store.load_option_bars(occ)
            if df is None or df.empty:
                no_bars += len(meta["trade_rows"])
                continue
            for tr in meta["trade_rows"]:
                in_max, in_min = _slice_max_min(df, tr["first_entry"], tr["last_exit"])
                # To-expiry window ends at expiry's market close (4pm ET ~ 20:00 UTC).
                exp_end = datetime.combine(tr["expiry"], datetime.min.time()) + timedelta(hours=20)
                exp_max, exp_min = _slice_max_min(df, tr["first_entry"], exp_end)
                conn.execute("""
                    UPDATE trade_intents
                    SET mfe_in_trade_price  = COALESCE(?, mfe_in_trade_price),
                        mae_in_trade_price  = COALESCE(?, mae_in_trade_price),
                        mfe_to_expiry_price = COALESCE(?, mfe_to_expiry_price),
                        mae_to_expiry_price = COALESCE(?, mae_to_expiry_price)
                    WHERE intent_id = ?
                """, (in_max, in_min, exp_max, exp_min, tr["intent_id"]))
                if any(v is not None for v in (in_max, in_min, exp_max, exp_min)):
                    updated += 1
        conn.commit()

    print()
    print(f"contracts:  fetched={fetched}  cached={cached}  errors={errors}")
    print(f"trades:     updated={updated}  no_bars={no_bars}")
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
