"""On-disk store for raw minute bars.

Layout under ``data/bars/``::

    options/{occ_symbol}.parquet     # one file per option contract
    underlying/{ticker}.parquet      # one file per underlying ticker
    _manifest.csv                    # what's been pulled, with date ranges

Schema preserved as-returned by Polygon: ``t`` (ms epoch UTC, int64),
``o``, ``h``, ``l``, ``c`` (float64), ``v`` (float64), ``vw`` (float64,
may be NaN), ``n`` (int64). No transformation on write — readers
derive datetime from ``t`` if needed.

The manifest is the source of truth for "is this symbol+range
already saved?" Resumable pulls check the manifest first, never
the filesystem directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parent.parent
BARS_ROOT = REPO_ROOT / "data" / "bars"
MANIFEST_PATH = BARS_ROOT / "_manifest.csv"

OPTIONS_DIR = BARS_ROOT / "options"
UNDERLYING_DIR = BARS_ROOT / "underlying"

_MANIFEST_COLUMNS = [
    "kind",          # "option" or "underlying"
    "symbol",        # OCC symbol or equity ticker
    "from_date",     # ISO date string
    "to_date",       # ISO date string
    "n_bars",        # int
    "fetched_at",    # ISO timestamp (UTC)
    "status",        # "ok" | "empty" | "error"
    "note",          # free-text, error message or anything useful
]


@dataclass(frozen=True)
class ManifestRow:
    kind: str
    symbol: str
    from_date: date
    to_date: date
    n_bars: int
    fetched_at: datetime
    status: str
    note: str = ""


def ensure_dirs() -> None:
    OPTIONS_DIR.mkdir(parents=True, exist_ok=True)
    UNDERLYING_DIR.mkdir(parents=True, exist_ok=True)


def load_manifest() -> pd.DataFrame:
    if not MANIFEST_PATH.exists():
        return pd.DataFrame(columns=_MANIFEST_COLUMNS)
    return pd.read_csv(MANIFEST_PATH, dtype={"symbol": str, "status": str, "note": str})


def append_manifest(row: ManifestRow) -> None:
    ensure_dirs()
    df = load_manifest()
    new = pd.DataFrame([{
        "kind": row.kind,
        "symbol": row.symbol,
        "from_date": row.from_date.isoformat(),
        "to_date": row.to_date.isoformat(),
        "n_bars": row.n_bars,
        "fetched_at": row.fetched_at.isoformat(),
        "status": row.status,
        "note": row.note,
    }])
    out = pd.concat([df, new], ignore_index=True)
    out.to_csv(MANIFEST_PATH, index=False)


def manifest_covers(kind: str, symbol: str, from_d: date, to_d: date) -> bool:
    """True if the manifest already has a row that fully covers this range."""
    df = load_manifest()
    if df.empty:
        return False
    sub = df[(df["kind"] == kind) & (df["symbol"] == symbol) & (df["status"].isin(["ok", "empty"]))]
    if sub.empty:
        return False
    for _, r in sub.iterrows():
        f = date.fromisoformat(r["from_date"])
        t = date.fromisoformat(r["to_date"])
        if f <= from_d and t >= to_d:
            return True
    return False


def _option_path(occ_symbol: str) -> Path:
    safe = occ_symbol.replace(":", "_").replace("/", "_")
    return OPTIONS_DIR / f"{safe}.parquet"


def _underlying_path(ticker: str) -> Path:
    return UNDERLYING_DIR / f"{ticker.upper()}.parquet"


def _bars_to_df(bars: list[dict]) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame(columns=["t", "o", "h", "l", "c", "v", "vw", "n"])
    df = pd.DataFrame(bars)
    for col in ("t", "o", "h", "l", "c", "v", "vw", "n"):
        if col not in df.columns:
            df[col] = pd.NA
    df = df[["t", "o", "h", "l", "c", "v", "vw", "n"]]
    df["t"] = pd.to_numeric(df["t"], errors="coerce").astype("Int64")
    for col in ("o", "h", "l", "c", "v", "vw"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["n"] = pd.to_numeric(df["n"], errors="coerce").astype("Int64")
    return df


def write_option_bars(occ_symbol: str, bars: list[dict]) -> int:
    ensure_dirs()
    df = _bars_to_df(bars)
    df.to_parquet(_option_path(occ_symbol), index=False)
    return len(df)


def write_underlying_bars(ticker: str, bars: list[dict]) -> int:
    ensure_dirs()
    df = _bars_to_df(bars)
    df.to_parquet(_underlying_path(ticker), index=False)
    return len(df)


def load_option_bars(occ_symbol: str) -> pd.DataFrame:
    p = _option_path(occ_symbol)
    if not p.exists():
        raise FileNotFoundError(f"no saved bars for {occ_symbol} at {p}")
    df = pd.read_parquet(p)
    df["ts"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    return df


def load_underlying_bars(
    ticker: str,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> pd.DataFrame:
    p = _underlying_path(ticker)
    if not p.exists():
        raise FileNotFoundError(f"no saved bars for {ticker} at {p}")
    df = pd.read_parquet(p)
    df["ts"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    if start is not None:
        df = df[df["ts"] >= start]
    if end is not None:
        df = df[df["ts"] <= end]
    return df.reset_index(drop=True)
