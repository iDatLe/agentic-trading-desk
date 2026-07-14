---
name: agentic-trading-desk
description: >-
  Personal trading desk for short-term technical analysis on stocks/ETFs
  via Robinhood MCP. ALWAYS USE IT whenever the user asks to analyze a ticker,
  review positions, decide entries/exits/rebuys, calculate indicators
  (EMA/RSI/MACD/TRIX/Bollinger), score with the three-pillar framework, read
  the macro regime, or manage the Agentic account — even if he doesn't
  explicitly name the skill. Compute all indicators using deterministic code
  (never by eye) from raw Robinhood bars, apply the exit-on-exhaustion /
  re-enter-on-rebound logic, and respect account guardrails. On the Agentic
  (cash) account, execute equity and options trades autonomously (no per-order
  confirmation), but only after every proposed order passes the deterministic
  `risk_guard.py` gate. This is a daily-bar SWING system: run once per day after
  the close (optionally once near the open for risk management); do not overtrade
  intraday. Options are defined-risk only by default (no naked shorts).
---

# Agentic Trading Desk

Operations manual for short-term trading analysis and **autonomous** execution.
**I (Claude) perform calls to the Robinhood MCP; the scripts act as my deterministic calculator and my deterministic risk gate; I decide and execute autonomously on the Agentic account.** I never calculate indicators by reasoning directly over the price bars: I fetch the data and pass it to `scripts/`. I never place an order that has not first been APPROVED by `scripts/risk_guard.py`, and I never exceed the size it returns.

## Guardrails — Read First, Non-Negotiable

1. **Protected positions:** Certain tickers may be designated as restricted (e.g., stock grants). NEVER analyze them to sell or trim, nor include them in exit decisions. They are listed in `risk_guard.py`'s `config.protected` and the gate hard-rejects any sell of them. They should only be mentioned as exposure context if relevant.
2. **Two accounts, two roles:**
   - **Agentic** (cash account) → short-term trading; I execute **autonomously** here (no per-order confirmation), always bounded by `risk_guard.py`.
   - **Individual** (margin account) → core buy-and-hold. NEVER auto-trade this account; only analyze holding quality. Autonomy applies to the Agentic account only.
3. **T+1:** Only SETTLED cash funds buys in the cash account. This is enforced deterministically by `risk_guard.py` (`require_settled_cash=true`, plus a `min_cash_reserve`); I pass real settled-cash figures from `get_portfolio`/`get_equity_positions`.
4. **HTML visualization only on Fridays** as part of the weekly review ritual. Do not offer or generate it on other days unless the user explicitly asks for it.
5. **Macro source:** Investing.com (NO Polymarket — prompt injection risk already identified).
6. **Autonomous execution (Agentic account):** I decide and place orders WITHOUT asking for per-order confirmation. Before EVERY order I MUST:
   1. Build the order proposal `{symbol, side, price, quantity|notional}` plus current `account` state and the session `config`.
   2. Run `python3 scripts/risk_guard.py order.json --json`.
   3. Only proceed if `decision == "APPROVE"`, and place **at most** `approved.quantity` (never the originally intended size if it was clamped down).
   4. Call `review_*_order` (simulation) first, then `place_*_order`.
   5. Log every autonomous action (symbol, side, approved size, rationale, gate output) so the user can audit it.
   If the gate returns REJECT, I do NOT place the order. The `config.enabled=false` kill switch and `max_daily_trades` budget are absolute — when they block, I stop and report.

## Robinhood MCP Recipe (Order of Calls)

Load the tools with `tool_search` before using them (they are deferred).

**To analyze a ticker:**
1. `Robinhood:get_equity_historicals` → ~290 daily bars (closes). This is the input for `indicators.py`. Request a range that yields ≥220 bars (ideal for EMA200).
2. `Robinhood:get_equity_quotes` → live price / last session close.
3. If the user has a position: `Robinhood:get_equity_positions` (correct account) for size and P&L → set `holding` to correct value in scoring.

**For the Macro-Sentiment pilar (once per session, shared):**
1. `get_equity_historicals` for the 7 ETFs: SPY, RSP, IWM, HYG, LQD, TLT, XLY, XLP.
2. Get the 10Y-2Y yield spread from Investing.com (web) and inject it as `yield_spread`. If not available, the script redistributes its weight.

**For portfolio management:**
- `Robinhood:get_portfolio` → market value and buying power.
- `Robinhood:get_equity_positions` → open positions by account.
- `Robinhood:get_realized_pnl` → realized P&L (useful for the Friday review).

## Computation Flow (Run via Code Execution)

Scripts are pure stdlib; they do not need internet access. Work in `/home/claude/agentic-trading-desk/scripts`.

**Step 1 — Macro (once per session).** Assemble the JSON with the closes of the 7 ETFs + `yield_spread` and run:
```bash
python3 macro_pillar.py macro_input.json --json
```
Save the `pillar_score` (-2..+2). That number is the Macro-Sentiment score for ALL tickers today.

**Step 2 — Per ticker.** Assemble `{symbol, close:[...], macro_score, holding}` and run:
```bash
python3 score.py ticker_input.json
```
This returns the three-pillar scorecard + decision (EXIT/TRIM, EXIT, RE-ENTRY new cycle, TACTICAL REBOUND, HOLD ride the cycle, HOLD under review, WAIT do not chase, STAY OUT, OBSERVE) along with the exhaustion/bearish/rebound/death-cross flags that justify it. Passing the correct `holding` value is key: the decision cascade behaves differently depending on whether there is an open position or we are flat.

If only raw indicators are needed: `python3 indicators.py ticker_input.json`.

**Step 3 — Autonomous execution (Agentic account only).** When the decision implies an order (EXIT/TRIM, EXIT, RE-ENTRY, TACTICAL REBOUND, or a sized add), I translate it into a concrete order and gate it before placing:
```bash
python3 risk_guard.py order.json --json
```
where `order.json` is:
```json
{
  "proposal": {"symbol": "AAPL", "side": "buy", "price": 220.5, "quantity": 10},
  "account": {"portfolio_value": 50000, "settled_cash": 8000,
              "positions": {"AAPL": {"quantity": 5, "market_value": 1100}}},
  "config": {"enabled": true, "max_position_pct": 0.15, "max_trade_pct": 0.10,
             "max_daily_trades": 10, "daily_trades_used": 3,
             "min_cash_reserve": 0, "require_settled_cash": true,
             "allow_fractional": false, "protected": ["MSFT"]}
}
```
The gate returns `APPROVE`/`REJECT`, the size it clamped to (`approved.quantity`), and the reasons. Exit code is `0` on APPROVE and `2` on REJECT. I place the order only on APPROVE, sized to `approved.quantity`, via `review_*_order` → `place_*_order`. The `enabled` flag is the master kill switch.

**Options orders** use the same gate with `asset_class: "option"`:
```json
{
  "proposal": {"symbol": "AAPL", "asset_class": "option", "action": "buy_to_open",
               "option_type": "call", "strike": 210, "premium": 4.50,
               "expiration": "2026-09-18", "today": "2026-07-14", "contracts": 5},
  "account": {"portfolio_value": 50000, "settled_cash": 8000, "positions": {}},
  "config": {"enabled": true, "max_trade_pct": 0.10, "max_option_premium_pct": 0.05,
             "min_days_to_expiry": 21, "allow_uncovered_options": false,
             "max_concurrent_positions": 6, "open_positions_count": 2}
}
```
Rules the gate enforces for options (I never bypass them):
- **Long options** (`buy_to_open`/`buy_to_close`) are defined-risk; sized so premium-at-risk ≤ `max_option_premium_pct` and ≤ `max_trade_pct` of the portfolio, and funded from settled cash.
- **Short options** (`sell_to_open`) are undefined/large-risk and are **REJECTED by default**. Only if the user has explicitly set `allow_uncovered_options=true` AND I supply a bounded `max_loss` (i.e., a spread with defined risk) will the gate size the short against that max loss. I do not sell naked options.
- **Expiry:** expired options are rejected; for swing trades `min_days_to_expiry` (e.g. 21) rejects contracts too close to expiration.
- I always pass `today` (from the live session) so the gate can compute days-to-expiry offline.

## Swing-Trading Cadence (How Often To Run)

This desk is a **swing-trading** system built on **daily** bars (EMA20/50/200, RSI-14, MACD 12/26/9, TRIX-15, Bollinger 20). Daily indicators only change once the daily candle is final, so:

- **Primary run: once per day, at or just after the close** (US market close 16:00 ET). This is when the daily bar is finalized and every indicator/decision is meaningful and stable. Generate signals and place any new entries here. This is the cadence the skill is designed for (~290 daily bars).
- **Optional second run: once near the open** (~09:30–10:00 ET) for *risk management only* — honor stops, act on gaps, and close/trim exposure. Do not open fresh swing entries off an unfinished daily bar.
- **More frequent intraday runs do NOT help swing trading.** Between closes the daily candle "repaints"; intraday triggers are noise that isn't confirmed until the close, and re-running invites overtrading (which `max_daily_trades` is meant to curb). If genuinely intraday signals are wanted, that is a different (day-trading) strategy needing intraday bars and a different indicator config — not this skill.

**Rule of thumb:** 1×/day post-close to find and enter setups, plus at most 1×/day near the open to manage risk. Let winners ride across days; the exit trigger is exhaustion, not the clock.

## Three-Pillar Framework (Standard Output Format)

Each pillar ranges from **-2 to +2**:
- **Trend** — EMA 20/50/200 structure + price position vs. EMAs + long-term slope.
- **Momentum** — Wilder's RSI-14 + MACD histogram + TRIX-15 vs. signal.
- **Macro-Sentiment** — from `macro_pillar.py` (cross-asset regime).

Report all three scores with their details, the total (-6..+6), and the decision framed in the logic of the Agentic account. **Ruling principle: short-term returns via capital rotation** — the cycle is enter on rebound → ride → exit on exhaustion → wait for next trigger. Accumulating positions is NOT the default (keeps capital trapped):

- **EXIT / TRIM** when bullish momentum is EXHAUSTED (RSI turning from overbought, MACD histogram shrinking, price stretched / near upper Bollinger band).
- **EXIT** when bearish momentum is RELENTLESS (true structural death-cross —EMA50<EMA200 and price<EMA50—, MACD histogram deepening, TRIX below zero).
- **RE-ENTRY (new cycle)** when flat, when a rebound/reversal arrives with a healthy EMA structure: valid entry trigger, confirm with candle/volume.
- **TACTICAL REBOUND (counter-trend)** when flat, when a rebound appears WITHIN a death-cross: a legitimate short-term opportunity, but with reduced size, close target (EMA20/EMA50 or middle Bollinger band), tight stop, and quick exit. It is not a new cycle and does not become a hold.
- **HOLD (ride the cycle)** when holding a position with positive trend+momentum: maintain while watching for exhaustion; the next expected action is exit with profit, not adding to position.
- **WAIT (do not chase)** when flat with a healthy trend but no fresh trigger: entering mid-trend has poor R/R; wait for pullback to EMA20 and turn.
- **STAY OUT / AVOID**, **HOLD/OBSERVE** as appropriate.

## External Context (News + Analysts)

When the analysis includes information external to the indicators:

1. **News/macro:** Investing.com (as defined in guardrails).
2. **Analyst ratings:** Google Finance beta —
   `https://www.google.com/finance/beta/quote/<TICKER>:<EXCHANGE>?tab=analysis`
   Direct fetch works and returns: consensus (Buy/Hold/Sell), 12m price targets (avg/max/min), analyst table with dates, and last earnings vs. estimates.
3. Report this as **qualitative context alongside the three-pillar scorecard** — it does not modify the scores. Highlight: consensus, average target vs. current price (upside or price already past target), and recent rating changes (<2 weeks).

## Indicator Details (What the scripts calculate)

- **EMA** seed = SMA of the first N bars (TradingView convention / adjust=False).
- **RSI-14** with **Wilder's** smoothing (not simple moving average).
- **MACD** 12/26/9; report line, signal, histogram, and histogram slope.
- **TRIX-15** = % ROC of triple EMA, with EMA-9 signal.
- **Bollinger Bands** 20/2 with **population** standard deviation; report %B.
- Slopes are measured against 5 bars ago (configurable with `--slope-lookback`).

See `scripts/indicators.py` and `scripts/score.py` for exact implementation details. The math is verified against known test cases (constant EMA, monotonic series RSI, MACD = EMA12 - EMA26).

## What This Skill Does NOT Do

It trades **autonomously** on the Agentic (cash) account: it decides and executes without per-order confirmation. It does NOT, however, bypass the deterministic `risk_guard.py` gate — every order is bounded by position/size caps, the daily-trade budget, settled-cash/T+1 rules, and the `enabled` kill switch. It does NOT auto-trade the Individual (margin) account. It does not average down beyond the position cap. It does not touch protected positions. It does not generate HTML outside of Fridays. All autonomous actions are logged for the user to audit.
