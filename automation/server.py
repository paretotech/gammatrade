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

from . import state, telemetry, gates, pregame, triggers as triggers_mod, analysis, journal, eod_analysis, secrets as user_secrets
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
    # Load user-managed API keys from disk into os.environ before any
    # module that reads them runs. Shell-exported values always win.
    user_secrets.load_into_env()
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
            ("/journal", "Journal", "edit-3"),
            ("/settings", "Settings", "sliders"),
        ],
        "kill_active": gates.kill_active(),
        "pause_active": gates.pause_active(),
    }


# ─── Dashboard ──────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    positions = state.open_positions()
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
            "open_count": len(positions),
            "open_value": total_open_value,
            "today": metric,
            "chains": chains,
            "waiting_triggers": waiting_triggers,
            **nav_context(),
        },
    )


# ─── New Entry ──────────────────────────────────────────────────────────────

@app.get("/api/decision-card", response_class=JSONResponse)
async def decision_card_api(
    ticker: str,
    strike: Optional[float] = None,
    right: str = "C",
    direction: Optional[str] = None,
) -> JSONResponse:
    """JSON payload powering the pre-trade decision card. The entry form
    calls this on every relevant input change."""
    from . import analytics as _a
    try:
        rules = app.state.rules
        cap = rules.daily_loss_cap()
    except Exception:
        cap = 1000.0
    return JSONResponse(_a.pretrade_decision_card(
        ticker=ticker, strike=strike, right=right,
        direction=direction, daily_loss_cap=cap,
    ))


@app.get("/entries/new", response_class=HTMLResponse)
async def new_entry_form(
    request: Request,
    ticker: Optional[str] = None,
    level: Optional[float] = None,
    direction: Optional[str] = None,
    sector: Optional[str] = None,
    note_date: Optional[str] = None,
    extra: Optional[str] = None,
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

    prefill = {
        "ticker": ticker_u,
        "level": level,
        "direction": direction,
        "sector": sector,
        "note_date": note_date,
        "extras_json": _json.dumps(extras_list),
        "default_expiry": default_friday_expiry(),
        "default_strike": default_strike,
        "default_right": "P" if direction in ("below", "hold_below", "bounce_below") else "C",
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
    # We route both paths through analyze_pregame() so the deterministic
    # setup-eval merge runs against the CURRENT levels snapshot on every
    # render — cached pregames pick up newly-published levels without
    # re-analysis.
    if run_analysis:
        ai_result = analysis.analyze_pregame(parsed, note_date, force=False)
    else:
        cached = analysis.get_cached(note_date)
        if cached:
            if cached.get("analysis"):
                cached = {**cached, "analysis": analysis._merge_setup_eval(cached["analysis"], parsed)}
            ai_result = {**cached, "cached": True}
        else:
            ai_result = None

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
            "equity":   _a.equity_curve(range_key=rng),
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


@app.get("/trades/{intent_id}", response_class=HTMLResponse)
async def trade_detail(request: Request, intent_id: str) -> HTMLResponse:
    """Full lifecycle view of a single trade: intent + every fill + MFE/MAE
    + pregame mention (if any) + journal mention (if any)."""
    from . import analytics as _a

    # Match closed_trades' shape for this intent so we get computed fields
    # (realized_pnl, capture %, tp_fills, MFE/MAE percentages, etc.).
    all_closed = _a.closed_trades(limit=5000, range_key="all")
    trade = next((t for t in all_closed if t["intent_id"] == intent_id), None)

    raw_intent = state.get_intent(intent_id)
    if trade is None and raw_intent is None:
        return RedirectResponse(url="/analytics/trades", status_code=303)

    # All fills (raw), regardless of whether the trade is fully closed.
    with state.connect() as conn:
        fills = [dict(r) for r in conn.execute(
            "SELECT ts, side, contracts, price, is_entry, tp_tier "
            "FROM fills WHERE intent_id=? ORDER BY ts ASC, rowid ASC",
            (intent_id,),
        ).fetchall()]

    # Pregame mention: was this ticker in the day's pregame analysis?
    pregame_mention = None
    entry_ts = (trade and trade.get("first_entry_ts")) or (raw_intent and raw_intent.get("created_at"))
    entry_date = entry_ts[:10] if entry_ts else None
    if entry_date:
        from .analysis import get_cached as _get_pregame
        cached = _get_pregame(entry_date)
        if cached and cached.get("status") == "ok" and cached.get("analysis"):
            ticker = (trade or raw_intent or {}).get("ticker")
            picks = cached["analysis"].get("picks") or []
            for p in picks:
                if (p.get("ticker") or "").upper() == (ticker or "").upper():
                    pregame_mention = {"date": entry_date, **p}
                    break

    # Journal mention: was there a journal entry for that date?
    journal_mention = None
    if entry_date:
        je = journal.load(entry_date)
        if je and any(je.get(k, "").strip() for k in
                      ("plan_adherence", "wins", "losses", "lessons", "mfe_gaps", "notes")):
            journal_mention = {"date": entry_date, **je}

    return TEMPLATES.TemplateResponse(
        "trade_detail.html",
        {
            "request":         request,
            "page_title":      "Trade detail",
            "intent_id":       intent_id,
            "trade":           trade,           # None if still open
            "raw_intent":      raw_intent,      # always present
            "fills":           fills,
            "pregame_mention": pregame_mention,
            "journal_mention": journal_mention,
            **nav_context(),
        },
    )


@app.get("/trades/{intent_id}/chart.json", response_class=JSONResponse)
async def trade_chart_data(intent_id: str) -> JSONResponse:
    """Option-price lifecycle data for the per-trade detail chart.

    Returns 1-minute close bars for the option contract spanning the period
    from market open on the entry day through expiry close, plus marker
    points for each fill (entry, TP1/2/3, stop, expiry) so the front-end
    can overlay them on the line.

    Source: cached Polygon parquet bars (data/bars/options/). Returns 404
    if the contract has no cached bars yet — user should run the Polygon
    backfill via Settings → API keys.
    """
    from datetime import date as _date
    import pandas as pd

    raw_intent = state.get_intent(intent_id)
    if raw_intent is None:
        return JSONResponse({"error": "intent not found"}, status_code=404)

    # Build OCC symbol from the intent's contract identity
    from src.contract_symbols import build_occ_symbol
    from src import bars_store

    try:
        expiry_d = _date.fromisoformat(str(raw_intent["expiry"])[:10])
        occ = build_occ_symbol(
            ticker=raw_intent["ticker"],
            expiry=expiry_d,
            option_type=raw_intent["right"],
            strike=float(raw_intent["strike"]),
        )
    except (KeyError, ValueError, TypeError) as exc:
        return JSONResponse(
            {"error": f"could not derive OCC symbol: {exc}"}, status_code=400
        )

    try:
        df = bars_store.load_option_bars(occ)
    except FileNotFoundError:
        return JSONResponse(
            {
                "error": "no cached bars for this contract",
                "occ":   occ,
                "hint":  "run the Polygon backfill on Settings → API keys",
            },
            status_code=404,
        )

    if df.empty:
        return JSONResponse({"error": "empty bars file", "occ": occ}, status_code=404)

    # Pull fills so the front end can overlay markers
    with state.connect() as conn:
        fills = [dict(r) for r in conn.execute(
            "SELECT ts, side, contracts, price, is_entry, tp_tier "
            "FROM fills WHERE intent_id=? ORDER BY ts ASC, rowid ASC",
            (intent_id,),
        ).fetchall()]

    # Series: close-by-minute. Each bar gets a sequential `i` index so the
    # front-end can plot on a linear axis indexed by trading-minute
    # position — that collapses overnight/weekend gaps because the bars
    # list is RTH-only (Polygon doesn't emit aggregates after hours).
    # We keep `t` (real epoch ms) so tooltips can show the actual time.
    df = df.dropna(subset=["t", "c"]).sort_values("t").reset_index(drop=True)
    bars_payload = [
        {"i": i, "t": int(row["t"]), "c": float(row["c"])}
        for i, row in enumerate(df[["t", "c"]].to_dict(orient="records"))
    ]

    # Helper: nearest bar-index for an arbitrary real timestamp (epoch ms).
    # Used to place fill markers on the compressed axis even when the fill
    # ticked between bars or during a closed-hours minute Polygon skipped.
    import bisect as _bisect
    _bar_ts = df["t"].astype("int64").tolist()
    def _t_to_i(real_t_ms: int) -> int:
        if not _bar_ts:
            return 0
        j = _bisect.bisect_left(_bar_ts, real_t_ms)
        if j == 0:
            return 0
        if j >= len(_bar_ts):
            return len(_bar_ts) - 1
        # snap to nearer neighbor
        return j if abs(_bar_ts[j] - real_t_ms) < abs(_bar_ts[j-1] - real_t_ms) else j-1

    # Compute the chart's x-axis bounds. The lower bound is the first
    # entry fill. The upper bound goes to expiry close IF the bars cover
    # most of the lifecycle, but caps at last_meaningful_event + small
    # padding when bars are sparse — otherwise a one-day-active contract
    # produces 14 days of empty whitespace which makes the markers
    # impossible to read.
    from zoneinfo import ZoneInfo as _ZI
    _ET = _ZI("America/New_York")

    # Lower bound: first entry fill if we have one, otherwise the first bar.
    entry_fills = [f for f in fills if f["is_entry"] == 1]
    if entry_fills:
        try:
            entry_dt = datetime.fromisoformat(entry_fills[0]["ts"][:19]).replace(tzinfo=_ET)
            x_min = int(entry_dt.timestamp() * 1000)
        except ValueError:
            x_min = int(df["t"].iloc[0])
    else:
        x_min = int(df["t"].iloc[0])

    # Upper bound: chart shows entry → close-of-day on the day of the
    # last exit fill (4 PM ET). We intentionally hide everything past
    # that — the chart's job is reviewing YOUR execution. Capping at
    # session close (rather than +Nh from exit) is the natural unit:
    # the trading day either was, or wasn't, the day you closed out.
    #
    # For open trades (no exit fill yet) we extend to the last cached
    # bar so the chart tracks the live position.
    expiry_close_dt = datetime.combine(expiry_d, datetime.min.time(), tzinfo=_ET).replace(hour=16)
    expiry_close_ms = int(expiry_close_dt.timestamp() * 1000)
    last_bar_ms     = int(df["t"].iloc[-1])

    last_exit_fill_ms = None
    last_exit_eod_ms  = None
    for f in fills:
        if f.get("is_entry") == 1:
            continue
        try:
            fdt = datetime.fromisoformat(f["ts"][:19]).replace(tzinfo=_ET)
            ms  = int(fdt.timestamp() * 1000)
        except (KeyError, TypeError, ValueError):
            continue
        if last_exit_fill_ms is None or ms > last_exit_fill_ms:
            last_exit_fill_ms = ms
            eod_dt = datetime.combine(
                fdt.date(), datetime.min.time(), tzinfo=_ET,
            ).replace(hour=16)
            last_exit_eod_ms = int(eod_dt.timestamp() * 1000)

    if last_exit_eod_ms is not None:
        x_max = min(expiry_close_ms, last_exit_eod_ms)
    else:
        pad_ms = 4 * 60 * 60 * 1000   # 4 hours for open trades
        x_max  = min(expiry_close_ms, last_bar_ms + pad_ms)

    # Index-based axis bounds for the compressed (trading-time) view.
    # i_max = index of the last bar whose real timestamp is ≤ x_max.
    # We want strict "no bars past EOD" so the next trading session's
    # bars (across a weekend or holiday) don't sneak onto the axis.
    i_min = 0
    j = _bisect.bisect_right(_bar_ts, x_max)  # first bar strictly past x_max
    i_max = max(0, j - 1)

    # Markers: each fill at its timestamp. Fill timestamps are stored as
    # naive ET (broker local time) — localize explicitly so the host
    # timezone doesn't change the result.
    def _to_epoch_ms(ts: str) -> Optional[int]:
        if not ts:
            return None
        try:
            dt = datetime.fromisoformat(ts[:19])
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_ET)
        return int(dt.timestamp() * 1000)

    # Marker classification:
    #   entry          — is_entry == 1
    #   stop           — any exit whose price is BELOW entry OR BELOW the
    #                    most recent prior TP exit (loss-taking or
    #                    giving-back-gains exit). This rule overrides any
    #                    tp_tier the broker tagged, because a "TP3" fill
    #                    that lands below the TP2 fill is actually a stop.
    #   tp1 / tp2 / tp3 — broker-tagged take-profit fill that's also at a
    #                    higher price than entry AND than prior TPs.
    #   close          — non-TP exit at a price >= entry and >= prior TP
    #                    (discretionary profitable exit that didn't carry
    #                    a TP tier tag).
    #
    # Fills are walked chronologically so prev_tp_price reflects the last
    # CLASSIFIED TP fill (skipping any reclassified as stops).
    entry_price   = None
    prev_tp_price = None
    markers = []
    for f in fills:
        t_ms = _to_epoch_ms(f["ts"])
        if t_ms is None:
            continue
        price = float(f["price"])
        if f["is_entry"] == 1:
            kind, label = "entry", "Entry"
            if entry_price is None:
                entry_price = price
        else:
            # Compare against the higher of entry and prior TP — that's
            # the floor below which the exit is "giving back".
            refs = [v for v in (entry_price, prev_tp_price) if v is not None]
            floor = max(refs) if refs else None
            if floor is not None and price < floor:
                kind, label = "stop", "Stop"
            elif f["tp_tier"]:
                tier = int(f["tp_tier"])
                kind, label = f"tp{tier}", f"TP{tier}"
                prev_tp_price = price
            else:
                kind, label = "close", "Close"
        markers.append({
            "i":         _t_to_i(t_ms),
            "t":         t_ms,
            "y":         price,
            "kind":      kind,
            "label":     label,
            "side":      f["side"],
            "contracts": f["contracts"],
        })

    # MFE marker — single dot at (timestamp, high) of the bar that
    # printed the max favorable price WHILE the trade was active.
    # Bounded by x_min..(last_exit or last_bar), so we never plot a
    # marker in the post-exit window the chart no longer shows.
    mfe_marker = None
    if "h" in df.columns and not df["h"].dropna().empty:
        mfe_upper = last_exit_fill_ms if last_exit_fill_ms is not None else last_bar_ms
        mfe_df = df[(df["t"] >= x_min) & (df["t"] <= mfe_upper)].dropna(subset=["h"])
        if not mfe_df.empty:
            idx = mfe_df["h"].idxmax()
            _mfe_t = int(mfe_df.loc[idx, "t"])
            mfe_marker = {
                "i":     _t_to_i(_mfe_t),
                "t":     _mfe_t,
                "y":     float(mfe_df.loc[idx, "h"]),
                "kind":  "mfe",
                "label": "MFE peak",
            }

    # Expiry marker — only included when expiry close is within (or
    # close to) the visible window. When the contract died days before
    # expiry we omit the marker so it doesn't pin the axis open; the
    # expiry date is already shown in the page header.
    expiry_marker = None
    if expiry_close_ms <= x_max:
        expiry_marker = {"t": expiry_close_ms, "y": float(df["c"].iloc[-1])}

    # Capture summary stats — rendered as a small text block in the
    # chart's top-right corner. Computed from the same fills + intent
    # MFE columns we already have, so the chart route is self-contained.
    realized_pct  = None
    mfe_in_pct    = None
    mfe_exp_pct   = None
    capture_pct   = None
    days_to_peak  = None  # +N days from last exit to MFE peak when MFE is post-exit

    buy_qty   = sum(f["contracts"] for f in fills if f.get("is_entry") == 1)
    sell_qty  = sum(f["contracts"] for f in fills if f.get("is_entry") != 1)
    buy_cost  = sum(f["price"] * f["contracts"] for f in fills if f.get("is_entry") == 1)
    sell_rev  = sum(f["price"] * f["contracts"] for f in fills if f.get("is_entry") != 1)

    if buy_qty > 0:
        avg_entry = buy_cost / buy_qty
        if sell_qty > 0:
            avg_exit = sell_rev / sell_qty
            realized_pct = (avg_exit / avg_entry - 1) * 100

        mfe_in_price  = raw_intent.get("mfe_in_trade_price")
        mfe_exp_price = raw_intent.get("mfe_to_expiry_price")
        if mfe_in_price is not None:
            mfe_in_pct  = (mfe_in_price  / avg_entry - 1) * 100
        if mfe_exp_price is not None:
            mfe_exp_pct = (mfe_exp_price / avg_entry - 1) * 100

        if mfe_in_pct is not None and mfe_in_pct > 0 and realized_pct is not None:
            capture_pct = max(0, min(100, realized_pct / mfe_in_pct * 100))

    # If the to-expiry MFE is meaningfully higher than the in-trade MFE,
    # the user "missed" a post-exit move. Surface how many days after
    # the last exit the peak occurred — but only when the bars file has
    # data that lets us locate it.
    if (mfe_in_pct is not None and mfe_exp_pct is not None
            and mfe_exp_pct > mfe_in_pct + 0.5  # ignore noise
            and last_exit_fill_ms is not None):
        if "h" in df.columns:
            post_df = df[df["t"] > last_exit_fill_ms].dropna(subset=["h"])
            if not post_df.empty:
                peak_t  = int(post_df.loc[post_df["h"].idxmax(), "t"])
                days_to_peak = (peak_t - last_exit_fill_ms) / (1000 * 86400)

    # Day-boundary ticks for the compressed x-axis. Walk the bars and emit
    # one tick at the first bar whose ET date differs from the previous —
    # that's the start of a new trading session in compressed time.
    axis_ticks = []
    prev_date = None
    for i, real_t in enumerate(_bar_ts):
        d = datetime.fromtimestamp(real_t / 1000, tz=_ET).date()
        if d != prev_date:
            axis_ticks.append({"i": i, "label": d.strftime("%b %d")})
            prev_date = d

    return JSONResponse({
        "occ":           occ,
        "ticker":        raw_intent["ticker"],
        "strike":        float(raw_intent["strike"]),
        "right":         raw_intent["right"],
        "expiry":        str(expiry_d),
        # Compressed (RTH-only) axis — bar indices, no closed-hours gaps.
        "i_min":         i_min,
        "i_max":         i_max,
        "axis_ticks":    axis_ticks,      # [{i, label}] at each new trading day
        # Real-time bounds kept for reference but not used by the chart now.
        "x_min":         x_min,
        "x_max":         x_max,
        "entry_price":   entry_price,
        "bars":          bars_payload,    # [{i, t, c}]
        "markers":       markers,         # [{i, t, y, kind, label, ...}]
        "mfe_marker":    mfe_marker,
        "expiry_marker": expiry_marker,
        "stats": {
            "realized_pct":  realized_pct,
            "mfe_in_pct":    mfe_in_pct,
            "mfe_exp_pct":   mfe_exp_pct,
            "capture_pct":   capture_pct,
            "days_to_peak":  days_to_peak,
        },
    })


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


@app.post("/analytics/trades/import-ibkr", response_class=HTMLResponse)
async def analytics_trades_import_ibkr(
    request: Request,
    ibkr_csv: UploadFile = File(...),
) -> RedirectResponse:
    """Accept an IBKR Flex Transaction History CSV and run the IBKR
    ingestion pipeline.

    Saves to data/ibkr_exports/ then runs scripts/import_ibkr_to_db.py
    in a detached subprocess. The IBKR pipeline writes directly to the
    automation SQLite — it does NOT touch data/master_trade_log.csv,
    which is TD-shaped and not suitable for IBKR data.
    """
    import subprocess
    from pathlib import Path as _Path

    REPO = _Path(__file__).resolve().parent.parent
    exports_dir = REPO / "data" / "ibkr_exports"
    exports_dir.mkdir(parents=True, exist_ok=True)

    filename = (ibkr_csv.filename or "ibkr_import.csv").strip()
    if not filename.lower().endswith(".csv"):
        return RedirectResponse(
            url=f"/analytics/trades?import_error={urlquote('Only .csv files accepted')}",
            status_code=303,
        )

    safe_name = filename.replace("/", "_").replace("..", "")
    dest = exports_dir / safe_name
    contents = await ibkr_csv.read()
    if len(contents) == 0:
        return RedirectResponse(
            url=f"/analytics/trades?import_error={urlquote('Uploaded file was empty')}",
            status_code=303,
        )
    if len(contents) > 5 * 1024 * 1024:
        return RedirectResponse(
            url=f"/analytics/trades?import_error={urlquote('File too large (>5MB)')}",
            status_code=303,
        )
    dest.write_bytes(contents)
    telemetry.log_event("ibkr_import_uploaded", {
        "filename": safe_name, "size_bytes": len(contents),
    })

    log_path = REPO / "data" / "ibkr_exports" / "_import.log"
    cmd = (
        f"cd {REPO} && "
        f"python3 -u scripts/import_ibkr_to_db.py --dir data/ibkr_exports"
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


@app.get("/analytics/cohort", response_class=HTMLResponse)
async def analytics_cohort(
    request: Request,
    range: Optional[str] = None,
) -> HTMLResponse:
    """Cohort benchmark — user metrics vs the reference_trades cohort."""
    from . import analytics as _a
    rng = _norm_range(range)
    return TEMPLATES.TemplateResponse(
        "analytics_cohort.html",
        {
            "request":    request,
            "page_title": "Analytics",
            "active_tab": "cohort",
            "tab_slug":   "cohort",
            "range_key":  rng,
            "data":       _a.cohort_benchmark(range_key=rng),
            **nav_context(),
        },
    )


@app.get("/analytics/chains", response_class=HTMLResponse)
async def analytics_chains(
    request: Request,
    range: Optional[str] = None,
    roll: Optional[int] = None,
) -> HTMLResponse:
    """Chain analysis — groups trades on same ticker+side where each new
    leg's BUY happens within `roll` minutes of an EXIT FILL of an earlier
    leg, plus monotonic strike progression (calls chase up / puts chase
    down)."""
    from . import analytics as _a
    rng = _norm_range(range)
    m = roll if roll in (10, 30, 60, 120, 240) else 60
    return TEMPLATES.TemplateResponse(
        "analytics_chains.html",
        {
            "request":         request,
            "page_title":      "Analytics",
            "active_tab":      "chains",
            "tab_slug":        "chains",
            "range_key":       rng,
            "roll_minutes":    m,
            "data":            _a.chain_analysis(range_key=rng, max_roll_minutes=m),
            **nav_context(),
        },
    )


@app.get("/analytics/ladders", response_class=HTMLResponse)
async def analytics_ladders(
    request: Request,
    range: Optional[str] = None,
    tp1: Optional[float] = None,
    tp2: Optional[float] = None,
    tp3: Optional[float] = None,
    s1: Optional[float] = None,
    s2: Optional[float] = None,
    s3: Optional[float] = None,
    stop: Optional[float] = None,
    trail: Optional[int] = None,
    simulate: Optional[int] = None,
) -> HTMLResponse:
    """TP-ladder analysis. With `simulate=1` and ladder params, also
    replays the trades against the supplied ladder and renders an
    actual-vs-simulated comparison."""
    from . import analytics as _a
    rng = _norm_range(range)
    sim_data = None
    sim_form = {
        "tp1": tp1 if tp1 is not None else 20.0,
        "tp2": tp2 if tp2 is not None else 30.0,
        "tp3": tp3 if tp3 is not None else 40.0,
        "s1":  s1  if s1  is not None else 0.5,
        "s2":  s2  if s2  is not None else 0.25,
        "s3":  s3  if s3  is not None else 0.25,
        "stop": stop if stop is not None else 0.0,
        "trail": bool(trail),
    }
    if simulate:
        sim_data = _a.simulate_ladder(
            range_key=rng,
            tp1_pct=sim_form["tp1"], tp2_pct=sim_form["tp2"], tp3_pct=sim_form["tp3"],
            split1=sim_form["s1"], split2=sim_form["s2"], split3=sim_form["s3"],
            init_stop_pct=sim_form["stop"] if sim_form["stop"] > 0 else None,
            trail_after_tp1=sim_form["trail"],
        )
    return TEMPLATES.TemplateResponse(
        "analytics_ladders.html",
        {
            "request":    request,
            "page_title": "Analytics",
            "active_tab": "ladders",
            "tab_slug":   "ladders",
            "range_key":  rng,
            "data":       _a.ladder_analysis(range_key=rng),
            "sim_form":   sim_form,
            "sim_data":   sim_data,
            **nav_context(),
        },
    )


@app.get("/analytics/winners", response_class=HTMLResponse)
async def analytics_winners(
    request: Request,
    range: Optional[str] = None,
) -> HTMLResponse:
    """Winner profile — side-by-side comparison of winners vs losers
    (and top-quartile vs bottom-quartile by realized %) across every
    per-trade feature we have."""
    from . import analytics as _a
    rng = _norm_range(range)
    return TEMPLATES.TemplateResponse(
        "analytics_winners.html",
        {
            "request":    request,
            "page_title": "Analytics",
            "active_tab": "winners",
            "tab_slug":   "winners",
            "range_key":  rng,
            "data":       _a.winner_profile(range_key=rng),
            **nav_context(),
        },
    )


@app.get("/analytics/levels", response_class=HTMLResponse)
async def analytics_levels(
    request: Request,
    range: Optional[str] = None,
    proximity: Optional[float] = None,
) -> HTMLResponse:
    """Connect published support/resistance levels to trade outcomes:
      - position-class win rates (ATH break, near resistance/support, mid-range)
      - per-(ticker, level) performance
      - ATH-break detail list
    """
    from . import analytics as _a
    rng  = _norm_range(range)
    prox = proximity if proximity in (0.3, 0.5, 1.0, 2.0) else 0.5
    return TEMPLATES.TemplateResponse(
        "analytics_levels.html",
        {
            "request":      request,
            "page_title":   "Analytics",
            "active_tab":   "levels",
            "tab_slug":     "levels",
            "range_key":    rng,
            "proximity":    prox,
            "data":         _a.levels_analysis(range_key=rng, proximity_pct=prox),
            "losers_data":  _a.loser_levels_analysis(range_key=rng, proximity_pct=prox),
            **nav_context(),
        },
    )


@app.get("/analytics/leakage", response_class=HTMLResponse)
async def analytics_leakage(
    request: Request,
    range: Optional[str] = None,
    lookback: Optional[int] = None,
) -> HTMLResponse:
    """Two rollups in one page:
      - MFE-vs-realized scatter (one dot per closed trade)
      - Stop-discipline aggregate (recovery stats per Stop fill)
    """
    from . import analytics as _a
    rng = _norm_range(range)
    lb  = lookback if lookback in (30, 60, 120, 240) else 60
    return TEMPLATES.TemplateResponse(
        "analytics_leakage.html",
        {
            "request":          request,
            "page_title":       "Analytics",
            "active_tab":       "leakage",
            "tab_slug":         "leakage",
            "range_key":        rng,
            "lookback_minutes": lb,
            "scatter":          _a.leakage_scatter(range_key=rng),
            "discipline":       _a.stop_discipline(range_key=rng, lookback_minutes=lb),
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


# ─── Journal ─────────────────────────────────────────────────────────────

@app.get("/journal", response_class=HTMLResponse)
async def journal_index(request: Request) -> HTMLResponse:
    entries = journal.list_entries(limit=200)
    today_str = date.today().isoformat()
    today_in_list = any(e["date"] == today_str for e in entries)
    return TEMPLATES.TemplateResponse(
        "journal_index.html",
        {
            "request":       request,
            "page_title":    "Journal",
            "entries":       entries,
            "today":         today_str,
            "today_in_list": today_in_list,
            **nav_context(),
        },
    )


@app.get("/journal/{entry_date}", response_class=HTMLResponse)
async def journal_view(request: Request, entry_date: str) -> HTMLResponse:
    try:
        date.fromisoformat(entry_date)
    except ValueError:
        return RedirectResponse(url="/journal", status_code=303)
    entry     = journal.load(entry_date) or {"date": entry_date}
    summary   = journal.day_summary(entry_date)
    adherence = journal.adherence_for_day(entry_date)
    # EOD review is optional — only render the panel if a cached run exists;
    # otherwise the template shows the "Run EOD review" button.
    eod_review = eod_analysis.get_cached(entry_date)
    if eod_review:
        eod_review = {**eod_review, "cached": True}
    api_key_set = bool(os.environ.get("ANTHROPIC_API_KEY"))
    return TEMPLATES.TemplateResponse(
        "journal_entry.html",
        {
            "request":     request,
            "page_title":  f"Journal · {entry_date}",
            "entry_date":  entry_date,
            "entry":       entry,
            "summary":     summary,
            "adherence":   adherence,
            "eod_review":  eod_review,
            "api_key_set": api_key_set,
            **nav_context(),
        },
    )


@app.post("/journal/{entry_date}/analyze-eod", response_class=HTMLResponse)
async def journal_analyze_eod(request: Request, entry_date: str) -> RedirectResponse:
    """Trigger a Claude EOD review for this date. Optional — only runs when
    explicitly clicked. Requires ANTHROPIC_API_KEY; otherwise the cached
    result will be a "no_api_key" stub.
    """
    try:
        date.fromisoformat(entry_date)
    except ValueError:
        return RedirectResponse(url="/journal", status_code=303)
    entry     = journal.load(entry_date) or {"date": entry_date}
    summary   = journal.day_summary(entry_date)
    adherence = journal.adherence_for_day(entry_date)
    eod_analysis.analyze_eod(entry_date, summary, adherence, entry, force=True)
    return RedirectResponse(url=f"/journal/{entry_date}#eod-review", status_code=303)


@app.post("/journal/{entry_date}", response_class=HTMLResponse)
async def journal_save(
    request: Request,
    entry_date: str,
    plan_adherence: str = Form(""),
    wins:           str = Form(""),
    losses:         str = Form(""),
    lessons:        str = Form(""),
    mfe_gaps:       str = Form(""),
    notes:          str = Form(""),
) -> RedirectResponse:
    try:
        date.fromisoformat(entry_date)
    except ValueError:
        return RedirectResponse(url="/journal", status_code=303)
    journal.save({
        "date":           entry_date,
        "plan_adherence": plan_adherence,
        "wins":           wins,
        "losses":         losses,
        "lessons":        lessons,
        "mfe_gaps":       mfe_gaps,
        "notes":          notes,
    })
    return RedirectResponse(url=f"/journal/{entry_date}?saved=1", status_code=303)


# ─── Settings (landing) ─────────────────────────────────────────────────────

@app.get("/settings", response_class=HTMLResponse)
async def settings_landing(request: Request) -> RedirectResponse:
    return RedirectResponse(url="/settings/rules", status_code=303)


# ─── Settings: API keys ─────────────────────────────────────────────────────
#
# Generic per-key endpoints so adding a new managed secret is just a config
# entry in automation/secrets.py:MANAGED — no new routes needed.

def _mfe_backfill_status() -> dict:
    """How many closed trades have MFE/MAE backfilled vs total."""
    with state.connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM trade_intents WHERE status IN ('filled','closed')"
        ).fetchone()[0]
        done = conn.execute("""
            SELECT COUNT(*) FROM trade_intents
            WHERE status IN ('filled','closed')
              AND mfe_in_trade_price IS NOT NULL
              AND mfe_to_expiry_price IS NOT NULL
        """).fetchone()[0]
    pct = round(done / total * 100, 1) if total else 0
    return {"total": total, "done": done, "pct": pct, "pending": total - done}


def _backfill_log_tail(name: str, n: int = 12) -> str:
    p = Path(__file__).resolve().parent.parent / "data" / f"_{name}.log"
    if not p.exists():
        return ""
    try:
        lines = p.read_text().splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return ""


@app.get("/settings/keys", response_class=HTMLResponse)
async def settings_keys_view(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        "keys.html",
        {
            "request":           request,
            "page_title":        "Settings",
            "active_tab":        "keys",
            "keys":              user_secrets.status(),
            "key_meta":          _KEY_META,
            "mfe_backfill":      _mfe_backfill_status(),
            "mfe_backfill_log":  _backfill_log_tail("mfe_mae_backfill"),
            **nav_context(),
        },
    )


@app.post("/settings/backfill/mfe-mae", response_class=HTMLResponse)
async def settings_backfill_mfe_mae(request: Request) -> RedirectResponse:
    """Kick off the Polygon MFE/MAE backfill in a detached subprocess so
    the request returns immediately. Status is visible on this same page;
    log lines stream to data/_mfe_mae_backfill.log."""
    import subprocess
    REPO = Path(__file__).resolve().parent.parent
    log_path = REPO / "data" / "_mfe_mae_backfill.log"
    cmd = f"cd {REPO} && python3 -u scripts/backfill_mfe_mae.py"
    subprocess.Popen(
        ["bash", "-c", cmd],
        stdout=open(log_path, "ab"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return RedirectResponse(url="/settings/keys?backfill=started", status_code=303)


# Human-readable metadata for each managed secret. Kept here (not in
# secrets.py) because it's UI copy, not data layer concern.
_KEY_META: dict[str, dict] = {
    "anthropic_api_key": {
        "label":   "ANTHROPIC_API_KEY",
        "purpose": "Used by pregame analysis + EOD review.",
        "doc_url": "https://console.anthropic.com/settings/keys",
        "prefix_hint": "sk-ant-…",
    },
    "polygon_api_key": {
        "label":   "POLYGON_API_KEY",
        "purpose": "Used for MFE/MAE backfill and live option-price fetches via polygon.io.",
        "doc_url": "https://polygon.io/dashboard/api-keys",
        "prefix_hint": "(alphanumeric)",
    },
}


@app.post("/settings/keys/{key_name}", response_class=HTMLResponse)
async def settings_keys_save(
    request: Request,
    key_name: str,
    value: str = Form(""),
) -> RedirectResponse:
    if key_name not in dict(user_secrets.known_keys()):
        return RedirectResponse(url="/settings/keys", status_code=303)
    val = (value or "").strip()
    if val:
        # Empty submission is a no-op so a stray blank submit can't wipe a
        # working key — explicit deletion goes through /delete.
        user_secrets.save(key_name, val)
    return RedirectResponse(url=f"/settings/keys?saved={key_name}", status_code=303)


@app.post("/settings/keys/{key_name}/delete", response_class=HTMLResponse)
async def settings_keys_delete(request: Request, key_name: str) -> RedirectResponse:
    if key_name not in dict(user_secrets.known_keys()):
        return RedirectResponse(url="/settings/keys", status_code=303)
    user_secrets.clear(key_name)
    return RedirectResponse(url=f"/settings/keys?deleted={key_name}", status_code=303)


# ─── Settings: Broker ───────────────────────────────────────────────────────

@app.get("/settings/levels", response_class=HTMLResponse)
async def settings_levels_view(request: Request, q: Optional[str] = None) -> HTMLResponse:
    """Per-ticker support/resistance level browser. Each row is the LATEST
    snapshot for that ticker; the optional `q` query filters by ticker prefix.
    Rolls in a "stale check" against the most recent option-bar price for the
    ticker (or last-known underlying close) so the user can see which tickers
    need a fresh level publish."""
    from . import levels as _lv
    snaps = _lv.latest_for_all()

    if q:
        ql = q.strip().upper()
        snaps = [s for s in snaps if ql in s.ticker]

    # Last cached underlying close per ticker — used to flag stale snapshots
    # whose published levels no longer bracket the current price. Falls back
    # to the snapshot's own current_price when no underlying bars are cached.
    try:
        from src import bars_store
    except Exception:
        bars_store = None

    rows = []
    for s in snaps:
        last_close = s.current_price
        if bars_store is not None:
            try:
                df = bars_store.load_underlying_bars(s.ticker)
                last_close = float(df["c"].dropna().iloc[-1])
            except (FileNotFoundError, IndexError, ValueError, KeyError):
                pass
        stale_info = (
            _lv.needs_refresh(s, last_close)
            if last_close is not None
            else {"stale": False, "reasons": []}
        )
        rows.append({
            "ticker":        s.ticker,
            "asof_ts":       s.asof_ts,
            "current_price": s.current_price,
            "last_close":    last_close,
            "levels_below":  s.levels_below,
            "levels_above":  s.levels_above,
            "source":        s.source,
            "note":          s.note,
            "stale":         stale_info["stale"],
            "stale_reasons": stale_info["reasons"],
        })

    return TEMPLATES.TemplateResponse(
        "settings_levels.html",
        {
            "request":     request,
            "page_title":  "Settings",
            "active_tab":  "levels",
            "rows":        rows,
            "q":           q or "",
            **nav_context(),
        },
    )


@app.post("/settings/levels/paste", response_class=HTMLResponse)
async def settings_levels_paste(
    request: Request,
    text: str = Form(""),
) -> RedirectResponse:
    """Bulk update levels by pasting the chartist's text format:

        AAPL  (Current Price: $291.10)
        Levels Below: [270.02, 274.33, 277.33, 282.54, 288.72]
        Levels Above: [293.86, 300.01, 310.58, 315.16, 318.09]

    Multiple tickers per paste are supported (back-to-back blocks).
    Each parsed block is upserted as a new snapshot stamped with NOW.
    """
    from . import levels as _lv
    snaps = _lv.parse_pasted_levels(text, source="manual")
    for s in snaps:
        _lv.upsert(s)
    n = len(snaps)
    flag = f"updated={n}" if n else "parse_failed"
    return RedirectResponse(url=f"/settings/levels?{flag}", status_code=303)


@app.post("/settings/levels/{ticker}", response_class=HTMLResponse)
async def settings_levels_upsert(
    request: Request,
    ticker: str,
    levels_below: str = Form(""),
    levels_above: str = Form(""),
    current_price: Optional[float] = Form(None),
) -> RedirectResponse:
    """Hand-edited save. Stamps a new asof_ts so this snapshot becomes the
    'latest' for the ticker. Levels arrive as pipe-separated strings to match
    the bulk-import format."""
    from . import levels as _lv
    snap = _lv.LevelSnapshot(
        ticker=ticker.upper(),
        asof_ts=datetime.now().isoformat(timespec="seconds"),
        current_price=current_price,
        levels_below=_lv._parse_pipe_levels(levels_below),
        levels_above=_lv._parse_pipe_levels(levels_above),
        source="manual",
        note="",
    )
    _lv.upsert(snap)
    return RedirectResponse(url="/settings/levels?saved=" + ticker.upper(),
                            status_code=303)


@app.post("/settings/levels/{ticker}/delete", response_class=HTMLResponse)
async def settings_levels_delete(ticker: str) -> RedirectResponse:
    from . import levels as _lv
    _lv.delete_ticker(ticker)
    return RedirectResponse(url="/settings/levels", status_code=303)


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
        result = await broker.submit_entry(intent)
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
