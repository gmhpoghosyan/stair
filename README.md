# stair

Local bot for the "hedge with repeated adds" strategy on Binance USD-M futures (spec: ТЗ v0.1, 31.10.2026).
A first entry is hedged by an equal opposite position. Adds of 1/10 Q0 are then opened at user levels
and cut back on profitable pullbacks, which widens the spread between the long and short averages.

Status: **demo stage** (R15). Live trading is locked behind an explicit switch.

## Components

```
app ──► execution ──► strategy ──► core
 │          │  └────► exchange ──► core
 │          └───────► storage ───► core, strategy (snapshots)
 └────► control ────► storage
```

| Component | Path | Responsibility |
|---|---|---|
| core | `stair/core` | Domain types, threshold/average formulas, serialization. No I/O. |
| strategy | `stair/strategy` | `Cycle` state machine (section 8). Takes fills, statuses and quotes and returns order intents. Pure, so the backtest can drive it. |
| exchange | `stair/exchange` | `ExchangeGateway` contract plus `BinanceFutures` (hedge mode, `positionSide`, no `reduceOnly`). |
| storage | `stair/storage` | SQLite journal: cycle snapshots, event log, trades keyed by id, command queue. |
| execution | `stair/execution` | `Runner` orchestrator, `OrderSync` (fills before statuses, idempotent), reconciliation, single-instance lock. |
| control | `stair/control` | Command submission API. The CLI uses it today; a local UI can reuse it (Q23). |
| dashboard | `stair/dashboard` | Read-only local web view (journal + exchange read calls). |
| app | `stair/app` | Settings (`config.toml` + `.env`) and the CLI. |

Each component depends only on the components to its right in the diagram. A simulated gateway that implements
`ExchangeGateway` (see `tests/conftest.py::FakeGateway`) runs the real runner. The backtest gateway will plug in the same way.

## Setup

```
python -m pip install -r requirements.txt
cp .env.example .env        # fill in keys
python -m stair check       # connectivity, hedge mode, fees, filters, balance
python -m stair hedge-mode on   # account-wide; needs no open positions/orders (R01)
python -m stair margin-type cross   # per symbol; needs no open position/orders on it (Q03)
```

## Running

Terminal 1 hosts the bot:

```
python -m stair run
```

Terminal 2 sends commands (they go through the journal's command queue):

```
python -m stair enter long --qty 1 --price 95.30 [--post-only]   # first entry (R20: you spot the event)
python -m stair level add resistance 98.5 --exit 98.0            # SHORT adds of 0.1 x Q0 (R08, R09)
python -m stair level add support 92.0                           # LONG adds, default exit +0.5%
python -m stair level rm 2
python -m stair status
python -m stair log -n 50
python -m stair pause | resume
python -m stair close                                            # full manual close (R14)
python -m stair ack                                              # clear RECOVERY after fixing a mismatch
```

Dashboard (read-only, local only, opens the browser at http://127.0.0.1:8765):

```
python -m stair dashboard [--port 8765] [--no-exchange] [--no-browser]
```

It shows live exchange positions for every symbol, the active cycle (sides, adds, levels, working orders),
closed cycles with net results and a running-total chart, and the journal. It also warns when the bot is stopped
while a position is unhedged, and when exchange and bot quantities differ.

Ctrl+C stops only the bot. Positions and resting orders stay on the exchange, and `run` resumes after reconciling.

## Live trading

`exchange.env = "live"` (or `--env live`) refuses to start unless `STAIR_LIVE_CONFIRM=I_ACCEPT_REAL_MONEY_RISK`
is set in the environment. Demo and live use separate journals (`data/stair-demo.sqlite`, `data/stair-live.sqlite`).

## Tests

`python -m pytest`. Tests are named after the spec's acceptance checks (T01–T23) where those checks can be tested now.

Open questions from the spec and the defaults implemented for them are in [docs/DECISIONS.md](docs/DECISIONS.md).
