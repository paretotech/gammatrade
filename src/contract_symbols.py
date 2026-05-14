"""Translate human trade descriptions into Polygon OCC option symbols.

Polygon uses the OCC 21-character option symbol prefixed with ``O:``.
Layout: ``O:<ROOT><YY><MM><DD><C|P><STRIKE*1000 zero-padded to 8>``.
Example: ``O:MU260501C00500000`` = MU, 2026-05-01 expiry, Call, $500 strike.

Two input dialects are supported:

* the user / TD format — full description with explicit expiry::
      "MU 500C 1MAY26"

* reference format — underlying + strike + C/P only, expiry inferred
  from the trade date and the DTE column on the sheet::
      ("AAPL 212.5C", trade_date=2026-04-17, dte=0)  -> 2026-04-17 expiry
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional
import re


@dataclass(frozen=True)
class OptionContract:
    ticker: str
    expiry: date
    option_type: str  # "C" or "P"
    strike: float

    def to_occ(self) -> str:
        return build_occ_symbol(self.ticker, self.expiry, self.option_type, self.strike)


def build_occ_symbol(ticker: str, expiry: date, option_type: str, strike: float) -> str:
    """Assemble a Polygon OCC symbol.

    ``strike`` is the dollar strike (e.g. 212.5). It is multiplied by 1000
    and zero-padded to 8 digits, per the OCC convention.

    Index quirk: SPX weekly expirations (any date that is not the 3rd Friday
    of the month) list under the ``SPXW`` root on Polygon. The 3rd-Friday
    AM-settled monthlies remain under ``SPX``. The ticker is rewritten here
    so callers always pass ``"SPX"`` and we do the right thing.
    """
    root = ticker.strip().upper()
    if not root:
        raise ValueError("ticker is empty")
    opt = option_type.strip().upper()
    if opt not in ("C", "P"):
        raise ValueError(f"option_type must be 'C' or 'P', got {option_type!r}")
    strike_int = round(strike * 1000)
    if strike_int <= 0:
        raise ValueError(f"strike must be positive, got {strike}")
    root = _polygon_root_for(root, expiry)
    return f"O:{root}{expiry:%y%m%d}{opt}{strike_int:08d}"


def _polygon_root_for(ticker: str, expiry: date) -> str:
    if ticker == "SPX" and not _is_third_friday(expiry):
        return "SPXW"
    return ticker


def _is_third_friday(d: date) -> bool:
    return d.weekday() == 4 and 15 <= d.day <= 21


_SHANE_RE = re.compile(
    r"""^\s*
    (?P<ticker>[A-Z]{1,6})\s+
    (?P<strike>\d+(?:\.\d+)?)
    (?P<opt>[CP])\s+
    (?P<day>\d{1,2})(?P<mon>[A-Z]{3})(?P<yy>\d{2})
    \s*$""",
    re.VERBOSE | re.IGNORECASE,
)

_BRANDO_RE = re.compile(
    r"""^\s*
    (?P<ticker>[A-Z]{1,6})\s+
    (?P<strike>\d+(?:\.\d+)?)
    (?P<opt>[CP])
    \s*$""",
    re.VERBOSE | re.IGNORECASE,
)

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def parse_shane_description(desc: str) -> OptionContract:
    """Parse a full TD-style description like ``"MU 500C 1MAY26"``."""
    m = _SHANE_RE.match(desc)
    if not m:
        raise ValueError(f"not a the user-format description: {desc!r}")
    month = _MONTHS.get(m.group("mon").upper())
    if month is None:
        raise ValueError(f"bad month in {desc!r}")
    expiry = date(2000 + int(m.group("yy")), month, int(m.group("day")))
    return OptionContract(
        ticker=m.group("ticker").upper(),
        expiry=expiry,
        option_type=m.group("opt").upper(),
        strike=float(m.group("strike")),
    )


def parse_brando_description(
    desc: str,
    trade_date: date,
    dte: Optional[int] = None,
) -> OptionContract:
    """Parse a reference alert like ``"AAPL 212.5C"`` and infer expiry.

    Expiry rule:
      * If ``dte`` is provided, expiry = ``trade_date + dte`` calendar days.
        This is the standard path — the sheet has a DTE column for every row.
      * Otherwise, fall back to the Friday of the trade week (the first
        Friday on or after ``trade_date``). reference's non-index trades are
        usually that Friday.
    """
    m = _BRANDO_RE.match(desc)
    if not m:
        raise ValueError(f"not a reference-format description: {desc!r}")
    if dte is not None and dte >= 0:
        # Valid DTE -> expiry = trade_date + dte calendar days
        expiry = trade_date + timedelta(days=int(dte))
    else:
        # Missing or invalid DTE (negative, etc. — occasionally appears as a
        # sheet typo) falls back to the Friday-of-week rule for reference's
        # usual non-index pattern. Caller gets a best-effort expiry rather
        # than a hard parse failure; any real mismatch will surface as
        # "no_bars" downstream.
        expiry = _next_friday_on_or_after(trade_date)
    return OptionContract(
        ticker=m.group("ticker").upper(),
        expiry=expiry,
        option_type=m.group("opt").upper(),
        strike=float(m.group("strike")),
    )


def _next_friday_on_or_after(d: date) -> date:
    # Monday=0 ... Friday=4, Sunday=6
    offset = (4 - d.weekday()) % 7
    return d + timedelta(days=offset)


# Discord alert format from Brando's EliteOptions feed. Examples:
#   "@Elite BOUGHT |  QQQ MAY 15 712C $3.87"
#   "@Elite BOUGHT | NVDA MAY 15 225C $3.20"
#   "@Elite SOLD | SPY JUN 21 590P"   (no fill price = exit alert)
# The mention may be @Elite, @everyone, or a role like <@&123>; we tolerate
# anything before the action verb. Expiry has no year — caller passes the
# alert date and we resolve to the next occurrence of (mon, day) on or after it.
_BRANDO_DISCORD_RE = re.compile(
    r"""
    (?:@\S+|<@[!&]?\d+>)\s+              # role/user mention
    (?P<action>[A-Z]+)\s*\|\s*           # BOUGHT / SOLD / TRIMMED / STOPPED
    (?P<ticker>[A-Z]{1,6})\s+
    (?P<mon>[A-Z]{3})\s+(?P<day>\d{1,2})\s+
    (?P<strike>\d+(?:\.\d+)?)(?P<opt>[CP])
    (?:\s+\$?(?P<price>\d+(?:\.\d+)?))?  # optional fill price
    """,
    re.VERBOSE | re.IGNORECASE,
)


@dataclass(frozen=True)
class BrandoAlert:
    action: str               # "BOUGHT" | "SOLD" | "TRIMMED" | ...
    contract: OptionContract
    fill_price: Optional[float]


def parse_brando_discord_alert(text: str, alert_date: date) -> BrandoAlert:
    """Parse a Brando-style Discord alert.

    Resolves the year-less ``MON DAY`` expiry to the next such date on or
    after ``alert_date``. This handles the December-alert / January-expiry
    case correctly without needing the alert's year encoded separately.
    """
    m = _BRANDO_DISCORD_RE.search(text)
    if not m:
        raise ValueError(f"not a Brando-style alert: {text!r}")

    month = _MONTHS.get(m.group("mon").upper())
    if month is None:
        raise ValueError(f"bad month in {text!r}")
    day = int(m.group("day"))

    year = alert_date.year
    try:
        expiry = date(year, month, day)
    except ValueError as e:
        raise ValueError(f"invalid expiry date in {text!r}: {e}") from e
    if expiry < alert_date:
        expiry = date(year + 1, month, day)

    contract = OptionContract(
        ticker=m.group("ticker").upper(),
        expiry=expiry,
        option_type=m.group("opt").upper(),
        strike=float(m.group("strike")),
    )
    price = m.group("price")
    return BrandoAlert(
        action=m.group("action").upper(),
        contract=contract,
        fill_price=float(price) if price else None,
    )
