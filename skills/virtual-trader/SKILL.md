---
name: virtual-trader
description: Intraday virtual paper trading for SPX500 and Gold using real yfinance market data. No broker account, no KYC. Reads active strategy from agent-network cycle state — including its timeframe (15m, 1h, 4h) — generates entry/exit signals on the correct candle interval, and simulates execution. Writes virtual_account.json for trading-report to consume.
division: trading
schedule: daily 18:00 (runs first, before trading-report)
requires: yfinance, pandas
---

## Role
Simulate paper trades on SPX500 (^GSPC) and Gold (GC=F) using real intraday market data from Yahoo Finance.
No broker account required. The active strategy is loaded from agent-network, and its declared timeframe
(15m, 1h, or 4h) is used to fetch the correct candle interval. 4h candles are built by resampling 1h data.

## Data Sources
- Price data: Yahoo Finance via `yfinance` (`^GSPC` for SPX500, `GC=F` for Gold)
- Active strategy: `C:\Users\Tyler\agent-network\state\spx500_cycle_state.json`
  - Reads `active_strategy.strategy_schema.metadata.timeframe` for candle interval (15m / 1h / 4h)
- Account state: `C:\Users\Tyler\agent-network\state\virtual_account.json`

## Timeframe Resolution
Timeframe is read from the active strategy schema. Fallback is `1d` if not set.

| Strategy timeframe | yfinance fetch  | Candles returned |
|--------------------|-----------------|------------------|
| 15m                | interval=15m, 5d  | ~130 bars        |
| 1h                 | interval=1h, 30d  | ~480 bars        |
| 4h                 | interval=1h, 30d → resample | ~120 bars |
| 1d (fallback)      | interval=1d, 3mo  | ~63 bars         |

## Account Setup
- Default starting balance: $10,000 (virtual)
- Risk per trade: 1% of account balance
- Stop loss: 1% from entry price
- Instruments: SPX500 (S&P 500), Gold (Gold Futures)

## Signal Engine
Parses the active strategy_id from agent-network and runs matching indicators
against candles at the strategy's declared timeframe:

1. **EMA + Price Above + ATR Expanding (Long)**
   - Entry: Price > EMA(n), ATR expanding (current ATR > 5-bar average)
   - Exit: Price < EMA(n)

2. **Bollinger Lower Band Touch + ATR Expanding**
   - Entry: Previous close touched lower Bollinger band, price rebounding, ATR expanding
   - Exit: Price reaches Bollinger middle band

3. **Generic EMA Crossover (fallback)**
   - Entry: EMA20 > EMA50, ATR expanding
   - Exit: EMA20 crosses below EMA50

## Trade Record Format
Each trade written to `virtual_account.json` trade_log:
```json
{
  "order_id":    "VA-20260321-A1B2C3",
  "type":        "entry | exit",
  "strategy_id": "active strategy name from agent-network",
  "side":        "buy | sell",
  "symbol":      "SPX500 | GOLD",
  "filled_price": 5000.00,
  "qty":         2,
  "risk_usd":    100.00,
  "reason":      "signal description",
  "pnl":         null,
  "r_multiple":  null,
  "timestamp":   "2026-03-21T18:00:00+00:00"
}
```

## Output
Returns dict consumed by division-chief-trading:
```json
{
  "status":          "success | partial | failed",
  "trades_made":     0,
  "open_positions":  0,
  "account_balance": 10000.00,
  "strategy_id":     "active strategy name",
  "summary":         "human-readable summary",
  "escalate":        false,
  "escalation_reason": ""
}
```

## Escalation
Escalate only when `status: failed` (yfinance unreachable, state file corrupt).
Do NOT escalate for: no signals today, open positions held, partial data.

## Error Handling
- yfinance unavailable: log error, return status: partial with error message
- agent-network state missing: use default strategy (Bollinger + ATR)
- Individual instrument failure: skip that instrument, continue others
- Always write virtual_account.json even if some instruments failed
