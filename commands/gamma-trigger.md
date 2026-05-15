---
name: gamma-trigger
description: Fast yes/no check on a live setup. Pass a ticker — surfaces current price (if known), reference cohort stats on that ticker, your own history, and a soft lean. <5s end-to-end.
---

Quick live-setup check.

**Usage:**
```
/gamma-trigger MU
/gamma-trigger NVDA
```

```bash
cd "${CLAUDE_PLUGIN_ROOT:?CLAUDE_PLUGIN_ROOT not set — run this as a slash command}"

TICKER="${1:?Usage: /gamma-trigger <TICKER>}"
TICKER="$(echo "$TICKER" | tr '[:lower:]' '[:upper:]')"

python3 -c "
from automation import state, analytics
ticker = '$TICKER'

# Reference cohort stats for this ticker
with state.connect() as conn:
    ref_rows = conn.execute(
        '''SELECT realized_roi, regime, status FROM reference_trades
           WHERE ticker = ? AND status = \"fully_closed\"''',
        (ticker,)
    ).fetchall()

ref_n = len(ref_rows)
if ref_n > 0:
    from statistics import median
    rois = [r['realized_roi'] for r in ref_rows if r['realized_roi'] is not None]
    wins = sum(1 for r in rois if r > 0)
    print(f'REFERENCE COHORT · {ticker}')
    print(f'  n={ref_n}  win={wins/ref_n*100:.0f}%  median ROI {median(rois):+.0f}%')
else:
    print(f'REFERENCE COHORT · {ticker} — no history')

# User's own history
trades = [t for t in analytics.closed_trades(limit=10000) if t['ticker'] == ticker]
if trades:
    user_rois = [t['roi']*100 for t in trades]
    from statistics import median
    wins = sum(1 for r in user_rois if r > 0)
    print(f'YOUR HISTORY · {ticker}')
    print(f'  n={len(trades)}  win={wins/len(trades)*100:.0f}%  median ROI {median(user_rois):+.0f}%')
else:
    print(f'YOUR HISTORY · {ticker} — no closed trades yet')
"
```

After the mechanical pull, your Claude Code session adds a 1-2 line soft lean based on the numbers — high reference win rate + good user history = LIKE; mixed = WATCH; bad on both = PASS. Never absolute GO/PASS verdicts.
