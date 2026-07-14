#!/usr/bin/env python3
"""
risk_guard.py
=============
Deterministic PRE-TRADE risk gate for AUTONOMOUS execution (equities + options).

In autonomous mode the agent decides *and* places orders on the Agentic (cash)
account without asking the user before each trade. This module is the
deterministic safety layer that BOUNDS those autonomous decisions. It follows
the same philosophy as the rest of the desk: the LLM decides *what* to do;
deterministic code enforces *how much* is allowed.

Contract:
  - It NEVER invents a trade. It only takes a proposed order and returns
    APPROVE / REJECT, clamping the size down to hard risk limits.
  - The approved size is always <= the proposed size. The agent must place at
    most `approved.quantity` (contracts for options); never more.
  - A global kill switch (`config.enabled = false`) rejects everything.

Two asset classes are supported via `proposal.asset_class`:
  - "equity" (default): shares. Risk = shares * price.
  - "option": contracts. LONG options are defined-risk (max loss = premium).
    SHORT ("*_to_open") options are undefined/large-risk and are REJECTED unless
    `config.allow_uncovered_options=true` AND the proposal supplies a bounded
    `max_loss` (dollars) so the gate can size against it. Expiry guards and a
    premium cap keep swing-trade sizing sane.

stdlib only. Python 3.9+.
"""
from __future__ import annotations
import argparse
import datetime as _dt
import json
import math
import sys
from typing import Optional


DEFAULT_CONFIG = {
    "enabled": True,
    "max_position_pct": 0.15,        # max fraction of portfolio in ONE equity symbol
    "max_trade_pct": 0.10,           # max fraction of portfolio per SINGLE order (notional)
    "max_daily_trades": 10,
    "daily_trades_used": 0,
    "min_cash_reserve": 0.0,         # settled cash kept untouched on buys
    "require_settled_cash": True,    # buys funded only by settled cash (cash acct / T+1)
    "allow_fractional": False,       # equity: whole shares only when False
    "protected": [],                 # symbols that must never be sold/trimmed
    # --- options / swing ---
    "max_option_premium_pct": 0.05,  # max premium-at-risk in ONE option position
    "allow_uncovered_options": False,  # block naked/short options unless max_loss supplied
    "min_days_to_expiry": 0,         # 0 = off; swing trades should set e.g. 21
    "max_concurrent_positions": 0,   # 0 = off; cap number of simultaneously open positions
    "open_positions_count": 0,       # current open position count (agent supplies)
}

OPTION_MULTIPLIER = 100.0


def _num(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _size_from_notional(notional: float, unit_price: float, fractional: bool) -> float:
    if unit_price <= 0:
        return 0.0
    qty = notional / unit_price
    return qty if fractional else math.floor(qty)


def _days_to_expiry(expiration, today) -> Optional[int]:
    if not expiration:
        return None
    try:
        exp = _dt.date.fromisoformat(str(expiration))
    except ValueError:
        return None
    if today:
        try:
            ref = _dt.date.fromisoformat(str(today))
        except ValueError:
            ref = _dt.date.today()
    else:
        ref = _dt.date.today()
    return (exp - ref).days


# ==========================================================================
# Dispatcher
# ==========================================================================

def evaluate(payload: dict) -> dict:
    proposal = payload.get("proposal", {}) or {}
    account = payload.get("account", {}) or {}
    cfg = {**DEFAULT_CONFIG, **(payload.get("config", {}) or {})}
    asset_class = str(proposal.get("asset_class", "equity")).lower()

    # --- Pre-checks common to both asset classes ---
    pre = _common_precheck(proposal, cfg)
    if pre is not None:
        return pre

    if asset_class in ("option", "options"):
        return _evaluate_option(proposal, account, cfg)
    return _evaluate_equity(proposal, account, cfg)


def _common_precheck(proposal: dict, cfg: dict) -> Optional[dict]:
    symbol = str(proposal.get("symbol", "")).upper()
    side = str(proposal.get("side", proposal.get("action", ""))).lower()
    if not symbol:
        return _reject("", side, 0.0, ["missing symbol"], unit="")
    if not cfg["enabled"]:
        return _reject(symbol, side, 0.0,
                       ["autonomous trading disabled (config.enabled=false) — kill switch active"],
                       unit="")
    if _num(cfg["daily_trades_used"]) >= _num(cfg["max_daily_trades"]):
        return _reject(symbol, side, 0.0,
                       [f"daily trade limit reached ({int(_num(cfg['daily_trades_used']))}/"
                        f"{int(_num(cfg['max_daily_trades']))})"], unit="")
    return None


def _opens_new_position(symbol: str, positions: dict) -> bool:
    return _num((positions.get(symbol, {}) or {}).get("quantity")) <= 0


def _concurrency_block(cfg: dict, opening_new: bool) -> Optional[str]:
    cap = int(_num(cfg["max_concurrent_positions"]))
    if cap > 0 and opening_new and int(_num(cfg["open_positions_count"])) >= cap:
        return (f"max concurrent positions reached "
                f"({int(_num(cfg['open_positions_count']))}/{cap})")
    return None


# ==========================================================================
# EQUITY
# ==========================================================================

def _evaluate_equity(proposal: dict, account: dict, cfg: dict) -> dict:
    symbol = str(proposal.get("symbol", "")).upper()
    side = str(proposal.get("side", proposal.get("action", ""))).lower()
    price = _num(proposal.get("price"))
    fractional = bool(cfg["allow_fractional"])
    clamps: list[str] = []
    notes: list[str] = []

    if side not in ("buy", "sell"):
        return _reject(symbol, side, price, [f"invalid side '{side}' (expected buy/sell)"])
    if price <= 0:
        return _reject(symbol, side, price, ["price missing or <= 0"])

    if proposal.get("quantity") is not None:
        req_qty = _num(proposal.get("quantity"))
    elif proposal.get("notional") is not None:
        req_qty = _size_from_notional(_num(proposal.get("notional")), price, fractional)
    else:
        return _reject(symbol, side, price, ["proposal must include 'quantity' or 'notional'"])
    if req_qty <= 0:
        return _reject(symbol, side, price, ["requested size <= 0"], requested_qty=req_qty)

    positions = account.get("positions", {}) or {}
    pos = positions.get(symbol, {}) or {}
    held_qty = _num(pos.get("quantity"))
    held_mv = _num(pos.get("market_value"), held_qty * price)
    portfolio_value = _num(account.get("portfolio_value"))

    # ---- SELL ----
    if side == "sell":
        if symbol in {s.upper() for s in cfg["protected"]}:
            return _reject(symbol, side, price, [f"{symbol} is protected — never sell/trim"],
                           requested_qty=req_qty)
        if held_qty <= 0:
            return _reject(symbol, side, price,
                           [f"no open position in {symbol} to sell (no shorting)"],
                           requested_qty=req_qty)
        approved = min(req_qty, held_qty)
        if approved < req_qty:
            clamps.append(f"sell size clamped to held quantity {held_qty:g}")
        if not fractional:
            approved = math.floor(approved)
        if approved <= 0:
            return _reject(symbol, side, price, ["nothing sellable after clamping to whole shares"],
                           requested_qty=req_qty)
        return _approve(symbol, side, price, approved, req_qty, clamps, notes)

    # ---- BUY ----
    cblock = _concurrency_block(cfg, _opens_new_position(symbol, positions))
    if cblock:
        return _reject(symbol, side, price, [cblock], requested_qty=req_qty)

    req_notional = req_qty * price
    if cfg["require_settled_cash"]:
        available_cash = _num(account.get("settled_cash")); cash_src = "settled_cash"
    else:
        available_cash = _num(account.get("buying_power")); cash_src = "buying_power"
    available_cash -= _num(cfg["min_cash_reserve"])
    if available_cash <= 0:
        return _reject(symbol, side, price,
                       [f"no {cash_src} available after reserve "
                        f"(reserve={_num(cfg['min_cash_reserve']):g})"], requested_qty=req_qty)

    caps = [("requested", req_notional), (f"{cash_src} available", available_cash)]
    if portfolio_value > 0:
        caps.append((f"per-trade cap {cfg['max_trade_pct']:.0%}",
                     portfolio_value * _num(cfg["max_trade_pct"])))
        caps.append((f"position cap {cfg['max_position_pct']:.0%}",
                     portfolio_value * _num(cfg["max_position_pct"]) - held_mv))
    else:
        notes.append("portfolio_value missing/0: percent caps skipped (cash-only limit)")

    binding_name, allowed_notional = min(caps, key=lambda kv: kv[1])
    if allowed_notional <= 0:
        return _reject(symbol, side, price,
                       [f"no room to buy {symbol}: binding limit '{binding_name}' "
                        f"= {allowed_notional:.2f}"], requested_qty=req_qty)
    approved_qty = _size_from_notional(allowed_notional, price, fractional)
    if approved_qty <= 0:
        return _reject(symbol, side, price,
                       [f"binding limit '{binding_name}' (${allowed_notional:.2f}) "
                        f"is below one share (${price:.2f})"], requested_qty=req_qty)
    if approved_qty < req_qty:
        clamps.append(f"buy size clamped by '{binding_name}' to {approved_qty:g} share(s)")
    return _approve(symbol, side, price, approved_qty, req_qty, clamps, notes)


# ==========================================================================
# OPTIONS
# ==========================================================================

def _evaluate_option(proposal: dict, account: dict, cfg: dict) -> dict:
    symbol = str(proposal.get("symbol", "")).upper()
    action = str(proposal.get("action", proposal.get("side", ""))).lower().replace("-", "_")
    otype = str(proposal.get("option_type", "")).lower()
    premium = _num(proposal.get("premium"))
    mult = _num(proposal.get("multiplier"), OPTION_MULTIPLIER) or OPTION_MULTIPLIER
    req_contracts = _num(proposal.get("contracts"))
    clamps: list[str] = []
    notes: list[str] = []

    instrument = {
        "option_type": otype, "strike": proposal.get("strike"),
        "expiration": proposal.get("expiration"), "multiplier": mult,
    }

    # normalize action synonyms: "buy"->buy_to_open, "sell"->sell_to_open
    if action == "buy":
        action = "buy_to_open"
    elif action == "sell":
        action = "sell_to_open"
    valid_actions = {"buy_to_open", "buy_to_close", "sell_to_open", "sell_to_close"}
    if action not in valid_actions:
        return _reject(symbol, action, premium, [f"invalid option action '{action}'"],
                       unit="contracts", instrument=instrument)
    if otype not in ("call", "put"):
        return _reject(symbol, action, premium, ["option_type must be 'call' or 'put'"],
                       unit="contracts", instrument=instrument)
    if premium <= 0:
        return _reject(symbol, action, premium, ["premium missing or <= 0"],
                       unit="contracts", instrument=instrument)
    if req_contracts <= 0:
        return _reject(symbol, action, premium, ["contracts missing or <= 0"],
                       unit="contracts", instrument=instrument)

    # --- Expiry guards ---
    dte = _days_to_expiry(proposal.get("expiration"),
                          proposal.get("today") or cfg.get("today"))
    if dte is not None:
        instrument["days_to_expiry"] = dte
        if dte < 0:
            return _reject(symbol, action, premium, [f"option already expired ({dte} DTE)"],
                           requested_qty=req_contracts, unit="contracts", instrument=instrument)
        min_dte = int(_num(cfg["min_days_to_expiry"]))
        opening = action.endswith("_to_open")
        if opening and min_dte > 0 and dte < min_dte:
            return _reject(symbol, action, premium,
                           [f"{dte} DTE below min_days_to_expiry={min_dte} "
                            f"(too close to expiry for a swing)"],
                           requested_qty=req_contracts, unit="contracts", instrument=instrument)

    portfolio_value = _num(account.get("portfolio_value"))

    # ---- CLOSING actions (risk-reducing) ----
    if action in ("buy_to_close", "sell_to_close"):
        held = _num(proposal.get("held_contracts"))
        approved = req_contracts
        if held > 0 and approved > held:
            approved = held
            clamps.append(f"close size clamped to held contracts {held:g}")
        approved = math.floor(approved)
        if approved <= 0:
            return _reject(symbol, action, premium, ["nothing to close after clamping"],
                           requested_qty=req_contracts, unit="contracts", instrument=instrument)
        notes.append("closing action reduces exposure")
        return _approve(symbol, action, premium, approved, req_contracts, clamps, notes,
                        unit="contracts", instrument=instrument)

    # ---- SELL_TO_OPEN (short option: undefined/large risk) ----
    if action == "sell_to_open":
        if not cfg["allow_uncovered_options"]:
            return _reject(symbol, action, premium,
                           ["short options (sell_to_open) are disabled "
                            "(config.allow_uncovered_options=false)"],
                           requested_qty=req_contracts, unit="contracts", instrument=instrument)
        max_loss_total = _num(proposal.get("max_loss"))
        if max_loss_total <= 0:
            return _reject(symbol, action, premium,
                           ["sell_to_open requires a positive 'max_loss' (defined risk) to size against"],
                           requested_qty=req_contracts, unit="contracts", instrument=instrument)
        per_contract_loss = max_loss_total / req_contracts
        caps = [("requested", max_loss_total)]
        if portfolio_value > 0:
            caps.append((f"per-trade cap {cfg['max_trade_pct']:.0%}",
                         portfolio_value * _num(cfg["max_trade_pct"])))
        else:
            notes.append("portfolio_value missing/0: percent caps skipped")
        binding_name, allowed_risk = min(caps, key=lambda kv: kv[1])
        approved = math.floor(allowed_risk / per_contract_loss) if per_contract_loss > 0 else 0
        if approved <= 0:
            return _reject(symbol, action, premium,
                           [f"binding limit '{binding_name}' (${allowed_risk:.2f}) below "
                            f"one contract's max loss (${per_contract_loss:.2f})"],
                           requested_qty=req_contracts, unit="contracts", instrument=instrument)
        if approved < req_contracts:
            clamps.append(f"short size clamped by '{binding_name}' to {approved:g} contract(s)")
        notes.append(f"defined-risk short: max loss ~${approved * per_contract_loss:.2f}")
        return _approve(symbol, action, premium, approved, req_contracts, clamps, notes,
                        unit="contracts", instrument=instrument)

    # ---- BUY_TO_OPEN (long option: defined risk = premium paid) ----
    cblock = _concurrency_block(cfg, opening_new=True)
    if cblock:
        return _reject(symbol, action, premium, [cblock],
                       requested_qty=req_contracts, unit="contracts", instrument=instrument)

    cost_per_contract = premium * mult
    req_cost = req_contracts * cost_per_contract
    if cfg["require_settled_cash"]:
        available_cash = _num(account.get("settled_cash")); cash_src = "settled_cash"
    else:
        available_cash = _num(account.get("buying_power")); cash_src = "buying_power"
    available_cash -= _num(cfg["min_cash_reserve"])
    if available_cash <= 0:
        return _reject(symbol, action, premium,
                       [f"no {cash_src} available after reserve "
                        f"(reserve={_num(cfg['min_cash_reserve']):g})"],
                       requested_qty=req_contracts, unit="contracts", instrument=instrument)

    caps = [("requested", req_cost), (f"{cash_src} available", available_cash)]
    if portfolio_value > 0:
        caps.append((f"per-trade cap {cfg['max_trade_pct']:.0%}",
                     portfolio_value * _num(cfg["max_trade_pct"])))
        caps.append((f"option premium cap {cfg['max_option_premium_pct']:.0%}",
                     portfolio_value * _num(cfg["max_option_premium_pct"])))
    else:
        notes.append("portfolio_value missing/0: percent caps skipped (cash-only limit)")

    binding_name, allowed_cost = min(caps, key=lambda kv: kv[1])
    if allowed_cost <= 0:
        return _reject(symbol, action, premium,
                       [f"no room: binding limit '{binding_name}' = {allowed_cost:.2f}"],
                       requested_qty=req_contracts, unit="contracts", instrument=instrument)
    approved = math.floor(allowed_cost / cost_per_contract) if cost_per_contract > 0 else 0
    if approved <= 0:
        return _reject(symbol, action, premium,
                       [f"binding limit '{binding_name}' (${allowed_cost:.2f}) below one "
                        f"contract cost (${cost_per_contract:.2f})"],
                       requested_qty=req_contracts, unit="contracts", instrument=instrument)
    if approved < req_contracts:
        clamps.append(f"long size clamped by '{binding_name}' to {approved:g} contract(s)")
    notes.append(f"defined-risk long: max loss = premium ${approved * cost_per_contract:.2f}")
    return _approve(symbol, action, premium, approved, req_contracts, clamps, notes,
                    unit="contracts", instrument=instrument)


# ==========================================================================
# Result builders
# ==========================================================================

def _unit_notional(qty: float, unit_price: float, unit: str, instrument: Optional[dict]) -> float:
    if unit == "contracts":
        mult = _num((instrument or {}).get("multiplier"), OPTION_MULTIPLIER) or OPTION_MULTIPLIER
        return qty * unit_price * mult
    return qty * unit_price


def _result(decision: str, symbol: str, side: str, unit_price: float,
            approved_qty: float, requested_qty: float, reasons: list, clamps: list,
            notes: list, unit: str, instrument: Optional[dict]) -> dict:
    out = {
        "decision": decision,
        "asset_class": "option" if unit == "contracts" else "equity",
        "symbol": symbol,
        "side": side,
        "unit": unit or ("contracts" if instrument else "shares"),
        "unit_price": round(unit_price, 4),
        "requested": {"quantity": round(requested_qty, 6),
                      "notional": round(_unit_notional(requested_qty, unit_price, unit, instrument), 2)},
        "approved": {"quantity": round(approved_qty, 6),
                     "notional": round(_unit_notional(approved_qty, unit_price, unit, instrument), 2)},
        "reasons": reasons,
        "clamps": clamps,
        "notes": notes,
    }
    if instrument is not None:
        out["instrument"] = instrument
    return out


def _approve(symbol, side, unit_price, approved_qty, requested_qty, clamps, notes,
             unit: str = "shares", instrument: Optional[dict] = None) -> dict:
    return _result("APPROVE", symbol, side, unit_price, approved_qty, requested_qty,
                   [], clamps, notes, unit, instrument)


def _reject(symbol, side, unit_price, reasons, requested_qty: float = 0.0,
            unit: str = "shares", instrument: Optional[dict] = None) -> dict:
    return _result("REJECT", symbol, side, unit_price, 0.0, requested_qty,
                   reasons, [], [], unit, instrument)


def render(r: dict) -> str:
    L = []
    mark = "APPROVE ✔" if r["decision"] == "APPROVE" else "REJECT ✗"
    unit = r.get("unit", "shares")
    head = f"{r['side'].upper()} {r['symbol']}"
    if r.get("asset_class") == "option" and r.get("instrument"):
        i = r["instrument"]
        head += f" {i.get('option_type','')} {i.get('strike','')} exp {i.get('expiration','')}"
        if "days_to_expiry" in i:
            head += f" ({i['days_to_expiry']} DTE)"
    L.append("=" * 58)
    L.append(f" RISK GUARD · {head} @ {r['unit_price']}")
    L.append("=" * 58)
    L.append(f"  Decision : {mark}")
    L.append(f"  Requested: {r['requested']['quantity']:g} {unit} "
             f"(${r['requested']['notional']:.2f})")
    L.append(f"  Approved : {r['approved']['quantity']:g} {unit} "
             f"(${r['approved']['notional']:.2f})")
    if r["clamps"]:
        L.append("  Clamps   : " + "; ".join(r["clamps"]))
    if r["reasons"]:
        L.append("  Rejected : " + "; ".join(r["reasons"]))
    if r["notes"]:
        L.append("  Notes    : " + "; ".join(r["notes"]))
    L.append("-" * 58)
    return "\n".join(L)


def _selftest() -> int:
    scenarios = {
        "EQUITY buy clamped by per-trade cap": {
            "proposal": {"symbol": "AAPL", "side": "buy", "price": 200.0, "quantity": 100},
            "account": {"portfolio_value": 50000, "settled_cash": 20000, "positions": {}},
            "config": {"max_trade_pct": 0.10, "max_position_pct": 0.15},
        },
        "EQUITY sell protected -> reject": {
            "proposal": {"symbol": "MSFT", "side": "sell", "price": 400.0, "quantity": 5},
            "account": {"portfolio_value": 50000,
                        "positions": {"MSFT": {"quantity": 20, "market_value": 8000}}},
            "config": {"protected": ["MSFT"]},
        },
        "EQUITY concurrency cap reached": {
            "proposal": {"symbol": "NFLX", "side": "buy", "price": 600.0, "quantity": 1},
            "account": {"portfolio_value": 50000, "settled_cash": 20000, "positions": {}},
            "config": {"max_concurrent_positions": 5, "open_positions_count": 5},
        },
        "OPTION long call clamped by premium cap": {
            "proposal": {"symbol": "AAPL", "asset_class": "option", "action": "buy_to_open",
                         "option_type": "call", "strike": 210, "premium": 4.50,
                         "expiration": "2026-09-18", "today": "2026-07-14", "contracts": 20},
            "account": {"portfolio_value": 50000, "settled_cash": 20000, "positions": {}},
            "config": {"max_option_premium_pct": 0.05, "min_days_to_expiry": 21},
        },
        "OPTION too close to expiry for swing": {
            "proposal": {"symbol": "TSLA", "asset_class": "option", "action": "buy_to_open",
                         "option_type": "put", "strike": 240, "premium": 3.0,
                         "expiration": "2026-07-18", "today": "2026-07-14", "contracts": 3},
            "account": {"portfolio_value": 50000, "settled_cash": 20000, "positions": {}},
            "config": {"min_days_to_expiry": 21},
        },
        "OPTION naked short blocked by default": {
            "proposal": {"symbol": "SPY", "asset_class": "option", "action": "sell_to_open",
                         "option_type": "put", "strike": 430, "premium": 5.0,
                         "expiration": "2026-08-15", "today": "2026-07-14", "contracts": 2},
            "account": {"portfolio_value": 50000, "settled_cash": 20000, "positions": {}},
            "config": {},
        },
        "OPTION defined-risk short spread (enabled + max_loss)": {
            "proposal": {"symbol": "SPY", "asset_class": "option", "action": "sell_to_open",
                         "option_type": "put", "strike": 430, "premium": 5.0, "max_loss": 3000,
                         "expiration": "2026-08-15", "today": "2026-07-14", "contracts": 2},
            "account": {"portfolio_value": 50000, "settled_cash": 20000, "positions": {}},
            "config": {"allow_uncovered_options": True, "max_trade_pct": 0.10},
        },
        "OPTION close reduces exposure": {
            "proposal": {"symbol": "AAPL", "asset_class": "option", "action": "sell_to_close",
                         "option_type": "call", "strike": 210, "premium": 6.0, "held_contracts": 4,
                         "expiration": "2026-09-18", "today": "2026-07-14", "contracts": 10},
            "account": {"portfolio_value": 50000, "settled_cash": 20000, "positions": {}},
            "config": {},
        },
        "KILL SWITCH": {
            "proposal": {"symbol": "SPY", "side": "buy", "price": 450.0, "quantity": 1},
            "account": {"portfolio_value": 50000, "settled_cash": 20000, "positions": {}},
            "config": {"enabled": False},
        },
    }
    print("[self-test: risk_guard scenarios]\n", file=sys.stderr)
    for name, payload in scenarios.items():
        print(f"### {name}")
        print(render(evaluate(payload)))
        print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Deterministic pre-trade risk gate for autonomous execution (equities + options).")
    ap.add_argument("input", nargs="?",
                    help="JSON: {proposal, account, config}. Without file: self-test.")
    ap.add_argument("--json", action="store_true", help="Machine-readable JSON output.")
    args = ap.parse_args()

    if not args.input:
        return _selftest()

    with open(args.input) as f:
        payload = json.load(f)
    r = evaluate(payload)
    print(json.dumps(r, indent=2, ensure_ascii=False) if args.json else render(r))
    return 0 if r["decision"] == "APPROVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
