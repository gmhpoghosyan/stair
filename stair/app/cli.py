"""Command line front end. `run` hosts the bot; the other commands talk to it through the
command queue or read the journal, so they work from a second terminal."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from ..control import submit
from ..core.models import Side
from ..exchange import BinanceFutures
from ..execution import InstanceLock, Runner
from ..storage import Store
from .settings import Settings, load_settings


def _gateway(s: Settings) -> BinanceFutures:
    gateway = BinanceFutures(s.api_key, s.api_secret, s.env)
    gateway.sync_time()
    return gateway


def cmd_check(s: Settings, args) -> None:
    gw = _gateway(s)
    symbol = s.strategy.symbol
    rules = gw.market_rules(symbol)
    quote = gw.quote(symbol)
    print(f"env            {s.env} ({gw.base})")
    print(f"hedge mode     {'ON' if gw.hedge_mode() else 'OFF  <- required (R01): python -m stair hedge-mode on'}")
    print(f"margin type    {gw.margin_type(symbol)} ({symbol}; change with `python -m stair margin-type cross`)")
    print(f"symbol         {symbol} tick {rules.tick_size} step {rules.step_size} "
          f"min qty {rules.min_qty} min notional {rules.min_notional}")
    print(f"fees           maker {rules.maker_fee} taker {rules.taker_fee}")
    print(f"book           bid {quote.bid} ask {quote.ask} mark {quote.mark}")
    print(f"balances       {', '.join(f'{k} {v}' for k, v in gw.balances().items()) or 'none'}")
    for p in gw.positions(symbol):
        print(f"position       {p.side.value} {p.qty} @ {p.entry_price} lev {p.leverage}x {p.margin_type}")
    orders = gw.open_orders(symbol)
    print(f"open orders    {len(orders)}")
    min_q0 = rules.min_notional / (s.strategy.add_fraction * quote.ask)
    print(f"min Q0 for adds ~{min_q0:.2f} (add = {s.strategy.add_fraction} x Q0 must reach min notional)")


def cmd_hedge_mode(s: Settings, args) -> None:
    gw = _gateway(s)
    gw.set_hedge_mode(args.state == "on")
    print(f"hedge mode is now {'ON' if gw.hedge_mode() else 'OFF'} (account-wide setting)")


def cmd_margin_type(s: Settings, args) -> None:
    gw = _gateway(s)
    symbol = s.strategy.symbol
    gw.set_margin_type(symbol, args.type == "cross")
    print(f"{symbol} margin type is now {gw.margin_type(symbol)}")


def cmd_run(s: Settings, args) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(s.data_dir / f"stair-{s.env}.log", encoding="utf-8")])
    store = Store(s.db_path)
    with InstanceLock(s.lock_path):
        runner = Runner(_gateway(s), store, s.strategy, trigger_price=s.trigger_price,
                        reconcile_every_s=s.reconcile_every_s, levels=s.levels)
        runner.preflight()
        logging.getLogger("stair").info("running on %s %s; Ctrl+C stops the bot only", s.env, s.strategy.symbol)
        try:
            runner.run(s.poll_interval_s, s.fast_poll_interval_s)
        except KeyboardInterrupt:
            if runner.cycle:
                store.save_cycle(runner.cycle)
            print("\nstopped. Positions and resting orders stay on the exchange; "
                  "nothing was closed (section 10). Restart with `run` to resume.")


def _send(s: Settings, kind: str, payload: dict | None = None) -> None:
    status, result = submit(Store(s.db_path), kind, payload)
    if status == "pending":
        print("queued; no runner answered. Start `python -m stair run` to process it.")
    else:
        print(("OK: " if status == "done" else "ERROR: ") + (result or ""))
        if status != "done":
            sys.exit(1)


def cmd_enter(s: Settings, args) -> None:
    _send(s, "enter", {"side": args.side, "qty": args.qty, "price": args.price, "post_only": args.post_only})


def cmd_level(s: Settings, args) -> None:
    if args.action == "rm":
        _send(s, "level_rm", {"id": args.id})
    else:
        _send(s, "level_add", {"kind": args.kind, "price": args.price, "exit": args.exit})


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, Decimal):
        return f"{v:.6f}".rstrip("0").rstrip(".") if v else "0"
    return str(v)


def cmd_status(s: Settings, args) -> None:
    store = Store(s.db_path)
    cycle = store.load_cycle()
    if cycle is None:
        last = store.last_finished_cycle()
        print("no active cycle" + (f"; last {last.id} finished: {last.note}" if last else ""))
        return
    m = cycle.summary()
    long, short = m["long"], m["short"]
    print(f"[{s.env}] {cycle.rules.symbol} cycle {m['cycle']}  state {m['state']}  {m['note']}")
    if m["recovery"]:
        print(f"RECOVERY: {m['recovery']}  -> fix, then `python -m stair ack`")
    print(f"first {m['first_side']}  E0 {_fmt(m['e0'])}  Q0 {_fmt(m['q0'])}  F0 {_fmt(m['f0'])}  branch {_fmt(m['branch'])}")
    if m["thresholds"]:
        print(f"hedge thresholds  positive {m['thresholds']['positive']}  negative {m['thresholds']['negative']}")
    print(f"LONG  {_fmt(long.qty)} @ {_fmt(long.avg)}  realized {_fmt(long.realized)}  fees {_fmt(long.fees)}")
    print(f"SHORT {_fmt(short.qty)} @ {_fmt(short.avg)}  realized {_fmt(short.realized)}  fees {_fmt(short.fees)}")
    print(f"delta {_fmt(m['delta'])}  spread of averages ES-EL {_fmt(m['spread_of_averages'])}  "
          f"base net spread {_fmt(m['base_net_spread'])}")
    print(f"bid {_fmt(m['bid'])} ask {_fmt(m['ask'])}  unrealized {_fmt(m['unrealized'])}  "
          f"funding {_fmt(m['funding'])}  total {_fmt(m['total'])}  closed adds {_fmt(m['closed_adds_result'])}")
    auto = store.get("auto_levels")
    if auto:
        print(f"pivot levels ({auto['interval']}): resistance {auto['resistance']} support {auto['support']}"
              f"{'  SIGNAL ' + auto['signal'] if auto['signal'] else ''}")
    for lv in cycle.levels:
        print(f"level {lv.id} [{lv.source}]: {'resistance' if lv.side is Side.SHORT else 'support'} {lv.price} "
              f"exit {lv.exit_price or 'default'} {'busy ' + lv.add_id if lv.add_id else 'armed'}")
    for add in cycle.adds.values():
        print(f"add {add.id}: {add.side.value} {add.qty} @ {add.price} -> {add.exit_price} {add.state.value} "
              f"result {_fmt(cycle.add_result(add))} {add.note}")
    for o in cycle.orders.values():
        if not o.is_terminal:
            print(f"order {o.client_id} {o.purpose.value} {o.side} {o.position_side.value} "
                  f"{_fmt(o.filled)}/{_fmt(o.qty)} @ {o.price or 'MKT'} {o.status.value}")
    view = store.get("exchange_positions")
    if view:
        print("exchange: " + ", ".join(f"{k} {v['qty']} @ {v['entry_price']} liq {v['liquidation_price']}"
                                       for k, v in view.items()))


def cmd_dashboard(s: Settings, args) -> None:
    from ..dashboard import DashboardData, make_server
    gateway = None if args.no_exchange else _gateway(s)
    data = DashboardData(s.db_path, s.env, s.strategy.symbol, gateway)
    server = make_server(data, args.port)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"dashboard on {url} (read-only, local only). Ctrl+C to stop.")
    if not args.no_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


def cmd_log(s: Settings, args) -> None:
    for ts, cycle_id, kind, payload in Store(s.db_path).recent_events(args.n):
        stamp = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")
        print(f"{stamp} {cycle_id or '-':8} {kind:8} {json.dumps(json.loads(payload))[:160]}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stair", description="Hedge cycle bot for Binance USD-M futures")
    p.add_argument("--config", type=Path, default=Path("config.toml"))
    p.add_argument("--env-file", type=Path, default=Path(".env"))
    p.add_argument("--env", choices=["demo", "live"], help="override exchange.env from config")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="connectivity, hedge mode, fees, filters, balances").set_defaults(fn=cmd_check)
    hm = sub.add_parser("hedge-mode", help="switch account position mode (account-wide)")
    hm.add_argument("state", choices=["on", "off"])
    hm.set_defaults(fn=cmd_hedge_mode)
    mt = sub.add_parser("margin-type", help="cross or isolated margin for the configured symbol (Q03)")
    mt.add_argument("type", choices=["cross", "isolated"])
    mt.set_defaults(fn=cmd_margin_type)
    sub.add_parser("run", help="run the bot loop").set_defaults(fn=cmd_run)
    en = sub.add_parser("enter", help="start a cycle with a limit entry (R04, R20)")
    en.add_argument("side", choices=["long", "short"])
    en.add_argument("--qty", required=True, help="coins, e.g. 1")
    en.add_argument("--price", required=True, help="limit price")
    en.add_argument("--post-only", action="store_true", help="GTX: rejected instead of taking")
    en.set_defaults(fn=cmd_enter)
    lv = sub.add_parser("level", help="manage support/resistance levels for adds (R08)")
    lv_sub = lv.add_subparsers(dest="action", required=True)
    la = lv_sub.add_parser("add")
    la.add_argument("kind", choices=["resistance", "support"])
    la.add_argument("price")
    la.add_argument("--exit", help="take-profit price for adds at this level (Q13)")
    lr = lv_sub.add_parser("rm")
    lr.add_argument("id", type=int)
    lv.set_defaults(fn=cmd_level)
    for name, kind, text in (("pause", "pause", "stop new adds (Q24)"), ("resume", "resume", "resume after pause"),
                             ("close", "close", "close the whole cycle at market (R14)"),
                             ("ack", "ack_recovery", "clear RECOVERY after fixing a mismatch")):
        sub.add_parser(name, help=text).set_defaults(fn=lambda s, a, k=kind: _send(s, k))
    sub.add_parser("status", help="show the active cycle").set_defaults(fn=cmd_status)
    db = sub.add_parser("dashboard", help="local web dashboard: open positions, active and closed cycles")
    db.add_argument("--port", type=int, default=8765)
    db.add_argument("--no-exchange", action="store_true", help="journal only, no exchange calls")
    db.add_argument("--no-browser", action="store_true")
    db.set_defaults(fn=cmd_dashboard)
    lg = sub.add_parser("log", help="recent journal events")
    lg.add_argument("-n", type=int, default=30)
    lg.set_defaults(fn=cmd_log)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    settings = load_settings(args.config, args.env_file, args.env)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    args.fn(settings, args)
