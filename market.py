from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from json import JSONDecodeError, load
from time import monotonic
from urllib.parse import urlencode
from urllib.request import urlopen


UPBIT = "업비트 현재가"
STOCK = "Alpha Vantage 종가"
DEMO = "데모 가격"


@dataclass(frozen=True)
class Instrument:
    name: str
    kind: str
    demo_closes: tuple[int, ...] = ()


SYMBOLS = {
    "BTC": Instrument("비트코인", "coin"),
    "ETH": Instrument("이더리움", "coin"),
    "XRP": Instrument("엑스알피", "coin"),
    "AAPL": Instrument("애플", "stock", (280_000, 285_000, 282_000, 294_000, 290_000)),
    "MSFT": Instrument("마이크로소프트", "stock", (510_000, 520_000, 518_000, 530_000, 525_000)),
    "TSLA": Instrument("테슬라", "stock", (390_000, 382_000, 405_000, 397_000, 410_000)),
}


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: Decimal
    source: str
    as_of: str
    fetched_at: str
    fx_rate: Decimal | None = None
    fx_as_of: str | None = None


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def request_json(url, params):
    with urlopen(f"{url}?{urlencode(params)}", timeout=4) as response:
        return load(response, parse_float=Decimal)


def positive_decimal(raw):
    price = Decimal(str(raw))
    if not price.is_finite() or not 0 < price <= 10**12:
        raise ValueError("시세 범위 오류")
    return price


class Market:
    def __init__(self, api_key):
        self.api_key = api_key
        self.cache = {}

    def cached(self, key, ttl, fetch):
        entry = self.cache.get(key)
        if entry and monotonic() < entry[0]:
            return entry[1]
        try:
            value = fetch()
        except (OSError, JSONDecodeError, ValueError, InvalidOperation, KeyError, TypeError,
                IndexError, AttributeError, OverflowError):
            value = None
        self.cache[key] = (monotonic() + (ttl if value is not None else 30), value)
        return value

    def upbit_quote(self, symbol):
        row = request_json("https://api.upbit.com/v1/ticker", {"markets": f"KRW-{symbol}"})[0]
        if row["market"] != f"KRW-{symbol}":
            raise ValueError("종목 코드 오류")
        traded_at = datetime.fromtimestamp(int(row["trade_timestamp"]) / 1000, timezone.utc)
        return Quote(symbol, positive_decimal(row["trade_price"]), UPBIT, traded_at.isoformat(), now())

    def alpha(self, function, **params):
        return request_json("https://www.alphavantage.co/query", {
            "function": function, "apikey": self.api_key, "outputsize": "compact", **params,
        })

    def daily_series(self, function, response_key, **params):
        data = self.alpha(function, **params)[response_key]
        dates = sorted(data)[-30:]
        if not dates:
            raise ValueError("일봉 데이터가 비어 있어요.")
        points = [(date.fromisoformat(day).isoformat(), positive_decimal(data[day]["4. close"]))
                  for day in dates]
        return points, now()

    def stock_series(self, symbol):
        return self.cached(("stock", symbol), 3600, lambda: self.daily_series(
            "TIME_SERIES_DAILY", "Time Series (Daily)", symbol=symbol))

    def fx_series(self):
        return self.cached("fx", 3600, lambda: self.daily_series(
            "FX_DAILY", "Time Series FX (Daily)", from_symbol="USD", to_symbol="KRW"))

    def quote(self, symbol):
        instrument = SYMBOLS[symbol]
        if instrument.kind == "coin":
            return self.cached(("ticker", symbol), 30, lambda: self.upbit_quote(symbol))

        stock_data = self.stock_series(symbol) if self.api_key else None
        fx_data = self.fx_series() if stock_data else None
        if stock_data and fx_data:
            closes, fetched_at = stock_data
            stock_date, close_usd = closes[-1]
            fx_date, rate = fx_data[0][-1]
            return Quote(symbol, close_usd * rate, STOCK, stock_date, fetched_at, rate, fx_date)
        return Quote(symbol, Decimal(instrument.demo_closes[-1]), DEMO, "가상 데이터 마지막 구간", now())

    def coin_series(self, symbol):
        data = request_json("https://api.upbit.com/v1/candles/days", {"market": f"KRW-{symbol}", "count": 30})
        return [(date.fromisoformat(row["candle_date_time_kst"][:10]).isoformat(),
                 positive_decimal(row["trade_price"])) for row in reversed(data)]

    def chart(self, symbol, source):
        if SYMBOLS[symbol].kind == "coin" and source == UPBIT:
            points = self.cached(("candles", symbol), 300, lambda: self.coin_series(symbol))
            label, currency = "업비트 일봉", "KRW"
        elif SYMBOLS[symbol].kind == "stock" and source == STOCK and self.api_key:
            stock_data = self.stock_series(symbol)
            points = stock_data[0] if stock_data else None
            label, currency = "Alpha Vantage 일별 종가", "USD"
        elif SYMBOLS[symbol].kind == "stock" and source == DEMO:
            points = [(f"구간 {i}", price) for i, price in enumerate(SYMBOLS[symbol].demo_closes, 1)]
            label, currency = "데모 가격 · 가상 데이터", "KRW"
        else:
            return None
        if not points:
            return None
        return {"labels": [day for day, _ in points], "values": [float(price) for _, price in points],
                "source": label, "currency": currency}
