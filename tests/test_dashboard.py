import json
import threading
import urllib.request
from decimal import Decimal as D

from conftest import fill, places
from stair.dashboard import DashboardData, make_server
from stair.storage import Store


def test_dashboard_reports_active_and_closed_cycles(tmp_path, cycle, gateway):
    from test_engine import hedged
    store = Store(tmp_path / "d.sqlite")
    hedged(cycle)
    closes = places(cycle.close())
    for o in closes:
        fill(cycle, o.client_id, o.qty, "101", "0.01")
    store.save_cycle(cycle)  # finished -> archived
    data = DashboardData(tmp_path / "d.sqlite", "demo", "SOLUSDT", gateway).snapshot()
    assert data["cycle"] is None and len(data["closed"]) == 1
    closed = data["closed"][0]
    # long 1 @ 100 -> 101 (+1), short 1 @ 100.5 -> 101 (-0.5), two close fees of 0.01
    assert D(closed["realized"]) == D("0.5") and D(closed["net"]) == D("0.48")
    assert data["exchange"]["enabled"] and data["exchange"]["error"] is None


def test_dashboard_http_endpoints(tmp_path, gateway):
    data = DashboardData(tmp_path / "e.sqlite", "demo", "SOLUSDT", gateway)
    server = make_server(data, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert b"Stair dashboard" in urllib.request.urlopen(base + "/").read()
        state = json.loads(urllib.request.urlopen(base + "/api/state").read())
        assert state["cycle"] is None and state["env"] == "demo"
    finally:
        server.shutdown()


def test_dashboard_shows_adds_and_next_triggers(tmp_path, cycle, gateway):
    from decimal import Decimal as D
    from test_engine import hedged
    from stair.core.models import Side
    hedged(cycle)
    resting = places(cycle.on_quote(D("100.4"), D("100.41"), 10.0))
    short_add = next(o for o in resting if o.position_side is Side.SHORT)
    fill(cycle, short_add.client_id, "0.1", "101.51")
    store = Store(tmp_path / "a.sqlite")
    store.save_cycle(cycle)
    view = DashboardData(tmp_path / "a.sqlite", "demo", "SOLUSDT", gateway).snapshot()["cycle"]
    states = {a["side"]: (a["state"], a["exit_price"]) for a in view["adds"]}
    assert states == {"SHORT": ("open, exit working", "101.00"), "LONG": ("waiting at trigger", None)}
    cycle.on_quote(D("101.2"), D("101.21"), 11.0)  # HEDGED summary: SHORT busy, LONG waiting at its trigger
    assert cycle.summary()["add_triggers"] == {"LONG": D("99"), "SHORT": None}
