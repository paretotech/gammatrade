"""IBKR Flex-query / Transaction History CSV reader.

IBKR statement CSVs are multi-section files where every row's first field
names the section ("Statement", "Summary", "Transaction History", …) and
the second field is "Header" or "Data". This reader walks that structure,
pulls Buy/Sell option rows out of the Transaction History section, parses
the OCC option symbol, and groups buys and sells per contract into trades.

Output shape (one dict per closed-or-partial trade):
    {
        "ticker":          "NVDA",
        "expiry":          "2026-05-01",      # ISO date
        "right":           "C" | "P",
        "strike":          215.0,
        "contracts":       2,                  # filled qty (entries)
        "entry_date":      "2026-04-24",       # earliest buy date
        "entry_price":     3.635,              # qty-weighted average
        "exit_qty":        2,                  # total sell qty
        "exit_price":      4.81,               # qty-weighted average (or None if no sells)
        "exit_date":       "2026-04-24",       # latest sell date (or None)
        "status":          "filled" | "pending",
        "buys":   [(date, qty, price), ...],
        "sells":  [(date, qty, price), ...],
    }

Time-of-day is not in this CSV; everything is dated only. The importer
stores entry timestamps as "<date>T00:00:00" — analytics views that bucket
by date still work correctly; minute-level views won't.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


# OCC option symbol layout (21 chars):
#   chars 0-5   : ticker, space-padded right
#   chars 6-11  : expiry YYMMDD
#   char 12     : C or P
#   chars 13-20 : strike * 1000, zero-padded
def parse_occ_symbol(s: str) -> tuple[str, str, str, float] | None:
    """Parse an OCC option symbol → (ticker, expiry_iso, right, strike).

    Returns None if the input doesn't look like a valid OCC symbol.
    """
    if not s:
        return None
    s = s.strip()
    # Some IBKR exports use double-space between ticker and expiry segment;
    # collapse all whitespace and re-pack.
    parts = s.split()
    if len(parts) < 2:
        return None
    ticker = parts[0]
    tail = "".join(parts[1:])
    if len(tail) != 15:  # 6 (date) + 1 (right) + 8 (strike)
        return None
    yy, mm, dd = tail[0:2], tail[2:4], tail[4:6]
    right = tail[6]
    strike_raw = tail[7:15]
    if right not in ("C", "P"):
        return None
    try:
        strike = int(strike_raw) / 1000.0
        # YYMMDD assumes 20xx
        expiry_iso = f"20{yy}-{mm}-{dd}"
    except ValueError:
        return None
    return (ticker, expiry_iso, right, strike)


@dataclass
class _Fill:
    date: str       # ISO YYYY-MM-DD
    qty: float      # positive for buy, negative for sell (as in CSV)
    price: float
    side: str       # "Buy" or "Sell"


def _read_transaction_section(path: Path) -> list[dict]:
    """Walk the multi-section CSV and return Transaction History rows as
    dicts keyed by the section's column names (Date, Symbol, Quantity, …).
    """
    txn_header: list[str] | None = None
    rows: list[dict] = []
    with path.open("r", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 3:
                continue
            section = row[0]
            kind = row[1]
            if section != "Transaction History":
                continue
            if kind == "Header":
                # Skip the leading two columns (section, kind)
                txn_header = row[2:]
                continue
            if kind == "Data" and txn_header is not None:
                data = row[2:]
                # Pad / truncate to header length so dict zip works cleanly.
                if len(data) < len(txn_header):
                    data = data + [""] * (len(txn_header) - len(data))
                rows.append(dict(zip(txn_header, data[:len(txn_header)])))
    return rows


def _to_float(v) -> float | None:
    try:
        return float(v) if v not in (None, "", "-") else None
    except (ValueError, TypeError):
        return None


def read_ibkr_csv(path: Path) -> list[dict]:
    """Parse an IBKR Flex Transaction History CSV → list of trade dicts.

    Groups fills by (ticker, expiry, right, strike) and aggregates buys
    and sells per contract. A contract whose total sell qty equals its
    total buy qty is reported with status="filled"; partial (sells < buys)
    is reported with status="pending" so the user can see them as open.
    """
    rows = _read_transaction_section(path)

    # Bucket fills per contract. Skip non-option / non-Buy-Sell rows.
    fills_by_contract: dict[tuple[str, str, str, float], list[_Fill]] = defaultdict(list)
    for r in rows:
        txn_type = (r.get("Transaction Type") or "").strip()
        if txn_type not in ("Buy", "Sell"):
            continue
        sym = (r.get("Symbol") or "").strip()
        parsed = parse_occ_symbol(sym)
        if parsed is None:
            continue
        qty = _to_float(r.get("Quantity"))
        price = _to_float(r.get("Price"))
        date = (r.get("Date") or "").strip()
        if qty is None or price is None or not date:
            continue
        fills_by_contract[parsed].append(_Fill(
            date=date, qty=qty, price=price, side=txn_type,
        ))

    trades: list[dict] = []
    for (ticker, expiry, right, strike), fills in fills_by_contract.items():
        buys = [f for f in fills if f.side == "Buy"]
        sells = [f for f in fills if f.side == "Sell"]
        buy_qty = sum(abs(f.qty) for f in buys)
        sell_qty = sum(abs(f.qty) for f in sells)
        if buy_qty == 0:
            # Sell-only / opening-short — not supported by this importer (the
            # rest of the app assumes long premium trades). Skip.
            continue
        avg_buy = sum(abs(f.qty) * f.price for f in buys) / buy_qty
        avg_sell = (sum(abs(f.qty) * f.price for f in sells) / sell_qty
                    if sell_qty > 0 else None)
        first_buy = min(f.date for f in buys)
        last_sell = max((f.date for f in sells), default=None)

        status = "filled" if sell_qty >= buy_qty else "pending"
        trades.append({
            "ticker": ticker,
            "expiry": expiry,
            "right": right,
            "strike": strike,
            "contracts": int(buy_qty),
            "entry_date": first_buy,
            "entry_price": round(avg_buy, 4),
            "exit_qty": int(min(sell_qty, buy_qty)),
            "exit_price": round(avg_sell, 4) if avg_sell is not None else None,
            "exit_date": last_sell,
            "status": status,
            "buys": [(f.date, int(abs(f.qty)), f.price) for f in buys],
            "sells": [(f.date, int(abs(f.qty)), f.price) for f in sells],
        })

    # Stable order: by entry date asc then ticker
    trades.sort(key=lambda t: (t["entry_date"], t["ticker"], t["expiry"], t["strike"]))
    return trades


def read_ibkr_directory(d: Path) -> list[dict]:
    """Read every .csv in a directory and merge results."""
    out: list[dict] = []
    if not d.exists():
        return out
    for path in sorted(d.glob("*.csv")):
        out.extend(read_ibkr_csv(path))
    return out
