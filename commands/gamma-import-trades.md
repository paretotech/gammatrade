---
name: gamma-import-trades
description: Import a broker CSV (TD Ameritrade orderStatus or IBKR TRANSACTIONS) into the local trade store. Auto-detects format, dedupes on (symbol, date, entry-minute). Pass the CSV path as the first argument.
---

Import a broker CSV into gammatrade.

**Usage:**
```
/gamma-import-trades ~/Downloads/orderStatus.csv
/gamma-import-trades ~/Downloads/U21669630.TRANSACTIONS.7D.csv
```

Supported formats:
- **TD Ameritrade** — `*-orderStatus-*.csv` daily / historical exports
- **Interactive Brokers** — `*.TRANSACTIONS.*.csv` from IB Flex Web Reports

The script copies the CSV into `~/.gamma/data/td_exports/` (the plugin's import dir), runs the dedupe pipeline, and loads new trades into the SQLite. Run multiple times safely — already-imported rows are skipped.

```bash
PLUGIN_DIR="${CLAUDE_PLUGIN_DIR:-$HOME/.claude/plugins/gammatrade}"
cd "$PLUGIN_DIR"

CSV_PATH="${1:?Usage: /gamma-import-trades <path-to-csv>}"

if [ ! -f "$CSV_PATH" ]; then
    echo "✗ File not found: $CSV_PATH" >&2
    exit 1
fi

mkdir -p data/td_exports
DEST="data/td_exports/$(basename "$CSV_PATH")"
cp "$CSV_PATH" "$DEST"
echo "✓ saved to $DEST"

echo "→ running append + import pipeline..."
python3 -u scripts/append_td_exports.py \
    --master data/master_trade_log.csv \
    --exports data/td_exports/ || { echo "✗ append step failed"; exit 1; }

python3 -u scripts/import_broker_to_db.py \
    --master data/master_trade_log.csv || { echo "✗ db import failed"; exit 1; }

echo "✓ done — refresh http://localhost:8765/analytics/trades to see new rows"
```
