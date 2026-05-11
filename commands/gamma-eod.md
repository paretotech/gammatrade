---
name: gamma-eod
description: End-of-day journal. Pulls today's closed trades from the local SQLite, walks through structured reflection — wins, losses, plan adherence, MFE-vs-realized, lessons.
---

Run an end-of-day review of today's trading.

```bash
cd "${CLAUDE_PLUGIN_ROOT:?CLAUDE_PLUGIN_ROOT not set — run this as a slash command}"

python3 -c "
from automation import analytics
from datetime import date
print(f'EOD review for {date.today().isoformat()}')
print()
trades = analytics.closed_trades(limit=200, range_key='today')
print(f'{len(trades)} closed trades today')
for t in trades:
    print(f'  {t[\"ticker\"]:5}  ROI {t[\"roi\"]*100:+.0f}%  \${t[\"realized_pnl\"]:+.0f}  MFE {t[\"mfe_in_pct\"] or \"—\"} → Captured {t[\"capture_pct\"] or \"—\"}%')
print()
s = analytics.pnl_summary(range_key='today')
print(f'Today: \${s[\"today\"]:+.0f}  ·  win {s[\"win_rate\"]}%  ·  median ROI {s[\"median_roi_pct\"]:+}%')
"
```

After this prints today's mechanical summary, your Claude Code session walks through structured reflection:

1. **Plan adherence**: did each entry match the night-before plan? Any reactive trades?
2. **MFE-vs-realized**: which trades had the biggest capture gap? Why exited early?
3. **Risk-cap check**: did any limit get breached (daily $ loss, daily count, sector concentration)?
4. **One lesson**: the single most actionable takeaway

Save the reflection to the webapp's journal using `automation.journal.save()`,
which writes to `~/.gamma/automation/journal/<YYYY-MM-DD>.json` and appears
immediately on the Journal tab:

```python
from automation import journal
from datetime import date
journal.save({
    "date":           date.today().isoformat(),
    "plan_adherence": "...",   # free-form per the walkthrough
    "wins":           "...",
    "losses":         "...",
    "mfe_gaps":       "...",
    "lessons":        "...",   # the one-line takeaway
    "notes":          "...",
})
```

The save is idempotent — re-running `/gamma-eod` on the same day updates
the existing entry (keeping `ts_created`, refreshing `ts_updated`). Open
`http://localhost:8765/journal/<YYYY-MM-DD>` to see it rendered alongside
the auto-computed plan-adherence score (which compares the day's actual
trades against the pregame analysis cached at
`~/.gamma/automation/analyses/<date>.json`).
