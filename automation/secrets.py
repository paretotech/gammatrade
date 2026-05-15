"""User-managed API-key secrets stored at ~/.gamma/automation/secrets.json.

The webapp's Settings → API keys tab writes here so the user doesn't have
to export ANTHROPIC_API_KEY in their shell before launching the server.
On startup, automation.server calls ``load_into_env()`` to copy any saved
secrets into ``os.environ`` so the rest of the code (analysis.py,
eod_analysis.py) keeps reading ``os.environ.get(...)`` unchanged.

A shell-exported value always wins. If ``ANTHROPIC_API_KEY`` is already
set in the process environment when ``load_into_env()`` runs, the saved
file value is ignored for that key — this avoids surprising the user
who set it intentionally in their shell.

Storage shape:
    { "anthropic_api_key": "sk-ant-..." }

The file is written with mode 0600 (owner read/write only).
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Iterable

SECRETS_PATH = Path.home() / ".gamma" / "automation" / "secrets.json"


# Keys we know how to manage. Maps the JSON key in the secrets file to the
# env-var name the rest of the code uses.
MANAGED: dict[str, str] = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "polygon_api_key":   "POLYGON_API_KEY",
}


def _read() -> dict:
    if not SECRETS_PATH.exists():
        return {}
    try:
        return json.loads(SECRETS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write(data: dict) -> None:
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: write to a sibling tempfile, chmod, then rename.
    tmp = SECRETS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    tmp.replace(SECRETS_PATH)


def load_into_env() -> dict[str, str]:
    """Copy each managed secret into ``os.environ`` unless the env var is
    already set (shell wins). Returns a dict of {env_var: source} where
    source is "env" (was already set), "file" (loaded from disk), or
    "unset" (not available).
    """
    data = _read()
    sources: dict[str, str] = {}
    for json_key, env_var in MANAGED.items():
        if os.environ.get(env_var):
            sources[env_var] = "env"
            continue
        saved = (data.get(json_key) or "").strip()
        if saved:
            os.environ[env_var] = saved
            sources[env_var] = "file"
        else:
            sources[env_var] = "unset"
    return sources


def save(json_key: str, value: str) -> None:
    """Persist a single secret and immediately reflect it in os.environ."""
    if json_key not in MANAGED:
        raise ValueError(f"unknown secret key: {json_key}")
    value = (value or "").strip()
    data = _read()
    if value:
        data[json_key] = value
        os.environ[MANAGED[json_key]] = value
    else:
        data.pop(json_key, None)
        os.environ.pop(MANAGED[json_key], None)
    _write(data)


def clear(json_key: str) -> None:
    """Delete a secret from disk and pop it from os.environ."""
    if json_key not in MANAGED:
        raise ValueError(f"unknown secret key: {json_key}")
    data = _read()
    data.pop(json_key, None)
    _write(data)
    os.environ.pop(MANAGED[json_key], None)


def status() -> dict:
    """Lightweight status snapshot for the settings UI. Returns:

        {
          "anthropic_api_key": {
            "set":        bool,
            "source":     "env" | "file" | "unset",
            "masked":     "sk-ant-…ABCD" | None,
          }
        }

    Never returns the full value. ``source`` lets the template tell the
    user whether the value came from the shell (so editing in the UI may
    be overridden on next restart) or from the saved file.
    """
    data = _read()
    out: dict[str, dict] = {}
    for json_key, env_var in MANAGED.items():
        live = os.environ.get(env_var) or ""
        saved = data.get(json_key) or ""
        # Prefer the live env value for masking — it's what's actually in use.
        active = live or saved
        if os.environ.get(env_var) and not saved:
            source = "env"
        elif live and saved and live == saved:
            source = "file"
        elif live and not saved:
            source = "env"
        elif saved:
            source = "file"
        else:
            source = "unset"
        out[json_key] = {
            "set":    bool(active),
            "source": source,
            "masked": _mask(active),
        }
    return out


def _mask(value: str) -> str | None:
    """Mask all but the leading prefix + last 4 chars."""
    if not value:
        return None
    if len(value) <= 8:
        return "…" + value[-2:]
    # Keep "sk-ant-" / "sk-" prefix visible if present.
    for prefix in ("sk-ant-", "sk-"):
        if value.startswith(prefix):
            return prefix + "…" + value[-4:]
    return value[:3] + "…" + value[-4:]


def known_keys() -> Iterable[tuple[str, str]]:
    """(json_key, env_var) pairs of all managed secrets — used by the
    settings template to enumerate fields."""
    return list(MANAGED.items())
