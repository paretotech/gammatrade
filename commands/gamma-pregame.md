---
name: gamma-pregame
description: Analyze tonight's pregame plan against historical reference data. Reads the latest pregame file from data/pregames/ (or pass a path), pulls per-pick stats from the reference cohort + your own trades, and returns a structured day-read + per-pick verdict.
---

Run pregame analysis for the trading day.

**Usage:**
```
/gamma-pregame                                       # uses latest in data/pregames/
/gamma-pregame ~/Downloads/Friday-May-10.docx        # explicit file
```

This command does NOT use the Anthropic API — instead, it invokes the **pregame-analysis** skill which runs entirely inside your Claude Code session.

Steps:
1. Locate the pregame source (latest .docx in `data/pregames/` or the file you passed)
2. Read it
3. Pull historical stats per ticker from the SQLite reference cohort + your own trades
4. Generate structured analysis (day read, per-pick verdict, operational notes)
5. Save the result to `data/pregames/<date>.analysis.json` for the webapp to display

```bash
PLUGIN_DIR="${CLAUDE_PLUGIN_DIR:-$HOME/.claude/plugins/gammatrade}"
cd "$PLUGIN_DIR"

PREGAME_PATH="${1:-}"
if [ -z "$PREGAME_PATH" ]; then
    PREGAME_PATH="$(ls -t data/pregames/*.docx data/pregames/*.txt 2>/dev/null | head -1)"
fi

if [ -z "$PREGAME_PATH" ] || [ ! -f "$PREGAME_PATH" ]; then
    echo "✗ No pregame file found. Drop one in data/pregames/ or pass a path."
    exit 1
fi

echo "→ analyzing $PREGAME_PATH"
echo "  (Claude Code will read the file + reference cohort, then generate the analysis)"
```

After this prints the file path, your Claude Code session should:

1. Read the pregame file (.docx via the docx skill or .txt directly)
2. Connect to the local SQLite at `~/.gamma/automation/state.db`
3. For each pick mentioned, query `reference_trades` for historical stats (median ROI, win rate, sample size on that ticker / sector / regime)
4. Query the `trade_intents` table for the user's own history on that ticker
5. Output a structured analysis matching the schema in `skills/pregame-analysis/SKILL.md`
6. Save to `data/pregames/<YYYY-MM-DD>.analysis.json`
7. Open `http://localhost:8765/pregame/<YYYY-MM-DD>` to see it in the UI
