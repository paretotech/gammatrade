# Gamma Automation — IBKR-backed mechanical execution

Notion-style web UI for the gamma trading automation layer. See
`docs/ibkr_automation_spec.md` for full architecture.

## Quick start

```bash
pip install -r automation/requirements.txt
python -m automation.server
```

Then open <http://localhost:8765>.

The server seeds two mock trades (NVDA closed at TP1, MU open) and a few
demo events on first boot so the UI has something to render. Once you wire
the real broker, set `IBKR_LIVE=1` in env and `MockBroker` is replaced.

## Layout

```
automation/
├── server.py        # FastAPI app, all routes
├── state.py         # SQLite state store (single file at ~/.gamma/automation/state.db)
├── rules.py         # YAML rules engine
├── gates.py         # Pre-trade gates (familiarity, sector cap, daily cap, etc.)
├── orders.py        # Order manager + MockBroker (swap for ib_insync wrapper)
├── telemetry.py     # Append-only event log
├── config/
│   └── rules.yaml   # Editable rule surface — engine is fixed; this file moves
├── templates/       # Jinja templates (Tailwind CDN, htmx, Alpine.js)
└── requirements.txt
```

## Stack

- **Python 3.10+** — FastAPI + uvicorn
- **Server-rendered HTML** — Jinja2 templates
- **Styling** — Tailwind via CDN (no build step)
- **Interactivity** — htmx + Alpine.js (no React, no JS bundler)
- **State** — SQLite single file
- **Broker** — `ib_insync` (currently mocked)

## Pages

| Path | What it does |
|---|---|
| `/` | Dashboard — open positions, today's P&L, discipline counters |
| `/entries/new` | New entry form with pre-trade gates |
| `/positions` | Open + recent positions table |
| `/chains` | Active chain state (R1 → R2 → R3) |
| `/rules` | Rules editor — view + edit rules.yaml |
| `/telemetry` | KPIs, latency targets, event histogram |
| `/logs` | Filterable tail of events.jsonl |
| `/kill` | Kill switch + pause toggles |

## Wiring real IBKR

Replace `MockBroker` in `automation/orders.py` with an `ib_insync` wrapper:

```python
from ib_insync import IB, Option, MarketOrder, StopOrder, LimitOrder

class IBKRBroker:
    def __init__(self):
        self.ib = IB()
    def connect(self):
        self.ib.connect('127.0.0.1', 7497, clientId=1)  # 7497 paper, 7496 live
    # ... implement submit_entry, place_be_stop, place_tp_ladder
```

Test on paper account for 4 weeks before going live. Phase rollout per
spec doc.
