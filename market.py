from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from json import load
from time import monotonic
from urllib.parse import urlencode
from urllib.request import urlopen


FIRST_DAY = "2024-01-01"
LAST_DAY = "2024-06-30"
UPBIT = "업비트 과거 일봉 종가"
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


def demo_series(instrument):
    start, end = date.fromisoformat(FIRST_DAY), date.fromisoformat(LAST_DAY)
    points = {}
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        if day.weekday() < 5:
            # 반복 가능한 가상 경로. 실제 미국 거래소 휴장일은 적용하지 않는다.
            week = offset // 7
            base = instrument.demo_closes[(week + day.weekday()) % len(instrument.demo_closes)]
            points[day.isoformat()] = Decimal(base + week * 500)
    return points


class Market:
    def __init__(self):
        self.cache = {}

    def coin_series(self, symbol):
        pair = f"KRW-{symbol}"
        next_day = date.fromisoformat(LAST_DAY) + timedelta(days=1)
        rows = request_json("https://api.upbit.com/v1/candles/days", {
            "market": pair, "count": 200, "to": f"{next_day}T00:00:00Z",
        })
        points = {}
        for row in rows:
            if row["market"] != pair:
                raise ValueError("종목 코드 오류")
            day = date.fromisoformat(row["candle_date_time_utc"][:10]).isoformat()
            if FIRST_DAY <= day <= LAST_DAY:
                points[day] = positive_decimal(row["trade_price"])
        return points

    def series(self, symbol):
        cached = self.cache.get(symbol)
        if cached and monotonic() < cached[0]:
            return cached[1:]
        instrument = SYMBOLS[symbol]
        source = UPBIT if instrument.kind == "coin" else DEMO
        try:
            points = self.coin_series(symbol) if source == UPBIT else demo_series(instrument)
        except (OSError, ValueError, InvalidOperation, KeyError, TypeError):
            points = {}
        # 완료된 과거 일봉은 서버 실행 중 다시 받을 필요가 없다.
        expiry = float("inf") if points else monotonic() + 30
        fetched = now()
        self.cache[symbol] = (expiry, points, source, fetched)
        return points, source, fetched

    def quote(self, symbol, day, *, valuation=False):
        points, source, fetched = self.series(symbol)
        price_day = max((key for key in points if key <= day), default=None) if valuation else day
        price = points.get(price_day)
        return Quote(symbol, price, source, price_day, fetched) if price is not None else None

    def chart(self, symbol, day):
        points, source, _ = self.series(symbol)
        labels = sorted(key for key in points if key <= day)[-30:]
        if not labels:
            return None
        return {"labels": labels, "values": [float(points[key]) for key in labels],
                "source": source, "currency": "KRW"}
