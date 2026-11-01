# Open questions: implemented defaults

The spec leaves Q03–Q30 open. The code needs a concrete behaviour for each one, so the defaults below were
chosen to be conservative. Each one is either a config value or a single place in the code, so an answer from the
spec owner replaces it locally.

| Q | Default implemented | Where |
|---|---|---|
| Q03 | USDT-M perpetual only. Margin type is not changed; `check` and `status` show it. | `exchange/binance.py` |
| Q04 | Q0 is entered in coins. Capital limits are not enforced yet. | `enter --qty` |
| Q05 | Any number of symbols, one active cycle per symbol (`enter --symbol`; `strategy.symbol` is the default). `enter` is refused while that symbol has an active cycle or foreign positions/orders. Leverage is set per symbol on first use. | `Runner._enter` |
| Q06 | The event is a manual CLI command. | `enter` |
| Q07 | Plain limit entry. If the price crosses the book, the command warns that it fills as taker. No breakout trigger. | `Runner._enter` |
| Q09 | Positive hedge: GTC limit at the fee-adjusted threshold. Negative hedge: MARKET (`negative_hedge_order`). A rejection with zero fill re-arms the thresholds, up to `hedge_max_attempts`. After that, or after a partial fill followed by a cancel, the cycle goes to PAUSED and `resume` re-sends the remainder. | `Cycle._place_hedge`, `_hedge_progress` |
| Q10 | Executable price: the bid when the hedge sells, the ask when it buys. Checked on every poll (touch), not on candle close. `runtime.trigger_price` can switch to `last` or `mark`. | `Runner._trigger_prices` |
| Q11 | All values are per-config: `hedge_spread`, `adverse_move`, `leverage`, `add_fraction`, `add_trigger_pct`, `add_exit_pct`. | `config.toml` |
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

## Adds

| Topic | Implemented | Where |
|---|---|---|
| When | Only while HEDGED, after either hedge branch. Not while PAUSED, CLOSING or in RECOVERY. | `Cycle.on_quote` |
| Trigger | 1% (`add_trigger_pct`) beyond the leg's entry price as the exchange shows it, without fees. Rounded to the tick away from the market. | `Cycle.add_trigger` |
| Negative spread | While the short average is below the long average (no fees, legs as they are now, checked on every tick), the reference swaps: SHORT add 1% above the long entry, LONG add 1% below the short entry. Once the spread is zero or positive the own-leg rule is back. | `Cycle.spread_negative`, `Cycle.add_trigger` |
| Re-pricing | An unfilled resting add whose trigger no longer matches (the other leg moved, or the spread changed sign) is cancelled and re-placed at the new trigger. A partly filled add is left as it is. | `_manage_adds` |
| Size | `add_fraction` (0.1) × Q0, rounded down to the lot step. Below exchange minimums the add is skipped, and `enter` warns about it. | `Cycle.add_qty` |
| Orders | A GTC limit at the trigger rests on the exchange, so it fills while the bot is offline. If the price is already past the trigger, it fills at once as taker. After it fills, the exit rests as a GTC limit. | `_place_add`, `_place_add_exit` |
| Exit | The price at which the add nets 0.5% (`add_exit_pct`) of its entry notional after its actual entry fee and a maker exit fee. Rounded so the net is at least the target. | `Cycle.add_exit` |
| One per leg | A leg has at most one live add. A further move does nothing until that add closes. Both legs can have one at the same time. | `_manage_adds` |
| Moving trigger | Closing an add does not change the leg's entry price (exchange weighted-average model), so the add stays in it and the next trigger is computed from the new price. | `Book.apply` |
| Partial fill | A partly filled add whose exit is reached cancels its remainder, and the filled part goes to its exit. | `_manage_adds` |
| Losing add | Held: no stop, no timeout (see Q19). | — |
| Close | `close` cancels resting adds and exits, then closes both legs at MARKET, open adds included. | `_close_progress` |

## Known gaps before longer demo runs

- No risk limits (Q19). Until they exist, keep demo position sizes small.
- Market data comes from REST polling: 0.25 s while unhedged (`fast_poll_interval_s`), 1 s otherwise. A quiet
  tick costs one bookTicker call, plus openOrders while orders are working; trades are read only when an order
  report shows new executed quantity (and every 15 s as a safety net). A WebSocket stream (bookTicker and the
  user data stream) is the next latency step.
- Backtest component: not started (R15 puts it after demo).
