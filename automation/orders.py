"""Order manager — IBKR connection mocked initially.

When IBKR_LIVE=1 in env, swap MockBroker for IBKRBroker (ib_insync).
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional

from . import state, telemetry


@dataclass
class TradeIntent:
    ticker: str
    expiry: str
    strike: float
    right: str
    contracts: int
    order_type: str
    limit_price: Optional[float]
    regime_tag: str
    chain_role: str
    sector: str
    brando_alert_id: Optional[str] = None
    notes: Optional[str] = None
    chain_id: Optional[str] = None


class MockBroker:
    """Stub broker — accepts orders, returns fake fills.

    Replace with ib_insync IB() wrapper when wiring real IBKR.
    """

    mode = "mock"
    host = None
    port = None
    client_id = None
    account_id = None
    last_error = None
    last_attempt_ts = None

    def __init__(self) -> None:
        self.connected = False

    async def connect(self) -> bool:
        self.connected = True
        telemetry.log_event("broker_connect", {"backend": "mock"})
        return True

    def disconnect(self) -> None:
        self.connected = False

    def health(self) -> dict:
        return {
            "backend": "mock",
            "mode": "mock",
            "readonly": False,  # mock simulates fills locally; not "real" anyway
            "connected": self.connected,
            "host": None, "port": None, "client_id": None,
            "account_id": None,
            "last_error": None, "last_attempt_ts": None,
            "buying_power": None, "net_liquidation": None,
        }

    def submit_entry(self, intent: TradeIntent) -> dict:
        telemetry.log_event(
            "entry_submitted",
            {"ticker": intent.ticker, "order_type": intent.order_type},
        )
        # Simulated fill at limit (or 1.00 default for MKT in mock)
        fill_price = intent.limit_price or 1.00
        return {"status": "filled", "price": fill_price, "contracts": intent.contracts}

    def place_be_stop(self, intent_id: str, price: float, qty: int) -> str:
        telemetry.log_event(
            "be_stop_placed",
            {"intent_id": intent_id, "stop_price": price, "qty": qty, "latency_ms": 0},
        )
        return f"mock_be_{intent_id[:8]}"

    def place_tp_ladder(
        self, intent_id: str, tp1: float, tp2: float, tp3: float, splits: list[float], qty: int
    ) -> list[str]:
        telemetry.log_event(
            "tp_ladder_placed",
            {"intent_id": intent_id, "tp1": tp1, "tp2": tp2, "tp3": tp3, "qty": qty},
        )
        return [f"mock_tp{i}_{intent_id[:8]}" for i in (1, 2, 3)]


class IBKRBroker:
    """Real IBKR Gateway wrapper using ib_insync.

    Status: Phase 0 — connection + health surface only. Order methods are
    stubbed and raise NotImplementedError; they get filled in for Phase 1
    (BE-stop on entry fill).

    Env switches:
      BROKER=ibkr_paper  → host=127.0.0.1 port=7497 (paper account)
      BROKER=ibkr_live   → host=127.0.0.1 port=7496 (live account — ⚠)
      BROKER=mock        → MockBroker (default)
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 7497,
                 client_id: int = 1, mode: str = "paper",
                 readonly: bool = True) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id
        self.mode = mode  # "paper" | "live"
        self.readonly = readonly  # if True, broker refuses all order placement
        self.connected = False
        self.last_error: Optional[str] = None
        self.last_attempt_ts: Optional[str] = None
        self.account_id: Optional[str] = None
        self._ib = None  # ib_insync.IB instance, lazy

    def _import(self):
        try:
            from ib_insync import IB
            return IB
        except ImportError as e:
            self.last_error = f"ib_insync not installed: {e}"
            return None

    async def connect(self) -> bool:
        """Attempt to connect to IBKR Gateway. Returns True on success.

        Async because ib_insync uses asyncio internally — calling the sync
        `ib.connect()` from inside FastAPI's running event loop raises
        "This event loop is already running". Use `connectAsync` instead.

        Does not raise on failure — sets self.last_error and returns False
        so the server doesn't crash if Gateway isn't running.
        """
        from datetime import datetime as _dt
        self.last_attempt_ts = _dt.utcnow().isoformat()

        IB = self._import()
        if IB is None:
            self.connected = False
            return False

        try:
            self._ib = IB()
            # ib_insync's readonly=True asks Gateway to enforce read-only at
            # the protocol level — orders simply can't be placed even if
            # buggy code tries. Defense-in-depth alongside our own guard.
            await self._ib.connectAsync(self.host, self.port,
                                         clientId=self.client_id,
                                         timeout=4, readonly=self.readonly)
            self.connected = self._ib.isConnected()
            if self.connected:
                accts = self._ib.managedAccounts()
                self.account_id = accts[0] if accts else None
                self.last_error = None
                telemetry.log_event("broker_connect", {
                    "backend": "ibkr", "mode": self.mode,
                    "host": self.host, "port": self.port,
                    "account": self.account_id,
                })
            else:
                self.last_error = "isConnected() returned False"
        except Exception as e:
            self.connected = False
            self.last_error = f"{type(e).__name__}: {e}"
            telemetry.log_event("broker_connect_failed", {
                "backend": "ibkr", "error": self.last_error,
                "host": self.host, "port": self.port,
            })
        return self.connected

    def disconnect(self) -> None:
        if self._ib is not None and self._ib.isConnected():
            try:
                self._ib.disconnect()
            except Exception:
                pass
        self.connected = False

    def health(self) -> dict:
        """Return current broker health for the UI."""
        info = {
            "backend": "ibkr",
            "mode": self.mode,
            "readonly": self.readonly,
            "host": self.host,
            "port": self.port,
            "client_id": self.client_id,
            "connected": self.connected,
            "account_id": self.account_id,
            "last_error": self.last_error,
            "last_attempt_ts": self.last_attempt_ts,
            "buying_power": None,
            "net_liquidation": None,
        }
        if self.connected and self._ib is not None:
            try:
                values = self._ib.accountSummary(self.account_id) if self.account_id else self._ib.accountSummary()
                for v in values:
                    if v.tag == "BuyingPower":
                        info["buying_power"] = float(v.value)
                    elif v.tag == "NetLiquidation":
                        info["net_liquidation"] = float(v.value)
            except Exception:
                pass
        return info

    # ─── Order methods ─────────────────────────────────────────────────────
    # In readonly mode, submit_entry() routes through whatIfOrderAsync —
    # validates contract resolution + order construction without placing.
    # In writable mode, _assert_writable() guards every order-placing call.

    def _assert_writable(self, what: str) -> None:
        if self.readonly:
            raise PermissionError(
                f"Broker is in read-only mode; {what} refused. "
                f"Set BROKER_READONLY=0 to enable order placement."
            )

    @staticmethod
    def _serialize_contract(contract) -> dict:
        return {
            "conId": getattr(contract, "conId", None),
            "symbol": getattr(contract, "symbol", None),
            "expiry": getattr(contract, "lastTradeDateOrContractMonth", None),
            "strike": getattr(contract, "strike", None),
            "right": getattr(contract, "right", None),
            "exchange": getattr(contract, "exchange", None),
            "primaryExchange": getattr(contract, "primaryExchange", None),
            "multiplier": getattr(contract, "multiplier", None),
            "currency": getattr(contract, "currency", None),
            "secType": getattr(contract, "secType", None),
            "tradingClass": getattr(contract, "tradingClass", None),
            "localSymbol": getattr(contract, "localSymbol", None),
        }

    @staticmethod
    def _clean_numeric(v):
        """IBKR returns whatIf numerics as strings, sometimes '' or '*' for
        unavailable. Parse to float; return None when empty / not parseable."""
        if v is None:
            return None
        if isinstance(v, (int, float)):
            # Sentinel for "no value" is sys.float_info.max (~1.79e308)
            if isinstance(v, float) and v > 1e300:
                return None
            return float(v)
        if isinstance(v, str):
            v = v.strip()
            if not v or v in ("*", "N/A"):
                return None
            try:
                f = float(v)
                return None if f > 1e300 else f
            except ValueError:
                return None
        return None

    @staticmethod
    def _serialize_order(order) -> dict:
        lmt = getattr(order, "lmtPrice", None)
        # Hide IBKR's "no limit price" sentinel (1.79e308)
        if isinstance(lmt, float) and lmt > 1e300:
            lmt = None
        return {
            "action": getattr(order, "action", None),
            "totalQuantity": getattr(order, "totalQuantity", None),
            "orderType": getattr(order, "orderType", None),
            "lmtPrice": lmt,
            "tif": getattr(order, "tif", None),
            "whatIf": getattr(order, "whatIf", False),
        }

    async def submit_entry(self, intent: "TradeIntent",
                              force_dry_run: bool = False) -> dict:
        """Submit an entry order. In readonly mode OR when force_dry_run=True
        → whatIfOrderAsync (no order placed). In writable mode AND
        force_dry_run=False → placeOrder (real placement).

        Callers that surface a "dry-run" button must pass force_dry_run=True
        so the broker's writable state cannot accidentally promote a UI
        dry-run into a real order.
        """
        if not self.connected or self._ib is None:
            return {"status": "error", "error": "Broker not connected"}

        try:
            from ib_insync import Option, Stock, MarketOrder, LimitOrder
        except ImportError as e:
            return {"status": "error", "error": f"ib_insync import failed: {e}"}

        # 1. Build contract — IBKR expects expiry as YYYYMMDD
        expiry_str = (intent.expiry or "").replace("-", "")
        right = (intent.right or "").upper()
        if right in ("C", "P"):
            contract = Option(
                intent.ticker, expiry_str, intent.strike, right,
                "SMART", "100", "USD",
            )
        else:
            contract = Stock(intent.ticker, "SMART", "USD")

        # 2. Qualify contract — resolves to a specific conId. Fails cleanly
        # if the strike doesn't exist in the chain.
        try:
            qualified = await self._ib.qualifyContractsAsync(contract)
        except Exception as e:
            return {"status": "error",
                    "error": f"qualifyContractsAsync failed: {type(e).__name__}: {e}",
                    "contract_attempted": self._serialize_contract(contract)}

        if not qualified or not getattr(qualified[0], "conId", None):
            return {"status": "error",
                    "error": "Contract not found in IBKR. Check expiry format / strike availability.",
                    "contract_attempted": self._serialize_contract(contract)}

        contract = qualified[0]

        # 3. Build order
        action = "BUY"  # long-premium options strategy; sells happen via TP exits later
        qty = int(intent.contracts)
        order_type = (intent.order_type or "MKT").upper()
        if order_type == "LMT":
            limit = float(intent.limit_price) if intent.limit_price else 0.0
            order = LimitOrder(action, qty, limit)
        else:
            order = MarketOrder(action, qty)

        # 4. Branch: dry-run vs real order placement
        if self.readonly or force_dry_run:
            # Brief market-data ping so IBKR can compute margin/commission.
            # Without a subscribed quote, whatIf returns empty values for
            # options. Snapshot is enough; we don't need streaming.
            try:
                self._ib.reqMktData(contract, "", snapshot=True, regulatorySnapshot=False)
                # Wait briefly for quote to populate
                await asyncio.sleep(1.0)
            except Exception:
                pass  # not fatal — margin may just be empty

            order.whatIf = True
            try:
                state = await self._ib.whatIfOrderAsync(contract, order)
            except Exception as e:
                return {"status": "error",
                        "error": f"whatIfOrderAsync failed: {type(e).__name__}: {e}",
                        "contract": self._serialize_contract(contract),
                        "order": self._serialize_order(order)}

            telemetry.log_event("broker_whatif", {
                "ticker": intent.ticker,
                "expiry": intent.expiry,
                "strike": intent.strike,
                "right": right,
                "qty": qty,
                "conId": contract.conId,
            })

            return {
                "status": "dry_run_ok",
                "mode": "whatIf",
                "contract": self._serialize_contract(contract),
                "order": self._serialize_order(order),
                "whatif": {
                    "init_margin_change": self._clean_numeric(getattr(state, "initMarginChange", None)),
                    "maint_margin_change": self._clean_numeric(getattr(state, "maintMarginChange", None)),
                    "equity_with_loan_change": self._clean_numeric(getattr(state, "equityWithLoanChange", None)),
                    "commission": self._clean_numeric(getattr(state, "commission", None)),
                    "commission_currency": getattr(state, "commissionCurrency", None) or "USD",
                    "min_commission": self._clean_numeric(getattr(state, "minCommission", None)),
                    "max_commission": self._clean_numeric(getattr(state, "maxCommission", None)),
                    "warning_text": getattr(state, "warningText", None),
                },
            }

        # writable mode — actual placement
        self._assert_writable("submit_entry")

        try:
            trade = self._ib.placeOrder(contract, order)
        except Exception as e:
            return {"status": "error",
                    "error": f"placeOrder failed: {type(e).__name__}: {e}",
                    "contract": self._serialize_contract(contract),
                    "order": self._serialize_order(order)}

        # Wait briefly for IBKR to move past PendingSubmit. We don't block on
        # Filled — a working LMT may sit; caller polls via /positions later.
        deadline = asyncio.get_event_loop().time() + 5.0
        terminal_or_live = {"Submitted", "PreSubmitted", "Filled",
                            "Cancelled", "Inactive", "ApiCancelled"}
        while asyncio.get_event_loop().time() < deadline:
            if trade.orderStatus.status in terminal_or_live:
                break
            await asyncio.sleep(0.1)

        avg = trade.orderStatus.avgFillPrice
        telemetry.log_event("entry_submitted", {
            "backend": "ibkr",
            "mode": self.mode,
            "ticker": intent.ticker,
            "conId": contract.conId,
            "order_id": trade.order.orderId,
            "perm_id": trade.order.permId,
            "qty": qty,
            "order_type": order_type,
            "status": trade.orderStatus.status,
            "filled": trade.orderStatus.filled,
            "avg_fill_price": avg or None,
        })

        return {
            "status": "placed",
            "broker_status": trade.orderStatus.status,
            "order_id": trade.order.orderId,
            "perm_id": trade.order.permId,
            "filled": trade.orderStatus.filled,
            "remaining": trade.orderStatus.remaining,
            "avg_fill_price": avg if avg else None,
            "contract": self._serialize_contract(contract),
            "order": self._serialize_order(order),
        }

    async def dry_run_tp_orders(self, intent: "TradeIntent",
                                  tp_orders: list[dict]) -> list[dict]:
        """Run whatIfOrderAsync on multiple SELL orders against the same
        contract — the TP ladder side of the round-trip. Each entry in
        `tp_orders` is a dict {tier, price, qty}. Returns a list of result
        dicts in the same order.
        """
        if not self.connected or self._ib is None:
            return [{"status": "error", "error": "Broker not connected"}]

        try:
            from ib_insync import Option, Stock, LimitOrder
        except ImportError as e:
            return [{"status": "error", "error": f"ib_insync import failed: {e}"}]

        # Re-resolve the contract
        expiry_str = (intent.expiry or "").replace("-", "")
        right = (intent.right or "").upper()
        if right in ("C", "P"):
            contract = Option(intent.ticker, expiry_str, intent.strike, right,
                              "SMART", "100", "USD")
        else:
            contract = Stock(intent.ticker, "SMART", "USD")
        try:
            qualified = await self._ib.qualifyContractsAsync(contract)
        except Exception as e:
            return [{"status": "error", "error": f"qualify failed: {type(e).__name__}: {e}"}]
        if not qualified or not getattr(qualified[0], "conId", None):
            return [{"status": "error", "error": "Contract not found"}]
        contract = qualified[0]

        # Brief market-data snapshot to populate margin calc
        try:
            self._ib.reqMktData(contract, "", snapshot=True, regulatorySnapshot=False)
            await asyncio.sleep(1.0)
        except Exception:
            pass

        results = []
        for tp in tp_orders:
            tier = tp.get("tier")
            price = float(tp.get("price"))
            qty = int(tp.get("qty"))
            if qty <= 0 or price <= 0:
                results.append({"tier": tier, "status": "skipped",
                                "reason": f"qty={qty} price={price}"})
                continue

            order = LimitOrder("SELL", qty, price)
            order.whatIf = True
            order.tif = "GTC"

            try:
                state = await self._ib.whatIfOrderAsync(contract, order)
            except Exception as e:
                results.append({"tier": tier, "status": "error",
                                "error": f"{type(e).__name__}: {e}"})
                continue

            results.append({
                "tier": tier,
                "status": "ok",
                "price": price,
                "qty": qty,
                "order": self._serialize_order(order),
                "whatif": {
                    "init_margin_change": self._clean_numeric(getattr(state, "initMarginChange", None)),
                    "maint_margin_change": self._clean_numeric(getattr(state, "maintMarginChange", None)),
                    "equity_with_loan_change": self._clean_numeric(getattr(state, "equityWithLoanChange", None)),
                    "commission": self._clean_numeric(getattr(state, "commission", None)),
                    "commission_currency": getattr(state, "commissionCurrency", None) or "USD",
                    "min_commission": self._clean_numeric(getattr(state, "minCommission", None)),
                    "max_commission": self._clean_numeric(getattr(state, "maxCommission", None)),
                    "warning_text": getattr(state, "warningText", None),
                },
            })
        return results

    async def dry_run_stop_order(self, intent: "TradeIntent",
                                   stop_price: float, qty: int) -> dict:
        """Run whatIfOrderAsync on a SELL Stop order — the BE/trail stop side.
        Returns a result dict with the same shape as the TP whatIfs."""
        if not self.connected or self._ib is None:
            return {"status": "error", "error": "Broker not connected"}

        try:
            from ib_insync import Option, Stock, StopOrder
        except ImportError as e:
            return {"status": "error", "error": f"ib_insync import failed: {e}"}

        if qty <= 0 or stop_price <= 0:
            return {"status": "skipped",
                    "reason": f"qty={qty} stop_price={stop_price}"}

        # Resolve contract
        expiry_str = (intent.expiry or "").replace("-", "")
        right = (intent.right or "").upper()
        if right in ("C", "P"):
            contract = Option(intent.ticker, expiry_str, intent.strike, right,
                              "SMART", "100", "USD")
        else:
            contract = Stock(intent.ticker, "SMART", "USD")
        try:
            qualified = await self._ib.qualifyContractsAsync(contract)
        except Exception as e:
            return {"status": "error",
                    "error": f"qualifyContractsAsync failed: {type(e).__name__}: {e}"}
        if not qualified or not getattr(qualified[0], "conId", None):
            return {"status": "error", "error": "Contract not found"}
        contract = qualified[0]

        # Brief market-data snapshot for margin calc
        try:
            self._ib.reqMktData(contract, "", snapshot=True, regulatorySnapshot=False)
            await asyncio.sleep(1.0)
        except Exception:
            pass

        order = StopOrder("SELL", qty, stop_price)
        order.whatIf = True
        order.tif = "GTC"

        try:
            state = await self._ib.whatIfOrderAsync(contract, order)
        except Exception as e:
            return {"status": "error",
                    "error": f"whatIfOrderAsync failed: {type(e).__name__}: {e}"}

        return {
            "status": "ok",
            "stop_price": stop_price,
            "qty": qty,
            "order": self._serialize_order(order),
            "whatif": {
                "init_margin_change": self._clean_numeric(getattr(state, "initMarginChange", None)),
                "maint_margin_change": self._clean_numeric(getattr(state, "maintMarginChange", None)),
                "equity_with_loan_change": self._clean_numeric(getattr(state, "equityWithLoanChange", None)),
                "commission": self._clean_numeric(getattr(state, "commission", None)),
                "commission_currency": getattr(state, "commissionCurrency", None) or "USD",
                "min_commission": self._clean_numeric(getattr(state, "minCommission", None)),
                "max_commission": self._clean_numeric(getattr(state, "maxCommission", None)),
                "warning_text": getattr(state, "warningText", None),
            },
        }

    def place_be_stop(self, intent_id: str, price: float, qty: int) -> str:
        self._assert_writable("place_be_stop")
        raise NotImplementedError(
            "IBKRBroker.place_be_stop not yet implemented (Phase 1)."
        )

    def place_tp_ladder(self, intent_id: str, tp1: float, tp2: float,
                        tp3: float, splits: list[float], qty: int) -> list[str]:
        self._assert_writable("place_tp_ladder")
        raise NotImplementedError(
            "IBKRBroker.place_tp_ladder not yet implemented (Phase 2)."
        )


def get_broker():
    """Select broker per BROKER env var.

    BROKER values:
      mock        (default) — MockBroker, no real orders
      ibkr_paper            — IBKRBroker on port 7497 (paper account)
      ibkr_live             — IBKRBroker on port 7496 (LIVE — ⚠)

    BROKER_READONLY env var:
      1 (default) — broker refuses all order placement
      0           — order placement allowed (only when Phase 1+ wired)

    Live mode forces readonly=True UNLESS BROKER_READONLY is explicitly
    set to "0" — even then, a strong UI warning surfaces.
    """
    mode = os.environ.get("BROKER", "mock").lower()
    readonly_env = os.environ.get("BROKER_READONLY", "1")
    readonly = readonly_env != "0"

    if mode == "ibkr_paper":
        return IBKRBroker(host="127.0.0.1", port=7497, mode="paper",
                          readonly=readonly)
    if mode == "ibkr_live":
        return IBKRBroker(host="127.0.0.1", port=7496, mode="live",
                          readonly=readonly)
    return MockBroker()


# ─── Order engine — lifecycle simulation ────────────────────────────────────
#
# Implements the canonical mechanical-BE rule (strategy_rules.md Tier 1 #1):
#
#   "After TP1 fires, the stop on the residual stays at entry. Never move
#    it lower. Never move it higher. Never cancel."
#
# When a TP fires:
#   - SELL fill is recorded for the TP's quantity
#   - BE stop quantity is REDUCED by the TP's quantity (keeps protecting
#     the new residual; level NEVER changes)
#   - TP order marked filled
#
# When BE stop fires (option price ≤ entry):
#   - SELL fill recorded for full residual at entry price (no loss)
#   - All remaining TPs auto-canceled (position is flat)
#   - BE stop marked filled

from . import state


def _residual(intent_id: str) -> int:
    fills = state.get_fills(intent_id)
    bought = sum(f["contracts"] for f in fills if f["is_entry"])
    sold = sum(f["contracts"] for f in fills if not f["is_entry"])
    return bought - sold


def _maybe_propose_roll(intent: dict, fired_tier: int, fill_price: float,
                         fill_qty: int, avg_entry: float, rules) -> None:
    """When a TP fires, check the position's roll plan. For any rule whose
    `trigger` matches the fired TP tier, create a `proposed_roll` intent
    at the next strike with the retained-profit metadata. The user
    confirms or cancels via the position detail UI."""
    import json as _json

    raw = intent.get("roll_plan_json") or "[]"
    try:
        plan = _json.loads(raw)
    except (_json.JSONDecodeError, TypeError):
        return
    if not isinstance(plan, list):
        return

    trigger_key = f"tp{fired_tier}"
    matching_rules = [r for r in plan
                       if isinstance(r, dict) and r.get("trigger") == trigger_key]
    if not matching_rules:
        return

    # Skip if a proposal already exists for (parent, tier) — idempotency.
    with state.connect() as conn:
        existing = conn.execute(
            "SELECT intent_id FROM trade_intents "
            "WHERE parent_intent_id = ? AND triggered_by_tp = ? "
            "  AND status = 'proposed_roll'",
            (intent["intent_id"], fired_tier),
        ).fetchone()
    if existing:
        return

    # Compute realized profit on this TP fill, and the retained portion.
    realized_per_contract = (fill_price - avg_entry) * 100  # $/contract
    realized_total = realized_per_contract * fill_qty

    # Determine new chain role (R1 → R2 → R3)
    parent_role = (intent.get("chain_role") or "solo").upper()
    if parent_role in ("SOLO", "R1"):
        new_role = "R2"
    elif parent_role == "R2":
        new_role = "R3"
    else:
        new_role = "R3"  # cap at R3

    default_next_strike = rules.roll_next_strike(intent["ticker"], intent["strike"],
                                                   intent["right"])

    for rule in matching_rules:
        retain_pct = float(rule.get("retain_pct", 0.5))
        retained = realized_total * retain_pct
        # Per-rule strike override (set by user in the roll-plan UI). Falls
        # back to the engine default (current + offset by right).
        try:
            rule_strike = float(rule.get("next_strike")) if rule.get("next_strike") is not None else None
        except (TypeError, ValueError):
            rule_strike = None
        proposal_strike = rule_strike if rule_strike and rule_strike > 0 else float(default_next_strike)

        proposal = {
            "ticker": intent["ticker"],
            "expiry": intent["expiry"],
            "strike": proposal_strike,
            "right": intent["right"],
            "contracts": 1,  # placeholder — user adjusts on confirm
            "order_type": "MKT",
            "limit_price": None,
            "regime_tag": intent["regime_tag"],
            "chain_role": new_role,
            "chain_id": intent.get("chain_id"),
            "sector": intent["sector"],
            "brando_alert_id": None,
            "notes": (f"Auto-proposed roll from {intent['ticker']} "
                       f"{int(intent['strike'])}{intent['right']} after TP{fired_tier}. "
                       f"${retained:.0f} retained ({retain_pct*100:.0f}% of "
                       f"${realized_total:.0f} realized)."),
            "status": "proposed_roll",
            "parent_intent_id": intent["intent_id"],
            "triggered_by_tp": fired_tier,
            "retained_profit_usd": retained,
            # Carry the same TP/stop/roll choices forward to the new leg
            "tp_ladder_choice": intent.get("tp_ladder_choice", "auto"),
            "tp_split_choice": intent.get("tp_split_choice", "50_25_25"),
            "roll_plan": intent.get("roll_plan", "default"),
            "roll_plan_json": intent.get("roll_plan_json"),
            "stop_discipline": intent.get("stop_discipline", "be_stop"),
            "stop_initial_pct": intent.get("stop_initial_pct", 0),
        }

        proposal_id = state.insert_trade_intent(proposal)
        telemetry.log_event("roll_proposed", {
            "parent_intent_id": intent["intent_id"],
            "proposal_id": proposal_id,
            "fired_tier": fired_tier,
            "next_strike": proposal_strike,
            "retained_usd": round(retained, 2),
            "retain_pct": retain_pct,
            "new_role": new_role,
        })


def evaluate_position_orders(intent_id: str, option_price: float,
                              rules=None) -> list[dict]:
    """Walk all working orders for this intent against current option price.
    Fire any order whose condition is met. Returns a list of events fired.

    Tracks MFE on each tick and recomputes the stop level per the chosen
    stop discipline (be_stop / half_mfe / custom_trail / none). Stop level
    is one-way ratchet: never moves down.

    Order matters: TPs evaluated lowest-target-first, then stop check. A
    realistic broker tick takes TPs on a sweep-up; takes stop on a sweep-down.
    """
    state.set_option_price(intent_id, option_price)
    events: list[dict] = []
    intent = state.get_intent(intent_id)
    if not intent:
        return events

    # Track MFE
    state.set_max_option_price(intent_id, option_price)
    intent = state.get_intent(intent_id)  # reload for updated MFE
    mfe = intent.get("max_option_price") or option_price

    working = state.get_working_orders(intent_id)
    tps = sorted([o for o in working if o["kind"] in ("tp1", "tp2", "tp3")],
                 key=lambda o: o["target_price"])
    be = next((o for o in working if o["kind"] == "be_stop"), None)

    # Compute average entry price (for stop-level baseline)
    fills = state.get_fills(intent_id)
    entry_fills = [f for f in fills if f["is_entry"]]
    if entry_fills:
        avg_entry = sum(f["price"] * f["contracts"] for f in entry_fills) / sum(f["contracts"] for f in entry_fills)
    else:
        avg_entry = be["target_price"] if be else option_price

    # Ratchet the stop level per discipline
    if be and rules is not None:
        new_stop = rules.compute_stop_level(
            intent.get("stop_discipline") or "be_stop",
            avg_entry, mfe, be["target_price"],
            intent.get("stop_trigger_pct"),
            intent.get("stop_trail_pct"),
        )
        if new_stop is None:
            # 'none' discipline → cancel BE order
            state.mark_order_canceled(be["order_id"])
            be = None
        elif new_stop > be["target_price"]:
            state.update_order_target(be["order_id"], new_stop)
            be["target_price"] = new_stop
            telemetry.log_event("stop_ratcheted", {
                "intent_id": intent_id, "new_stop": new_stop, "mfe": mfe,
                "discipline": intent.get("stop_discipline"),
            })
            events.append({"kind": "stop_ratcheted", "new_stop": new_stop, "mfe": mfe})

    # Phase 1: any TPs that should fire?
    for tp in tps:
        if option_price >= tp["target_price"]:
            residual = _residual(intent_id)
            qty = min(tp["quantity"], residual)
            if qty <= 0:
                # Residual is 0 already; cancel rather than fill
                state.mark_order_canceled(tp["order_id"])
                continue
            tier = int(tp["kind"][2:])
            state.insert_fill(intent_id, "SELL", qty, tp["target_price"],
                              is_entry=False, tp_tier=tier)
            state.mark_order_filled(tp["order_id"])
            telemetry.log_event("tp_filled", {
                "intent_id": intent_id, "tier": tier,
                "qty": qty, "price": tp["target_price"],
                "ticker": intent["ticker"],
            })
            events.append({"kind": "tp_filled", "tier": tier,
                           "qty": qty, "price": tp["target_price"]})

            # Roll proposal — does this TP trigger any roll rule?
            if rules is not None:
                _maybe_propose_roll(intent, tier, tp["target_price"], qty,
                                     avg_entry, rules)
            # Ratchet BE stop level per the configured phase rules:
            # - After TP1 fires, target = entry × (1 + stop_after_tp1_pct)
            # - After TP2 fires, target = entry × (1 + stop_after_tp2_pct)
            # - Quantity reduces by the TP fill qty
            # One-way ratchet — never lower the stop.
            if be:
                new_be_qty = max(0, be["quantity"] - qty)
                if new_be_qty <= 0:
                    state.mark_order_canceled(be["order_id"])
                    be = None
                else:
                    new_target = None
                    if tier == 1 and intent.get("stop_after_tp1_pct") is not None:
                        new_target = avg_entry * (1 + intent["stop_after_tp1_pct"])
                    elif tier == 2 and intent.get("stop_after_tp2_pct") is not None:
                        new_target = avg_entry * (1 + intent["stop_after_tp2_pct"])
                    if new_target is not None and new_target > be["target_price"]:
                        state.update_order_target(be["order_id"], round(new_target, 2))
                        be["target_price"] = round(new_target, 2)
                        telemetry.log_event("stop_phased_up", {
                            "intent_id": intent_id, "after_tp": tier,
                            "new_stop": round(new_target, 2),
                        })
                    state.update_order_quantity(be["order_id"], new_be_qty)
                    be["quantity"] = new_be_qty

    # Phase 2: BE stop check (price moved DOWN to entry)
    # Warmup gate — protects against open-spread / fill-slippage tick that
    # marks the option below entry seconds after a market BUY. Stop is placed
    # but doesn't fire until warmup expires. Default 60s (configurable in
    # /settings/risk → Position sizing).
    if be and option_price <= be["target_price"]:
        warmup_active = False
        if rules is not None and entry_fills:
            warmup_s = int(((rules.raw or {}).get("risk_limits") or {})
                            .get("entry_warmup_seconds", 60))
            if warmup_s > 0:
                from datetime import datetime
                earliest = min(entry_fills, key=lambda f: f["ts"])["ts"]
                try:
                    fill_ts = datetime.fromisoformat(earliest)
                    elapsed = (datetime.utcnow() - fill_ts).total_seconds()
                    if elapsed < warmup_s:
                        warmup_active = True
                        events.append({
                            "kind": "be_stop_warmup_blocked",
                            "elapsed_s": round(elapsed, 1),
                            "warmup_s": warmup_s,
                            "stop_level": be["target_price"],
                            "option_price": option_price,
                        })
                except (TypeError, ValueError):
                    pass

        if not warmup_active:
            residual = _residual(intent_id)
            if residual > 0:
                state.insert_fill(intent_id, "SELL", residual, be["target_price"],
                                  is_entry=False, tp_tier=None)
                state.mark_order_filled(be["order_id"])
                telemetry.log_event("be_stop_fired", {
                    "intent_id": intent_id, "qty": residual,
                    "price": be["target_price"], "ticker": intent["ticker"],
                })
                events.append({"kind": "be_stop_fired", "qty": residual,
                               "price": be["target_price"]})
                # Cancel any remaining TPs — position flat
                for tp in tps:
                    fresh = next((o for o in state.get_working_orders(intent_id)
                                  if o["order_id"] == tp["order_id"]), None)
                    if fresh:
                        state.mark_order_canceled(fresh["order_id"])
                        events.append({"kind": "tp_canceled", "tier": int(tp["kind"][2:])})

    return events
