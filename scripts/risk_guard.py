#!/usr/bin/env python3
"""
risk_guard.py
=============
Deterministic PRE-TRADE risk gate for AUTONOMOUS execution.

In autonomous mode the agent decides *and* places orders on the Agentic (cash)
account without asking the user before each trade. This module is the
deterministic safety layer that BOUNDS those autonomous decisions. It follows
the same philosophy as the rest of the desk: the LLM decides *what* to do;
deterministic code enforces *how much* is allowed.

Contract:
  - It NEVER invents a trade. It only takes a proposed order and returns
    APPROVE / REJECT, clamping the size down to hard risk limits.
  - The approved size is always <= the proposed size. The agent must place at
    most `approved.quantity` (or `approved.notional`); never more.
  - A global kill switch (`config.enabled = false`) rejects everything.

stdlib only. Python 3.9+.

Input JSON:
{
  "proposal": {
    "symbol": "AAPL",
    "side": "buy" | "sell",
    "price": 220.5,
    "quantity": 10            // OR "notional": 2000  (one of the two)
  },
  "account": {
    "portfolio_value": 50000,
    "settled_cash": 8000,        // settled / T+1 withdrawable cash
    "buying_power": 12000,       // may include unsettled; used only if require_settled_cash=false
    "positions": { "AAPL": { "quantity": 5, "market_value": 1100 } }
  },
  "config": {
    "enabled": true,             // global kill switch; false => REJECT all
    "max_position_pct": 0.15,    // max fraction of portfolio in ONE symbol
    "max_trade_pct": 0.10,       // max fraction of portfolio per SINGLE order
    "max_daily_trades": 10,
    "daily_trades_used": 0,
    "min_cash_reserve": 0,       // settled cash to keep untouched on buys
    "require_settled_cash": true,// buys funded only by settled cash (cash acct / T+1)
    "allow_fractional": false,   // if false, buy/sell whole shares only
    "protected": ["MSFT"]        // symbols that must never be sold/trimmed
  }
}
"""
from __future__ import annotations
import argparse
import json
import math
import sys
from typing import Optional


DEFAULT_CONFIG = {
    "enabled": True,
    "max_position_pct": 0.15,
    "max_trade_pct": 0.10,
    "max_daily_trades": 10,
    "daily_trades_used": 0,
    "min_cash_reserve": 0.0,
    "require_settled_cash": True,
    "allow_fractional": False,
    "protected": [],
}


def _num(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _size_from_notional(notional: float, price: float, fractional: bool) -> float:
    if price <= 0:
        return 0.0
    qty = notional / price
    return qty if fractional else math.floor(qty)


def evaluate(payload: dict) -> dict:
    proposal = payload.get("proposal", {}) or {}
    account = payload.get("account", {}) or {}
    cfg = {**DEFAULT_CONFIG, **(payload.get("config", {}) or {})}

    reasons: list[str] = []      # why the order was rejected
    clamps: list[str] = []       # size reductions that were applied
    notes: list[str] = []

    symbol = str(proposal.get("symbol", "")).upper()
    side = str(proposal.get("side", "")).lower()
    price = _num(proposal.get("price"))
    fractional = bool(cfg["allow_fractional"])

    def deny(reason: str, req_qty: float = 0.0) -> dict:
        reasons.append(reason)
        return _result("REJECT", symbol, side, price, 0.0, req_qty, reasons, clamps, notes)

    # --- Basic validation ---
    if not symbol:
        return deny("missing symbol")
    if side not in ("buy", "sell"):
        return deny(f"invalid side '{side}' (expected buy/sell)")
    if price <= 0:
        return deny("price missing or <= 0")

    # --- Global kill switch ---
    if not cfg["enabled"]:
        return deny("autonomous trading disabled (config.enabled=false) — kill switch active")

    # --- Daily trade budget ---
    if _num(cfg["daily_trades_used"]) >= _num(cfg["max_daily_trades"]):
        return deny(
            f"daily trade limit reached ({int(_num(cfg['daily_trades_used']))}/"
            f"{int(_num(cfg['max_daily_trades']))})"
        )

    # --- Requested size (from quantity or notional) ---
    if proposal.get("quantity") is not None:
        req_qty = _num(proposal.get("quantity"))
    elif proposal.get("notional") is not None:
        req_qty = _size_from_notional(_num(proposal.get("notional")), price, fractional)
    else:
        return deny("proposal must include 'quantity' or 'notional'")
    if req_qty <= 0:
        return deny("requested size <= 0")

    positions = account.get("positions", {}) or {}
    pos = positions.get(symbol, {}) or {}
    held_qty = _num(pos.get("quantity"))
    held_mv = _num(pos.get("market_value"), held_qty * price)
    portfolio_value = _num(account.get("portfolio_value"))

    # ======================================================================
    # SELL
    # ======================================================================
    if side == "sell":
        if symbol in {s.upper() for s in cfg["protected"]}:
            return deny(f"{symbol} is protected — never sell/trim", req_qty)
        if held_qty <= 0:
            return deny(f"no open position in {symbol} to sell (no shorting)", req_qty)
        approved = min(req_qty, held_qty)
        if approved < req_qty:
            clamps.append(f"sell size clamped to held quantity {held_qty:g}")
        if not fractional:
            approved = math.floor(approved)
        if approved <= 0:
            return deny("nothing sellable after clamping to whole shares", req_qty)
        return _result("APPROVE", symbol, side, price, approved, req_qty,
                       reasons, clamps, notes)

    # ======================================================================
    # BUY
    # ======================================================================
    req_notional = req_qty * price

    if cfg["require_settled_cash"]:
        available_cash = _num(account.get("settled_cash"))
        cash_src = "settled_cash"
    else:
        available_cash = _num(account.get("buying_power"))
        cash_src = "buying_power"
    available_cash -= _num(cfg["min_cash_reserve"])

    if available_cash <= 0:
        return deny(
            f"no {cash_src} available after reserve "
            f"(reserve={_num(cfg['min_cash_reserve']):g})", req_qty
        )

    caps = [("requested", req_notional)]
    caps.append((f"{cash_src} available", available_cash))

    if portfolio_value > 0:
        max_trade_notional = portfolio_value * _num(cfg["max_trade_pct"])
        caps.append((f"per-trade cap {cfg['max_trade_pct']:.0%}", max_trade_notional))
        position_room = portfolio_value * _num(cfg["max_position_pct"]) - held_mv
        caps.append((f"position cap {cfg['max_position_pct']:.0%}", position_room))
    else:
        notes.append("portfolio_value missing/0: percent caps skipped (cash-only limit)")

    binding_name, allowed_notional = min(caps, key=lambda kv: kv[1])
    if allowed_notional <= 0:
        return deny(
            f"no room to buy {symbol}: binding limit '{binding_name}' "
            f"= {allowed_notional:.2f}", req_qty
        )

    approved_qty = _size_from_notional(allowed_notional, price, fractional)
    if approved_qty <= 0:
        return deny(
            f"binding limit '{binding_name}' (${allowed_notional:.2f}) "
            f"is below one share (${price:.2f})", req_qty
        )
    if approved_qty < req_qty:
        clamps.append(f"buy size clamped by '{binding_name}' to {approved_qty:g} share(s)")

    return _result("APPROVE", symbol, side, price, approved_qty, req_qty,
                   reasons, clamps, notes)


def _result(decision: str, symbol: str, side: str, price: float,
            approved_qty: float, requested_qty: float,
            reasons: list, clamps: list, notes: list) -> dict:
    return {
        "decision": decision,
        "symbol": symbol,
        "side": side,
        "price": round(price, 4),
        "requested": {"quantity": round(requested_qty, 6),
                      "notional": round(requested_qty * price, 2)},
        "approved": {"quantity": round(approved_qty, 6),
                     "notional": round(approved_qty * price, 2)},
        "reasons": reasons,
        "clamps": clamps,
        "notes": notes,
    }


def render(r: dict) -> str:
    L = []
    mark = "APPROVE ✔" if r["decision"] == "APPROVE" else "REJECT ✗"
    L.append("=" * 54)
    L.append(f" RISK GUARD · {r['side'].upper()} {r['symbol']} @ {r['price']}")
    L.append("=" * 54)
    L.append(f"  Decision : {mark}")
    L.append(f"  Requested: {r['requested']['quantity']:g} "
             f"(${r['requested']['notional']:.2f})")
    L.append(f"  Approved : {r['approved']['quantity']:g} "
             f"(${r['approved']['notional']:.2f})")
    if r["clamps"]:
        L.append("  Clamps   : " + "; ".join(r["clamps"]))
    if r["reasons"]:
        L.append("  Rejected : " + "; ".join(r["reasons"]))
    if r["notes"]:
        L.append("  Notes    : " + "; ".join(r["notes"]))
    L.append("-" * 54)
    return "\n".join(L)


def _selftest() -> int:
    scenarios = {
        "buy clamped by per-trade cap": {
            "proposal": {"symbol": "AAPL", "side": "buy", "price": 200.0, "quantity": 100},
            "account": {"portfolio_value": 50000, "settled_cash": 20000,
                        "positions": {}},
            "config": {"max_trade_pct": 0.10, "max_position_pct": 0.15},
        },
        "buy blocked by settled cash": {
            "proposal": {"symbol": "NVDA", "side": "buy", "price": 120.0, "notional": 5000},
            "account": {"portfolio_value": 50000, "settled_cash": 300, "buying_power": 9000,
                        "positions": {}},
            "config": {"require_settled_cash": True},
        },
        "buy blocked by position cap (already large)": {
            "proposal": {"symbol": "TSLA", "side": "buy", "price": 250.0, "quantity": 10},
            "account": {"portfolio_value": 50000, "settled_cash": 20000,
                        "positions": {"TSLA": {"quantity": 32, "market_value": 8000}}},
            "config": {"max_position_pct": 0.15},
        },
        "sell protected -> reject": {
            "proposal": {"symbol": "MSFT", "side": "sell", "price": 400.0, "quantity": 5},
            "account": {"portfolio_value": 50000,
                        "positions": {"MSFT": {"quantity": 20, "market_value": 8000}}},
            "config": {"protected": ["MSFT"]},
        },
        "sell clamped to held": {
            "proposal": {"symbol": "AMD", "side": "sell", "price": 150.0, "quantity": 50},
            "account": {"portfolio_value": 50000,
                        "positions": {"AMD": {"quantity": 12, "market_value": 1800}}},
            "config": {},
        },
        "kill switch": {
            "proposal": {"symbol": "SPY", "side": "buy", "price": 450.0, "quantity": 1},
            "account": {"portfolio_value": 50000, "settled_cash": 20000, "positions": {}},
            "config": {"enabled": False},
        },
        "daily limit reached": {
            "proposal": {"symbol": "SPY", "side": "buy", "price": 450.0, "quantity": 1},
            "account": {"portfolio_value": 50000, "settled_cash": 20000, "positions": {}},
            "config": {"max_daily_trades": 5, "daily_trades_used": 5},
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
        description="Deterministic pre-trade risk gate for autonomous execution.")
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
    # Non-zero exit on REJECT so callers/automation can branch on it.
    return 0 if r["decision"] == "APPROVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
