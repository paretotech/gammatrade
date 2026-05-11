"""Pregame note ingestion + historical comparison.

Parses reference-style pregame notes into structured picks, levels, and
regime context. Compares each pick against reference enriched
trade history and the strategy guide.
"""
from __future__ import annotations

import csv
import re
import statistics
from dataclasses import dataclass, asdict, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional


REPO_ROOT = Path(__file__).parent.parent
BRANDO_CSV = REPO_ROOT / "data" / "brando_enriched.csv"
SHANE_CSV = REPO_ROOT / "data" / "shane_enriched.csv"
STRATEGY_GUIDE = REPO_ROOT / "docs" / "strategy_rules.md"

PREGAME_DIR = Path.home() / ".gamma" / "automation" / "pregames"


# ─── Section parser ─────────────────────────────────────────────────────────

SECTION_HEADERS = [
    "DATE", "NIGHT BEFORE", "SHANE COMMENTARY NIGHT BEFORE",
    "REGIME CHECK", "SHANE COMMENTARY PREMARKET", "PRE-GAME PICKS",
    "POST-GAME", "NOTES",
]


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def split_sections(raw: str) -> dict[str, str]:
    """Split a pregame note into named sections by header.

    Header line may have trailing content like "(pre-market)" or
    "(top 3-5 setups I'm hunting)" — anything after the header keyword
    on that same line.
    """
    headers_re = re.compile(
        r"^\s*(" + "|".join(re.escape(h) for h in SECTION_HEADERS) + r")\b.*$",
        re.MULTILINE | re.IGNORECASE,
    )
    sections: dict[str, str] = {}
    matches = list(headers_re.finditer(raw))
    for i, m in enumerate(matches):
        name = m.group(1).upper().strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        sections[name] = raw[start:end].strip()
    return sections


# ─── Ticker-level extractor ─────────────────────────────────────────────────

# Matches lines like "NVDA above 212". Ticker must be at start of line
# (after optional bullet/whitespace) — avoids grabbing "Hold above 7300" as
# ticker=HOLD.
LEVEL_LINE_RE = re.compile(
    r"^\s*[-•*]?\s*(?P<ticker>[A-Z]{2,5})\b\s+"
    r"(?P<rest>.*?)"
    r"(?P<level>\b\d{2,5}(?:\.\d{1,2})?)\b",
    re.IGNORECASE,
)

# Tokens that look like tickers but aren't (uppercase words used in commentary)
TICKER_STOPWORDS = {
    "PDH", "PDL", "PDC", "ATH", "ATL", "ET", "ES", "NQ", "VIX", "RS",
    "OR", "AND", "BUT", "IF", "TO", "FOR", "AT", "BY", "IN", "ON", "BE",
    "ABOVE", "BELOW", "HOLD", "WAIT", "BACK", "BOUNCE", "DEFAULT", "NOT",
    "READY", "JUST", "TRICKY", "BETTER", "ALMOST", "WATCH", "TIMES",
    "POST", "NEEDS", "WHEN", "STARTING", "STAY", "NEED", "BREAK", "SOON",
    "MIDDLE", "CLOSER", "CLOSE", "LOOKS", "OK", "LOOKING", "MAY", "NEW",
    "BULLISH", "BEARISH", "UNTIL", "MAX", "DAILY", "LOSS", "FUTURES",
    "INDEX", "EXPECTED", "REGIME", "CASH", "REGIMES", "WITH", "PRE",
    "GAME", "PICKS", "TOP", "SETUPS", "HUNTING", "NIGHT", "BEFORE",
    "PREMARKET", "COMMENTARY", "DATE", "FRIDAY", "MONDAY", "TUESDAY",
    "WEDNESDAY", "THURSDAY", "SATURDAY", "SUNDAY", "MAY", "JUN", "JUL",
    "AUG", "SEP", "OCT", "NOV", "DEC", "JAN", "FEB", "MAR", "APR",
}

VERB_WORDS_RE = re.compile(
    r"\b(above|below|hold|wait|bounce|back to|approaching|break)\b",
    re.IGNORECASE,
)


@dataclass
class Pick:
    ticker: str
    level: float
    verb: str
    direction: str  # 'above' | 'below'
    raw_line: str


def _classify_direction(line_lower: str) -> tuple[str, str]:
    """Map verb in line to (direction, verb_label).

    Order of precedence (most specific phrase wins):
      'hold above'   → hold_above
      'hold below'   → hold_below
      'bounce above' → bounce_above
      'bounce below' → bounce_below
      'bounce'       → bounce_above (default upward bounce per reference convention)
      'hold'         → hold_above   (default upward hold)
      'below'        → below
      otherwise      → above

    Note: explicit 'hold above' / 'hold below' is checked BEFORE the bare
    'hold' fallback, so "hold above 700" parses correctly. The bare 'hold X
    bullish, below X bearish' reference phrasing parses as hold_above (the
    bullish/actionable read).
    """
    if "hold above" in line_lower:
        return "hold_above", "hold above"
    if "hold below" in line_lower:
        return "hold_below", "hold below"
    if "bounce above" in line_lower:
        return "bounce_above", "bounce off (up)"
    if "bounce below" in line_lower:
        return "bounce_below", "bounce off (down)"
    if "bounce" in line_lower:
        return "bounce_above", "bounce off (up)"
    if "hold" in line_lower:
        return "hold_above", "hold above"
    if "below" in line_lower:
        return "below", "below"
    return "above", "above"


def extract_picks(picks_section: str) -> list[Pick]:
    """Parse the PRE-GAME PICKS section (or any line list of TICKER LEVEL).

    Ticker must be at start of line (avoids "Hold above 7300" → HOLD).
    Stopwords filtered (HOLD, WAIT, ABOVE, etc.).
    Direction parsed from verb: 'hold above', 'bounce off', etc.
    """
    out: list[Pick] = []
    seen_pairs: set[tuple[str, float]] = set()
    for line in picks_section.splitlines():
        line = line.strip()
        if not line:
            continue
        m = LEVEL_LINE_RE.match(line)
        if not m:
            continue
        ticker = m.group("ticker").upper()
        if ticker in TICKER_STOPWORDS:
            continue
        try:
            level = float(m.group("level"))
        except ValueError:
            continue
        if level < 5:
            continue

        direction, verb = _classify_direction(line.lower())

        key = (ticker, level)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)

        out.append(Pick(ticker=ticker, level=level, verb=verb,
                        direction=direction, raw_line=line))
    return out


def extract_watchlist(night_before: str) -> list[Pick]:
    """Parse the broader NIGHT BEFORE list. These are CONTEXT only — see
    data-derived."""
    return extract_picks(night_before)


# ─── Macro confluence (QQQ / SPX / SPY / VIX gates) ────────────────────────

MACRO_TICKERS = ("QQQ", "SPX", "SPY", "VIX")

# Plausible price ranges per index, used to pick the right number when a
# line has multiple (e.g. "SPX above 7340 to be bullish, below 7300 bearish")
_INDEX_RANGES = {
    "QQQ": (200, 1500),
    "SPX": (2000, 20000),
    "SPY": (200, 1500),
    "VIX": (5, 100),
}


_DIRECTION_VERBS_NEAR = re.compile(
    r"\b(above|below|hold above|hold below|hold|bounce|break|stay above|stay below)\b",
    re.IGNORECASE,
)


def extract_macro_confluence(sections: dict[str, str],
                              exclude_tickers: list[str] | None = None
                              ) -> list[dict]:
    """Scan commentary + regime sections for QQQ/SPX/SPY/VIX confluence.

    For each ticker mention, look for the nearest number AFTER the ticker
    (within ~80 chars on the same line) that fits the ticker's plausible
    range AND has a direction verb between them. This avoids picking up
    "VIX: 16.9" current values or grabbing another ticker's level.

    First occurrence per ticker wins (REGIME CHECK read first).
    """
    exclude = {t.upper() for t in (exclude_tickers or [])}
    target_sections = [
        "REGIME CHECK",
        "SHANE COMMENTARY NIGHT BEFORE",
        "SHANE COMMENTARY PREMARKET",
        "NIGHT BEFORE",
    ]
    seen: set[str] = set()
    out: list[dict] = []

    for sec in target_sections:
        text = sections.get(sec, "")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            for m in re.finditer(r"\b(QQQ|SPX|SPY|VIX)\b", line, re.IGNORECASE):
                ticker = m.group(1).upper()
                if ticker in exclude or ticker in seen:
                    continue

                # Search the chunk AFTER the ticker (up to next ticker or 80 chars)
                tail_start = m.end()
                next_ticker = re.search(r"\b(QQQ|SPX|SPY|VIX)\b", line[tail_start:],
                                         re.IGNORECASE)
                tail_end = tail_start + (next_ticker.start() if next_ticker else 80)
                chunk = line[tail_start:tail_end]

                # Need an explicit direction verb in the chunk
                if not _DIRECTION_VERBS_NEAR.search(chunk):
                    continue

                # First number in the chunk that fits ticker's range
                level = None
                for n in re.findall(r"\b(\d{2,5}(?:\.\d{1,2})?)\b", chunk):
                    try:
                        val = float(n)
                    except ValueError:
                        continue
                    lo, hi = _INDEX_RANGES[ticker]
                    if lo < val < hi:
                        level = val
                        break
                if level is None:
                    continue

                # Direction parsed from the local chunk (not whole line)
                direction, _ = _classify_direction(chunk.lower())
                seen.add(ticker)
                out.append({
                    "ticker": ticker,
                    "direction": direction,
                    "level": level,
                    "raw_line": line,
                })
    return out


def extract_regime(regime_section: str) -> dict[str, str]:
    """Pull VIX, regime, max-loss out of the regime block."""
    out: dict[str, str] = {}
    for line in regime_section.splitlines():
        line = line.strip().lstrip("-").strip()
        if not line:
            continue
        m = re.match(r"VIX\s*[:=]?\s*([0-9.]+)", line, re.IGNORECASE)
        if m:
            out["vix"] = m.group(1)
            continue
        m = re.match(r"(?:Expected\s+)?regime\s*[:=]?\s*([A-Za-z _-]+)", line, re.IGNORECASE)
        if m:
            out["regime"] = m.group(1).strip()
            continue
        m = re.match(r"Max\s+daily\s+loss\s*[:=]?\s*\$?([0-9,]+)", line, re.IGNORECASE)
        if m:
            out["max_daily_loss"] = m.group(1).replace(",", "")
            continue
    return out


# ─── Historical-stats lookup ────────────────────────────────────────────────

_brando_cache: list[dict] | None = None
_shane_cache: list[dict] | None = None


def _load_brando() -> list[dict]:
    global _brando_cache
    if _brando_cache is None:
        if not BRANDO_CSV.exists():
            _brando_cache = []
        else:
            with open(BRANDO_CSV) as f:
                _brando_cache = list(csv.DictReader(f))
    return _brando_cache


def _load_shane() -> list[dict]:
    global _shane_cache
    if _shane_cache is None:
        if not SHANE_CSV.exists():
            _shane_cache = []
        else:
            with open(SHANE_CSV) as f:
                _shane_cache = list(csv.DictReader(f))
    return _shane_cache


def _to_float(s: Any) -> Optional[float]:
    if s in (None, "", "NA", "nan"):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _ticker_records(rows: list[dict], ticker: str) -> list[dict]:
    return [r for r in rows if (r.get("ticker", "") or "").upper() == ticker.upper()]


def _stats(rows: list[dict], roi_field: str = "adjusted_roi") -> dict[str, Any]:
    base = {"n": len(rows), "median_roi": None, "mean_roi": None,
            "win_rate": None, "best": None, "worst": None}
    if not rows:
        return base
    rois = [_to_float(r.get(roi_field) or r.get("reported_roi")) for r in rows]
    rois = [x for x in rois if x is not None]
    if not rois:
        return base
    wins = sum(1 for x in rois if x > 0)
    return {
        "n": len(rows),
        "median_roi": statistics.median(rois),
        "mean_roi": statistics.mean(rois),
        "win_rate": wins / len(rois),
        "best": max(rois),
        "worst": min(rois),
    }


def _last_trade_date(rows: list[dict]) -> Optional[str]:
    if not rows:
        return None
    ts = [r.get("entry_ts") for r in rows if r.get("entry_ts")]
    if not ts:
        return None
    return max(ts)[:10]


def historical_compare(ticker: str) -> dict[str, Any]:
    """Pull per-ticker stats from reference CSVs."""
    b_rows = _ticker_records(_load_brando(), ticker)
    s_rows = _ticker_records(_load_shane(), ticker)

    b_calls = [r for r in b_rows if (r.get("type", "") or "").upper().startswith("C")]
    b_puts = [r for r in b_rows if (r.get("type", "") or "").upper().startswith("P")]

    return {
        "ticker": ticker,
        "reference": {
            "all": _stats(b_rows),
            "calls": _stats(b_calls),
            "puts": _stats(b_puts),
            "last_traded": _last_trade_date(b_rows),
        },
        "user": {
            "all": _stats(s_rows),
            "last_traded": _last_trade_date(s_rows),
        },
    }


# ─── Strategy-guide gates per ticker ────────────────────────────────────────

def strategy_check(ticker: str, rules) -> dict[str, Any]:
    """Run strategy-guide checks: familiarity, top-5, index-strike, blackouts."""
    top5 = {"TSLA", "META", "SPX", "MU", "QQQ"}
    return {
        "familiar": rules.is_familiar(ticker),
        "top5": ticker.upper() in top5,
        "is_index": ticker.upper() in {"QQQ", "SPX", "SPY"},
    }


# ─── Candidate generation ──────────────────────────────────────────────────

@dataclass
class Candidate:
    ticker: str
    level: float
    direction: str
    verb: str
    raw_line: str
    historical: dict[str, Any] = field(default_factory=dict)
    strategy: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0
    flags: list[str] = field(default_factory=list)


def score_candidate(c: Candidate) -> float:
    """Composite quality score (0-100). Higher = closer to A+."""
    score = 50.0
    if c.strategy.get("top5"):
        score += 20
    if c.strategy.get("familiar"):
        score += 10
    else:
        score -= 30
    b = c.historical.get("reference", {}).get("all", {})
    if b.get("n", 0) >= 10:
        score += 10
        if (b.get("median_roi") or 0) > 0.20:
            score += 10
        if (b.get("win_rate") or 0) > 0.65:
            score += 5
    return max(0, min(100, score))


def build_candidates(picks: list[Pick], rules) -> list[Candidate]:
    out: list[Candidate] = []
    for p in picks:
        c = Candidate(
            ticker=p.ticker, level=p.level, direction=p.direction,
            verb=p.verb, raw_line=p.raw_line,
        )
        c.historical = historical_compare(p.ticker)
        c.strategy = strategy_check(p.ticker, rules)

        if not c.strategy["familiar"]:
            c.flags.append(f"WATCH-ONLY: {p.ticker} not in familiar tickers")
        if c.strategy["is_index"]:
            c.flags.append("INDEX: structurally compressed; ATM or 1-OTM only")
        if c.strategy["top5"]:
            c.flags.append("TOP-5 ticker (TSLA/META/SPX/MU/QQQ)")

        b_n = c.historical["reference"]["all"].get("n", 0)
        if b_n < 5:
            c.flags.append(f"reference history thin (n={b_n})")

        c.score = score_candidate(c)
        out.append(c)
    out.sort(key=lambda c: -c.score)
    return out


# ─── Persistence ────────────────────────────────────────────────────────────

def save_pregame(d: date, raw: str) -> Path:
    PREGAME_DIR.mkdir(parents=True, exist_ok=True)
    path = PREGAME_DIR / f"{d.isoformat()}.txt"
    path.write_text(raw)
    return path


def load_pregame(d: date) -> Optional[str]:
    path = PREGAME_DIR / f"{d.isoformat()}.txt"
    if path.exists():
        return path.read_text()
    return None


def list_pregames() -> list[dict]:
    if not PREGAME_DIR.exists():
        return []
    out = []
    for path in sorted(PREGAME_DIR.glob("*.txt"), reverse=True):
        try:
            d = date.fromisoformat(path.stem)
        except ValueError:
            continue
        text = path.read_text()
        sections = split_sections(text)
        picks = extract_picks(sections.get("PRE-GAME PICKS", ""))
        out.append({
            "date": d.isoformat(),
            "path": str(path),
            "size": len(text),
            "n_picks": len(picks),
            "tickers": [p.ticker for p in picks],
        })
    return out


# ─── Public parse function ──────────────────────────────────────────────────

def parse_pregame(raw: str, rules) -> dict[str, Any]:
    """Parse a raw pregame note into structured form with historical stats."""
    sections = split_sections(raw)
    picks = extract_picks(sections.get("PRE-GAME PICKS", ""))
    watchlist = extract_watchlist(sections.get("NIGHT BEFORE", ""))
    regime = extract_regime(sections.get("REGIME CHECK", ""))

    # Dedupe watchlist by ticker (keep first); strip any in picks
    pick_tickers = {p.ticker for p in picks}
    seen = set()
    watch_dedup: list[Pick] = []
    for w in watchlist:
        if w.ticker in pick_tickers or w.ticker in seen:
            continue
        seen.add(w.ticker)
        watch_dedup.append(w)

    candidates = build_candidates(picks, rules)
    watch_candidates = build_candidates(watch_dedup, rules)

    # Macro confluence — exclude any index tickers that ARE primary picks
    # (would be self-confluence)
    pick_tickers_list = [p.ticker for p in picks]
    macro_confluence = extract_macro_confluence(sections, exclude_tickers=pick_tickers_list)

    return {
        "sections": sections,
        "picks": picks,
        "watchlist": watch_dedup,
        "regime": regime,
        "candidates": candidates,  # actionable
        "watch_candidates": watch_candidates,  # context only
        "macro_confluence": macro_confluence,
        "raw": raw,
    }


# ─── DOCX reader ────────────────────────────────────────────────────────────

def read_docx(path: Path) -> str:
    """Extract text from a .docx file. Requires python-docx."""
    try:
        from docx import Document
    except ImportError:
        raise RuntimeError("python-docx not installed. pip install python-docx")
    doc = Document(str(path))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
