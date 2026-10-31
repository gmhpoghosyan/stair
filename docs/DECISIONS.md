# Open questions: implemented defaults

The spec leaves Q03–Q30 open. The code needs a concrete behaviour for each one, so the defaults below were
chosen to be conservative. Each one is either a config value or a single place in the code, so an answer from the
spec owner replaces it locally.

| Q | Default implemented | Where |
|---|---|---|
| Q03 | USDT-M perpetual only. Margin type is not changed; `check` and `status` show it. | `exchange/binance.py` |
| Q04 | Q0 is entered in coins. Capital limits are not enforced yet. | `enter --qty` |
| Q05 | One symbol (`strategy.symbol`), one active cycle. `enter` is refused while one is active or the symbol has foreign positions/orders. | `Runner._enter` |
| Q06 | The event is a manual CLI command. Levels are entered manually. | `enter`, `level add` |
| Q07 | Plain limit entry. If the price crosses the book, the command warns that it fills as taker. No breakout trigger. | `Runner._enter` |
| Q09 | Positive hedge: GTC limit at the fee-adjusted threshold. Negative hedge: MARKET (`negative_hedge_order`). A rejection with zero fill re-arms the thresholds, up to `hedge_max_attempts`. After that, or after a partial fill followed by a cancel, the cycle goes to PAUSED and `resume` re-sends the remainder. | `Cycle._place_hedge`, `_hedge_progress` |
| Q10 | Executable price: the bid when the hedge sells, the ask when it buys. Checked on every poll (touch), not on candle close. `runtime.trigger_price` can switch to `last` or `mark`. | `Runner._trigger_prices` |
| Q11 | All values are per-config: `hedge_spread`, `adverse_move`, `leverage`, `add_fraction`. | `config.toml` |
| Q12 | Levels come from confirmed pivots (LuxAlgo "Support and Resistance Levels with Breaks" logic, 15/15 bars) on `levels.interval` (30m), from closed candles only. The latest resistance and support are synced into the cycle as `auto` levels; manual levels still work. Break signals (volume osc > 20) are logged as entry candidates, not traded. No 4h/1d confirmation yet. | `stair/levels`, `LevelFeed` |
| Q13 | Add exit = level `--exit` or `add_exit_pct` (0.5%) from the add price. | `Cycle._default_exit` |
| Q14 | At most `max_adds_per_side` (3) live adds per side and one per level. Pending adds count toward the limit. A gap over several levels opens each level's resting order independently. | `_place_new_adds` |
| Q15 | Long and short adds may be open at the same time. | — |
| Q16 | A losing add is held: no stop, no timeout. (Limits for Q19 are still needed.) | — |
| Q17 | A level re-arms as soon as its add is fully closed. The resting post-only order needs a new touch to fill, so sitting at the level does not repeat (T05). | `_release_level` |
| Q18 | Base quantities stay at Q0 until manual close. | — |
| Q19 | **Not implemented**: no loss, drawdown or margin limits and no emergency auto-close. | — |
| Q20 | `close` cancels every cycle order, waits for final fills, then closes both sides at MARKET. The bot then waits for the next manual `enter`. | `Cycle._close_progress` |
| Q21 | Adds and add exits are resting exchange orders, so they keep working while the PC is off. Hedge triggers are local and do not. After a restart the bot reconciles, then continues automatically. | `Runner.preflight` |
| Q22 | External changes are not accepted. A position mismatch on two consecutive checks puts the cycle into RECOVERY, which halts price-driven actions until `ack`. | `Runner._reconcile` |
| Q23 | New bot (this repo). CLI first; the `control` component is ready for a local web UI. | `stair/control` |
| Q24 | `pause` is allowed only when HEDGED. It cancels unfilled add entries and blocks new adds; exits keep being managed. | `Cycle.pause` |
| Q25 | The positive target includes opening fees only. The hedge fee is estimated at the taker rate, so a maker fill only does better. Funding is tracked in the cycle total, not in the target. | `positive_hedge_price` |
| Q26 | The negative threshold is a raw price move before costs: E0 × (1 ∓ a). | `negative_hedge_price` |
| Q27 | Allowed: an add is cut on its own exit even if exchange realized PnL against the side average is negative. | `_add_progress` |
| Q28 | The filter is ON (`require_avg_improvement`): long adds only below EL, short adds only above ES. | `_improves_average` |
| Q29 | Entries are not re-placed. A cancel, expiry or post-only rejection without fills finishes the cycle, and you re-enter. | `_entry_progress` |
| Q30 | When a hedge threshold is reached during a partial entry, the remainder is cancelled. Q0 is fixed at the final filled quantity before the hedge is sent. | `_check_hedge_triggers` |

## Known gaps before longer demo runs

- No risk limits (Q19). Until they exist, keep demo position sizes small.
- Market data comes from REST polling: 0.25 s while unhedged (`fast_poll_interval_s`), 1 s otherwise. A quiet
  tick costs one bookTicker call, plus openOrders while orders are working; trades are read only when an order
  report shows new executed quantity (and every 15 s as a safety net). A WebSocket stream (bookTicker and the
  user data stream) is the next latency step.
- T19 (checking on Binance Demo how the average behaves after a partial reduce) still needs to be run manually.
- Backtest component: not started (R15 puts it after demo).
