"""Read TD Waterhouse daily order-status CSV exports.

Each daily TD export has a 3-line header (As of Date / Account / blank)
then a header row with order columns. This module parses one or many
such files into a unified fills DataFrame and reconstructs trade records
in the user's master-log format.

Filename convention: ``TD_<account>-orderStatus-<DD-Mon-YYYY>.csv``.
Drop new exports into ``data/td_exports/`` and the nightly pipeline
will pick them up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd


# Symbol forms seen in TD exports:
#   "AMZN C 01MAY26 260.00 (100) US"    — normal trade row
#   "QQQ C 10APR26 615.00 US"            — system expiry row (no qty wrapper)
# Either way we want the bare option description.
_SYMBOL_TRAIL_RE = re.compile(r"\s*(?:\(\d+\))?\s*(?:US|CA)\s*$", re.IGNORECASE)


def normalize_symbol(s: str) -> str:
    """Strip the trailing ``(100) US`` from TD symbols to match master-log format."""
    if not isinstance(s, str):
        return ""
    return _SYMBOL_TRAIL_RE.sub("", s).strip()


_IBKR_OCC_RE = re.compile(
    r"^([A-Z]+)\s+(\d{2})(\d{2})(\d{2})([CP])(\d{8})$"
)
# Format: NVDA  260429C00210000 → ticker NVDA, exp 2026-04-29, C, strike 210.000


def _ibkr_symbol_to_canonical(ibkr_sym: str) -> str:
    """Convert IBKR OCC-style symbol → TD canonical 'NVDA C 29APR26 210.00'.
    Returns '' on parse failure."""
    if not isinstance(ibkr_sym, str):
        return ""
    # Normalize internal whitespace to a single space; ticker / OCC core stay separated.
    s = re.sub(r"\s+", " ", ibkr_sym.strip())
    m = _IBKR_OCC_RE.match(s)
    if not m:
        return ""
    ticker, yy, mm, dd, right, strike_raw = m.groups()
    months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    try:
        expiry = f"{int(dd):02d}{months[int(mm) - 1]}{yy}"
    except (ValueError, IndexError):
        return ""
    strike = int(strike_raw) / 1000.0
    return f"{ticker} {right} {expiry} {strike:.2f}"


def read_ibkr_export(path: Path) -> pd.DataFrame:
    """Parse one IBKR 'Transactions' CSV export.

    IBKR uses a multi-section CSV. We only want the Transaction History
    section's Data rows. Convert each fill to the same canonical shape
    the TD reader emits so the rest of the pipeline is format-agnostic.

    IBKR conventions:
      - Quantity sign indicates direction (positive=Buy, negative=Sell)
      - No explicit Buy-to-Open / Sell-to-Close distinction; we map
        Buy → 'Buy to Open', Sell → 'Sell to Close'
      - Symbol field is OCC-style (NVDA  260429C00210000)
      - Adjustments / FX / cash rows are dropped
    """
    rows = []
    with open(path, "r", newline="") as f:
        for line in f:
            parts = [p.strip().strip('"') for p in line.rstrip("\n").split(",")]
            if len(parts) < 3:
                continue
            section, row_type = parts[0], parts[1]
            if section != "Transaction History" or row_type != "Data":
                continue
            # Header row of section appears as: Transaction History,Header,Date,...
            # Already filtered out by row_type=="Data"
            rows.append(parts)
    if not rows:
        return pd.DataFrame(columns=[
            "account", "action", "symbol_norm", "order_dt",
            "fill_qty", "fill_price", "raw_status", "orig_qty", "is_expiry",
        ])

    # Column positions per the header line:
    # 0:Section 1:Type 2:Date 3:Account 4:Description 5:TransactionType 6:Symbol
    # 7:Quantity 8:Price 9:Currency 10:Gross 11:Commission 12:Net
    out = []
    for r in rows:
        if len(r) < 13:
            continue
        tx_type = r[5]
        if tx_type not in ("Buy", "Sell"):
            continue  # skip Adjustment / FX / interest
        sym_canonical = _ibkr_symbol_to_canonical(r[6])
        if not sym_canonical:
            continue
        try:
            qty_raw = float(r[7])
            price = float(r[8])
        except ValueError:
            continue
        try:
            order_dt = pd.to_datetime(r[2])
        except (ValueError, TypeError):
            continue
        is_sell = qty_raw < 0
        action = "Sell to Close" if is_sell else "Buy to Open"
        out.append({
            "account": r[3],
            "action": action,
            "symbol_norm": sym_canonical,
            "order_dt": order_dt,
            "fill_qty": abs(qty_raw),
            "fill_price": price,
            "raw_status": "Filled",
            "orig_qty": abs(qty_raw),
            "is_expiry": False,
        })
    return pd.DataFrame(out)


def _is_ibkr_format(path: Path) -> bool:
    """Sniff first line — IBKR CSVs start with 'Statement,Header,'."""
    try:
        with open(path, "r") as f:
            first = f.readline()
        return first.startswith("Statement,Header,")
    except OSError:
        return False


def read_td_export(path: Path) -> pd.DataFrame:
    """Parse one TD daily / historical export CSV.

    Keeps "Filled" rows including the system-generated expiry rows that
    appear with action="Sell", fill_qty=0, price=0 — those mark a
    position as expired and the appender needs to see them so it can
    update the corresponding OPEN row in the master log.

    Cancelled / Expired (limit-order timeout) / Rejected rows are dropped.

    Columns: account, action, symbol_norm, order_dt, fill_qty,
    fill_price, raw_status, is_expiry.
    """
    raw = pd.read_csv(path, skiprows=3)
    raw["symbol_norm"] = raw["Symbol"].astype(str).apply(normalize_symbol)
    raw["order_dt"] = pd.to_datetime(raw["Order Date"], errors="coerce")
    fills = raw[raw["Fill Status"].str.strip().str.lower().eq("filled")].copy()
    fills = fills.rename(columns={
        "Account": "account",
        "Action": "action",
        "Fill Quantity": "fill_qty",
        "Avg Fill Price": "fill_price",
        "Fill Status": "raw_status",
        "Original Quantity": "orig_qty",
    })
    # System expiry rows: action="Sell" (note: not "Sell to Close"), filled but
    # with 0 quantity and 0 price. Those represent position expiry.
    fills["is_expiry"] = (
        fills["action"].astype(str).str.strip().str.lower().eq("sell")
        & (fills["fill_qty"].fillna(0) == 0)
        & (fills["orig_qty"].fillna(0) > 0)
    )
    return fills[[
        "account", "action", "symbol_norm", "order_dt",
        "fill_qty", "fill_price", "raw_status", "orig_qty", "is_expiry",
    ]].reset_index(drop=True)


def read_td_directory(d: Path) -> pd.DataFrame:
    """Concatenate every broker export CSV in a directory into one fills DataFrame.

    Supports both TD (daily ``TD_*.csv`` / historical ``*orderStatus*.csv``)
    and IBKR (``*TRANSACTIONS*.csv`` from IB Flex). The reader auto-detects
    format per file by sniffing the first line.
    """
    files = sorted(set(
        list(d.glob("TD_*.csv"))
        + list(d.glob("*orderStatus*.csv"))
        + list(d.glob("*TRANSACTIONS*.csv"))
        + list(d.glob("U*.csv"))  # IBKR account-prefixed exports
    ))
    if not files:
        return pd.DataFrame(columns=[
            "account", "action", "symbol_norm", "order_dt",
            "fill_qty", "fill_price", "raw_status",
        ])
    parts = []
    for p in files:
        try:
            if _is_ibkr_format(p):
                parts.append(read_ibkr_export(p))
            else:
                parts.append(read_td_export(p))
        except Exception:
            # Skip a malformed file rather than abort the whole import
            continue
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    df = df.drop_duplicates(subset=["account", "action", "symbol_norm", "order_dt", "fill_qty", "fill_price"])
    df = df.sort_values("order_dt").reset_index(drop=True)
    return df


@dataclass
class TradeFromFills:
    symbol: str
    entry_dt: datetime          # local ET (assume tz-naive matches master log)
    entry_price: float
    contracts: int
    stcs: list[tuple[datetime, float, int]]  # (ts, price, qty)

    @property
    def status(self) -> str:
        closed = sum(q for _, _, q in self.stcs)
        if closed == 0:
            return "no_exit_live"
        if closed >= self.contracts:
            return "fully_closed"
        return "partial_closed_live"


def _parse_expiry_from_symbol(symbol: str) -> Optional[date]:
    """Extract the expiry date from a normalized option symbol like
    'HOOD C 17APR26 100.00'. Returns None on parse failure."""
    m = re.match(
        r"^[A-Z]+\s+[CP]\s+(\d{1,2})([A-Z]{3})(\d{2})\s+\d+(?:\.\d+)?$",
        symbol.strip(),
        re.IGNORECASE,
    )
    if not m:
        return None
    months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    month = months.get(m.group(2).upper())
    if month is None:
        return None
    return date(2000 + int(m.group(3)), month, int(m.group(1)))


def fills_to_trades(fills: pd.DataFrame, today: Optional[date] = None) -> list[TradeFromFills]:
    """Group fills by (account, symbol_norm) and FIFO-match BTOs with STCs.

    Each BTO becomes one trade. STCs are consumed in order of order_dt
    until the BTO's contracts are fully closed, then move to the next BTO.

    System expiry rows (is_expiry=True) close any remaining open contracts
    on matching BTOs at $0 (worthless expiry — the common case). For the
    rare ITM expiry where the broker auto-exercised, the master log can
    be corrected by hand; the system rarely captures the exercise as a
    fill on its own.
    """
    trades: list[TradeFromFills] = []
    if fills.empty:
        return trades
    if today is None:
        today = date.today()
    for (_acct, sym), grp in fills.groupby(["account", "symbol_norm"]):
        grp = grp.sort_values("order_dt").reset_index(drop=True)
        btos: list[TradeFromFills] = []
        for _, row in grp.iterrows():
            ts = row["order_dt"]
            action = str(row["action"]).strip().lower()
            is_expiry = bool(row.get("is_expiry"))
            if pd.isna(ts):
                continue
            if is_expiry:
                # Close all remaining open contracts on every BTO of this symbol.
                for trade in btos:
                    closed = sum(q for _, _, q in trade.stcs)
                    open_qty = trade.contracts - closed
                    if open_qty <= 0:
                        continue
                    trade.stcs.append((
                        ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                        0.0,
                        open_qty,
                    ))
                continue
            qty = int(round(row["fill_qty"]))
            price = float(row["fill_price"])
            if qty <= 0:
                continue
            if action == "buy to open":
                btos.append(TradeFromFills(
                    symbol=sym,
                    entry_dt=ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                    entry_price=price,
                    contracts=qty,
                    stcs=[],
                ))
            elif action == "sell to close":
                remaining = qty
                for trade in btos:
                    closed = sum(q for _, _, q in trade.stcs)
                    open_qty = trade.contracts - closed
                    if open_qty <= 0 or remaining <= 0:
                        continue
                    take = min(open_qty, remaining)
                    trade.stcs.append((
                        ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                        price,
                        take,
                    ))
                    remaining -= take
        # After processing all fills for this symbol: infer expiry-worthless
        # for any BTO that's still open if the option's expiry has passed.
        # Catches 0DTE trades that expired without leaving a TD ticket.
        expiry = _parse_expiry_from_symbol(sym)
        if expiry is not None and expiry < today:
            for trade in btos:
                closed = sum(q for _, _, q in trade.stcs)
                open_qty = trade.contracts - closed
                if open_qty <= 0:
                    continue
                # Synthesize close at 4 PM ET on expiry date, price 0.
                expiry_ts = datetime.combine(expiry, datetime.min.time().replace(hour=16))
                trade.stcs.append((expiry_ts, 0.0, open_qty))
        trades.extend(btos)
    return trades


def trade_to_master_row(t: TradeFromFills) -> dict:
    """Build a row matching the user's master-log column shape."""
    d = t.entry_dt.date()
    row: dict = {
        "Date": f"{d.month}/{d.day}/{d.year % 100}",
        "Ticker/option strike": t.symbol,
        "Time of alert (EST)": t.entry_dt.strftime("%H:%M:%S"),
        "Chart setup/reasoning": "",
        "Entry": t.entry_price,
        "Contracts": t.contracts,
    }
    # TP1-4 from STCs in order
    for i in range(1, 5):
        if i - 1 < len(t.stcs):
            stc_ts, stc_price, stc_qty = t.stcs[i - 1]
            pct = round(100 * stc_qty / t.contracts)
            mins = (stc_ts - t.entry_dt).total_seconds() / 60
            if mins < 60:
                tit = f"{int(round(mins))}m"
            else:
                h = int(mins // 60); m = int(mins % 60)
                tit = f"{h}h{m}m" if m else f"{h}h"
            roi = (stc_price - t.entry_price) / t.entry_price
            row[f"TP{i} Exit"] = stc_price
            row[f"TP{i} %" if i != 2 else "TP2%"] = f"{pct}%"
            row[f"TP{i} Alert"] = ""
            row[f"TP{i} Time In Trade"] = tit
            row[f"TP{i} ROI"] = f"{roi*100:.2f}%"
            row[f"TP{i} Exit Reason"] = ""
        else:
            row[f"TP{i} Exit"] = ""
            row[f"TP{i} %" if i != 2 else "TP2%"] = ""
            row[f"TP{i} Alert"] = ""
            row[f"TP{i} Time In Trade"] = ""
            row[f"TP{i} ROI"] = ""
            row[f"TP{i} Exit Reason"] = ""

    # Weighted ROI / status
    if not t.stcs:
        row["Weighted ROI"] = "OPEN"
    else:
        weighted_pct = 0.0
        for stc_ts, stc_price, stc_qty in t.stcs:
            roi = (stc_price - t.entry_price) / t.entry_price
            weighted_pct += roi * (stc_qty / t.contracts)
        # If not fully closed, still tag as OPEN
        closed = sum(q for _, _, q in t.stcs)
        if closed < t.contracts:
            row["Weighted ROI"] = "OPEN"
        else:
            row["Weighted ROI"] = f"{weighted_pct*100:.2f}%"
    row["Lesson"] = ""
    row["Emotions During Trade"] = ""
    row["MFE"] = ""
    row["MAE"] = ""
    row["Max Price Within 30 minutes of selling"] = ""
    return row


_PRESERVE_ON_UPDATE = {
    "Chart setup/reasoning",
    "Lesson",
    "Emotions During Trade",
    "TP1 Exit Reason", "TP2 Exit Reason", "TP3 Exit Reason", "TP4 Exit Reason",
    "TP1 Alert", "TP2 Alert", "TP3 Alert", "TP4 Alert",
    "MFE", "MAE", "Max Price Within 30 minutes of selling",
}


def _is_open_row(row: pd.Series) -> bool:
    weighted = str(row.get("Weighted ROI", "")).strip().upper()
    return weighted in ("OPEN", "")


def merge_td_into_master(
    master_df: pd.DataFrame,
    td_fills: pd.DataFrame,
    minute_tolerance: int = 5,
) -> tuple[pd.DataFrame, list[dict], list[dict]]:
    """Append new TD trades and update OPEN rows with closing fills.

    Three outcomes per TD-derived trade:
      1. No match in master  → APPEND new row
      2. Matches an OPEN row → UPDATE that row's TP fields + Weighted ROI,
         preserving manual text columns (setup notes, lesson, emotions, etc.)
      3. Matches a CLOSED row → SKIP (master is authoritative once closed)

    Match key: (Ticker/option strike, Date, entry minute ±N).
    Returns (merged_df, newly_appended_rows, updated_rows).
    """
    trades = fills_to_trades(td_fills)
    if not trades:
        return master_df.copy(), [], []

    df = master_df.copy()

    # Build symbol+date → list of (master_idx, entry_minute) for matching
    sym_date_index: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for idx, row in df.iterrows():
        sym = str(row.get("Ticker/option strike", "")).strip()
        d = str(row.get("Date", "")).strip()
        t = str(row.get("Time of alert (EST)", "")).strip()
        if not (sym and d):
            continue
        try:
            hh, mm = (t.split(":") + ["0"])[:2]
            minute_of_day = int(hh) * 60 + int(mm)
        except (ValueError, AttributeError):
            minute_of_day = -1
        sym_date_index.setdefault((sym, d), []).append((idx, minute_of_day))

    new_rows: list[dict] = []
    updated_rows: list[dict] = []

    for trade in trades:
        candidate = trade_to_master_row(trade)
        sym = candidate["Ticker/option strike"]
        d = candidate["Date"]
        t = candidate["Time of alert (EST)"]
        try:
            hh, mm = (t.split(":") + ["0"])[:2]
            cand_minute = int(hh) * 60 + int(mm)
        except ValueError:
            cand_minute = -1

        candidates_in_master = sym_date_index.get((sym, d), [])
        matched_idx = None
        for midx, mmin in candidates_in_master:
            if abs(mmin - cand_minute) <= minute_tolerance:
                matched_idx = midx
                break

        if matched_idx is None:
            new_rows.append(candidate)
            continue

        existing = df.iloc[matched_idx]
        if not _is_open_row(existing):
            # Already closed in master; master is source of truth
            continue

        # UPDATE: take new fields from candidate, preserve manual text.
        # The master CSV is loaded as all-string by callers, so coerce values.
        def _as_str(v):
            if v is None:
                return ""
            if isinstance(v, float) and pd.isna(v):
                return ""
            return str(v)

        for col, val in candidate.items():
            if col not in df.columns:
                continue
            if col in _PRESERVE_ON_UPDATE:
                existing_val = existing.get(col, "")
                if pd.isna(existing_val) or str(existing_val).strip() == "":
                    df.at[matched_idx, col] = _as_str(val)
                # else: leave existing
            else:
                df.at[matched_idx, col] = _as_str(val)
        updated_rows.append({
            "Date": d,
            "Ticker/option strike": sym,
            "new Weighted ROI": candidate.get("Weighted ROI"),
        })

    if new_rows:
        appended = pd.DataFrame(new_rows)
        if len(df.columns) == 0:
            # First-ever append: master is empty and has no schema yet.
            # Adopt the new rows' columns directly instead of reindexing onto
            # an empty column set (which would produce a 0-column frame).
            df = appended.copy()
        else:
            for col in df.columns:
                if col not in appended.columns:
                    appended[col] = ""
            appended = appended[df.columns]
            df = pd.concat([df, appended], ignore_index=True)

    return df, new_rows, updated_rows
