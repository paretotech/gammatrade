"""Pure enrichment logic: given bars + timestamps, compute metrics.

No API calls live here — the Polygon client lives in polygon_client.py.
This module takes already-fetched minute bars for both the option and
its underlying, plus the trade's entry/exit/expiry timestamps, and
returns a flat dict of metrics. Keeping it pure makes it cheap to unit
test with synthetic bars.

Timestamps throughout this module are timezone-aware UTC ``datetime``.
Polygon returns bars keyed on ``t`` in ms since epoch UTC.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


@dataclass
class EnrichmentResult:
    # Core metrics — every MFE/MAE has a ROI (fractional return from entry)
    # and an absolute option price, so downstream tools can read whichever.
    entry_price: Optional[float]
    exit_price: Optional[float]

    mfe_to_exit: Optional[float]
    mfe_to_exit_price: Optional[float]
    mae_to_exit: Optional[float]
    mae_to_exit_price: Optional[float]
    mfe_to_expiry: Optional[float]
    mfe_to_expiry_price: Optional[float]
    mae_to_expiry: Optional[float]
    mae_to_expiry_price: Optional[float]
    mfe_to_peak: Optional[float]
    mfe_to_peak_price: Optional[float]
    mae_to_peak: Optional[float]
    mae_to_peak_price: Optional[float]

    time_to_mfe_min: Optional[int]
    time_to_mae_min: Optional[int]

    post_exit_30min_max_price: Optional[float]
    post_exit_30min_max_roi: Optional[float]

    entry_bar_volume: Optional[int]
    exit_bar_volume: Optional[int]

    # Underlying context
    underlying_at_entry: Optional[float]
    underlying_at_exit: Optional[float]
    underlying_at_mfe: Optional[float]
    underlying_at_mae: Optional[float]
    underlying_5min_dir_pct: Optional[float]

    path_shape: Optional[str]
    sanity_flag: str  # "" / "suspect_entry_price" / "suspect_mfe"
    enrichment_status: str  # ok / partial / no_bars
    enrichment_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def enrich_trade(
    option_bars: list[dict[str, Any]],
    underlying_bars: list[dict[str, Any]],
    entry_ts: datetime,
    entry_price: float,
    exit_ts: Optional[datetime],
    expiry_close_ts: datetime,
    realized_roi: Optional[float] = None,
) -> EnrichmentResult:
    """Compute enrichment metrics for a single trade.

    ``option_bars`` and ``underlying_bars`` are Polygon minute aggregates,
    each bar a dict with keys ``t`` (ms epoch UTC), ``o``, ``h``, ``l``,
    ``c``, ``v``. Must be sorted ascending by ``t`` — Polygon returns
    them that way when we request ``sort=asc``.

    If ``option_bars`` is empty, returns a skeletal result with
    ``enrichment_status = "no_bars"``.
    """
    if not option_bars:
        return EnrichmentResult(
            entry_price=entry_price,
            exit_price=None,
            mfe_to_exit=None, mfe_to_exit_price=None,
            mae_to_exit=None, mae_to_exit_price=None,
            mfe_to_expiry=None, mfe_to_expiry_price=None,
            mae_to_expiry=None, mae_to_expiry_price=None,
            mfe_to_peak=None, mfe_to_peak_price=None,
            mae_to_peak=None, mae_to_peak_price=None,
            sanity_flag="",
            time_to_mfe_min=None, time_to_mae_min=None,
            post_exit_30min_max_price=None, post_exit_30min_max_roi=None,
            entry_bar_volume=None, exit_bar_volume=None,
            underlying_at_entry=None, underlying_at_exit=None,
            underlying_at_mfe=None, underlying_at_mae=None,
            underlying_5min_dir_pct=None,
            path_shape=None,
            enrichment_status="no_bars",
            enrichment_note="no option bars returned by polygon",
        )

    # Window bars by timestamp for each MFE/MAE flavor
    entry_ms = _to_ms(entry_ts)
    expiry_ms = _to_ms(expiry_close_ts)
    exit_ms = _to_ms(exit_ts) if exit_ts else expiry_ms  # fallback: exit = expiry close

    bars_after_entry = [b for b in option_bars if b["t"] >= entry_ms]
    bars_entry_to_exit = [b for b in bars_after_entry if b["t"] <= exit_ms]
    bars_entry_to_expiry = [b for b in bars_after_entry if b["t"] <= expiry_ms]

    def peak_from(bars):
        if not bars:
            return None
        return max(b["h"] for b in bars)

    def trough_from(bars):
        if not bars:
            return None
        return min(b["l"] for b in bars)

    peak_exit = peak_from(bars_entry_to_exit)
    peak_expiry = peak_from(bars_entry_to_expiry)
    peak_overall = peak_from(bars_after_entry)

    trough_exit = trough_from(bars_entry_to_exit)
    trough_expiry = trough_from(bars_entry_to_expiry)
    trough_overall = trough_from(bars_after_entry)

    mfe_to_exit = _pct(peak_exit, entry_price)
    mae_to_exit = _pct(trough_exit, entry_price)
    mfe_to_expiry = _pct(peak_expiry, entry_price)
    mae_to_expiry = _pct(trough_expiry, entry_price)
    mfe_to_peak = _pct(peak_overall, entry_price)
    mae_to_peak = _pct(trough_overall, entry_price)

    # Time-to-MFE / MAE measured from entry, in whole minutes
    time_to_mfe_min = _minutes_to_extreme(bars_entry_to_expiry, entry_ms, "h", peak_expiry)
    time_to_mae_min = _minutes_to_extreme(bars_entry_to_expiry, entry_ms, "l", trough_expiry)

    # 30-min post-exit max price
    post_bars = [b for b in option_bars if exit_ms < b["t"] <= exit_ms + 30 * 60 * 1000]
    post_max_price = peak_from(post_bars)
    post_max_roi = _pct(post_max_price, entry_price)

    # Bar volumes at entry and exit minute (use the bar whose t is the last one <= target_ms)
    entry_bar = _bar_at_or_before(option_bars, entry_ms)
    exit_bar = _bar_at_or_before(option_bars, exit_ms)
    entry_vol = entry_bar["v"] if entry_bar else None
    exit_vol = exit_bar["v"] if exit_bar else None

    # Underlying context
    und_entry = _underlying_at(underlying_bars, entry_ms)
    und_exit = _underlying_at(underlying_bars, exit_ms)
    und_mfe = _underlying_at(underlying_bars, _ts_of_extreme(bars_entry_to_expiry, "h", peak_expiry)) if peak_expiry is not None else None
    und_mae = _underlying_at(underlying_bars, _ts_of_extreme(bars_entry_to_expiry, "l", trough_expiry)) if trough_expiry is not None else None

    # 5-min underlying direction: close at entry bar vs close 5 minutes before
    und_5min_dir = _five_min_direction(underlying_bars, entry_ms)

    # Exit price: the close of the exit bar
    exit_price = exit_bar["c"] if exit_bar else None

    # Sanity checks — surface rows whose source entry_price disagrees badly
    # with the first bar we pulled (typical cause: strike typo in source),
    # or whose MFE is so large it's almost certainly driven by a wrong
    # contract. Nothing here is fatal; these are review hints.
    first_bar_open = option_bars[0].get("o") if option_bars else None
    sanity_flag = ""
    if first_bar_open and entry_price > 0:
        ratio = entry_price / first_bar_open
        if ratio < 0.10 or ratio > 10:
            sanity_flag = "suspect_entry_price"
    if sanity_flag == "" and mfe_to_peak is not None and mfe_to_peak > 5.0:
        # MFE > 500% on a real trade is rare; usually it's a typo / contract
        # mismatch. Flag for review but don't overwrite a stronger flag.
        sanity_flag = "suspect_mfe"

    path_shape = _classify_path(mfe_to_peak=mfe_to_peak, realized_roi=realized_roi)

    status = "ok"
    note = ""
    if und_entry is None or und_exit is None:
        status = "partial"
        note = "underlying bars missing at entry or exit"

    return EnrichmentResult(
        entry_price=entry_price,
        exit_price=exit_price,
        mfe_to_exit=mfe_to_exit,
        mfe_to_exit_price=peak_exit,
        mae_to_exit=mae_to_exit,
        mae_to_exit_price=trough_exit,
        mfe_to_expiry=mfe_to_expiry,
        mfe_to_expiry_price=peak_expiry,
        mae_to_expiry=mae_to_expiry,
        mae_to_expiry_price=trough_expiry,
        mfe_to_peak=mfe_to_peak,
        mfe_to_peak_price=peak_overall,
        mae_to_peak=mae_to_peak,
        mae_to_peak_price=trough_overall,
        time_to_mfe_min=time_to_mfe_min,
        time_to_mae_min=time_to_mae_min,
        post_exit_30min_max_price=post_max_price,
        post_exit_30min_max_roi=post_max_roi,
        entry_bar_volume=entry_vol,
        exit_bar_volume=exit_vol,
        underlying_at_entry=und_entry,
        underlying_at_exit=und_exit,
        underlying_at_mfe=und_mfe,
        underlying_at_mae=und_mae,
        underlying_5min_dir_pct=und_5min_dir,
        path_shape=path_shape,
        sanity_flag=sanity_flag,
        enrichment_status=status,
        enrichment_note=note,
    )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _to_ms(ts: datetime) -> int:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp() * 1000)


def _pct(price: Optional[float], entry: float) -> Optional[float]:
    if price is None or entry <= 0:
        return None
    return price / entry - 1.0


def _minutes_between(start_ms: int, end_ms: int) -> int:
    return max(0, int((end_ms - start_ms) / 60000))


def _minutes_to_extreme(bars, entry_ms: int, key: str, extreme_val: Optional[float]) -> Optional[int]:
    if extreme_val is None or not bars:
        return None
    # First bar whose high/low equals the extreme
    for b in bars:
        if b[key] == extreme_val:
            return _minutes_between(entry_ms, b["t"])
    return None


def _ts_of_extreme(bars, key: str, extreme_val: Optional[float]) -> Optional[int]:
    if extreme_val is None or not bars:
        return None
    for b in bars:
        if b[key] == extreme_val:
            return b["t"]
    return None


def _bar_at_or_before(bars, target_ms: int) -> Optional[dict]:
    best = None
    for b in bars:
        if b["t"] <= target_ms:
            best = b
        else:
            break
    return best


def _underlying_at(bars, target_ms: Optional[int]) -> Optional[float]:
    if target_ms is None:
        return None
    bar = _bar_at_or_before(bars, target_ms)
    return bar["c"] if bar else None


def _five_min_direction(bars, entry_ms: int) -> Optional[float]:
    entry_bar = _bar_at_or_before(bars, entry_ms)
    prior_bar = _bar_at_or_before(bars, entry_ms - 5 * 60 * 1000)
    if not entry_bar or not prior_bar or prior_bar["c"] <= 0:
        return None
    return entry_bar["c"] / prior_bar["c"] - 1.0


def _classify_path(
    mfe_to_peak: Optional[float],
    realized_roi: Optional[float],
) -> Optional[str]:
    """Outcome-aware shape categories tuned to the reference dataset.

    Six categories built so each has a clear trader interpretation:
      * underwater     -- never showed a meaningful (>=25%) rally
      * spike_collapse -- rallied >=25% but ended in a major loss
      * round_trip     -- rallied, ended near breakeven
      * captured_modest-- realized 25-50%
      * partial_big    -- realized >=50% but kept <50% of MFE
      * captured_big   -- realized >=50% AND >=50% of MFE captured

    The 25% MFE floor reflects options' baseline noise — anything below
    that is essentially a non-event for a reference-style swing entry.
    """
    if mfe_to_peak is None:
        return None
    realized = realized_roi if realized_roi is not None else 0.0

    if mfe_to_peak < 0.25:
        return "underwater"
    if realized >= 0.50 and realized >= 0.5 * mfe_to_peak:
        return "captured_big"
    if realized >= 0.50:
        return "partial_big"
    if realized >= 0.25:
        return "captured_modest"
    if realized > -0.25:
        return "round_trip"
    return "spike_collapse"
