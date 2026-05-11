#!/usr/bin/env bash
# Post-install bootstrap for gammatrade plugin.
# - Installs Python deps
# - Initializes the SQLite store
# - Loads the bundled anonymized reference cohort
# - Prints the URL to open

set -e

PLUGIN_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PLUGIN_DIR"

echo "→ gammatrade post-install"

# 1. Python deps (system python3, no venv created here)
if ! command -v python3 >/dev/null 2>&1; then
    echo "✗ python3 not found in PATH. Install Python 3.11+ first." >&2
    exit 1
fi

echo "→ installing python deps..."
python3 -m pip install --quiet --upgrade pip 2>/dev/null || true
python3 -m pip install --quiet \
    "fastapi" \
    "uvicorn[standard]" \
    "jinja2" \
    "python-multipart" \
    "pyyaml" \
    "pandas" \
    "pyarrow" \
    "anthropic" || {
    echo "✗ pip install failed. See output above." >&2
    exit 1
}

# 2. Initialize the SQLite (idempotent)
mkdir -p "$HOME/.gamma/automation"
python3 -c "from automation import state; state.init_db(); print('  ✓ db initialized at', state.DB_PATH)"

# 3. Load the bundled reference cohort (idempotent)
python3 scripts/load_reference_cohort.py || true

# 4. Print the next-step banner
cat <<EOF

╔══════════════════════════════════════════════════════════╗
║  gammatrade ready                                        ║
║                                                          ║
║  Start the dashboard:                                    ║
║      /gamma-start                                        ║
║  or manually:                                            ║
║      python3 -m uvicorn automation.server:app --port 8765 ║
║                                                          ║
║  Then open http://localhost:8765                         ║
║                                                          ║
║  Inside Claude Code, try:                                ║
║      /gamma-pregame    (analyze tonight's pregame)       ║
║      /gamma-trigger    (live setup check)                ║
║      /gamma-eod        (end-of-day journal)              ║
╚══════════════════════════════════════════════════════════╝

EOF
