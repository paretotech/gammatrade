"""SQLite state store — durable single source of truth alongside IBKR.

Schema mirrors docs/ibkr_automation_spec.md. Used by the order manager,
read by the web UI.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Any, Iterator, Optional
import uuid

DB_PATH = Path.home() / ".gamma" / "automation" / "state.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_intents (
    intent_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    expiry TEXT NOT NULL,
    strike REAL NOT NULL,
    right TEXT NOT NULL,
    contracts INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    limit_price REAL,
    regime_tag TEXT NOT NULL,
    chain_role TEXT NOT NULL,
    chain_id TEXT,
    sector TEXT NOT NULL,
    brando_alert_id TEXT,
    status TEXT NOT NULL,
    rejection_reason TEXT,
    notes TEXT,
    tp_ladder_choice TEXT DEFAULT 'auto',
    tp_custom_pcts TEXT,
    tp_split_choice TEXT DEFAULT '50_25_25',
    roll_plan TEXT DEFAULT 'default',
    roll_pct_custom REAL,
    roll_plan_json TEXT DEFAULT '[{"trigger":"tp1","retain_pct":0.5}]',
    current_option_price REAL,
    stop_discipline TEXT DEFAULT 'be_stop',
    stop_trigger_pct REAL,
    stop_trail_pct REAL,
    stop_initial_pct REAL DEFAULT 0,
    stop_after_tp1_pct REAL DEFAULT 0,
    stop_after_tp2_pct REAL DEFAULT 0.05,
    max_option_price REAL,
    parent_intent_id TEXT,
    triggered_by_tp INTEGER,
    retained_profit_usd REAL
);

CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    side TEXT NOT NULL,
    contracts INTEGER NOT NULL,
    price REAL NOT NULL,
    fee REAL DEFAULT 0,
    is_entry INTEGER NOT NULL,
    tp_tier INTEGER,
    FOREIGN KEY (intent_id) REFERENCES trade_intents(intent_id)
);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    last_status_at TEXT NOT NULL,
    target_price REAL,
    quantity INTEGER NOT NULL,
    FOREIGN KEY (intent_id) REFERENCES trade_intents(intent_id)
);

CREATE TABLE IF NOT EXISTS chain_state (
    chain_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    r1_intent_id TEXT,
    r2_intent_id TEXT,
    r3_intent_id TEXT,
    cumulative_realized REAL DEFAULT 0,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS triggers (
    trigger_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    -- primary watch condition
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL,  -- 'above' | 'below' (of underlying)
    level REAL NOT NULL,
    -- additional conditions: JSON list of {ticker, direction, level}
    -- ALL must be met simultaneously (AND), in addition to primary
    extra_conditions TEXT,
    -- intent payload (the trade to fire on break)
    expiry TEXT NOT NULL,
    strike REAL NOT NULL,
    right TEXT NOT NULL,
    contracts INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    limit_price REAL,
    regime_tag TEXT NOT NULL,
    chain_role TEXT NOT NULL,
    sector TEXT NOT NULL,
    notes TEXT,
    pregame_date TEXT,
    -- lifecycle
    status TEXT NOT NULL DEFAULT 'waiting',  -- waiting | fired | canceled | expired | rejected
    last_evaluated_at TEXT,
    last_seen_price REAL,
    fired_at TEXT,
    fired_intent_id TEXT,
    rejection_reason TEXT,
    -- TP ladder + roll plan + stop discipline (carried into intent on fire)
    tp_ladder_choice TEXT DEFAULT 'auto',
    tp_custom_pcts TEXT,
    tp_split_choice TEXT DEFAULT '50_25_25',
    roll_plan TEXT DEFAULT 'default',
    roll_pct_custom REAL,
    roll_plan_json TEXT DEFAULT '[{"trigger":"tp1","retain_pct":0.5}]',
    stop_discipline TEXT DEFAULT 'be_stop',
    stop_trigger_pct REAL,
    stop_trail_pct REAL,
    stop_initial_pct REAL DEFAULT 0,
    stop_after_tp1_pct REAL DEFAULT 0,
    stop_after_tp2_pct REAL DEFAULT 0.05
);

CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT PRIMARY KEY,
    price REAL NOT NULL,
    updated_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual'  -- manual | broker
);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    price REAL NOT NULL,
    ts TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual'
);

CREATE INDEX IF NOT EXISTS idx_price_history_ticker_ts ON price_history(ticker, ts);

CREATE TABLE IF NOT EXISTS daily_metrics (
    date TEXT PRIMARY KEY,
    new_entries INTEGER DEFAULT 0,
    realized_pnl REAL DEFAULT 0,
    unrealized_pnl REAL DEFAULT 0,
    be_stop_fires INTEGER DEFAULT 0,
    tp1_fires INTEGER DEFAULT 0,
    tp2_fires INTEGER DEFAULT 0,
    tp3_fires INTEGER DEFAULT 0,
    panic_cuts INTEGER DEFAULT 0,
    discipline_violations INTEGER DEFAULT 0
);

-- Anonymized reference cohort that ships with the plugin. Isolated from
-- the user's own trades (trade_intents). Used by analytics to overlay a
-- baseline. Loaded once on first run from data/reference/reference_cohort.csv.
CREATE TABLE IF NOT EXISTS reference_trades (
    ref_id TEXT PRIMARY KEY,
    trader_handle TEXT NOT NULL,
    ticker TEXT NOT NULL,
    expiry TEXT,
    strike REAL,
    right TEXT,
    entry_ts TEXT,
    entry_price REAL,
    exit_ts TEXT,
    exit_price REAL,
    contracts INTEGER,
    realized_roi REAL,
    status TEXT,
    regime TEXT,
    sector TEXT,
    mfe_in_pct REAL,
    mae_in_pct REAL,
    mfe_to_expiry_pct REAL,
    mae_to_expiry_pct REAL,
    dte INTEGER,
    lotto INTEGER DEFAULT 0
);

-- Setup tags per trade. Multi-label: one trade can carry several tags
-- (e.g. "ath_break" + "chain_starter" + "0dte"). Sources:
--   'auto' — assigned by the rule-based tagger (tagging.auto_tag)
--   'ai'   — assigned by the Claude-based tagger (tagging.ai_tag)
--   'manual' — set by the user on the trade-detail page
CREATE TABLE IF NOT EXISTS trade_tags (
    intent_id  TEXT NOT NULL,
    tag        TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'auto',
    confidence REAL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (intent_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_trade_tags_intent ON trade_tags(intent_id);
CREATE INDEX IF NOT EXISTS idx_trade_tags_tag    ON trade_tags(tag);

-- Per-ticker support/resistance levels, sourced from a chartist's daily
-- snapshots. Each row is one (ticker, asof_ts) snapshot — we keep history
-- so we can see how levels evolved, but the application normally queries
-- "latest per ticker" (see analytics.latest_levels).
CREATE TABLE IF NOT EXISTS ticker_levels (
    ticker        TEXT NOT NULL,
    asof_ts       TEXT NOT NULL,       -- ISO timestamp of when the snapshot was published
    current_price REAL,
    levels_below  TEXT,                -- pipe-separated floats, ascending toward current
    levels_above  TEXT,                -- pipe-separated floats, ascending away from current
    source        TEXT,                -- 'discord_import' | 'manual' | other
    note          TEXT,                -- free-text (e.g. originating message_id)
    PRIMARY KEY (ticker, asof_ts)
);

CREATE INDEX IF NOT EXISTS idx_intents_status ON trade_intents(status);
CREATE INDEX IF NOT EXISTS idx_orders_intent ON orders(intent_id);
CREATE INDEX IF NOT EXISTS idx_fills_intent ON fills(intent_id);
CREATE INDEX IF NOT EXISTS idx_triggers_status ON triggers(status);
CREATE INDEX IF NOT EXISTS idx_triggers_ticker ON triggers(ticker);
CREATE INDEX IF NOT EXISTS idx_ref_ticker ON reference_trades(ticker);
CREATE INDEX IF NOT EXISTS idx_ref_regime ON reference_trades(regime);
CREATE INDEX IF NOT EXISTS idx_levels_ticker ON ticker_levels(ticker);
"""


def init_db(path: Path = DB_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        _migrate_db(conn)


def _migrate_db(conn) -> None:
    """Add columns to existing tables that may pre-date a schema change.
    SQLite doesn't auto-add new columns from CREATE TABLE IF NOT EXISTS, so
    additions need explicit ALTER TABLE on existing DBs."""
    additions = {
        "trade_intents": [
            ("parent_intent_id", "TEXT"),
            ("triggered_by_tp", "INTEGER"),
            ("retained_profit_usd", "REAL"),
            # MFE/MAE tracking (per data-derived / data-derived)
            # mfe_in_trade_price = peak option price seen between entry and exit
            # mae_in_trade_price = trough option price seen between entry and exit
            # mfe_to_expiry_price = peak option price between entry and expiry
            #   (counterfactual — what holding to expiry would've reached)
            ("mfe_in_trade_price", "REAL"),
            ("mae_in_trade_price", "REAL"),
            ("mfe_to_expiry_price", "REAL"),
            ("mae_to_expiry_price", "REAL"),
        ],
    }
    for table, cols in additions.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols:
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


@contextmanager
def connect(path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def new_id() -> str:
    return str(uuid.uuid4())


def insert_trade_intent(data: dict[str, Any], path: Path = DB_PATH) -> str:
    intent_id = new_id()
    row = {
        "intent_id": intent_id,
        "created_at": datetime.utcnow().isoformat(),
        "status": "pending",
        **data,
    }
    cols = ", ".join(row.keys())
    placeholders = ", ".join("?" * len(row))
    with connect(path) as conn:
        conn.execute(f"INSERT INTO trade_intents ({cols}) VALUES ({placeholders})", tuple(row.values()))
    return intent_id


def list_trade_intents(status: str | None = None, limit: int = 100, path: Path = DB_PATH) -> list[dict]:
    with connect(path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM trade_intents WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trade_intents ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def pending_intents(path: Path = DB_PATH) -> list[dict]:
    """Intents at status='pending' — entries that were accepted by gates
    but have not yet been recorded as filled. These are the staging area:
    something you've decided to take but the fill hasn't been logged
    (manual paper-trading) or routed to the broker yet.
    """
    with connect(path) as conn:
        rows = conn.execute(
            """SELECT intent_id, ticker, expiry, strike, right, contracts,
                      order_type, limit_price, regime_tag, sector,
                      notes, created_at, brando_alert_id
               FROM trade_intents
               WHERE status = 'pending'
               ORDER BY created_at DESC""",
        ).fetchall()
    return [dict(r) for r in rows]


def open_positions(path: Path = DB_PATH) -> list[dict]:
    """Return open positions (entries with fills, no full close yet).
    Each position is annotated with `tp_status` — a list of 3 dicts
    (TP1/TP2/TP3) with state, price, qty, roi, profit.
    """
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT i.intent_id, i.ticker, i.expiry, i.strike, i.right,
                   i.contracts, i.regime_tag, i.chain_role, i.sector,
                   i.created_at, i.status,
                   COALESCE(SUM(CASE WHEN f.is_entry = 1 THEN f.contracts ELSE 0 END), 0) AS entry_qty,
                   COALESCE(SUM(CASE WHEN f.is_entry = 0 THEN f.contracts ELSE 0 END), 0) AS exit_qty,
                   COALESCE(AVG(CASE WHEN f.is_entry = 1 THEN f.price END), 0) AS avg_entry_price
            FROM trade_intents i
            LEFT JOIN fills f ON f.intent_id = i.intent_id
            WHERE i.status IN ('filled', 'partial')
            GROUP BY i.intent_id
            HAVING entry_qty > exit_qty
            ORDER BY i.created_at DESC
            """
        ).fetchall()
    positions = [dict(r) for r in rows]
    for p in positions:
        p["tp_status"] = tp_status_for_position(p["intent_id"], p["avg_entry_price"], path)
    return positions


def tp_status_for_position(intent_id: str, avg_entry: float,
                            path: Path = DB_PATH) -> list[dict]:
    """Return [tp1_state, tp2_state, tp3_state]. Each:
       {tier, state, price, qty, roi, profit, target}
       state ∈ {filled, pending, canceled, not_planned}
    """
    with connect(path) as conn:
        fills = conn.execute(
            "SELECT tp_tier, contracts, price FROM fills "
            "WHERE intent_id = ? AND is_entry = 0 AND tp_tier IS NOT NULL",
            (intent_id,),
        ).fetchall()
        orders = conn.execute(
            "SELECT kind, status, target_price, quantity FROM orders "
            "WHERE intent_id = ? AND kind IN ('tp1','tp2','tp3')",
            (intent_id,),
        ).fetchall()

    fills_by_tier = {f["tp_tier"]: dict(f) for f in fills}
    orders_by_kind = {o["kind"]: dict(o) for o in orders}

    out = []
    for tier in (1, 2, 3):
        fill = fills_by_tier.get(tier)
        order = orders_by_kind.get(f"tp{tier}")
        if fill:
            roi = (fill["price"] - avg_entry) / avg_entry if avg_entry else 0
            profit = (fill["price"] - avg_entry) * fill["contracts"] * 100
            out.append({
                "tier": tier, "state": "filled",
                "price": fill["price"], "qty": fill["contracts"],
                "roi": roi, "profit": profit,
                "target": order["target_price"] if order else None,
            })
        elif order and order["status"] == "working":
            out.append({
                "tier": tier, "state": "pending",
                "target": order["target_price"], "qty": order["quantity"],
                "price": None, "roi": None, "profit": None,
            })
        elif order and order["status"] == "canceled":
            out.append({
                "tier": tier, "state": "canceled",
                "target": order["target_price"],
                "price": None, "qty": None, "roi": None, "profit": None,
            })
        else:
            out.append({
                "tier": tier, "state": "not_planned",
                "target": None, "price": None, "qty": None, "roi": None, "profit": None,
            })
    return out


def open_chains(path: Path = DB_PATH) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM chain_state WHERE status = 'active' ORDER BY started_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def daily_metric(d: date | None = None, path: Path = DB_PATH) -> dict:
    d = d or date.today()
    with connect(path) as conn:
        row = conn.execute("SELECT * FROM daily_metrics WHERE date = ?", (d.isoformat(),)).fetchone()
    if row:
        return dict(row)
    return {
        "date": d.isoformat(),
        "new_entries": 0,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "be_stop_fires": 0,
        "tp1_fires": 0,
        "tp2_fires": 0,
        "tp3_fires": 0,
        "panic_cuts": 0,
        "discipline_violations": 0,
    }


def count_today_entries(d: date | None = None, path: Path = DB_PATH) -> int:
    d = d or date.today()
    with connect(path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM trade_intents WHERE DATE(created_at) = ? AND status != 'rejected'",
            (d.isoformat(),),
        ).fetchone()
    return int(row["n"])


def get_intent(intent_id: str, path: Path = DB_PATH) -> Optional[dict]:
    with connect(path) as conn:
        row = conn.execute("SELECT * FROM trade_intents WHERE intent_id = ?", (intent_id,)).fetchone()
    return dict(row) if row else None


def get_working_orders(intent_id: str, path: Path = DB_PATH) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE intent_id = ? AND status = 'working' ORDER BY kind",
            (intent_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_orders(intent_id: str, path: Path = DB_PATH) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE intent_id = ? ORDER BY submitted_at",
            (intent_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_fills(intent_id: str, path: Path = DB_PATH) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM fills WHERE intent_id = ? ORDER BY ts", (intent_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_order_filled(order_id: str, path: Path = DB_PATH) -> None:
    with connect(path) as conn:
        conn.execute(
            "UPDATE orders SET status = 'filled', last_status_at = ? WHERE order_id = ?",
            (datetime.utcnow().isoformat(), order_id),
        )


def mark_order_canceled(order_id: str, path: Path = DB_PATH) -> None:
    with connect(path) as conn:
        conn.execute(
            "UPDATE orders SET status = 'canceled', last_status_at = ? WHERE order_id = ?",
            (datetime.utcnow().isoformat(), order_id),
        )


def update_order_quantity(order_id: str, qty: int, path: Path = DB_PATH) -> None:
    with connect(path) as conn:
        conn.execute(
            "UPDATE orders SET quantity = ?, last_status_at = ? WHERE order_id = ?",
            (qty, datetime.utcnow().isoformat(), order_id),
        )


def insert_fill(intent_id: str, side: str, contracts: int, price: float,
                is_entry: bool, tp_tier: Optional[int] = None,
                path: Path = DB_PATH) -> str:
    fill_id = new_id()
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO fills (fill_id, intent_id, ts, side, contracts, price, is_entry, tp_tier) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (fill_id, intent_id, datetime.utcnow().isoformat(),
             side, contracts, price, 1 if is_entry else 0, tp_tier),
        )
    return fill_id


def set_option_price(intent_id: str, price: float, path: Path = DB_PATH) -> None:
    with connect(path) as conn:
        conn.execute(
            "UPDATE trade_intents SET current_option_price = ? WHERE intent_id = ?",
            (price, intent_id),
        )


def set_max_option_price(intent_id: str, price: float, path: Path = DB_PATH) -> None:
    """Update MFE — only ratchets up, never down."""
    with connect(path) as conn:
        conn.execute(
            """UPDATE trade_intents SET max_option_price = ?
               WHERE intent_id = ? AND (max_option_price IS NULL OR max_option_price < ?)""",
            (price, intent_id, price),
        )


def update_order_target(order_id: str, target_price: float, path: Path = DB_PATH) -> None:
    with connect(path) as conn:
        conn.execute(
            "UPDATE orders SET target_price = ?, last_status_at = ? WHERE order_id = ?",
            (target_price, datetime.utcnow().isoformat(), order_id),
        )


def proposed_rolls_for_position(intent_id: str, path: Path = DB_PATH) -> list[dict]:
    """Return roll proposals (status='proposed_roll') whose parent is the
    given intent. Newest first."""
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM trade_intents "
            "WHERE parent_intent_id = ? AND status = 'proposed_roll' "
            "ORDER BY created_at DESC",
            (intent_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def all_proposed_rolls(path: Path = DB_PATH) -> list[dict]:
    """All open roll proposals across the system. For dashboard banner."""
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM trade_intents WHERE status = 'proposed_roll' "
            "ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def confirm_roll_proposal(proposal_id: str, contracts: int,
                           order_type: str = "MKT",
                           limit_price: Optional[float] = None,
                           path: Path = DB_PATH) -> bool:
    """Move a proposal from 'proposed_roll' → 'pending'. User has confirmed
    they want to open this position. Updates contracts/order/limit per
    user input."""
    with connect(path) as conn:
        cur = conn.execute(
            "UPDATE trade_intents SET status = 'pending', contracts = ?, "
            "  order_type = ?, limit_price = ? "
            "WHERE intent_id = ? AND status = 'proposed_roll'",
            (contracts, order_type.upper(), limit_price, proposal_id),
        )
    return cur.rowcount > 0


def roll_children_for_position(intent_id: str, path: Path = DB_PATH) -> list[dict]:
    """All children of a parent (any status), used to compute the lifecycle
    state of each roll rule. Newest first."""
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM trade_intents "
            "WHERE parent_intent_id = ? "
            "ORDER BY created_at DESC",
            (intent_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def cancel_roll_proposal(proposal_id: str, path: Path = DB_PATH) -> bool:
    with connect(path) as conn:
        cur = conn.execute(
            "UPDATE trade_intents SET status = 'canceled' "
            "WHERE intent_id = ? AND status = 'proposed_roll'",
            (proposal_id,),
        )
    return cur.rowcount > 0


def open_sector_count(sector: str, path: Path = DB_PATH) -> int:
    with connect(path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(DISTINCT i.intent_id) AS n
            FROM trade_intents i
            LEFT JOIN fills f ON f.intent_id = i.intent_id
            WHERE i.sector = ? AND i.status IN ('filled', 'partial')
            GROUP BY i.intent_id
            HAVING SUM(CASE WHEN f.is_entry = 1 THEN f.contracts ELSE -f.contracts END) > 0
            """,
            (sector,),
        ).fetchall()
    return len(row)


def seed_demo_data(path: Path = DB_PATH) -> None:
    """No-op for the public plugin. New installs start with empty
    trade_intents and populate via the broker-CSV import button. The
    bundled anonymized reference cohort lives in the `reference_trades`
    table and is loaded by scripts/load_reference_cohort.py on first run.
    """
    return
