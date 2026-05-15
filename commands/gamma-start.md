---
name: gamma-start
description: Start the gammatrade local FastAPI server on port 8765. Idempotent — checks if a server is already running before launching.
---

Start the gammatrade dashboard server.

```bash
cd "${CLAUDE_PLUGIN_ROOT:?CLAUDE_PLUGIN_ROOT not set — run this as a /gamma-start slash command}"

# Check if already running
if lsof -ti:8765 >/dev/null 2>&1; then
    echo "✓ Server already running at http://localhost:8765"
    exit 0
fi

# Ensure runtime deps are present (idempotent — pip skips already-installed)
if ! python3 -c "import uvicorn, fastapi, jinja2" 2>/dev/null; then
    echo "→ installing runtime dependencies..."
    python3 -m pip install --quiet --user -r automation/requirements.txt \
        || { echo "✗ pip install failed"; exit 1; }
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
