"""Regime classification — shared by build_regime_excel and /trade.

Backward-looking regime tags use reference's actual weekly ROI (the same logic
as data/brando_regime.xlsx). Forward-looking "current regime guess" combines
the most recent complete week's tag with this-week QQQ momentum and VIX
state, since today's regime can't yet be measured from reference's outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent


REGIME_ORDER = [
    "CATASTROPHIC",
    "COLD / DOWN",
    "chop / mixed",
    "mixed",
    "mild positive",
    "mild uptrend",
    "HOT TRENDING",
]


def classify_regime(win, median, mean, qqq):
    """Identical to scripts/build_regime_excel.classify_regime — kept here
    so /trade and the workbook never drift apart."""
    if pd.isna(qqq):
        qqq = 0
    if median <= -50 or mean <= -50:
        return "CATASTROPHIC"
    if win >= 65 and median >= 25 and qqq >= 3:
        return "HOT TRENDING"
    if win >= 60 and median >= 15:
        return "mild uptrend"
    if win >= 55 and median >= 5:
        return "mild positive"
    if win < 45 or median < -15:
        return "COLD / DOWN"
    if 45 <= win < 55 or -15 <= median < 5:
        return "chop / mixed"
    return "mixed"


def weekly_regime_history(n_weeks: int = 8) -> pd.DataFrame:
    """Last `n_weeks` of completed-week regime tags.

    Returns columns: week, n, win_pct, median_pct, mean_pct, qqq_ret, regime.
    """
    from src.bars_store import load_underlying_bars

    df = pd.read_csv(ROOT / "data" / "brando_enriched.csv")
    clean = df[df["adjusted_roi"].notna()].copy()
    clean["entry_dt"] = pd.to_datetime(clean["entry_ts"], format="ISO8601", utc=True)
    clean["week"] = (
        clean["entry_dt"]
        .dt.tz_convert("America/New_York")
        .dt.tz_localize(None)
        .dt.to_period("W-SUN")
    )

    qqq = load_underlying_bars("QQQ").sort_values("t").reset_index(drop=True)
    qqq["date_et"] = qqq["ts"].dt.tz_convert("America/New_York").dt.date
    qd = qqq.groupby("date_et").agg(c=("c", "last")).reset_index()
    qd["week"] = pd.to_datetime(qd["date_et"]).dt.to_period("W-SUN")
    qw = qd.groupby("week").agg(qo=("c", "first"), qc=("c", "last")).reset_index()
    qw["qqq_ret"] = ((qw["qc"] / qw["qo"]) - 1) * 100

    agg = (
        clean.groupby("week")
        .agg(
            n=("adjusted_roi", "count"),
            win_pct=("adjusted_roi", lambda s: round((s > 0).mean() * 100, 0)),
            median_pct=("adjusted_roi", lambda s: round(s.median() * 100, 1)),
            mean_pct=("adjusted_roi", lambda s: round(s.mean() * 100, 1)),
        )
        .reset_index()
    )
    merged = agg.merge(qw[["week", "qqq_ret"]], on="week", how="left")
    merged["qqq_ret"] = merged["qqq_ret"].round(1)
    merged["regime"] = merged.apply(
        lambda r: classify_regime(
            r["win_pct"], r["median_pct"], r["mean_pct"], r["qqq_ret"]
        ),
        axis=1,
    )
    merged = merged.sort_values("week").tail(n_weeks).reset_index(drop=True)
    merged["week"] = merged["week"].astype(str)
    return merged


def _qqq_ath_and_recent(asof: date) -> dict:
    from src.bars_store import load_underlying_bars

    q = load_underlying_bars("QQQ").sort_values("t").reset_index(drop=True)
    q["date_et"] = q["ts"].dt.tz_convert("America/New_York").dt.date
    daily = q.groupby("date_et").agg(c=("c", "last")).reset_index()
    daily = daily[daily["date_et"] <= asof]
    if daily.empty:
        return {}
    last_close = float(daily["c"].iloc[-1])
    ath = float(daily["c"].max())
    pct_from_ath = (last_close / ath - 1) * 100

    def trailing_ret(days: int) -> float | None:
        if len(daily) <= days:
            return None
        prior = float(daily["c"].iloc[-1 - days])
        return (last_close / prior - 1) * 100

    return {
        "qqq_close": round(last_close, 2),
        "qqq_ath": round(ath, 2),
        "qqq_pct_from_ath": round(pct_from_ath, 2),
        "qqq_5d_ret": (
            round(trailing_ret(5), 2) if trailing_ret(5) is not None else None
        ),
        "qqq_20d_ret": (
            round(trailing_ret(20), 2) if trailing_ret(20) is not None else None
        ),
        "qqq_last_date": daily["date_et"].iloc[-1].isoformat(),
    }


def _vixy_state(asof: date) -> dict:
    """VIXY proxy: rising VIXY = rising fear. Starter plan has no I:VIX."""
    from src.bars_store import load_underlying_bars

    try:
        v = load_underlying_bars("VIXY").sort_values("t").reset_index(drop=True)
    except Exception:
        return {"vixy_level": None, "vixy_5d_chg_pct": None}
    v["date_et"] = v["ts"].dt.tz_convert("America/New_York").dt.date
    daily = v.groupby("date_et").agg(c=("c", "last")).reset_index()
    daily = daily[daily["date_et"] <= asof]
    if daily.empty:
        return {"vixy_level": None, "vixy_5d_chg_pct": None}
    last = float(daily["c"].iloc[-1])
    five_back = float(daily["c"].iloc[-6]) if len(daily) >= 6 else None
    chg = (last / five_back - 1) * 100 if five_back else None
    return {
        "vixy_level": round(last, 2),
        "vixy_5d_chg_pct": round(chg, 2) if chg is not None else None,
    }


def _adjust_for_forward_signals(
    base_regime: str,
    qqq_5d_ret: float | None,
    qqq_pct_from_ath: float | None,
    vixy_5d_chg_pct: float | None,
) -> tuple[str, list[str]]:
    """Heuristic upgrade/downgrade of the most-recent-week tag based on
    forward-looking price/vol signals. Returns (guess, reasons)."""
    reasons: list[str] = []
    idx = REGIME_ORDER.index(base_regime) if base_regime in REGIME_ORDER else 3

    downgrade_score = 0
    upgrade_score = 0

    if qqq_5d_ret is not None:
        if qqq_5d_ret <= -2:
            downgrade_score += 2
            reasons.append(f"QQQ 5d {qqq_5d_ret:+.1f}% — meaningful weakness")
        elif qqq_5d_ret <= -0.5:
            downgrade_score += 1
            reasons.append(f"QQQ 5d {qqq_5d_ret:+.1f}% — soft")
        elif qqq_5d_ret >= 2:
            upgrade_score += 1
            reasons.append(f"QQQ 5d {qqq_5d_ret:+.1f}% — strong")

    if vixy_5d_chg_pct is not None:
        if vixy_5d_chg_pct >= 8:
            downgrade_score += 1
            reasons.append(f"VIXY +{vixy_5d_chg_pct:.0f}% over 5d — fear bid")
        elif vixy_5d_chg_pct <= -5:
            upgrade_score += 1
            reasons.append(f"VIXY {vixy_5d_chg_pct:.0f}% over 5d — vol compression")

    if qqq_pct_from_ath is not None:
        if qqq_pct_from_ath >= -0.5:
            reasons.append(
                f"QQQ {qqq_pct_from_ath:+.1f}% from ATH — at/near highs"
            )
        elif qqq_pct_from_ath <= -3:
            downgrade_score += 1
            reasons.append(
                f"QQQ {qqq_pct_from_ath:+.1f}% from ATH — meaningful pullback"
            )

    net = upgrade_score - downgrade_score
    if net >= 2:
        idx = min(idx + 1, len(REGIME_ORDER) - 1)
        reasons.append("→ tilting up one tier from last week")
    elif net <= -2:
        idx = max(idx - 1, 0)
        reasons.append("→ tilting down one tier from last week")
    else:
        reasons.append("→ holding last week's tier")

    return REGIME_ORDER[idx], reasons


def current_regime_guess(asof: date | None = None) -> dict:
    """Forward-looking guess at today's regime.

    Combines:
      - Most recent complete-week regime from reference's enriched data
      - Trailing 4-week regime sequence (cluster duration awareness)
      - QQQ % from ATH, 5d/20d returns
      - VIXY level + 5d change

    Returns a dict with `guess`, `confidence`, `rationale`, plus all inputs.
    """
    asof = asof or date.today()
    history = weekly_regime_history(n_weeks=6)
    last_row = history.iloc[-1] if len(history) else None
    last_regime = last_row["regime"] if last_row is not None else "mixed"
    trailing_4 = (
        history["regime"].tail(4).tolist() if len(history) else []
    )

    qq = _qqq_ath_and_recent(asof)
    vv = _vixy_state(asof)

    guess, reasons = _adjust_for_forward_signals(
        last_regime,
        qq.get("qqq_5d_ret"),
        qq.get("qqq_pct_from_ath"),
        vv.get("vixy_5d_chg_pct"),
    )

    cluster_warning = None
    if trailing_4 and trailing_4.count("HOT TRENDING") >= 4:
        cluster_warning = (
            "4+ consecutive HOT TRENDING weeks — historically near cluster end; "
            "next regime change is more likely than continuation"
        )
        reasons.append(cluster_warning)

    confidence = "medium"
    if last_row is not None and last_row["n"] < 5:
        confidence = "low (sparse week)"
    elif cluster_warning:
        confidence = "low (cluster fatigue risk)"
    elif trailing_4 and len(set(trailing_4)) == 1:
        confidence = "high (consistent trailing weeks)"

    return {
        "asof": asof.isoformat(),
        "last_complete_week": str(last_row["week"]) if last_row is not None else None,
        "last_week_regime": last_regime,
        "last_week_stats": (
            {
                "n": int(last_row["n"]),
                "win_pct": float(last_row["win_pct"]),
                "median_pct": float(last_row["median_pct"]),
                "qqq_ret": float(last_row["qqq_ret"]) if pd.notna(last_row["qqq_ret"]) else None,
            }
            if last_row is not None
            else None
        ),
        "trailing_4w_regimes": trailing_4,
        **qq,
        **vv,
        "guess": guess,
        "confidence": confidence,
        "rationale": reasons,
        "cluster_warning": cluster_warning,
    }


def format_regime_guess(g: dict) -> str:
    """Human-readable summary for /trade context."""
    lines = [
        f"**Regime guess (as of {g['asof']}): {g['guess']}** — confidence {g['confidence']}",
        f"- Last complete week ({g.get('last_complete_week')}): {g['last_week_regime']}",
    ]
    if g.get("last_week_stats"):
        s = g["last_week_stats"]
        lines.append(
            f"  · n={s['n']}, win {s['win_pct']:.0f}%, median {s['median_pct']:+.1f}%, QQQ {s['qqq_ret']:+.1f}%"
        )
    if g.get("trailing_4w_regimes"):
        lines.append(f"- Trailing 4 weeks: {' → '.join(g['trailing_4w_regimes'])}")
    if g.get("qqq_close") is not None:
        lines.append(
            f"- QQQ {g['qqq_close']} ({g['qqq_pct_from_ath']:+.2f}% from ATH {g['qqq_ath']}); "
            f"5d {g.get('qqq_5d_ret')}%, 20d {g.get('qqq_20d_ret')}%"
        )
    if g.get("vixy_level") is not None:
        lines.append(
            f"- VIXY {g['vixy_level']} ({g.get('vixy_5d_chg_pct')}% over 5d)"
        )
    if g["rationale"]:
        lines.append("- Rationale:")
        for r in g["rationale"]:
            lines.append(f"  · {r}")
    return "\n".join(lines)


def shane_fast_regime(asof: date | None = None) -> dict:
    """Fast regime gauge based on the user's last 5 closed entries.

    Trailing-4-week median is too slow — by the time it confirms COLD, the
    user has already over-traded the bad regime. The rolling-5-trade gauge
    fires within 2-3 trading days. Plus a same-day-loss kill switch that
    fires within minutes.

    Buckets (validated against April 2026 book):
      HOT:           r5_med >= +25% AND r5_wr >= 80%
      NORMAL:        r5_med >=   0% AND r5_wr >= 60%
      MILD-TRAP:     r5_med >=   0% AND r5_wr 40-60%   (looks fine, isn't)
      COLD:          r5_med <    0% OR  r5_wr <= 40%   (no new entries)

    Same-day kill switch: if 2+ losing entries already today, no more entries
    that session regardless of regime.

    Returns dict with regime, r5_med_pct, r5_wr_pct, last_5 (list of dicts),
    same_day_losses_today, kill_switch_active, recommended_cap.
    """
    asof = asof or date.today()
    df = pd.read_csv(
        ROOT / "data" / "shane_enriched.csv",
        usecols=["entry_ts", "ticker", "status", "adjusted_roi"],
    )
    df["entry_ts"] = pd.to_datetime(df["entry_ts"], format="ISO8601", utc=True)
    df = df.sort_values("entry_ts").reset_index(drop=True)
    df = df.assign(
        entry_date_et=df["entry_ts"].dt.tz_convert("America/New_York").dt.date,
    )
    df["ticker_day_rank"] = df.groupby(["ticker", "entry_date_et"]).cumcount() + 1

    closed_entries = df[
        (df["status"] == "fully_closed") & (df["ticker_day_rank"] == 1)
    ].copy()

    last5 = closed_entries.tail(5)
    if len(last5) < 5:
        regime = "INSUFFICIENT_DATA"
        r5_med = None
        r5_wr = None
    else:
        r5_med = float(last5["adjusted_roi"].median() * 100)
        r5_wr = float((last5["adjusted_roi"] > 0).mean() * 100)
        if r5_med >= 25 and r5_wr >= 80:
            regime = "HOT"
        elif r5_med < 0 or r5_wr <= 40:
            regime = "COLD"
        elif r5_med >= 0 and r5_wr >= 60:
            regime = "NORMAL"
        else:
            regime = "MILD-TRAP"

    today_entries = df[df["entry_date_et"] == asof]
    today_closed = today_entries[today_entries["status"] == "fully_closed"]
    same_day_losses = int((today_closed["adjusted_roi"] < 0).sum())
    kill_switch = same_day_losses >= 2

    cap_map = {
        "HOT": "lifted",
        "NORMAL": "1 entry max (profitable rolls don't count)",
        "MILD-TRAP": "1 entry max + ½ size + no 0DTE / no near-ATH",
        "COLD": "0 new entries — manage open chains only",
        "INSUFFICIENT_DATA": "1 entry max (default until 5 closed entries on file)",
    }

    return {
        "asof": asof.isoformat(),
        "regime": regime,
        "r5_median_pct": round(r5_med, 1) if r5_med is not None else None,
        "r5_win_pct": round(r5_wr, 0) if r5_wr is not None else None,
        "last_5_trades": [
            {
                "date": r["entry_date_et"].isoformat(),
                "ticker": r["ticker"],
                "roi_pct": round(float(r["adjusted_roi"] * 100), 0),
            }
            for _, r in last5.iterrows()
        ],
        "same_day_losses_today": same_day_losses,
        "kill_switch_active": kill_switch,
        "recommended_cap": cap_map[regime],
    }


def format_shane_fast_regime(g: dict) -> str:
    """Human-readable summary for /pregame and /trade headers."""
    lines = [
        f"**Fast regime (rolling-5 entries, as of {g['asof']}): {g['regime']}**",
    ]
    if g["r5_median_pct"] is not None:
        lines.append(
            f"- r5 median: {g['r5_median_pct']:+.1f}%  |  r5 win: {g['r5_win_pct']:.0f}%"
        )
        last5_str = " → ".join(
            f"{t['ticker']} {t['roi_pct']:+.0f}%" for t in g["last_5_trades"]
        )
        lines.append(f"- Last 5: {last5_str}")
    lines.append(f"- Cap: {g['recommended_cap']}")
    if g["kill_switch_active"]:
        lines.append(
            f"- ⛔ **SAME-DAY KILL SWITCH ACTIVE** "
            f"({g['same_day_losses_today']} losing entries today). "
            f"No more entries this session."
        )
    elif g["same_day_losses_today"] == 1:
        lines.append(
            "- ⚠ 1 losing entry today — one more loss triggers same-day kill switch."
        )
    return "\n".join(lines)


if __name__ == "__main__":
    g = current_regime_guess()
    print(format_regime_guess(g))
    print()
    fr = shane_fast_regime()
    print(format_shane_fast_regime(fr))
