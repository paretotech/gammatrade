"""FastAPI app — web UI for the gamma automation layer.

Run: python -m automation.server
Then open: http://localhost:8765
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Optional

import asyncio
import os
import tempfile
from urllib.parse import quote_plus as urlquote
from dataclasses import asdict

import yaml
from fastapi import FastAPI, Form, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import state, telemetry, gates, pregame, triggers as triggers_mod, analysis
from .rules import Rules, CONFIG_PATH
from .orders import TradeIntent, get_broker


BASE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(BASE / "templates"))
TEMPLATES.env.filters["money"] = lambda v: f"${v:,.0f}" if v >= 0 else f"-${-v:,.0f}"
TEMPLATES.env.filters["pct"] = lambda v: f"{v*100:+.1f}%"

app = FastAPI(title="Gamma Automation")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


# Lifespan: init DB, load rules, seed demo data, start watcher
@app.on_event("startup")
async def on_startup() -> None:
    state.init_db()
    state.seed_demo_data()
    telemetry.seed_demo_events()
    app.state.rules = Rules.load()
    app.state.broker = get_broker()
    await app.state.broker.connect()
    # Start the conditional-trigger watcher loop
    app.state.watcher_task = asyncio.create_task(
        triggers_mod.watcher_loop(gates, lambda: app.state.rules, interval_seconds=5)
    )


@app.on_event("shutdown")
async def on_shutdown() -> None:
    task = getattr(app.state, "watcher_task", None)
    if task:
        task.cancel()


def nav_context() -> dict:
    """Items shown in the sidebar."""
    return {
        "nav_items": [
            ("/", "Dashboard", "home"),
            ("/pregame", "Pregame", "book-open"),
            ("/entries/new", "New Entry", "plus-circle"),
            ("/triggers", "Triggers", "zap"),
            ("/analytics", "Analytics", "bar-chart"),
            ("/settings", "Settings", "sliders"),
        ],
        "kill_active": gates.kill_active(),
        "pause_active": gates.pause_active(),
    }


# ─── Dashboard ──────────────────────────────────────────────────────────────

def _render_dashboard(request: Request, *, parsed_alerts=None, alert_text: str = "") -> HTMLResponse:
    positions = state.open_positions()
    pending = state.pending_intents()
    metric = state.daily_metric()
    chains = state.open_chains()
    waiting_triggers = triggers_mod.list_triggers(status="waiting")
    for t in waiting_triggers:
        all_met, statuses = triggers_mod.all_conditions_met(t, app.state.rules)
        t["_statuses"] = statuses
        t["_all_met"] = all_met

    total_open_value = sum(p["avg_entry_price"] * (p["entry_qty"] - p["exit_qty"]) * 100 for p in positions)

    return TEMPLATES.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "page_title": "Dashboard",
            "positions": positions,
            "pending": pending,
            "open_count": len(positions),
            "open_value": total_open_value,
            "today": metric,
            "chains": chains,
            "waiting_triggers": waiting_triggers,
            "parsed_alerts": parsed_alerts,
            "alert_text": alert_text,
            **nav_context(),
        },
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    return _render_dashboard(request)


@app.post("/alerts/parse", response_class=HTMLResponse)
async def alerts_parse(request: Request,
                          alert_text: str = Form("")) -> HTMLResponse:
    """Parse pasted Brando-style Discord alerts. One alert per non-empty
    line. Returns the dashboard with a parse-result block at the top."""
    from datetime import date as _date
    from src.contract_symbols import parse_brando_discord_alert

    today = _date.today()
    parsed: list[dict] = []
    for raw in alert_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            a = parse_brando_discord_alert(line, today)
            parsed.append({
                "ok": True,
                "raw": line,
                "action": a.action,
                "ticker": a.contract.ticker,
                "expiry": a.contract.expiry.isoformat(),
                "strike": a.contract.strike,
                "right": a.contract.option_type,
                "fill_price": a.fill_price,
            })
        except ValueError as e:
            parsed.append({"ok": False, "raw": line, "error": str(e)})

    return _render_dashboard(request, parsed_alerts=parsed, alert_text=alert_text)


@app.post("/intents/{intent_id}/fill", response_class=HTMLResponse)
async def intent_record_fill(request: Request, intent_id: str,
                                fill_price: float = Form(...),
                                contracts: Optional[int] = Form(None)
                                ) -> RedirectResponse:
    """Record a synthetic BUY fill for a pending intent and transition it
    to status='filled'. Used to promote a staged intent (e.g. Brando alert)
    into an open position once the user confirms they actually took it."""
    intent = state.get_intent(intent_id)
    if not intent or intent.get("status") != "pending":
        return RedirectResponse(url="/", status_code=303)
    qty = contracts if contracts and contracts > 0 else int(intent["contracts"])
    state.insert_fill(intent_id, side="BUY", contracts=qty,
                      price=float(fill_price), is_entry=True)
    with state.connect() as conn:
        conn.execute("UPDATE trade_intents SET status = 'filled' WHERE intent_id = ?",
                     (intent_id,))
    telemetry.log_event("manual_entry_fill", {
        "intent_id": intent_id, "ticker": intent["ticker"],
        "price": float(fill_price), "contracts": qty,
    })
    return RedirectResponse(url="/", status_code=303)


@app.post("/intents/{intent_id}/cancel", response_class=HTMLResponse)
async def intent_cancel(request: Request, intent_id: str) -> RedirectResponse:
    intent = state.get_intent(intent_id)
    if not intent or intent.get("status") != "pending":
        return RedirectResponse(url="/", status_code=303)
    with state.connect() as conn:
        conn.execute("UPDATE trade_intents SET status = 'canceled' WHERE intent_id = ?",
                     (intent_id,))
    telemetry.log_event("intent_canceled", {
        "intent_id": intent_id, "ticker": intent["ticker"],
    })
    return RedirectResponse(url="/", status_code=303)


# ─── New Entry ──────────────────────────────────────────────────────────────

@app.get("/entries/new", response_class=HTMLResponse)
async def new_entry_form(
    request: Request,
    ticker: Optional[str] = None,
    level: Optional[float] = None,
    direction: Optional[str] = None,
    sector: Optional[str] = None,
    note_date: Optional[str] = None,
    extra: Optional[str] = None,
    expiry: Optional[str] = None,
    strike: Optional[float] = None,
    right: Optional[str] = None,
    limit_price: Optional[float] = None,
    source: Optional[str] = None,
) -> HTMLResponse:
    import json as _json
    rules: Rules = app.state.rules
    # Validate extras JSON server-side; fall back to empty list on bad input
    extras_list: list = []
    if extra:
        try:
            parsed = _json.loads(extra)
            if isinstance(parsed, list):
                extras_list = [
                    {"ticker": str(c["ticker"]).upper(),
                     "direction": str(c["direction"]),
                     "level": float(c["level"])}
                    for c in parsed
                    if isinstance(c, dict) and {"ticker", "direction", "level"} <= c.keys()
                ]
        except (ValueError, TypeError):
            pass
    from .rules import default_friday_expiry, default_strike_otm1
    ticker_u = (ticker or "").upper()
    default_strike = (default_strike_otm1(ticker_u, level, direction or "above")
                      if ticker_u and level else None)

    # Pre-compute TP defaults for each preset, assuming 5-9 DTE NORMAL.
    # Client-side Alpine populates inputs from these on preset change.
    presets_computed = {}
    for choice_name in ("auto", "intraday", "swing", "fixed_45_60_90"):
        l = rules.compute_ladder(choice_name, 5, "NORMAL")
        presets_computed[choice_name] = {
            "tp1": int(round(l["tp1_pct"] * 100)) if l.get("tp1_pct") else None,
            "tp2": int(round(l["tp2_pct"] * 100)) if l.get("tp2_pct") else None,
            "tp3": int(round(l["tp3_pct"] * 100)) if l.get("tp3_pct") else None,
            "splits": [int(s * 100) for s in l["splits"]],
        }

    right_u = (right or "").upper()
    prefill = {
        "ticker": ticker_u,
        "level": level,
        "direction": direction,
        "sector": sector,
        "note_date": note_date,
        "extras_json": _json.dumps(extras_list),
        "default_expiry": expiry or default_friday_expiry(),
        "default_strike": strike if strike is not None else default_strike,
        "default_right": right_u if right_u in ("C", "P") else (
            "P" if direction in ("below", "hold_below", "bounce_below") else "C"),
        "limit_price": limit_price,
        "order_type": "LMT" if limit_price is not None else "MKT",
        "source": source,
        "presets_json": _json.dumps(presets_computed),
    }
    return TEMPLATES.TemplateResponse(
        "entry_form.html",
        {
            "request": request,
            "page_title": "New Entry",
            "familiar_tickers": rules.raw["familiar_tickers"],
            "regimes": list(rules.raw["regime_multipliers"].keys()),
            "today_count": state.count_today_entries(),
            "daily_cap": rules.daily_count_cap("NORMAL"),
            "prefill": prefill,
            **nav_context(),
        },
    )


@app.post("/entries", response_class=HTMLResponse)
async def create_entry(
    request: Request,
    ticker: str = Form(...),
    expiry: str = Form(...),
    strike: float = Form(...),
    right: str = Form(...),
    contracts: int = Form(...),
    order_type: str = Form("MKT"),
    limit_price: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    trigger_level: Optional[str] = Form(None),
    trigger_direction: Optional[str] = Form("above"),
    pregame_date: Optional[str] = Form(None),
    extra_conditions_json: Optional[str] = Form(None),
    tp_ladder_choice: str = Form("auto"),
    tp_custom_tp1: Optional[str] = Form(None),
    tp_custom_tp2: Optional[str] = Form(None),
    tp_custom_tp3: Optional[str] = Form(None),
    tp_split_choice: str = Form("50_25_25"),
    roll_plan: str = Form("default"),
    roll_pct_custom: Optional[str] = Form(None),
    roll_plan_json: Optional[str] = Form(None),
    stop_discipline: str = Form("be_stop"),
    stop_trigger_pct: Optional[str] = Form(None),
    stop_trail_pct: Optional[str] = Form(None),
    stop_initial_pct: Optional[str] = Form(None),
    stop_after_tp1_pct: Optional[str] = Form(None),
    stop_after_tp2_pct: Optional[str] = Form(None),
) -> HTMLResponse:
    import json as _json
    def _f(s: Optional[str]) -> Optional[float]:
        if s is None or s == "":
            return None
        try:
            return float(s)
        except ValueError:
            return None

    rules: Rules = app.state.rules
    ticker_u = ticker.upper()
    limit_price_f = _f(limit_price)
    trigger_level_f = _f(trigger_level)
    tp1, tp2, tp3 = _f(tp_custom_tp1), _f(tp_custom_tp2), _f(tp_custom_tp3)
    roll_pct_custom_f = _f(roll_pct_custom)
    stop_trigger_pct_f = _f(stop_trigger_pct)
    stop_trail_pct_f = _f(stop_trail_pct)
    custom_pcts = None
    if tp_ladder_choice == "custom" and all(x is not None for x in (tp1, tp2, tp3)):
        custom_pcts = [tp1 / 100, tp2 / 100, tp3 / 100]
    intent_data = {
        "ticker": ticker_u,
        "expiry": expiry,
        "strike": strike,
        "right": right.upper(),
        "contracts": contracts,
        "order_type": order_type.upper(),
        "limit_price": limit_price_f,
        "regime_tag": "NORMAL",
        "chain_role": "solo",
        "sector": rules.sector_for(ticker_u),
        "brando_alert_id": None,
        "notes": notes,
        "tp_ladder_choice": tp_ladder_choice,
        "tp_custom_pcts": _json.dumps(custom_pcts) if custom_pcts else None,
        "tp_split_choice": tp_split_choice,
        "roll_plan": roll_plan,
        "roll_pct_custom": (roll_pct_custom_f / 100) if roll_pct_custom_f is not None else None,
        "roll_plan_json": roll_plan_json or '[]',
        "stop_discipline": stop_discipline,
        "stop_trigger_pct": (stop_trigger_pct_f / 100) if stop_trigger_pct_f is not None else None,
        "stop_trail_pct": (stop_trail_pct_f / 100) if stop_trail_pct_f is not None else None,
        "stop_initial_pct": (_f(stop_initial_pct) / 100) if _f(stop_initial_pct) is not None else 0.0,
        "stop_after_tp1_pct": (_f(stop_after_tp1_pct) / 100) if _f(stop_after_tp1_pct) is not None else 0.0,
        "stop_after_tp2_pct": (_f(stop_after_tp2_pct) / 100) if _f(stop_after_tp2_pct) is not None else 0.05,
    }

    # Branch: if a trigger level is given, create a trigger instead of
    # firing immediately. This is the default workflow per
    # data-derived.
    if trigger_level_f is not None:
        from datetime import time, timedelta
        eod_today = datetime.combine(date.today(), time(21, 0))
        eod = eod_today if datetime.utcnow() < eod_today else eod_today + timedelta(days=1)
        triggers_mod.create_trigger({
            "ticker": ticker_u,
            "direction": (trigger_direction or "above").lower(),
            "level": trigger_level_f,
            "extra_conditions": extra_conditions_json,
            "expiry": expiry,
            "strike": strike,
            "right": right.upper(),
            "contracts": contracts,
            "order_type": order_type.upper(),
            "limit_price": limit_price_f,
            "regime_tag": "NORMAL",
            "chain_role": "solo",
            "sector": rules.sector_for(ticker_u),
            "notes": notes,
            "pregame_date": pregame_date,
            "expires_at": eod.isoformat(),
            "tp_ladder_choice": tp_ladder_choice,
            "tp_custom_pcts": intent_data["tp_custom_pcts"],
            "tp_split_choice": tp_split_choice,
            "roll_plan": roll_plan,
            "roll_pct_custom": intent_data["roll_pct_custom"],
            "roll_plan_json": intent_data["roll_plan_json"],
            "stop_discipline": stop_discipline,
            "stop_trigger_pct": intent_data["stop_trigger_pct"],
            "stop_trail_pct": intent_data["stop_trail_pct"],
            "stop_initial_pct": intent_data["stop_initial_pct"],
            "stop_after_tp1_pct": intent_data["stop_after_tp1_pct"],
            "stop_after_tp2_pct": intent_data["stop_after_tp2_pct"],
        })
        return RedirectResponse(url="/triggers", status_code=303)

    ok, reason = gates.run_gates(intent_data, rules)
    if not ok:
        return TEMPLATES.TemplateResponse(
            "entry_rejected.html",
            {
                "request": request,
                "page_title": "Entry Rejected",
                "reason": reason,
                "intent": intent_data,
                **nav_context(),
            },
            status_code=200,
        )

    # Configurable risk gates (per-section caution/decline at /settings/risk)
    from . import risk as risk_mod
    risk_eval = risk_mod.evaluate(intent_data, rules)
    if risk_eval["blocked"]:
        telemetry.log_event("entry_blocked_by_risk", {
            "ticker": intent_data["ticker"],
            "reasons": risk_eval["decline_reasons"],
        })
        return TEMPLATES.TemplateResponse(
            "entry_rejected.html",
            {
                "request": request,
                "page_title": "Entry Rejected",
                "reason": "Risk-limit gate(s) declined this trade. See panel below.",
                "intent": intent_data,
                "risk_eval": risk_eval,
                **nav_context(),
            },
            status_code=200,
        )

    # Compute ladder for preview / persistence with user's choice
    expiry_date = datetime.fromisoformat(expiry).date()
    dte = (expiry_date - date.today()).days
    ladder = rules.compute_ladder(
        intent_data["tp_ladder_choice"],
        dte,
        intent_data["regime_tag"],
        custom_pcts=custom_pcts,
    )
    roll_pct = rules.compute_roll_pct(
        intent_data["roll_plan"],
        intent_data["roll_pct_custom"],
    )

    intent_id = state.insert_trade_intent(intent_data)
    telemetry.log_event("intent_created", {"intent_id": intent_id, **intent_data})
    if risk_eval["warnings"]:
        telemetry.log_event("entry_caution_warnings", {
            "intent_id": intent_id, "ticker": intent_data["ticker"],
            "warnings": [c["headline"] for c in risk_eval["checks"] if c["state"] == "caution"],
        })

    return TEMPLATES.TemplateResponse(
        "entry_accepted.html",
        {
            "request": request,
            "page_title": "Entry Accepted",
            "intent_id": intent_id,
            "intent": intent_data,
            "ladder": ladder,
            "roll_pct": roll_pct,
            "dte": dte,
            "risk_eval": risk_eval,
            **nav_context(),
        },
    )


# ─── Pregame ────────────────────────────────────────────────────────────────

@app.get("/pregame", response_class=HTMLResponse)
async def pregame_index(request: Request) -> HTMLResponse:
    notes = pregame.list_pregames()
    today_loaded = pregame.load_pregame(date.today()) is not None
    return TEMPLATES.TemplateResponse(
        "pregame_index.html",
        {
            "request": request,
            "page_title": "Pregame",
            "notes": notes,
            "today_loaded": today_loaded,
            "today": date.today().isoformat(),
            **nav_context(),
        },
    )


@app.post("/pregame", response_class=HTMLResponse)
async def pregame_create(
    request: Request,
    note_date: str = Form(...),
    pasted_text: Optional[str] = Form(None),
    upload: Optional[UploadFile] = File(None),
) -> RedirectResponse:
    """Accept either a pasted block of text or an uploaded .docx."""
    raw: str = ""
    if upload and upload.filename:
        # Persist temp file then parse
        suffix = Path(upload.filename).suffix.lower()
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
            f.write(await upload.read())
            tmp_path = Path(f.name)
        if suffix == ".docx":
            raw = pregame.read_docx(tmp_path)
        else:
            raw = tmp_path.read_text()
        tmp_path.unlink(missing_ok=True)
    elif pasted_text and pasted_text.strip():
        raw = pasted_text.strip()
    else:
        return RedirectResponse(url="/pregame", status_code=303)

    d = date.fromisoformat(note_date)
    pregame.save_pregame(d, raw)
    telemetry.log_event("pregame_saved", {"date": note_date, "size": len(raw)})
    return RedirectResponse(url=f"/pregame/{note_date}", status_code=303)


@app.get("/pregame/{note_date}", response_class=HTMLResponse)
async def pregame_view(request: Request, note_date: str,
                        run_analysis: int = 0) -> HTMLResponse:
    raw = pregame.load_pregame(date.fromisoformat(note_date))
    if not raw:
        return RedirectResponse(url="/pregame", status_code=303)

    rules: Rules = app.state.rules
    parsed = pregame.parse_pregame(raw, rules)

    dte_estimate = 5
    ladder_preview = rules.tp_ladder(dte_estimate, "NORMAL")

    # JSON-encode macro confluence for the promote-link query string
    import json as _json
    macro_for_url = _json.dumps([
        {"ticker": m["ticker"], "direction": m["direction"], "level": m["level"]}
        for m in parsed["macro_confluence"]
    ])

    # Claude analysis: cached by date; only runs when requested explicitly
    # (?run_analysis=1) to avoid burning tokens on every page view.
    if run_analysis:
        ai_result = analysis.analyze_pregame(parsed, note_date, force=False)
    else:
        ai_result = analysis.get_cached(note_date)
        if ai_result:
            ai_result = {**ai_result, "cached": True}

    return TEMPLATES.TemplateResponse(
        "pregame_view.html",
        {
            "request": request,
            "page_title": f"Pregame · {note_date}",
            "note_date": note_date,
            "parsed": parsed,
            "candidates": [asdict(c) | {"flags": c.flags} for c in parsed["candidates"]],
            "watch_candidates": [asdict(c) | {"flags": c.flags} for c in parsed["watch_candidates"]],
            "macro_confluence": parsed["macro_confluence"],
            "macro_for_url": macro_for_url,
            "ladder_preview": ladder_preview,
            "ai_result": ai_result,
            "api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
            **nav_context(),
        },
    )


@app.post("/pregame/{note_date}/analyze", response_class=HTMLResponse)
async def pregame_analyze(request: Request, note_date: str) -> RedirectResponse:
    """Force a fresh Claude analysis of the pregame (overwrites cache)."""
    raw = pregame.load_pregame(date.fromisoformat(note_date))
    if not raw:
        return RedirectResponse(url="/pregame", status_code=303)
    parsed = pregame.parse_pregame(raw, app.state.rules)
    analysis.analyze_pregame(parsed, note_date, force=True)
    return RedirectResponse(url=f"/pregame/{note_date}", status_code=303)


# ─── Triggers ───────────────────────────────────────────────────────────────

@app.get("/triggers", response_class=HTMLResponse)
async def triggers_view(request: Request) -> HTMLResponse:
    waiting = triggers_mod.list_triggers(status="waiting")
    fired = triggers_mod.list_triggers(status="fired", limit=20)
    rejected = triggers_mod.list_triggers(status="rejected", limit=10)
    canceled = triggers_mod.list_triggers(status="canceled", limit=10)
    expired = triggers_mod.list_triggers(status="expired", limit=10)
    prices = triggers_mod.list_prices()

    # Annotate waiting triggers with current condition statuses
    for t in waiting:
        all_met, statuses = triggers_mod.all_conditions_met(t, app.state.rules)
        t["_statuses"] = statuses
        t["_all_met"] = all_met

    return TEMPLATES.TemplateResponse(
        "triggers.html",
        {
            "request": request,
            "page_title": "Triggers",
            "waiting": waiting,
            "fired": fired,
            "rejected": rejected,
            "canceled": canceled,
            "expired": expired,
            "prices": prices,
            **nav_context(),
        },
    )


@app.post("/triggers", response_class=HTMLResponse)
async def trigger_create(
    request: Request,
    ticker: str = Form(...),
    direction: str = Form(...),  # 'above' | 'below'
    level: float = Form(...),
    expiry: str = Form(...),
    strike: float = Form(...),
    right: str = Form(...),
    contracts: int = Form(1),
    order_type: str = Form("MKT"),
    limit_price: Optional[str] = Form(None),
    regime_tag: str = Form("NORMAL"),
    chain_role: str = Form("solo"),
    sector: str = Form(...),
    notes: Optional[str] = Form(None),
    pregame_date: Optional[str] = Form(None),
    expires_eod: bool = Form(True),
    extra_conditions_json: Optional[str] = Form(None),
) -> RedirectResponse:
    limit_price_f = float(limit_price) if limit_price not in (None, "") else None
    expires_at = None
    if expires_eod:
        from datetime import time, timedelta
        # Use the next 21:00 UTC after now (5pm ET ≈ end of trading day).
        # If we're already past today's 21:00, use tomorrow's.
        eod_today = datetime.combine(date.today(), time(21, 0))
        eod = eod_today if datetime.utcnow() < eod_today else eod_today + timedelta(days=1)
        expires_at = eod.isoformat()

    triggers_mod.create_trigger({
        "ticker": ticker.upper(),
        "direction": direction,
        "level": level,
        "extra_conditions": extra_conditions_json,
        "expiry": expiry,
        "strike": strike,
        "right": right.upper(),
        "contracts": contracts,
        "order_type": order_type.upper(),
        "limit_price": limit_price_f,
        "regime_tag": regime_tag.upper(),
        "chain_role": chain_role,
        "sector": sector.lower(),
        "notes": notes,
        "pregame_date": pregame_date,
        "expires_at": expires_at,
    })
    return RedirectResponse(url="/triggers", status_code=303)


@app.get("/triggers/{trigger_id}/edit", response_class=HTMLResponse)
async def trigger_edit_form(request: Request, trigger_id: str) -> HTMLResponse:
    trig = triggers_mod.get_trigger(trigger_id)
    if not trig:
        return RedirectResponse(url="/triggers", status_code=303)
    if trig["status"] != "waiting":
        # Don't allow editing fired/canceled/expired
        return RedirectResponse(url="/triggers", status_code=303)

    rules: Rules = app.state.rules
    import json as _json

    # TP presets — same defaults as /entries/new
    presets_computed = {}
    for choice_name in ("auto", "intraday", "swing", "fixed_45_60_90"):
        l = rules.compute_ladder(choice_name, 5, "NORMAL")
        presets_computed[choice_name] = {
            "tp1": int(round(l["tp1_pct"] * 100)) if l.get("tp1_pct") else None,
            "tp2": int(round(l["tp2_pct"] * 100)) if l.get("tp2_pct") else None,
            "tp3": int(round(l["tp3_pct"] * 100)) if l.get("tp3_pct") else None,
            "splits": [int(s * 100) for s in l["splits"]],
        }

    # Pull the saved custom TP percentages (stored as fractions) back to ints
    tp_custom = {"tp1": None, "tp2": None, "tp3": None}
    raw_tp = trig.get("tp_custom_pcts")
    if raw_tp:
        try:
            arr = _json.loads(raw_tp)
            if isinstance(arr, list) and len(arr) >= 3:
                tp_custom = {
                    "tp1": int(round(arr[0] * 100)) if arr[0] is not None else None,
                    "tp2": int(round(arr[1] * 100)) if arr[1] is not None else None,
                    "tp3": int(round(arr[2] * 100)) if arr[2] is not None else None,
                }
        except (ValueError, TypeError):
            pass

    # Phase-based stop pcts (stored as fractions) → integer % strings
    def _pct_to_int_str(v, default: int) -> str:
        if v is None:
            return str(default)
        try:
            return str(int(round(float(v) * 100)))
        except (ValueError, TypeError):
            return str(default)

    stop_pcts = {
        "initial": _pct_to_int_str(trig.get("stop_initial_pct"), 0),
        "after_tp1": _pct_to_int_str(trig.get("stop_after_tp1_pct"), 0),
        "after_tp2": _pct_to_int_str(trig.get("stop_after_tp2_pct"), 5),
    }

    # Roll plan: prefer roll_plan_json (new multi-rule format); fall back to
    # legacy roll_plan presets so older triggers still surface their setting.
    roll_plan_init = trig.get("roll_plan_json")
    if not roll_plan_init or roll_plan_init == "[]":
        legacy = trig.get("roll_plan") or "default"
        legacy_pct_map = {"none": None, "default": 50, "aggressive": 70, "conservative": 35}
        pct = legacy_pct_map.get(legacy, 50)
        if legacy == "custom" and trig.get("roll_pct_custom"):
            pct = int(round(trig["roll_pct_custom"] * 100))
        roll_plan_init = "[]" if pct is None else _json.dumps([{"trigger": "tp1", "retain_pct": pct}])

    return TEMPLATES.TemplateResponse(
        "trigger_edit.html",
        {
            "request": request,
            "page_title": f"Edit trigger · {trig['ticker']}",
            "trigger": trig,
            "extras_json": trig.get("extra_conditions") or "[]",
            "presets_json": _json.dumps(presets_computed),
            "tp_custom": tp_custom,
            "stop_pcts": stop_pcts,
            "roll_plan_init": roll_plan_init,
            "regimes": list(rules.raw["regime_multipliers"].keys()),
            "familiar_tickers": rules.raw["familiar_tickers"],
            **nav_context(),
        },
    )


@app.post("/triggers/{trigger_id}/edit", response_class=HTMLResponse)
async def trigger_edit_save(
    request: Request,
    trigger_id: str,
    direction: str = Form(...),
    level: str = Form(...),
    expiry: str = Form(...),
    strike: str = Form(...),
    right: str = Form(...),
    contracts: int = Form(1),
    order_type: str = Form("MKT"),
    limit_price: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    extra_conditions_json: Optional[str] = Form(None),
    tp_ladder_choice: str = Form("auto"),
    tp_custom_tp1: Optional[str] = Form(None),
    tp_custom_tp2: Optional[str] = Form(None),
    tp_custom_tp3: Optional[str] = Form(None),
    tp_split_choice: str = Form("50_25_25"),
    roll_plan: str = Form("default"),
    roll_pct_custom: Optional[str] = Form(None),
    roll_plan_json: Optional[str] = Form(None),
    stop_discipline: str = Form("be_stop"),
    stop_trigger_pct: Optional[str] = Form(None),
    stop_trail_pct: Optional[str] = Form(None),
    stop_initial_pct: Optional[str] = Form(None),
    stop_after_tp1_pct: Optional[str] = Form(None),
    stop_after_tp2_pct: Optional[str] = Form(None),
) -> RedirectResponse:
    import json as _json
    def _f(s: Optional[str]) -> Optional[float]:
        if s is None or s == "":
            return None
        try:
            return float(s)
        except ValueError:
            return None

    tp1, tp2, tp3 = _f(tp_custom_tp1), _f(tp_custom_tp2), _f(tp_custom_tp3)
    custom_pcts = None
    if tp_ladder_choice == "custom" and all(x is not None for x in (tp1, tp2, tp3)):
        custom_pcts = [tp1 / 100, tp2 / 100, tp3 / 100]

    fields = {
        "direction": direction,
        "level": float(level),
        "expiry": expiry,
        "strike": float(strike),
        "right": right.upper(),
        "contracts": contracts,
        "order_type": order_type.upper(),
        "limit_price": _f(limit_price),
        "notes": notes,
        "extra_conditions": extra_conditions_json,
        "tp_ladder_choice": tp_ladder_choice,
        "tp_custom_pcts": _json.dumps(custom_pcts) if custom_pcts else None,
        "tp_split_choice": tp_split_choice,
        "roll_plan": roll_plan,
        "roll_pct_custom": (_f(roll_pct_custom) / 100) if _f(roll_pct_custom) is not None else None,
        "roll_plan_json": roll_plan_json or '[]',
        "stop_discipline": stop_discipline,
        "stop_trigger_pct": (_f(stop_trigger_pct) / 100) if _f(stop_trigger_pct) is not None else None,
        "stop_trail_pct": (_f(stop_trail_pct) / 100) if _f(stop_trail_pct) is not None else None,
        "stop_initial_pct": (_f(stop_initial_pct) / 100) if _f(stop_initial_pct) is not None else 0.0,
        "stop_after_tp1_pct": (_f(stop_after_tp1_pct) / 100) if _f(stop_after_tp1_pct) is not None else 0.0,
        "stop_after_tp2_pct": (_f(stop_after_tp2_pct) / 100) if _f(stop_after_tp2_pct) is not None else 0.05,
    }
    triggers_mod.update_trigger(trigger_id, fields)
    return RedirectResponse(url="/triggers", status_code=303)


@app.post("/triggers/{trigger_id}/cancel", response_class=HTMLResponse)
async def trigger_cancel(request: Request, trigger_id: str) -> RedirectResponse:
    triggers_mod.cancel_trigger(trigger_id)
    return RedirectResponse(url="/triggers", status_code=303)


@app.post("/triggers/evaluate", response_class=HTMLResponse)
async def trigger_evaluate_now(request: Request) -> RedirectResponse:
    """Force-tick the watcher (useful for testing without waiting 5s)."""
    triggers_mod.evaluate_all(gates, app.state.rules)
    return RedirectResponse(url="/triggers", status_code=303)


@app.post("/prices", response_class=HTMLResponse)
async def price_set(
    request: Request,
    ticker: str = Form(...),
    price: float = Form(...),
) -> RedirectResponse:
    triggers_mod.set_price(ticker.upper(), price, source="manual")
    # Tick evaluation immediately so the user sees the result
    triggers_mod.evaluate_all(gates, app.state.rules)
    return RedirectResponse(url="/triggers", status_code=303)


# ─── Positions ──────────────────────────────────────────────────────────────

@app.get("/positions/{intent_id}", response_class=HTMLResponse)
async def position_detail(request: Request, intent_id: str) -> HTMLResponse:
    intent = state.get_intent(intent_id)
    if not intent:
        return RedirectResponse(url="/", status_code=303)

    fills = state.get_fills(intent_id)
    orders = state.get_all_orders(intent_id)
    entry_qty = sum(f["contracts"] for f in fills if f["is_entry"])
    exit_qty = sum(f["contracts"] for f in fills if not f["is_entry"])
    residual = entry_qty - exit_qty
    avg_entry = (sum(f["price"] * f["contracts"] for f in fills if f["is_entry"]) / entry_qty
                 if entry_qty > 0 else 0)
    realized_pnl = sum((f["price"] - avg_entry) * f["contracts"] * 100
                       for f in fills if not f["is_entry"])

    # Which TP tiers have fired? (used to compute roll-readiness)
    fired_tiers = {f["tp_tier"] for f in fills if f.get("tp_tier")}

    # Proposed rolls for this position
    proposed_rolls = state.proposed_rolls_for_position(intent_id)

    # Lifecycle status per TP tier so the Roll plan can show
    # PROPOSED / OPEN / CANCELED / READY / PENDING accurately.
    roll_action_by_tier: dict[int, str] = {}
    for child in state.roll_children_for_position(intent_id):
        tier = child.get("triggered_by_tp")
        if not tier:
            continue
        cstatus = child["status"]
        if cstatus == "proposed_roll":
            label = "proposed"
        elif cstatus in ("pending", "partial", "filled"):
            label = "opened"
        elif cstatus == "canceled":
            label = "canceled"
        else:
            label = cstatus
        # Only overwrite with a "later" lifecycle stage (opened > proposed > canceled).
        priority = {"opened": 3, "proposed": 2, "canceled": 1}
        if tier not in roll_action_by_tier or priority.get(label, 0) > priority.get(roll_action_by_tier[tier], 0):
            roll_action_by_tier[tier] = label

    return TEMPLATES.TemplateResponse(
        "position_detail.html",
        {
            "request": request,
            "page_title": f"{intent['ticker']} {intent['expiry']} {int(intent['strike'])}{intent['right']}",
            "intent": intent,
            "fills": fills,
            "orders": orders,
            "entry_qty": entry_qty,
            "exit_qty": exit_qty,
            "residual": residual,
            "avg_entry": avg_entry,
            "realized_pnl": realized_pnl,
            "fired_tiers": sorted(fired_tiers),
            "roll_plan_json": intent.get("roll_plan_json") or "[]",
            "proposed_rolls": proposed_rolls,
            "roll_action_by_tier": roll_action_by_tier,
            **nav_context(),
        },
    )


@app.post("/positions/{intent_id}/simulate", response_class=HTMLResponse)
async def position_simulate(
    request: Request, intent_id: str, option_price: float = Form(...),
) -> RedirectResponse:
    """Simulate setting the option price and triggering any orders that fire."""
    from .orders import evaluate_position_orders
    evaluate_position_orders(intent_id, option_price, app.state.rules)
    return RedirectResponse(url=f"/positions/{intent_id}", status_code=303)


@app.post("/positions/{intent_id}/rolls/edit", response_class=HTMLResponse)
async def position_rolls_edit(
    request: Request, intent_id: str, roll_plan_json: str = Form(...),
) -> RedirectResponse:
    """Update the roll plan on a position."""
    intent = state.get_intent(intent_id)
    if not intent:
        return RedirectResponse(url="/", status_code=303)
    with state.connect() as conn:
        conn.execute(
            "UPDATE trade_intents SET roll_plan_json = ? WHERE intent_id = ?",
            (roll_plan_json, intent_id),
        )
    telemetry.log_event("roll_plan_updated", {
        "intent_id": intent_id, "roll_plan_json": roll_plan_json,
    })
    return RedirectResponse(url=f"/positions/{intent_id}", status_code=303)


@app.post("/positions/{intent_id}/rolls/{proposal_id}/confirm",
           response_class=HTMLResponse)
async def position_confirm_roll(
    request: Request, intent_id: str, proposal_id: str,
    contracts: int = Form(...),
    order_type: str = Form("MKT"),
    limit_price: Optional[str] = Form(None),
) -> RedirectResponse:
    """User confirmed a proposed roll — flip to 'pending'."""
    limit_f = None
    if limit_price and limit_price.strip():
        try:
            limit_f = float(limit_price)
        except ValueError:
            limit_f = None
    ok = state.confirm_roll_proposal(proposal_id, contracts,
                                       order_type=order_type, limit_price=limit_f)
    if ok:
        telemetry.log_event("roll_confirmed", {
            "proposal_id": proposal_id, "parent_intent_id": intent_id,
            "contracts": contracts, "order_type": order_type,
        })
    return RedirectResponse(url=f"/positions/{intent_id}", status_code=303)


@app.post("/positions/{intent_id}/rolls/{proposal_id}/cancel",
           response_class=HTMLResponse)
async def position_cancel_roll(request: Request, intent_id: str,
                                 proposal_id: str) -> RedirectResponse:
    ok = state.cancel_roll_proposal(proposal_id)
    if ok:
        telemetry.log_event("roll_canceled", {
            "proposal_id": proposal_id, "parent_intent_id": intent_id,
        })
    return RedirectResponse(url=f"/positions/{intent_id}", status_code=303)


@app.post("/orders/{order_id}/edit", response_class=HTMLResponse)
async def order_edit(
    request: Request,
    order_id: str,
    target_price: float = Form(...),
    quantity: int = Form(...),
) -> RedirectResponse:
    """Edit a working order's target price and/or quantity."""
    with state.connect() as conn:
        row = conn.execute(
            "SELECT intent_id, status, kind FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
    if not row:
        return RedirectResponse(url="/", status_code=303)
    if row["status"] != "working":
        return RedirectResponse(url=f"/positions/{row['intent_id']}", status_code=303)

    state.update_order_target(order_id, target_price)
    state.update_order_quantity(order_id, quantity)
    telemetry.log_event("order_edited", {
        "order_id": order_id, "kind": row["kind"],
        "target_price": target_price, "quantity": quantity,
    })
    return RedirectResponse(url=f"/positions/{row['intent_id']}", status_code=303)


@app.post("/orders/{order_id}/cancel", response_class=HTMLResponse)
async def order_cancel(request: Request, order_id: str) -> RedirectResponse:
    with state.connect() as conn:
        row = conn.execute(
            "SELECT intent_id, status, kind FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
    if not row or row["status"] != "working":
        return RedirectResponse(url="/", status_code=303)
    state.mark_order_canceled(order_id)
    telemetry.log_event("order_canceled_manual", {
        "order_id": order_id, "kind": row["kind"],
    })
    return RedirectResponse(url=f"/positions/{row['intent_id']}", status_code=303)


@app.post("/orders/{order_id}/restore", response_class=HTMLResponse)
async def order_restore(request: Request, order_id: str) -> RedirectResponse:
    """Undo cancel: flip a canceled order back to working with its prior
    target/qty. The engine will clamp qty to residual on next evaluation."""
    with state.connect() as conn:
        row = conn.execute(
            "SELECT intent_id, status, kind FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
    if not row or row["status"] != "canceled":
        return RedirectResponse(url="/", status_code=303)
    with state.connect() as conn:
        conn.execute(
            "UPDATE orders SET status = 'working', last_status_at = ? WHERE order_id = ?",
            (datetime.utcnow().isoformat(), order_id),
        )
    telemetry.log_event("order_restored", {
        "order_id": order_id, "kind": row["kind"],
    })
    return RedirectResponse(url=f"/positions/{row['intent_id']}", status_code=303)


@app.post("/orders/{order_id}/reset", response_class=HTMLResponse)
async def order_reset(request: Request, order_id: str) -> RedirectResponse:
    """Reset a working order's target_price to the auto-computed default
    from current rules (DTE bucket × regime multiplier for TPs; entry for
    BE stop). Quantity is preserved."""
    rules: Rules = app.state.rules
    with state.connect() as conn:
        row = conn.execute(
            "SELECT intent_id, status, kind FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
    if not row or row["status"] != "working":
        return RedirectResponse(url="/", status_code=303)

    intent = state.get_intent(row["intent_id"])
    if not intent:
        return RedirectResponse(url="/", status_code=303)

    fills = state.get_fills(row["intent_id"])
    entry_fills = [f for f in fills if f["is_entry"]]
    avg_entry = (sum(f["price"] * f["contracts"] for f in entry_fills)
                 / sum(f["contracts"] for f in entry_fills)) if entry_fills else 0
    expiry_date = date.fromisoformat(intent["expiry"])
    dte = (expiry_date - date.today()).days
    ladder = rules.compute_ladder(intent.get("tp_ladder_choice") or "auto",
                                   dte, intent["regime_tag"])

    new_target = None
    if row["kind"] == "tp1" and ladder.get("tp1_pct"):
        new_target = avg_entry * (1 + ladder["tp1_pct"])
    elif row["kind"] == "tp2" and ladder.get("tp2_pct"):
        new_target = avg_entry * (1 + ladder["tp2_pct"])
    elif row["kind"] == "tp3" and ladder.get("tp3_pct"):
        new_target = avg_entry * (1 + ladder["tp3_pct"])
    elif row["kind"] == "be_stop":
        new_target = avg_entry  # Tier 1 default

    if new_target is None:
        return RedirectResponse(url=f"/positions/{row['intent_id']}", status_code=303)

    state.update_order_target(order_id, round(new_target, 2))
    telemetry.log_event("order_reset", {
        "order_id": order_id, "kind": row["kind"], "new_target": round(new_target, 2),
    })
    return RedirectResponse(url=f"/positions/{row['intent_id']}", status_code=303)


@app.get("/positions", response_class=HTMLResponse)
async def positions(request: Request) -> HTMLResponse:
    rows = state.open_positions()
    closed = [r for r in state.list_trade_intents(limit=50) if r["status"] in ("filled", "closed", "rejected")]
    return TEMPLATES.TemplateResponse(
        "positions.html",
        {
            "request": request,
            "page_title": "Positions",
            "open_positions": rows,
            "recent_closed": closed[:20],
            **nav_context(),
        },
    )


# ─── Chains ─────────────────────────────────────────────────────────────────

@app.get("/chains", response_class=HTMLResponse)
async def chains(request: Request) -> HTMLResponse:
    rows = state.open_chains()
    return TEMPLATES.TemplateResponse(
        "chains.html",
        {
            "request": request,
            "page_title": "Chains",
            "chains": rows,
            **nav_context(),
        },
    )


# ─── Analytics ──────────────────────────────────────────────────────────────

@app.get("/analytics", response_class=HTMLResponse)
async def analytics_landing(request: Request) -> RedirectResponse:
    return RedirectResponse(url="/analytics/pnl", status_code=303)


def _norm_range(v: Optional[str]) -> str:
    return v if v in ("today", "week", "month", "90d", "all") else "all"


@app.get("/analytics/pnl", response_class=HTMLResponse)
async def analytics_pnl(
    request: Request,
    range: Optional[str] = None,
    month: Optional[str] = None,
) -> HTMLResponse:
    from . import analytics as _a
    from datetime import date as _date
    rng = _norm_range(range)
    # Parse month=YYYY-MM, default to current month
    today = _date.today()
    yr, mo = today.year, today.month
    if month:
        try:
            yr, mo = int(month[:4]), int(month[5:7])
        except (ValueError, IndexError):
            pass
    return TEMPLATES.TemplateResponse(
        "analytics_pnl.html",
        {
            "request": request,
            "page_title": "Analytics",
            "active_tab": "pnl",
            "tab_slug": "pnl",
            "range_key": rng,
            "summary": _a.pnl_summary(range_key=rng),
            "series": _a.daily_pnl_series(days=30),
            "calendar": _a.monthly_calendar(yr, mo),
            "advanced": _a.advanced_metrics(range_key=rng),
            **nav_context(),
        },
    )


@app.get("/analytics/trades", response_class=HTMLResponse)
async def analytics_trades(
    request: Request,
    range: Optional[str] = None,
    imported: Optional[str] = None,
    import_error: Optional[str] = None,
) -> HTMLResponse:
    from . import analytics as _a
    rng = _norm_range(range)
    return TEMPLATES.TemplateResponse(
        "analytics_trades.html",
        {
            "request": request,
            "page_title": "Analytics",
            "active_tab": "trades",
            "tab_slug": "trades",
            "range_key": rng,
            "trades": _a.closed_trades(limit=500, range_key=rng),
            "import_status": imported,    # "started" | "ok" | None
            "import_error": import_error,  # error message string or None
            **nav_context(),
        },
    )


@app.post("/analytics/trades/import", response_class=HTMLResponse)
async def analytics_trades_import(
    request: Request,
    td_csv: UploadFile = File(...),
) -> RedirectResponse:
    """Accept a TD orderStatus CSV, save it to data/td_exports/, then run the
    ingestion pipeline in the background:
        append_td_exports.py → run_delta.py → import_shane_to_automation.py

    Pipeline runs as a detached subprocess so the request returns immediately.
    User refreshes the page in 1-2 minutes to see new rows.
    """
    import subprocess
    from pathlib import Path as _Path

    REPO = _Path(__file__).resolve().parent.parent
    exports_dir = REPO / "data" / "td_exports"
    exports_dir.mkdir(parents=True, exist_ok=True)

    # Validate basic format
    filename = (td_csv.filename or "td_import.csv").strip()
    if not filename.lower().endswith(".csv"):
        return RedirectResponse(
            url=f"/analytics/trades?import_error={urlquote('Only .csv files accepted')}",
            status_code=303,
        )

    # Save with a safe name
    safe_name = filename.replace("/", "_").replace("..", "")
    dest = exports_dir / safe_name
    contents = await td_csv.read()
    if len(contents) == 0:
        return RedirectResponse(
            url=f"/analytics/trades?import_error={urlquote('Uploaded file was empty')}",
            status_code=303,
        )
    if len(contents) > 5 * 1024 * 1024:  # 5 MB cap
        return RedirectResponse(
            url=f"/analytics/trades?import_error={urlquote('File too large (>5MB)')}",
            status_code=303,
        )
    dest.write_bytes(contents)
    telemetry.log_event("td_import_uploaded", {
        "filename": safe_name, "size_bytes": len(contents),
    })

    # Fire pipeline in background (detached) so we return fast.
    # Use bash -c to chain; redirect output to a log file the user could tail.
    log_path = REPO / "data" / "td_exports" / "_import.log"
    cmd = (
        f"cd {REPO} && "
        f"python3 -u scripts/append_td_exports.py --master data/master_trade_log.csv --exports data/td_exports/ && "
        f"python3 -u scripts/import_broker_to_db.py --master data/master_trade_log.csv"
    )
    subprocess.Popen(
        ["bash", "-c", cmd],
        stdout=open(log_path, "ab"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    return RedirectResponse(
        url=f"/analytics/trades?imported=started",
        status_code=303,
    )


@app.get("/analytics/tickers", response_class=HTMLResponse)
async def analytics_tickers(request: Request, range: Optional[str] = None) -> HTMLResponse:
    from . import analytics as _a
    rng = _norm_range(range)
    ttp = _a.time_to_tp_by_ticker(range_key=rng)
    mm = _a.mfe_mae_by_ticker(range_key=rng)
    return TEMPLATES.TemplateResponse(
        "analytics_tickers.html",
        {
            "request": request,
            "page_title": "Analytics",
            "active_tab": "tickers",
            "tab_slug": "tickers",
            "range_key": rng,
            "rows": _a.ticker_leaderboard(min_trades=2, range_key=rng),
            "ttp_rows": ttp["rows"],
            "ttp_total": ttp["total"],
            "mm_rows": mm["rows"],
            "mm_total": mm["total"],
            **nav_context(),
        },
    )


@app.get("/analytics/sectors", response_class=HTMLResponse)
async def analytics_sectors(request: Request, range: Optional[str] = None) -> HTMLResponse:
    from . import analytics as _a
    rng = _norm_range(range)
    return TEMPLATES.TemplateResponse(
        "analytics_sectors.html",
        {
            "request": request,
            "page_title": "Analytics",
            "active_tab": "sectors",
            "tab_slug": "sectors",
            "range_key": rng,
            "rows": _a.sector_leaderboard(range_key=rng),
            **nav_context(),
        },
    )


@app.get("/analytics/trends", response_class=HTMLResponse)
async def analytics_trends(
    request: Request,
    range: Optional[str] = None,
    bucket: Optional[int] = None,
    entry_period: Optional[str] = None,
) -> HTMLResponse:
    from . import analytics as _a
    rng = _norm_range(range)
    bw = bucket if bucket in (5, 10, 60) else 5
    ep = entry_period if entry_period in ("day", "week", "month") else "week"
    return TEMPLATES.TemplateResponse(
        "analytics_trends.html",
        {
            "request": request,
            "page_title": "Analytics",
            "active_tab": "trends",
            "tab_slug": "trends",
            "range_key": rng,
            "bucket_minutes": bw,
            "entry_period": ep,
            "trends": _a.trends_summary(range_key=rng, bucket_minutes=bw),
            "entry_series": _a.entry_period_series(period=ep),
            **nav_context(),
        },
    )


# ─── Settings: Rules ────────────────────────────────────────────────────────

@app.get("/settings/rules", response_class=HTMLResponse)
@app.get("/rules", response_class=HTMLResponse)
async def rules_view(request: Request) -> HTMLResponse:
    with open(CONFIG_PATH) as f:
        raw_yaml = f.read()
    return TEMPLATES.TemplateResponse(
        "rules.html",
        {
            "request": request,
            "page_title": "Settings",
            "active_tab": "rules",
            "rules_yaml": raw_yaml,
            "rules": app.state.rules.raw,
            **nav_context(),
        },
    )


@app.post("/settings/rules", response_class=HTMLResponse)
@app.post("/rules", response_class=HTMLResponse)
async def rules_save(request: Request, rules_yaml: str = Form(...)) -> HTMLResponse:
    # Parse to validate before save
    try:
        parsed = yaml.safe_load(rules_yaml)
    except yaml.YAMLError as e:
        with open(CONFIG_PATH) as f:
            current = f.read()
        return TEMPLATES.TemplateResponse(
            "rules.html",
            {
                "request": request,
                "page_title": "Settings",
                "active_tab": "rules",
                "rules_yaml": rules_yaml,
                "rules": app.state.rules.raw,
                "error": f"YAML parse error: {e}",
                **nav_context(),
            },
            status_code=400,
        )

    with open(CONFIG_PATH, "w") as f:
        f.write(rules_yaml)
    app.state.rules = Rules.load()
    telemetry.log_event("rules_reloaded", {"by": "ui"})
    return RedirectResponse(url="/settings/rules", status_code=303)


# ─── Settings: Risk limits ──────────────────────────────────────────────────

def _risk_context() -> dict:
    raw = app.state.rules.raw or {}
    daily = raw.get("daily_caps", {}) or {}
    risk = raw.get("risk_limits", {}) or {}
    bw = raw.get("blackout_windows", {}) or {}

    def _bw(key: str, default_enabled: bool = True) -> dict:
        entry = bw.get(key) or {}
        return {
            "enabled": entry.get("enabled", default_enabled),
            "enforcement": entry.get("enforcement", "decline"),
        }

    cal = raw.get("event_calendar") or {}
    fomc_text = "\n".join(str(d) for d in (cal.get("fomc_meetings") or []))
    cpi_text = "\n".join(str(d) for d in (cal.get("cpi_releases") or []))
    earnings_text = "\n".join(
        f"{k.upper()}: {v}" for k, v in (cal.get("megacap_earnings") or {}).items()
    )

    return {
        "daily": {
            "loss_cap_dollars": daily.get("loss_cap_dollars", 1000),
            "loss_enforcement": daily.get("loss_enforcement", "decline"),
            "kill_after_losses": daily.get("kill_after_losses", 2),
            "count_enforcement": daily.get("count_enforcement", "decline"),
        },
        "count_based": daily.get("count_based", {}) or {},
        "risk": {
            "max_open_positions": risk.get("max_open_positions", 5),
            "per_trade_max_dollars": risk.get("per_trade_max_dollars", 1500),
            "normal_entry_dollars": risk.get("normal_entry_dollars", 1000),
            "min_dte": risk.get("min_dte", 0),
            "max_dte": risk.get("max_dte", 21),
            "size_enforcement": risk.get("size_enforcement", "decline"),
            "dte_enforcement": risk.get("dte_enforcement", "caution"),
            "entry_warmup_seconds": risk.get("entry_warmup_seconds", 60),
        },
        "sector": raw.get("sector_caps", {}) or {"warn_at": 3, "reject_at": 4},
        "blackouts": {
            "pre_fomc": _bw("pre_fomc"),
            "cpi_day": _bw("cpi_day"),
            "pre_megacap_earnings": _bw("pre_megacap_earnings"),
        },
        "fomc_text": fomc_text,
        "cpi_text": cpi_text,
        "earnings_text": earnings_text,
    }


@app.get("/settings/risk", response_class=HTMLResponse)
async def risk_view(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        "risk.html",
        {
            "request": request,
            "page_title": "Settings",
            "active_tab": "risk",
            **_risk_context(),
            **nav_context(),
        },
    )


def _norm_enforcement(v: Optional[str], default: str = "decline") -> str:
    return v if v in ("caution", "decline") else default


def _parse_dates_textarea(text: Optional[str]) -> list[str]:
    out: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            date.fromisoformat(s)
            out.append(s)
        except ValueError:
            continue
    # de-dupe + sort
    return sorted(set(out))


def _parse_earnings_textarea(text: Optional[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or ":" not in s:
            continue
        ticker, _, datestr = s.partition(":")
        ticker = ticker.strip().upper()
        datestr = datestr.strip()
        if not ticker:
            continue
        try:
            date.fromisoformat(datestr)
        except ValueError:
            continue
        out[ticker] = datestr
    return out


@app.post("/settings/risk", response_class=HTMLResponse)
async def risk_save(
    request: Request,
    loss_cap_dollars: int = Form(...),
    loss_enforcement: str = Form("decline"),
    kill_after_losses: int = Form(...),
    cap_default: int = Form(...),
    cap_hot: int = Form(...),
    cap_warm: int = Form(...),
    cap_normal: int = Form(...),
    cap_trap: int = Form(...),
    cap_cold: int = Form(...),
    count_enforcement: str = Form("decline"),
    normal_entry_dollars: int = Form(...),
    per_trade_max_dollars: int = Form(...),
    max_open_positions: int = Form(...),
    min_dte: int = Form(...),
    max_dte: int = Form(...),
    entry_warmup_seconds: int = Form(60),
    size_enforcement: str = Form("decline"),
    sector_warn: int = Form(...),
    sector_reject: int = Form(...),
    blackout_pre_fomc: Optional[str] = Form(None),
    blackout_pre_fomc_enforcement: str = Form("decline"),
    blackout_cpi_day: Optional[str] = Form(None),
    blackout_cpi_day_enforcement: str = Form("decline"),
    blackout_megacap_earnings: Optional[str] = Form(None),
    blackout_megacap_earnings_enforcement: str = Form("caution"),
    fomc_meetings: Optional[str] = Form(None),
    cpi_releases: Optional[str] = Form(None),
    megacap_earnings: Optional[str] = Form(None),
) -> HTMLResponse:
    try:
        with open(CONFIG_PATH) as f:
            raw = yaml.safe_load(f) or {}

        daily = raw.setdefault("daily_caps", {})
        daily["loss_cap_dollars"] = loss_cap_dollars
        daily["loss_enforcement"] = _norm_enforcement(loss_enforcement)
        daily["kill_after_losses"] = kill_after_losses
        cb = daily.setdefault("count_based", {})
        cb["default"] = cap_default
        cb["hot"] = cap_hot
        cb["warm"] = cap_warm
        cb["normal"] = cap_normal
        cb["trap"] = cap_trap
        cb["cold"] = cap_cold
        daily["count_enforcement"] = _norm_enforcement(count_enforcement)

        risk = raw.setdefault("risk_limits", {})
        risk["normal_entry_dollars"] = normal_entry_dollars
        risk["per_trade_max_dollars"] = per_trade_max_dollars
        risk["max_open_positions"] = max_open_positions
        risk["min_dte"] = min_dte
        risk["max_dte"] = max_dte
        risk["entry_warmup_seconds"] = max(0, min(600, entry_warmup_seconds))
        risk["size_enforcement"] = _norm_enforcement(size_enforcement)
        # dte_enforcement isn't in the form yet — preserve existing if any.
        risk.setdefault("dte_enforcement", "caution")

        sector = raw.setdefault("sector_caps", {})
        sector["warn_at"] = sector_warn
        sector["reject_at"] = sector_reject

        bw = raw.setdefault("blackout_windows", {})
        for key, enabled, mode, default_mode in [
            ("pre_fomc", blackout_pre_fomc, blackout_pre_fomc_enforcement, "decline"),
            ("cpi_day", blackout_cpi_day, blackout_cpi_day_enforcement, "decline"),
            ("pre_megacap_earnings", blackout_megacap_earnings,
             blackout_megacap_earnings_enforcement, "caution"),
        ]:
            entry = bw.setdefault(key, {})
            entry["enabled"] = enabled == "1"
            entry["enforcement"] = _norm_enforcement(mode, default_mode)

        cal = raw.setdefault("event_calendar", {})
        cal["fomc_meetings"] = _parse_dates_textarea(fomc_meetings)
        cal["cpi_releases"] = _parse_dates_textarea(cpi_releases)
        cal["megacap_earnings"] = _parse_earnings_textarea(megacap_earnings)

        with open(CONFIG_PATH, "w") as f:
            yaml.safe_dump(raw, f, sort_keys=False)
        app.state.rules = Rules.load()
        telemetry.log_event("risk_limits_saved", {
            "loss_cap_dollars": loss_cap_dollars,
            "kill_after_losses": kill_after_losses,
            "max_open_positions": max_open_positions,
        })
    except Exception as e:
        return TEMPLATES.TemplateResponse(
            "risk.html",
            {
                "request": request,
                "page_title": "Settings",
                "active_tab": "risk",
                "error": f"Save failed: {e}",
                **_risk_context(),
                **nav_context(),
            },
            status_code=400,
        )

    return TEMPLATES.TemplateResponse(
        "risk.html",
        {
            "request": request,
            "page_title": "Settings",
            "active_tab": "risk",
            "saved": True,
            **_risk_context(),
            **nav_context(),
        },
    )


# ─── Settings (landing) ─────────────────────────────────────────────────────

@app.get("/settings", response_class=HTMLResponse)
async def settings_landing(request: Request) -> RedirectResponse:
    return RedirectResponse(url="/settings/rules", status_code=303)


# ─── Settings: Broker ───────────────────────────────────────────────────────

@app.get("/settings/broker", response_class=HTMLResponse)
async def broker_view(request: Request) -> HTMLResponse:
    broker = app.state.broker
    return TEMPLATES.TemplateResponse(
        "broker.html",
        {
            "request": request,
            "page_title": "Settings",
            "active_tab": "broker",
            "broker": broker.health(),
            "broker_env": os.environ.get("BROKER", "mock"),
            **nav_context(),
        },
    )


@app.post("/settings/broker/reconnect", response_class=HTMLResponse)
async def broker_reconnect(request: Request) -> RedirectResponse:
    broker = app.state.broker
    if hasattr(broker, "disconnect"):
        broker.disconnect()
    await broker.connect()
    return RedirectResponse(url="/settings/broker", status_code=303)


@app.get("/settings/broker/dryrun", response_class=HTMLResponse)
async def broker_dryrun_form(request: Request) -> HTMLResponse:
    from .rules import default_friday_expiry
    return TEMPLATES.TemplateResponse(
        "broker_dryrun.html",
        {
            "request": request,
            "page_title": "Settings",
            "active_tab": "broker",
            "broker": app.state.broker.health(),
            "default_expiry": default_friday_expiry(),
            "result": None,
            "tp_results": None,
            "tp_meta": None,
            "stop_result": None,
            "stop_meta": None,
            "execute_result": None,
            **nav_context(),
        },
    )


@app.post("/settings/broker/dryrun", response_class=HTMLResponse)
async def broker_dryrun_submit(
    request: Request,
    ticker: str = Form(...),
    expiry: str = Form(...),
    strike: float = Form(...),
    right: str = Form(...),
    contracts: int = Form(1),
    order_type: str = Form("MKT"),
    limit_price: Optional[str] = Form(None),
    test_tp_ladder: Optional[str] = Form(None),
    tp_preset: str = Form("auto"),
    tp_split: str = Form("50_25_25"),
    stop_initial_pct: Optional[str] = Form(None),
) -> HTMLResponse:
    from .rules import default_friday_expiry
    from .orders import TradeIntent

    limit_f = None
    if limit_price and limit_price.strip():
        try:
            limit_f = float(limit_price)
        except ValueError:
            limit_f = None

    intent = TradeIntent(
        ticker=ticker.upper(),
        expiry=expiry,
        strike=strike,
        right=right.upper(),
        contracts=contracts,
        order_type=order_type.upper(),
        limit_price=limit_f,
        regime_tag="NORMAL",
        chain_role="solo",
        sector="dryrun",
    )

    broker = app.state.broker
    if not broker.health()["connected"] or broker.health()["backend"] != "ibkr":
        result = {
            "status": "error",
            "error": "Broker is not connected to IBKR. Set BROKER=ibkr_paper or ibkr_live and restart.",
        }
        tp_results = None
        tp_meta = None
    else:
        result = await broker.submit_entry(intent, force_dry_run=True)
        tp_results = None
        tp_meta = None

        # Optional TP ladder dry-run if checkbox is on + entry succeeded + LMT
        if (test_tp_ladder == "1" and result.get("status") == "dry_run_ok"
                and limit_f and limit_f > 0):
            from .rules import Rules
            rules: Rules = app.state.rules
            from datetime import date as _date
            expiry_d = _date.fromisoformat(expiry)
            dte = max(0, (expiry_d - _date.today()).days)
            ladder = rules.compute_ladder(tp_preset, dte, "NORMAL",
                                            split_choice=tp_split)
            splits = ladder.get("splits") or [0.5, 0.25, 0.25]

            tp_orders = []
            qtys = rules.split_qty(contracts, list(splits))
            for tier, (pct, sp, tp_qty) in enumerate(zip(
                    [ladder.get("tp1_pct"), ladder.get("tp2_pct"), ladder.get("tp3_pct")],
                    splits, qtys), 1):
                if not pct or sp <= 0 or tp_qty <= 0:
                    continue
                tp_price = round(limit_f * (1 + pct), 2)
                tp_orders.append({"tier": tier, "price": tp_price,
                                   "qty": tp_qty, "split": sp, "pct": pct})

            tp_meta = {
                "preset": tp_preset, "split": tp_split, "dte": dte,
                "ladder_label": ladder.get("label"),
                "configured": tp_orders,
                "entry_price": limit_f,
            }
            tp_results = await broker.dry_run_tp_orders(intent, tp_orders)

    # Optional Stop dry-run — same gating as TP ladder (LMT entry + checkbox)
    stop_result = None
    stop_meta = None
    if (test_tp_ladder == "1" and result.get("status") == "dry_run_ok"
            and limit_f and limit_f > 0):
        try:
            stop_pct_f = float(stop_initial_pct) if stop_initial_pct and stop_initial_pct.strip() else 0.0
        except ValueError:
            stop_pct_f = 0.0
        stop_price = round(limit_f * (1 + stop_pct_f / 100.0), 2)
        stop_meta = {
            "stop_initial_pct": stop_pct_f,
            "stop_price": stop_price,
            "entry_price": limit_f,
            "qty": contracts,
        }
        stop_result = await broker.dry_run_stop_order(intent, stop_price, contracts)

    return TEMPLATES.TemplateResponse(
        "broker_dryrun.html",
        {
            "request": request,
            "page_title": "Settings",
            "active_tab": "broker",
            "broker": broker.health(),
            "default_expiry": default_friday_expiry(),
            "result": result,
            "tp_results": tp_results,
            "tp_meta": tp_meta,
            "execute_result": None,
            "stop_result": stop_result,
            "stop_meta": stop_meta,
            "submitted": {
                "ticker": ticker.upper(), "expiry": expiry, "strike": strike,
                "right": right.upper(), "contracts": contracts,
                "order_type": order_type.upper(), "limit_price": limit_price,
                "test_tp_ladder": test_tp_ladder == "1",
                "tp_preset": tp_preset, "tp_split": tp_split,
                "stop_initial_pct": stop_initial_pct,
            },
            **nav_context(),
        },
    )


# ─── Live execution: place a real order via IBKR ────────────────────────────

@app.post("/settings/broker/execute", response_class=HTMLResponse)
async def broker_execute(
    request: Request,
    ticker: str = Form(...),
    expiry: str = Form(...),
    strike: float = Form(...),
    right: str = Form(...),
    contracts: int = Form(1),
    order_type: str = Form("MKT"),
    limit_price: Optional[str] = Form(None),
    confirm_execute: Optional[str] = Form(None),
) -> HTMLResponse:
    """Place a real order on IBKR. Requires:
      - broker connected, backend=ibkr
      - broker writable (BROKER_READONLY=0)
      - confirm_execute=yes (UI checkbox)
    Returns the broker_dryrun page with the live-execution result block.
    """
    from .rules import default_friday_expiry
    from .orders import TradeIntent

    broker = app.state.broker
    health = broker.health()

    limit_f = None
    if limit_price and limit_price.strip():
        try:
            limit_f = float(limit_price)
        except ValueError:
            limit_f = None

    submitted = {
        "ticker": ticker.upper(), "expiry": expiry, "strike": strike,
        "right": right.upper(), "contracts": contracts,
        "order_type": order_type.upper(), "limit_price": limit_price,
        "test_tp_ladder": False, "tp_preset": "auto", "tp_split": "50_25_25",
        "stop_initial_pct": None,
    }

    def render(execute_result):
        return TEMPLATES.TemplateResponse(
            "broker_dryrun.html",
            {
                "request": request,
                "page_title": "Settings",
                "active_tab": "broker",
                "broker": broker.health(),
                "default_expiry": default_friday_expiry(),
                "result": None,
                "tp_results": None, "tp_meta": None,
                "stop_result": None, "stop_meta": None,
                "execute_result": execute_result,
                "submitted": submitted,
                **nav_context(),
            },
        )

    if confirm_execute != "yes":
        return render({"status": "error",
                        "error": "Missing execute confirmation checkbox."})
    if not health["connected"] or health["backend"] != "ibkr":
        return render({"status": "error",
                        "error": "Broker not connected to IBKR."})
    if health.get("readonly"):
        return render({"status": "error",
                        "error": "Broker is in read-only mode. Restart server with "
                                  "BROKER_READONLY=0 to enable live execution."})

    intent = TradeIntent(
        ticker=ticker.upper(), expiry=expiry, strike=strike,
        right=right.upper(), contracts=contracts,
        order_type=order_type.upper(), limit_price=limit_f,
        regime_tag="MANUAL", chain_role="solo", sector="manual",
    )

    try:
        result = await broker.submit_entry(intent)
    except PermissionError as e:
        result = {"status": "error", "error": str(e)}
    except Exception as e:
        result = {"status": "error",
                  "error": f"submit_entry raised: {type(e).__name__}: {e}"}

    return render(result)


# ─── Settings: Event Log ────────────────────────────────────────────────────

@app.get("/settings/logs", response_class=HTMLResponse)
@app.get("/logs", response_class=HTMLResponse)
async def logs_view(request: Request) -> HTMLResponse:
    events = telemetry.tail_events(200)
    return TEMPLATES.TemplateResponse(
        "logs.html",
        {
            "request": request,
            "page_title": "Settings",
            "active_tab": "logs",
            "events": events,
            **nav_context(),
        },
    )


# ─── Settings: Kill Switch ──────────────────────────────────────────────────

@app.get("/settings/kill", response_class=HTMLResponse)
@app.get("/kill", response_class=HTMLResponse)
async def kill_view(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        "kill.html",
        {
            "request": request,
            "page_title": "Settings",
            "active_tab": "kill",
            **nav_context(),
        },
    )


@app.post("/settings/kill", response_class=HTMLResponse)
@app.post("/kill", response_class=HTMLResponse)
async def kill_toggle(
    request: Request,
    action: str = Form(...),  # 'kill_on', 'kill_off', 'pause_on', 'pause_off'
) -> RedirectResponse:
    if action == "kill_on":
        gates.toggle_kill(True)
        telemetry.log_event("kill_switch", {"action": "on"})
    elif action == "kill_off":
        gates.toggle_kill(False)
        telemetry.log_event("kill_switch", {"action": "off"})
    elif action == "pause_on":
        gates.toggle_pause(True)
        telemetry.log_event("pause_switch", {"action": "on"})
    elif action == "pause_off":
        gates.toggle_pause(False)
        telemetry.log_event("pause_switch", {"action": "off"})
    return RedirectResponse(url="/settings/kill", status_code=303)


# ─── WebSocket: live event stream ───────────────────────────────────────────

@app.websocket("/ws/events")
async def ws_events(ws: WebSocket) -> None:
    await ws.accept()
    try:
        while True:
            # In live mode this would stream broker events. For now just heartbeat.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass


# ─── JSON probes (htmx partial fragments) ───────────────────────────────────

@app.get("/api/today", response_class=JSONResponse)
async def api_today() -> dict:
    return state.daily_metric()


def main() -> None:
    import uvicorn
    uvicorn.run(
        "automation.server:app",
        host="127.0.0.1",
        port=8765,
        reload=False,
    )


if __name__ == "__main__":
    main()
