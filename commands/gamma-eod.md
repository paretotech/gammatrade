---
name: gamma-eod
description: End-of-day journal. Pulls today's closed trades from the local SQLite, walks through structured reflection — wins, losses, plan adherence, MFE-vs-realized, lessons.
---

Run an end-of-day review of today's trading.

```bash
PLUGIN_DIR="${CLAUDE_PLUGIN_DIR:-$HOME/.claude/plugins/gammatrade}"
cd "$PLUGIN_DIR"

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

Save the reflection to `data/journal/<YYYY-MM-DD>.md` so it shows up in the dashboard timeline.
