"""Rules engine — loads YAML config, exposes lookup helpers.

The engine is fixed; the YAML is the rule surface. Reload via SIGHUP.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Any, Optional
import yaml


CONFIG_PATH = Path(__file__).parent / "config" / "rules.yaml"


# ─── Defaults for the entry form ───────────────────────────────────────────

def default_friday_expiry(today: Optional[date] = None) -> str:
    """Nearest Friday strictly in the future.

    Mon–Thu → this Friday. Fri–Sun → next Friday.

    Note: defaults to the next future Friday rather than today (even on a
    Friday) because 0-DTE setups are expert-mode per
    `data-derived`. If you want 0DTE, override manually.
    """
    from datetime import timedelta
    today = today or date.today()
    weekday = today.weekday()  # Mon=0 … Sun=6
    if weekday < 4:
        days = 4 - weekday
    else:
        days = 4 + (7 - weekday)
    return (today + timedelta(days=days)).isoformat()


def default_strike_otm1(ticker: str, level: float, direction: str = "above") -> int:
    """1-OTM strike from the trigger level.

      - calls (direction is above/hold_above/bounce_above): smallest strike
        STRICTLY above level
      - puts  (direction is below/hold_below/bounce_below): largest strike
        STRICTLY below level

    Strike interval: SPX uses $5; everything else $1. Override per-ticker
    in rules.yaml `strike_intervals` if needed (see Rules.strike_interval).
    """
    import math
    interval = 5 if ticker.upper() == "SPX" else 1
    is_below = direction in ("below", "hold_below", "bounce_below")
    if is_below:
        n = math.ceil(level / interval)
        return int((n - 1) * interval)
    else:
        n = math.floor(level / interval)
        return int((n + 1) * interval)


@dataclass
class Rules:
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Rules":
        with open(path) as f:
            return cls(raw=yaml.safe_load(f))

    def save(self, path: Path = CONFIG_PATH) -> None:
        with open(path, "w") as f:
            yaml.safe_dump(self.raw, f, sort_keys=False, default_flow_style=False)

    def dte_bucket(self, dte: int) -> str:
        if dte <= 1:
            return "0-1"
        if dte <= 4:
            return "2-4"
        if dte <= 9:
            return "5-9"
        return "10-21"

    def tp_ladder(self, dte: int, regime: str) -> dict[str, Any]:
        """Default DTE+regime ladder (the 'auto' choice)."""
        bucket = self.dte_bucket(dte)
        ladder = self.raw["tp_ladder_by_dte"][bucket]
        mult = self.raw["regime_multipliers"].get(regime, {"tp_mult": 1.0, "stop_mult": 1.0})
        return {
            "bucket": bucket,
            "regime": regime,
            "tp1_pct": ladder["tp1_pct"] * mult["tp_mult"],
            "tp2_pct": ladder["tp2_pct"] * mult["tp_mult"],
            "tp3_pct": ladder["tp3_pct"] * mult["tp_mult"],
            "splits": list(ladder["splits"]),
            "stop_mult": mult["stop_mult"],
            "label": f"Auto · {bucket} DTE × {regime}",
        }

    @staticmethod
    def compute_splits(choice: str) -> list[float]:
        """Convert split choice to [s1, s2, s3] floats summing to 1.0.
        Choices: 100 | 50_50 | 50_25_25 | 33_33_34"""
        if choice == "100":
            return [1.0, 0.0, 0.0]
        if choice == "50_50":
            return [0.50, 0.50, 0.0]
        if choice == "33_33_34":
            return [0.333, 0.333, 0.334]
        return [0.50, 0.25, 0.25]  # default

    @staticmethod
    def split_qty(contracts: int, splits: list[float]) -> list[int]:
        """Allocate `contracts` across tiers using largest-remainder
        (Hamilton) method. Sum of result == contracts exactly. Tiers with
        split == 0 always get 0. Avoids the max(1, round(...)) over-counting
        that turns 1c / 50-25-25 into 1+1+1 instead of 1+0+0."""
        if contracts <= 0:
            return [0] * len(splits)
        active = [(i, s) for i, s in enumerate(splits) if s and s > 0]
        if not active:
            return [0] * len(splits)
        result = [0] * len(splits)
        floors: list[tuple[int, float, int]] = []  # (idx, fractional_remainder, floor)
        running_floor = 0
        for i, s in active:
            exact = contracts * s
            f = int(exact)
            result[i] = f
            running_floor += f
            floors.append((i, exact - f, f))
        leftover = contracts - running_floor
        # Small-size carve-out: when no tier reaches a whole contract, route
        # leftovers to the earliest non-zero tiers (TP1 first). Avoids the
        # 1c / 33-33-34 → [0,0,1] surprise where the deepest-TP wins on a
        # 0.001 spec difference.
        if running_floor == 0:
            order = [i for i, _ in active]
        else:
            # Standard Hamilton: largest fractional remainder wins, with the
            # earlier tier breaking ties (still biases toward TP1 within rounding noise).
            floors.sort(key=lambda t: (-t[1], t[0]))
            order = [t[0] for t in floors]
        for k in range(leftover):
            result[order[k % len(order)]] += 1
        return result

    def compute_ladder(self, choice: str, dte: int, regime: str,
                       custom_pcts: Optional[list[float]] = None,
                       split_choice: Optional[str] = None) -> dict[str, Any]:
        """Compute TP ladder given user choice. Choices:
          - 'auto'        : DTE+regime defaults
          - 'single_tp1'  : only TP1, 100% size
          - 'single_tp2'  : only TP2 target, 100% size
          - 'single_tp3'  : only TP3 target, 100% size
          - 'fixed_45_60_90' : +45/+60/+90 splits 50/25/25
          - 'custom'      : provided custom_pcts [tp1, tp2, tp3]
        """
        base = self.tp_ladder(dte, regime)
        # Override splits if user provided a split choice
        if split_choice:
            base = {**base, "splits": self.compute_splits(split_choice)}

        if choice == "auto":
            return base

        # Intraday — Mode 1 per strategy_rules.md line 265-267 (0-4 DTE).
        # Uses the 2-4 DTE bucket as the canonical intraday target since
        # 0-1 DTE values are extreme-variance (per data-derived).
        if choice == "intraday":
            ladder = self.raw["tp_ladder_by_dte"]["2-4"]
            mult = self.raw["regime_multipliers"].get(regime, {"tp_mult": 1.0, "stop_mult": 1.0})
            return {**base,
                    "tp1_pct": ladder["tp1_pct"] * mult["tp_mult"],
                    "tp2_pct": ladder["tp2_pct"] * mult["tp_mult"],
                    "tp3_pct": ladder["tp3_pct"] * mult["tp_mult"],
                    "label": f"Intraday · 2-4 DTE × {regime}"}

        # Swing — Mode 2 per strategy_rules.md (5-21 DTE).
        # Uses the 5-9 DTE bucket as the canonical swing target.
        if choice == "swing":
            ladder = self.raw["tp_ladder_by_dte"]["5-9"]
            mult = self.raw["regime_multipliers"].get(regime, {"tp_mult": 1.0, "stop_mult": 1.0})
            return {**base,
                    "tp1_pct": ladder["tp1_pct"] * mult["tp_mult"],
                    "tp2_pct": ladder["tp2_pct"] * mult["tp_mult"],
                    "tp3_pct": ladder["tp3_pct"] * mult["tp_mult"],
                    "label": f"Swing · 5-9 DTE × {regime}"}

        if choice == "single_tp1":
            return {**base, "tp1_pct": base["tp1_pct"], "tp2_pct": None,
                    "tp3_pct": None, "splits": [1.0, 0.0, 0.0],
                    "label": f"Single target · TP1 (+{base['tp1_pct']*100:.0f}%)"}

        if choice == "single_tp2":
            return {**base, "tp1_pct": base["tp2_pct"], "tp2_pct": None,
                    "tp3_pct": None, "splits": [1.0, 0.0, 0.0],
                    "label": f"Single target · TP2 (+{base['tp2_pct']*100:.0f}%)"}

        if choice == "single_tp3":
            return {**base, "tp1_pct": base["tp3_pct"], "tp2_pct": None,
                    "tp3_pct": None, "splits": [1.0, 0.0, 0.0],
                    "label": f"Single target · TP3 (+{base['tp3_pct']*100:.0f}%)"}

        if choice == "fixed_45_60_90":
            return {**base, "tp1_pct": 0.45, "tp2_pct": 0.60, "tp3_pct": 0.90,
                    "splits": [0.50, 0.25, 0.25],
                    "label": "Fixed +45/+60/+90 · +45 / +60 / +90"}

        if choice == "custom" and custom_pcts and len(custom_pcts) >= 3:
            return {**base, "tp1_pct": custom_pcts[0], "tp2_pct": custom_pcts[1],
                    "tp3_pct": custom_pcts[2], "splits": [0.50, 0.25, 0.25],
                    "label": f"Custom · +{custom_pcts[0]*100:.0f} / +{custom_pcts[1]*100:.0f} / +{custom_pcts[2]*100:.0f}"}

        # Fallback to auto
        return base

    def compute_stop_level(self, discipline: str, entry_price: float,
                            mfe_price: Optional[float] = None,
                            current_stop: Optional[float] = None,
                            trigger_pct: Optional[float] = None,
                            trail_pct: Optional[float] = None) -> Optional[float]:
        """Return the target stop level given discipline, entry, MFE, current.

        Disciplines:
          - 'be_stop'    : always entry_price (Tier 1 default)
          - 'half_mfe'   : once MFE >= entry × 1.5, trail at 50% of high;
                           one-way ratchet up; never below entry
          - 'custom_trail': trigger at entry × (1 + trigger_pct), trail at
                           trail_pct × MFE; one-way ratchet; never below entry
          - 'none'       : no stop (returns None — engine skips check)

        The ratchet is enforced by callers via current_stop comparison.
        """
        if discipline == "none":
            return None

        if discipline == "be_stop":
            return entry_price

        mfe = mfe_price if mfe_price is not None else entry_price

        if discipline == "half_mfe":
            if mfe < entry_price * 1.5:
                return entry_price  # not yet activated
            return max(entry_price, mfe * 0.50)

        if discipline == "custom_trail":
            tg = trigger_pct if trigger_pct is not None else 0.50
            tr = trail_pct if trail_pct is not None else 0.50
            if mfe < entry_price * (1 + tg):
                return entry_price
            return max(entry_price, mfe * tr)

        # Unknown → BE
        return entry_price

    def roll_next_strike(self, ticker: str, current_strike: float, right: str) -> int:
        """Compute the next strike for a roll: above for calls, below for puts.

        Uses `roll_strike_rules.call_offset_strikes` / `put_offset_strikes`
        from rules.yaml (default 1). Strike interval is $5 for SPX, $1 for
        everything else (matches default_strike_otm1 logic).

        Examples (default offset = 1):
          MU 600C  → 601C
          NVDA 215C → 216C
          SPX 7340C → 7345C  (×$5 interval)
          AAPL 290P → 289P
        """
        cfg = self.raw.get("roll_strike_rules") or {}
        call_offset = int(cfg.get("call_offset_strikes", 1))
        put_offset = int(cfg.get("put_offset_strikes", 1))
        interval = 5 if ticker.upper() == "SPX" else 1
        if right.upper() == "P":
            return int(current_strike - interval * put_offset)
        return int(current_strike + interval * call_offset)

    def compute_roll_pct(self, plan: str, custom: Optional[float] = None) -> Optional[float]:
        """Roll plan choices:
          - 'none'         : no chain roll planned
          - 'default'      : 50%
          - 'aggressive'   : 70% (early/morning)
          - 'conservative' : 35% (late/extended)
          - 'custom'       : user-supplied %
        Returns the roll fraction (0-1), or None for 'none'.
        """
        cfg = self.raw.get("chain_roll", {})
        if plan == "none":
            return None
        if plan == "default":
            return float(cfg.get("default_roll_pct", 0.50))
        if plan == "aggressive":
            return float(cfg.get("early_morning_roll_pct", 0.70))
        if plan == "conservative":
            return float(cfg.get("late_day_roll_pct", 0.35))
        if plan == "custom" and custom is not None:
            return max(0.0, min(1.0, float(custom)))
        return float(cfg.get("default_roll_pct", 0.50))

    def is_familiar(self, ticker: str) -> bool:
        return ticker.upper() in {t.upper() for t in self.raw["familiar_tickers"]}

    def sector_for(self, ticker: str) -> str:
        """Auto-derive sector from ticker via ticker_sectors map.
        Falls back to 'unknown' if no match."""
        ticker = ticker.upper()
        sectors = self.raw.get("ticker_sectors") or {}
        for sector, tickers in sectors.items():
            if ticker in {t.upper() for t in tickers}:
                return sector
        return "unknown"

    def index_strike_ok(self, ticker: str, otm_strikes: int) -> bool:
        rule = self.raw["index_strike_rule"]
        if ticker.upper() not in {t.upper() for t in rule["tickers"]}:
            return True
        return otm_strikes <= rule["max_otm_strikes"]

    def daily_loss_cap(self) -> float:
        return float(self.raw["daily_caps"]["loss_cap_dollars"])

    def daily_count_cap(self, regime: str) -> int:
        caps = self.raw["daily_caps"]["count_based"]
        return int(caps.get("hot" if regime == "HOT" else "default", 1))

    def sector_warn_at(self) -> int:
        return int(self.raw["sector_caps"]["warn_at"])

    def sector_reject_at(self) -> int:
        return int(self.raw["sector_caps"]["reject_at"])
