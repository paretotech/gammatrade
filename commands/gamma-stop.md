---
name: gamma-stop
description: Stop the gammatrade local server cleanly.
---

Stop the gammatrade FastAPI server.

```bash
if lsof -ti:8765 >/dev/null 2>&1; then
    lsof -ti:8765 | xargs kill -TERM 2>/dev/null
    sleep 1
    if lsof -ti:8765 >/dev/null 2>&1; then
        lsof -ti:8765 | xargs kill -9 2>/dev/null
    fi
    echo "✓ Server stopped"
else
    echo "Server was not running"
fi
```
