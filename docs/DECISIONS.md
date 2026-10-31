# Open questions: implemented defaults

The spec leaves Q03–Q30 open. The code needs a concrete behaviour for each one, so the defaults below were
chosen to be conservative. Each one is either a config value or a single place in the code, so an answer from the
spec owner replaces it locally.

| Q | Default implemented | Where |
|---|---|---|
| Q03 | USDT-M perpetual only. Margin type is not changed; `check` and `status` show it. | `exchange/binance.py` |
| Q04 | Q0 is entered in coins. Capital limits are not enforced yet. | `enter --qty` |
| Q05 | One symbol (`strategy.symbol`), one active cycle. `enter` is refused while one is active or the symbol has foreign positions/orders. | `Runner._enter` |
| Q06 | The event is a manual CLI command. | `enter` |
| Q07 | Plain limit entry. If the price crosses the book, the command warns that it fills as taker. No breakout trigger. | `Runner._enter` |
| Q09 | Positive hedge: GTC limit at the fee-adjusted threshold. Negative hedge: MARKET (`negative_hedge_order`). A rejection with zero fill re-arms the thresholds, up to `hedge_max_attempts`. After that, or after a partial fill followed by a cancel, the cycle goes to PAUSED and `resume` re-sends the remainder. | `Cycle._place_hedge`, `_hedge_progress` |
| Q10 | Executable price: the bid when the hedge sells, the ask when it buys. Checked on every poll (touch), not on candle close. `runtime.trigger_price` can switch to `last` or `mark`. | `Runner._trigger_prices` |
| Q11 | All values are per-config: `hedge_spread`, `adverse_move`, `leverage`. | `config.toml` |
| Q18 | Base quantities stay at Q0 until manual close. | — |
| Q19 | **Not implemented**: no loss, drawdown or margin limits and no emergency auto-close. | — |
| Q20 | `close` cancels every cycle order, waits for final fills, then closes both sides at MARKET. The bot then waits for the next manual `enter`. | `Cycle._close_progress` |
| Q21 | A positive hedge, once placed, is a resting exchange order and keeps working while the PC is off. Hedge triggers are local and do not. After a restart the bot reconciles, then continues automatically. | `Runner.preflight` |
| Q22 | External changes are not accepted. A position mismatch on two consecutive checks puts the cycle into RECOVERY, which halts price-driven actions until `ack`. | `Runner._reconcile` |
| Q23 | New bot (this repo). CLI first; the `control` component is ready for a local web UI. | `stair/control` |
| Q25 | The positive target includes opening fees only. The hedge fee is estimated at the taker rate, so a maker fill only does better. Funding is tracked in the cycle total, not in the target. | `positive_hedge_price` |
| Q26 | The negative threshold is a raw price move before costs: E0 × (1 ∓ a). | `negative_hedge_price` |
| Q29 | Entries are not re-placed. A cancel, expiry or post-only rejection without fills finishes the cycle, and you re-enter. | `_entry_progress` |
| Q30 | When a hedge threshold is reached during a partial entry, the remainder is cancelled. Q0 is fixed at the final filled quantity before the hedge is sent. | `_check_hedge_triggers` |

## Known gaps before longer demo runs

- No risk limits (Q19). Until they exist, keep demo position sizes small.
- Market data comes from REST polling: 0.25 s while unhedged (`fast_poll_interval_s`), 1 s otherwise. A quiet
  tick costs one bookTicker call, plus openOrders while orders are working; trades are read only when an order
  report shows new executed quantity (and every 15 s as a safety net). A WebSocket stream (bookTicker and the
  user data stream) is the next latency step.
- Backtest component: not started (R15 puts it after demo).
