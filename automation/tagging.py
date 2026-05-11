"""Setup tagging for closed trades.

Two paths:
  - auto_tag(trade)  — rule-based, deterministic, fast. Drives 80%+ of
    tagging using features we already compute (level bucket, DTE,
    chain membership, hold time, TP completion).
  - ai_tag(trade, playbook) — Claude API call when richer reasoning is
    needed (reading pregame notes for intent, classifying setups the
    rules don't cover). Cached per-trade — only re-runs when forced.

A trade can carry MULTIPLE tags (multi-label). Examples:
    ATH-break QQQ Call, 0DTE, first leg of a 4-leg chain, exited in 8m
    → tags: ['ath_break', '0dte', 'chain_starter', 'quick_scalp']

The catalog below defines every tag we know about, the human-readable
label, and which "family" it belongs to (so the UI can group them).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from . import state


# ─── Tag catalog ────────────────────────────────────────────────────────

TAG_FAMILIES = ["level", "structure", "time", "execution"]

# (tag_key, family, human_label, description)
TAG_CATALOG: list[tuple[str, str, str, str]] = [
    # Level-class setups
    ("ath_break",       "level",     "ATH break",
     "Entry above ALL mapped resistance — price-discovery setup."),
    ("near_resistance", "level",     "Near resistance",
     "Entry within 0.5% of a published resistance level (calls = break setup)."),
    ("near_support",    "level",     "Near support",
     "Entry within 0.5% of a published support level (calls = bounce setup)."),
    ("pinch_zone",      "level",     "Pinch zone",
     "Entry tight to BOTH support and resistance — resolves either way fast."),
    ("mid_range",       "level",     "Mid-range",
     "Entry > 0.5% from any published level — your weakest class."),
    ("off_level",       "level",     "Off-level",
     "Entry > 1% from any published level and no bucket fit."),
    ("below_all_support", "level",   "Break of structure",
     "Entry below ALL mapped support — capitulation / break-of-structure."),

    # Chain structure
    ("chain_starter",  "structure", "Chain starter",
     "First leg of a multi-leg chain on this ticker × side."),
    ("chain_rollup",   "structure", "Chain roll-up",
     "Subsequent leg of a chain that rolled UP into a higher strike."),
    ("chain_rolldown", "structure", "Chain roll-down",
     "Subsequent leg of a put chain that rolled DOWN into a lower strike."),
    ("solo",           "structure", "Solo",
     "Single-leg trade — no chain context."),

    # Time class
    ("0dte",            "time",     "0DTE",
     "DTE at entry == 0."),
    ("dte_1_2",         "time",     "1–2 DTE",
     "Short-dated 1–2 day-to-expiry."),
    ("dte_3_7",         "time",     "3–7 DTE",
     "Mid-dated 3–7 day-to-expiry."),
    ("dte_8_plus",      "time",     "8+ DTE",
     "Long-dated 8+ day-to-expiry."),
    ("same_day",        "time",     "Same-day",
     "Entry and exit on the same calendar day."),
    ("overnight_hold",  "time",     "Overnight hold",
     "Held across at least one overnight session."),
    ("opening_30m",     "time",     "Opening 30m",
     "Entry within the first 30 minutes of the regular session (09:30–10:00 ET)."),

    # Execution outcomes
    ("quick_scalp",   "execution", "Quick scalp",
     "Hold time < 10 min from entry to last exit."),
    ("full_ladder",   "execution", "Full ladder",
     "TP1 + TP2 + TP3 all hit (broker tags)."),
    ("tp1_only",      "execution", "TP1 only",
     "Only TP1 tier hit — remaining position stopped or expired."),
    ("stopped_out",   "execution", "Stopped out",
     "Final exit price < entry price (lost money)."),
    ("clean_win",     "execution", "Clean win",
     "First exit profitable, no give-back stop fills."),
]

TAG_INDEX = {t[0]: t for t in TAG_CATALOG}


# ─── Read paths ─────────────────────────────────────────────────────────

def get_tags(intent_id: str) -> list[dict]:
    """All tags currently attached to a trade."""
    with state.connect() as conn:
        rows = conn.execute(
            "SELECT tag, source, confidence, created_at FROM trade_tags "
            "WHERE intent_id = ? ORDER BY tag",
            (intent_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def all_known_tags() -> list[dict]:
    """Catalog view + counts of trades currently carrying each tag."""
    with state.connect() as conn:
        rows = conn.execute(
            "SELECT tag, COUNT(*) as n FROM trade_tags GROUP BY tag"
        ).fetchall()
    counts = {r["tag"]: r["n"] for r in rows}
    return [
        {"key": k, "family": fam, "label": lbl, "desc": desc,
         "n_trades": counts.get(k, 0)}
        for k, fam, lbl, desc in TAG_CATALOG
    ]


# ─── Write paths ────────────────────────────────────────────────────────

def set_tags(intent_id: str,
             tags: list[str],
             source: str = "auto",
             confidence: Optional[float] = None,
             replace: bool = False) -> None:
    """Upsert tags for a trade. When replace=True, drops all prior tags
    of the same source first (used by the auto-tagger on re-run)."""
    now = datetime.utcnow().isoformat(timespec="seconds")
    with state.connect() as conn:
        if replace:
            conn.execute(
                "DELETE FROM trade_tags WHERE intent_id = ? AND source = ?",
                (intent_id, source),
            )
        for tag in tags:
            if tag not in TAG_INDEX:
                continue   # silently skip unknown tags
            conn.execute(
                "INSERT INTO trade_tags (intent_id, tag, source, confidence, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(intent_id, tag) DO UPDATE SET "
                "  source = excluded.source, "
                "  confidence = excluded.confidence, "
                "  created_at = excluded.created_at",
                (intent_id, tag, source, confidence, now),
            )
        conn.commit()


def remove_tag(intent_id: str, tag: str) -> None:
    with state.connect() as conn:
        conn.execute(
            "DELETE FROM trade_tags WHERE intent_id = ? AND tag = ?",
            (intent_id, tag),
        )
        conn.commit()


# ─── Rule-based auto-tagger ─────────────────────────────────────────────

def _dte(entry_ts: Optional[str], expiry: Optional[str]) -> Optional[int]:
    if not entry_ts or not expiry:
        return None
    try:
        return (date.fromisoformat(str(expiry)[:10])
                - date.fromisoformat(entry_ts[:10])).days
    except (TypeError, ValueError):
        return None


def auto_tag(trade: dict, chain_membership: Optional[dict] = None) -> list[str]:
    """Compute the rule-based tag list for one closed-trade dict (as
    returned by analytics.closed_trades). Accepts an optional
    `chain_membership` lookup keyed by intent_id when the caller has
    already run chain_analysis.
    """
    from . import analytics as _a
    from . import levels as _lv

    tags: list[str] = []
    side    = trade.get("right")
    ticker  = trade.get("ticker")
    entry_ts = trade.get("first_entry_ts")
    exit_ts  = trade.get("last_exit_ts")
    pnl     = trade.get("realized_pnl") or 0

    # ── Level-class ──────────────────────────────────────────────────
    # Look up underlying + active levels at entry, classify via the
    # same _classify_position used elsewhere.
    if entry_ts and ticker:
        under = _a._underlying_price_at(ticker, entry_ts)
        snap  = _a._active_levels_at(ticker, entry_ts)
        if under is not None and snap is not None and (snap.levels_above or snap.levels_below):
            cls = _a._classify_position(under, snap, proximity_pct=0.5)
            bucket = cls.get("bucket")
            if bucket in ("ath_break", "near_resistance", "near_support",
                          "below_all_support", "mid_range", "off_level"):
                tags.append(bucket)
            # Pinch zone — tight to BOTH sides
            da = cls.get("dist_above_pct")
            db = cls.get("dist_below_pct")
            if da is not None and db is not None and da <= 0.5 and db <= 0.5:
                if "pinch_zone" not in tags:
                    tags.append("pinch_zone")

    # ── Time class ───────────────────────────────────────────────────
    d = _dte(entry_ts, trade.get("expiry"))
    if d is not None:
        if   d == 0:                tags.append("0dte")
        elif 1 <= d <= 2:           tags.append("dte_1_2")
        elif 3 <= d <= 7:           tags.append("dte_3_7")
        else:                       tags.append("dte_8_plus")

    if entry_ts and exit_ts:
        if entry_ts[:10] == exit_ts[:10]:
            tags.append("same_day")
        else:
            tags.append("overnight_hold")

    if entry_ts:
        try:
            t0 = datetime.fromisoformat(entry_ts[:19])
            # ET-based opening window — broker timestamps are ET-naive
            if t0.hour == 9 and t0.minute < 60:
                tags.append("opening_30m")
            elif t0.hour < 10:
                tags.append("opening_30m")
        except ValueError:
            pass

    # ── Chain structure ──────────────────────────────────────────────
    if chain_membership is not None:
        m = chain_membership.get(trade["intent_id"])
        if m:
            if m["is_starter"] and m["n_legs"] >= 2:
                tags.append("chain_starter")
            elif m["n_legs"] >= 2:
                tags.append("chain_rollup" if side == "C" else "chain_rolldown")
            else:
                tags.append("solo")

    # ── Execution outcomes ──────────────────────────────────────────
    if entry_ts and exit_ts:
        try:
            t0 = datetime.fromisoformat(entry_ts[:19])
            t1 = datetime.fromisoformat(exit_ts[:19])
            mins = (t1 - t0).total_seconds() / 60
            if mins < 10:
                tags.append("quick_scalp")
        except ValueError:
            pass

    tiers_hit = set(trade.get("tp_tiers_hit") or [])
    if {1, 2, 3} <= tiers_hit:
        tags.append("full_ladder")
    elif tiers_hit == {1}:
        tags.append("tp1_only")

    if pnl < 0:
        tags.append("stopped_out")
    if trade.get("first_exit_profitable"):
        tags.append("clean_win")

    # Dedupe, preserve insertion order
    seen = set()
    out  = []
    for t in tags:
        if t not in seen and t in TAG_INDEX:
            seen.add(t); out.append(t)
    return out


def retag_all(range_key: str = "all", source: str = "auto") -> dict:
    """Recompute the rule-based tags across every closed trade in range.
    Returns counts for telemetry / UI feedback."""
    from . import analytics as _a
    from collections import defaultdict

    trades = _a.closed_trades(limit=5000, range_key=range_key)

    # Build a chain_membership map (lazy import to avoid circularity).
    chain_membership: dict[str, dict] = {}
    try:
        chains = _a.chain_analysis(range_key=range_key)
        for c in chains["chains"]:
            for i, leg in enumerate(c.get("legs", [])):
                chain_membership[leg["intent_id"]] = {
                    "is_starter": (i == 0),
                    "n_legs":     c["n_legs"],
                }
    except Exception:
        pass   # chain analysis is best-effort; tagging still works without it

    n_tagged = 0
    tag_counts: dict[str, int] = defaultdict(int)
    for t in trades:
        tags = auto_tag(t, chain_membership=chain_membership)
        if tags:
            set_tags(t["intent_id"], tags, source=source, replace=True)
            n_tagged += 1
            for tg in tags:
                tag_counts[tg] += 1

    return {
        "n_trades":   len(trades),
        "n_tagged":   n_tagged,
        "tag_counts": dict(tag_counts),
    }


# ─── AI-based tagger (Claude) ───────────────────────────────────────────

def ai_tag(trade: dict,
           pregame_note: Optional[str] = None,
           force: bool = False) -> list[str]:
    """Ask Claude to assign tags from the catalog.

    Used when rule-based tagging doesn't fit cleanly (e.g. trader
    intent embedded in pregame text). Returns a list of tag keys that
    are in the catalog. Returns empty list when ANTHROPIC_API_KEY is
    unset — the rule-based tags still carry the trade.
    """
    import os, json
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return []

    try:
        import anthropic
    except ImportError:
        return []

    # Build a compact trade description
    fields = []
    for k in ("ticker", "right", "strike", "expiry", "first_entry_ts",
              "last_exit_ts", "actual_pct", "mfe_in_pct", "capture_pct",
              "realized_pnl", "tp_tiers_hit", "regime_tag", "chain_role"):
        v = trade.get(k)
        if v is not None:
            fields.append(f"  {k}: {v}")
    trade_block = "TRADE:\n" + "\n".join(fields)

    catalog_block = "AVAILABLE TAGS (return any combination of these keys):\n"
    for k, fam, lbl, desc in TAG_CATALOG:
        catalog_block += f"  {k} ({fam}): {desc}\n"

    pregame_block = f"PREGAME NOTE:\n{pregame_note}\n" if pregame_note else ""

    prompt = f"""You assign setup tags to options trades. Read the trade and any
pregame context, then return ONLY a JSON array of tag keys from the
catalog. No prose, no explanation. Maximum 6 tags.

{trade_block}

{pregame_block}

{catalog_block}

Return: ["tag1", "tag2", ...]"""

    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        # Extract JSON array
        start = text.find("[")
        end   = text.rfind("]")
        if start < 0 or end < 0:
            return []
        tags = json.loads(text[start:end+1])
        # Filter to valid tags
        return [t for t in tags if isinstance(t, str) and t in TAG_INDEX]
    except Exception:
        return []
