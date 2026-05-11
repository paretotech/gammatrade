---
name: gamma-start
description: Start the gammatrade local FastAPI server on port 8765. Idempotent — checks if a server is already running before launching.
---

Start the gammatrade dashboard server.

```bash
PLUGIN_DIR="$(cd "$(dirname "$(realpath "$0")")/../" 2>/dev/null && pwd)" \
    || PLUGIN_DIR="$HOME/.claude/plugins/gammatrade"

cd "$PLUGIN_DIR" 2>/dev/null || cd ~/.claude/plugins/gammatrade

# Check if already running
if lsof -ti:8765 >/dev/null 2>&1; then
    echo "✓ Server already running at http://localhost:8765"
    exit 0
fi

# Launch in background
nohup python3 -m uvicorn automation.server:app --port 8765 --host 127.0.0.1 \
    > /tmp/gammatrade.log 2>&1 &

# Wait for it to be ready
for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -s -o /dev/null -w '%{http_code}' http://localhost:8765/ 2>/dev/null | grep -q '^2\|^3'; then
        echo "✓ Server started at http://localhost:8765"
        echo "  log: /tmp/gammatrade.log"
        exit 0
    fi
    sleep 0.5
done

echo "⚠  Server didn't respond within 5s — check /tmp/gammatrade.log"
tail -20 /tmp/gammatrade.log
```

After this runs, open **http://localhost:8765** in your browser to see the dashboard.
