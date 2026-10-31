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
