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
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from automation import state
from automation import secrets as user_secrets
from src import bars_store
from src.contract_symbols import build_occ_symbol
from src.polygon_client import PolygonClient, PolygonError

# Load API keys saved via Settings → API keys into the environment so
# PolygonClient() can pick up POLYGON_API_KEY when the script is run
# directly (the server does this on startup, but the script doesn't
# import the server).
user_secrets.load_into_env()


# Broker CSVs (TD Ameritrade orderStatus, IBKR TRANSACTIONS) emit timestamps
# in US/Eastern local time without a tz suffix. The importer stores those
# strings as-is. We must localize them to ET before comparing against the
# Polygon bar timestamps (which are UTC epoch ms).
_ET = ZoneInfo("America/New_York")


def _parse_ts(s: str) -> datetime | None:
    """Parse 'YYYY-MM-DDTHH:MM:SS' (the importer's canonical format).

    Returns a tz-aware datetime localized to US/Eastern. Naive input is
    assumed to be ET (broker local time); anything that already carries a
    tzinfo is left alone.
    """
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s[:19])
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ET)
    return dt


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


def _to_epoch_ms(dt: datetime) -> int:
    """Convert any datetime to UTC epoch milliseconds. Naive datetimes are
    interpreted as ET (the broker timezone)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ET)
    return int(dt.timestamp() * 1000)


def _slice_max_min(bars_df, start_ts: datetime, end_ts: datetime):
    """Return (max_high, min_low) within [start_ts, end_ts] inclusive.

    Both timestamps must be tz-aware (or naive ET — we coerce in
    _to_epoch_ms). The bars frame's 't' column is UTC epoch ms.
    """
    if bars_df.empty:
        return None, None
    if start_ts is None or end_ts is None:
        return None, None
    if end_ts < start_ts:
        return None, None
    start_ms = _to_epoch_ms(start_ts)
    end_ms   = _to_epoch_ms(end_ts)
    mask = (bars_df["t"] >= start_ms) & (bars_df["t"] <= end_ms)
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
        last_exit   = _parse_ts(r["last_exit_ts"])  # None if no exit fill
        if first_entry is None:
            skipped_invalid += 1
            continue

        # A trade with no exit fill that's past expiry expired worthless —
        # the "in trade" window was effectively [entry, expiry close].
        # Falling back to first_entry (zero-width window) leaves MFE/MAE
        # null for these forever; using expiry close lets us recover
        # meaningful values. For trades whose expiry is still in the
        # future we leave last_exit None — they're genuinely still open.
        if last_exit is None and exp < date.today():
            last_exit = datetime.combine(
                exp, datetime.min.time(), tzinfo=_ET,
            ) + timedelta(hours=16)

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
    #
    # `manifest_covers` only checks the requested date range — it can't
    # tell that bars fetched on entry day are now stale because exits
    # happened weeks later. So we additionally re-fetch any contract
    # whose most-recent fill is later than the manifest's `fetched_at`.
    # ──────────────────────────────────────────────────────────────
    manifest_df = bars_store.load_manifest()

    def _needs_refetch(occ: str, latest_fill: datetime) -> bool:
        """True if the contract has fill activity newer than its cache."""
        sub = manifest_df[(manifest_df["kind"] == "option")
                          & (manifest_df["symbol"] == occ)]
        if sub.empty:
            return False
        try:
            last_fetch = max(
                datetime.fromisoformat(s) for s in sub["fetched_at"].astype(str)
            )
        except ValueError:
            return False
        # tz-normalize for comparison
        if last_fetch.tzinfo is None:
            last_fetch = last_fetch.replace(tzinfo=timezone.utc)
        lf_utc = latest_fill.astimezone(timezone.utc) if latest_fill.tzinfo else \
                 latest_fill.replace(tzinfo=_ET).astimezone(timezone.utc)
        # Re-fetch if any fill is more than 1h after we last pulled bars
        return lf_utc - last_fetch > timedelta(hours=1)

    fetched, cached, refetched, errors = 0, 0, 0, 0
    for i, (occ, meta) in enumerate(contracts.items(), 1):
        latest_fill = max((tr["last_exit"] or tr["first_entry"]
                           for tr in meta["trade_rows"]),
                          default=None)
        stale = latest_fill is not None and _needs_refetch(occ, latest_fill)
        if bars_store.manifest_covers("option", occ, meta["from"], meta["to"]) \
                and not stale:
            cached += 1
            continue
        if stale:
            refetched += 1
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
    no_exit = 0       # trades with no exit fill yet — in-trade window undefined
    empty_slice = 0   # bars exist but window slice is empty
    with sqlite3.connect(state.DB_PATH) as conn:
        for occ, meta in contracts.items():
            df = bars_store.load_option_bars(occ)
            if df is None or df.empty:
                no_bars += len(meta["trade_rows"])
                continue
            for tr in meta["trade_rows"]:
                # No exit yet → can still compute to-expiry (entry → expiry close)
                # but the in-trade window is undefined. Use entry alone so the
                # mask is empty rather than spuriously large.
                if tr["last_exit"] is None:
                    in_max, in_min = None, None
                    no_exit += 1
                else:
                    in_max, in_min = _slice_max_min(df, tr["first_entry"], tr["last_exit"])

                # To-expiry window ends at expiry's market close (4pm ET).
                # ZoneInfo handles DST so we don't have to track summer/winter.
                exp_end = datetime.combine(
                    tr["expiry"], datetime.min.time(), tzinfo=_ET,
                ) + timedelta(hours=16)
                exp_max, exp_min = _slice_max_min(df, tr["first_entry"], exp_end)

                # Track empty-slice cases for diagnostics
                if (in_max is None and tr["last_exit"] is not None
                        and exp_max is None):
                    empty_slice += 1

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
    print(f"contracts:  fetched={fetched}  refetched_stale={refetched}  "
          f"cached={cached}  errors={errors}")
    print(
        f"trades:     updated={updated}  no_bars={no_bars}  "
        f"no_exit_fill={no_exit}  empty_window={empty_slice}"
    )
    if no_exit:
        print(f"  ({no_exit} trade(s) have no exit fill yet — re-import after closing)")
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
