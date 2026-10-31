"""Binance USD-M Futures gateway (REST), hedge mode."""
from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
from decimal import Decimal

import requests

from ..core.models import (MarketRules, Order, OrderReport, OrderStatus, PositionView, Quote, Side,
                           Trade)
from .base import ExchangeError, OrderNotFound, UnknownResult

BASE_URLS = {"demo": "https://demo-fapi.binance.com", "live": "https://fapi.binance.com"}

_STATUS = {"NEW": OrderStatus.NEW, "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
           "FILLED": OrderStatus.FILLED, "CANCELED": OrderStatus.CANCELED,
           "REJECTED": OrderStatus.REJECTED, "EXPIRED": OrderStatus.EXPIRED,
           "EXPIRED_IN_MATCH": OrderStatus.EXPIRED}
_UNKNOWN_CODES = {-1007}  # backend timeout: execution status unknown
_NOT_FOUND_CODES = {-2011, -2013}
_TIME_CODES = {-1021}


def fmt(value: Decimal) -> str:
    return format(value.normalize(), "f")


def order_params(symbol: str, order: Order) -> dict:
    """Hedge mode requires positionSide and forbids reduceOnly (section 7, T07)."""
    params = {"symbol": symbol, "side": order.side, "positionSide": order.position_side.value,
              "type": order.order_type, "quantity": fmt(order.qty),
              "newClientOrderId": order.client_id, "newOrderRespType": "RESULT"}
    if order.order_type == "LIMIT":
        params["price"] = fmt(order.price)
        params["timeInForce"] = order.tif or "GTC"
    return params


def _report(data: dict) -> OrderReport:
    return OrderReport(client_id=data["clientOrderId"], exchange_id=int(data["orderId"]),
                       status=_STATUS.get(data["status"], OrderStatus.NEW),
                       executed_qty=Decimal(data["executedQty"]))


class BinanceFutures:
    def __init__(self, api_key: str, api_secret: str, env: str = "demo", *,
                 timeout: float = 10.0, recv_window: int = 5000, session: requests.Session | None = None):
        if env not in BASE_URLS:
            raise ValueError(f"unknown env {env!r}")
        self.env, self.base = env, BASE_URLS[env]
        self._key, self._secret = api_key, api_secret.encode()
        self._timeout, self._recv_window = timeout, recv_window
        self._session = session or requests.Session()
        self._offset_ms = 0
        self._rules: dict[str, MarketRules] = {}
        self._usdt_rates: dict[str, tuple[Decimal, float]] = {}
        self._rate_ttl_s = 60.0

    # ---- transport ------------------------------------------------------

    def sync_time(self) -> None:
        self._offset_ms = self.server_time_ms() - int(time.time() * 1000)

    def sign(self, params: dict) -> str:
        query = urllib.parse.urlencode(params)
        signature = hmac.new(self._secret, query.encode(), hashlib.sha256).hexdigest()
        return f"{query}&signature={signature}"

    def _request(self, method: str, path: str, params: dict | None = None, *, signed: bool = False,
                 _retry_time: bool = True):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if signed:
            params["timestamp"] = int(time.time() * 1000) + self._offset_ms
            params["recvWindow"] = self._recv_window
            query = self.sign(params)
        else:
            query = urllib.parse.urlencode(params)
        url = f"{self.base}{path}" + (f"?{query}" if query else "")
        try:
            resp = self._session.request(method, url, headers={"X-MBX-APIKEY": self._key},
                                         timeout=self._timeout)
        except requests.RequestException as exc:
            raise UnknownResult(f"{method} {path}: {exc}") from exc
        if resp.status_code >= 500:
            raise UnknownResult(f"{method} {path}: HTTP {resp.status_code} {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise UnknownResult(f"{method} {path}: non-JSON HTTP {resp.status_code}") from exc
        if resp.status_code >= 400 or (isinstance(data, dict) and isinstance(data.get("code"), int)
                                       and data["code"] < 0):
            code, msg = data.get("code"), data.get("msg", resp.text[:200])
            if code in _UNKNOWN_CODES:
                raise UnknownResult(f"{method} {path}: [{code}] {msg}")
            if code in _TIME_CODES and _retry_time:
                self.sync_time()  # rejected before execution, safe to repeat once
                return self._request(method, path, params, signed=signed, _retry_time=False)
            if code in _NOT_FOUND_CODES:
                raise OrderNotFound(code, msg, resp.status_code)
            raise ExchangeError(code, msg, resp.status_code)
        return data

    # ---- market data ----------------------------------------------------

    def server_time_ms(self) -> int:
        return int(self._request("GET", "/fapi/v1/time")["serverTime"])

    def market_rules(self, symbol: str) -> MarketRules:
        if symbol in self._rules:
            return self._rules[symbol]
        info = self._request("GET", "/fapi/v1/exchangeInfo")
        meta = next((s for s in info["symbols"] if s["symbol"] == symbol), None)
        if meta is None:
            raise ExchangeError(None, f"symbol {symbol} not found")
        if meta["status"] != "TRADING":
            raise ExchangeError(None, f"{symbol} status is {meta['status']}")
        filters = {f["filterType"]: f for f in meta["filters"]}
        fees = self._request("GET", "/fapi/v1/commissionRate", {"symbol": symbol}, signed=True)
        rules = MarketRules(symbol=symbol,
                            tick_size=Decimal(filters["PRICE_FILTER"]["tickSize"]).normalize(),
                            step_size=Decimal(filters["LOT_SIZE"]["stepSize"]).normalize(),
                            min_qty=Decimal(filters["LOT_SIZE"]["minQty"]),
                            min_notional=Decimal(filters.get("MIN_NOTIONAL", {}).get("notional", "0")),
                            maker_fee=Decimal(fees["makerCommissionRate"]),
                            taker_fee=Decimal(fees["takerCommissionRate"]))
        self._rules[symbol] = rules
        return rules

    def quote(self, symbol: str, *, last: bool = True, mark: bool = True) -> Quote:
        """Bid/ask always; last and mark cost one request each, so the hot loop skips them."""
        book = self._request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        quote = Quote(bid=Decimal(book["bidPrice"]), ask=Decimal(book["askPrice"]))
        if last:
            quote.last = Decimal(self._request("GET", "/fapi/v1/ticker/price", {"symbol": symbol})["price"])
        if mark:
            quote.mark = Decimal(self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})["markPrice"])
        return quote

    def to_usdt(self, amount: Decimal, asset: str) -> Decimal:
        """Fee conversion (BNB fees). The rate is cached briefly so a burst of fills costs one request."""
        if asset in ("USDT", "") or not amount:
            return amount
        cached = self._usdt_rates.get(asset)
        if cached is None or time.monotonic() - cached[1] > self._rate_ttl_s:
            price = self._request("GET", "/fapi/v1/ticker/price", {"symbol": f"{asset}USDT"})
            cached = self._usdt_rates[asset] = (Decimal(price["price"]), time.monotonic())
        return amount * cached[0]

    # ---- account --------------------------------------------------------

    def hedge_mode(self) -> bool:
        return bool(self._request("GET", "/fapi/v1/positionSide/dual", signed=True)["dualSidePosition"])

    def set_hedge_mode(self, enabled: bool) -> None:
        try:
            self._request("POST", "/fapi/v1/positionSide/dual",
                          {"dualSidePosition": "true" if enabled else "false"}, signed=True)
        except ExchangeError as exc:
            if exc.code != -4059:  # "No need to change position side."
                raise

    def margin_type(self, symbol: str) -> str:
        """'cross' or 'isolated'; per symbol, shared by both hedge sides."""
        rows = self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        return rows[0]["marginType"]

    def set_margin_type(self, symbol: str, cross: bool) -> None:
        """Needs no open position or order on the symbol."""
        try:
            self._request("POST", "/fapi/v1/marginType",
                          {"symbol": symbol, "marginType": "CROSSED" if cross else "ISOLATED"}, signed=True)
        except ExchangeError as exc:
            if exc.code != -4046:  # "No need to change margin type."
                raise

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

    def balances(self) -> dict[str, Decimal]:
        rows = self._request("GET", "/fapi/v2/balance", signed=True)
        return {r["asset"]: Decimal(r["balance"]) for r in rows if Decimal(r["balance"])}

    def positions(self, symbol: str | None) -> list[PositionView]:
        """Both hedge sides of `symbol`, or every non-zero position of the account when None."""
        rows = self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        out = []
        for r in rows:
            if r.get("positionSide") not in ("LONG", "SHORT"):
                continue
            qty = abs(Decimal(r["positionAmt"]))
            if symbol is None and not qty:
                continue
            out.append(PositionView(side=Side(r["positionSide"]), qty=qty,
                                    entry_price=Decimal(r["entryPrice"]),
                                    unrealized=Decimal(r["unRealizedProfit"]),
                                    leverage=int(r["leverage"]), margin_type=r["marginType"],
                                    liquidation_price=Decimal(r["liquidationPrice"]),
                                    symbol=r["symbol"], mark_price=Decimal(r["markPrice"])))
        return out

    def funding_since(self, symbol: str, start_ms: int) -> Decimal:
        rows = self._request("GET", "/fapi/v1/income", {"symbol": symbol, "incomeType": "FUNDING_FEE",
                                                        "startTime": start_ms, "limit": 1000}, signed=True)
        return sum((self.to_usdt(Decimal(r["income"]), r["asset"]) for r in rows), Decimal(0))

    # ---- orders ---------------------------------------------------------

    def open_orders(self, symbol: str) -> list[OrderReport]:
        return [_report(o) for o in self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True)]

    def place(self, symbol: str, order: Order) -> OrderReport:
        return _report(self._request("POST", "/fapi/v1/order", order_params(symbol, order), signed=True))

    def cancel(self, symbol: str, client_id: str) -> OrderReport:
        return _report(self._request("DELETE", "/fapi/v1/order",
                                     {"symbol": symbol, "origClientOrderId": client_id}, signed=True))

    def query(self, symbol: str, client_id: str) -> OrderReport:
        return _report(self._request("GET", "/fapi/v1/order",
                                     {"symbol": symbol, "origClientOrderId": client_id}, signed=True))

    def trades(self, symbol: str, *, from_id: int | None = None, start_ms: int | None = None) -> list[Trade]:
        out: list[Trade] = []
        params = {"symbol": symbol, "limit": 1000, "fromId": from_id,
                  "startTime": None if from_id is not None else start_ms}
        while True:
            rows = self._request("GET", "/fapi/v1/userTrades", params, signed=True)
            out += [Trade(trade_id=int(r["id"]), order_id=int(r["orderId"]), qty=Decimal(r["qty"]),
                          price=Decimal(r["price"]), commission=Decimal(r["commission"]),
                          commission_asset=r["commissionAsset"], time_ms=int(r["time"])) for r in rows]
            if len(rows) < 1000:
                return out
            params = {"symbol": symbol, "limit": 1000, "fromId": int(rows[-1]["id"]) + 1}
