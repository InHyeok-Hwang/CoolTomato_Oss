from decimal import Decimal, ROUND_HALF_UP
import re

from db import transaction
from market import SYMBOLS, now


SCENARIOS = {
    "student": ("대학생", 1_000_000),
    "new_worker": ("사회초년생", 5_000_000),
    "worker": ("직장인", 10_000_000),
    "custom": ("자유 설정", None),
}
COIN_SCALE = 100_000_000
MAX_CASH = 1_000_000_000
MAX_ORDER_UNITS = 1_000_000_000
SQLITE_MAX_INTEGER = 2**63 - 1


class TradeError(ValueError):
    pass


def account(connection):
    return connection.execute("SELECT * FROM account").fetchone()


def setup(connection, scenario, entered_amount):
    if scenario not in SCENARIOS:
        raise TradeError("투자 시나리오를 선택해 주세요.")
    amount = SCENARIOS[scenario][1]
    if amount is None:
        if not re.fullmatch(r"[0-9]{1,10}", entered_amount):
            raise TradeError("초기 투자금을 1원부터 10억 원 사이의 정수로 입력해 주세요.")
        amount = int(entered_amount)
        if not 1 <= amount <= MAX_CASH:
            raise TradeError("초기 투자금을 1원부터 10억 원 사이로 입력해 주세요.")
    with transaction(connection):
        if account(connection) is not None:
            raise TradeError("이미 투자 계좌가 설정되어 있어요.")
        connection.execute(
            "INSERT INTO account (scenario, initial_cash, cash) VALUES (?, ?, ?)",
            (scenario, amount, amount),
        )


def reset(connection, account_id):
    with transaction(connection):
        current = account(connection)
        if current is None or str(current['id']) != account_id:
            raise TradeError("계좌가 변경됐어요. 화면을 새로고침해 주세요.")
        connection.execute("DELETE FROM trades")
        connection.execute("DELETE FROM positions")
        connection.execute("DELETE FROM account")


def scale(symbol):
    return COIN_SCALE if SYMBOLS[symbol].kind == "coin" else 1


def quantity(symbol, units):
    return Decimal(units) / scale(symbol)


def won(value):
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


def parse_units(symbol, entered_quantity):
    pattern = r"[0-9]{1,10}(?:\.[0-9]{1,8})?" if scale(symbol) > 1 else r"[0-9]{1,10}"
    if not re.fullmatch(pattern, entered_quantity):
        message = "코인 수량은 소수점 아래 8자리까지 입력해 주세요."
        if scale(symbol) == 1:
            message = "주식 수량은 1주 단위로 입력해 주세요."
        raise TradeError(message)
    value = Decimal(entered_quantity)
    if not 0 < value <= MAX_ORDER_UNITS:
        raise TradeError("거래 수량은 0보다 크고 10억 이하여야 해요.")
    return int(value * scale(symbol))


def execute(connection, quote, side, entered_quantity, account_id, request_id):
    if side not in ("buy", "sell"):
        raise TradeError("매수 또는 매도를 선택해 주세요.")
    symbol = quote.symbol
    units = parse_units(symbol, entered_quantity)
    amount = won(quote.price * quantity(symbol, units))
    if amount < 1:
        raise TradeError("거래 금액이 1원 이상이 되도록 수량을 입력해 주세요.")
    if amount > SQLITE_MAX_INTEGER:
        raise TradeError("거래 금액이 너무 커요.")

    with transaction(connection):
        current = account(connection)
        if current is None or str(current['id']) != account_id:
            raise TradeError("계좌가 변경됐어요. 화면을 새로고침해 주세요.")
        if connection.execute("SELECT 1 FROM trades WHERE request_id = ?", (request_id,)).fetchone():
            raise TradeError("이미 처리된 주문이에요.")
        position = connection.execute("SELECT * FROM positions WHERE symbol = ?", (symbol,)).fetchone()
        held = position["units"] if position else 0
        cost = position["cost"] if position else 0
        cash = current["cash"]

        if side == "buy":
            if amount > cash:
                raise TradeError("매수 금액이 보유 현금을 초과해요.")
            remaining, new_cost, new_cash = held + units, cost + amount, cash - amount
        else:
            if units > held:
                raise TradeError("매도 수량이 보유 수량을 초과해요.")
            remaining = held - units
            new_cost = won(Decimal(cost) * remaining / held)
            new_cash = cash + amount

        if max(remaining, new_cost, new_cash) > SQLITE_MAX_INTEGER:
            raise TradeError("거래 후 잔액 또는 보유 수량이 저장 가능한 범위를 초과해요.")
        if remaining:
            connection.execute(
                "INSERT INTO positions VALUES (?, ?, ?) ON CONFLICT(symbol) DO UPDATE "
                "SET units = excluded.units, cost = excluded.cost",
                (symbol, remaining, new_cost),
            )
        else:
            connection.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
        connection.execute("UPDATE account SET cash = ? WHERE id = ?", (new_cash, current["id"]))
        connection.execute(
            """INSERT INTO trades (request_id, symbol, side, units, unit_price, amount,
                    source, as_of, fetched_at, fx_rate, fx_as_of, traded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (request_id, symbol, side, units, str(quote.price), amount, quote.source,
             quote.as_of, quote.fetched_at, str(quote.fx_rate) if quote.fx_rate else None,
             quote.fx_as_of, now()),
        )


def portfolio(current, positions, quotes):
    rows = []
    for position in positions:
        symbol = position["symbol"]
        quote = quotes.get(symbol)
        held = quantity(symbol, position["units"])
        value = won(held * quote.price) if quote else None
        rows.append({"symbol": symbol, "quantity": held, "cost": position["cost"],
                     "value": value, "profit": value - position["cost"] if quote else None,
                     "quote": quote})

    if any(row["value"] is None for row in rows):
        return rows, None, None, None
    total = current["cash"] + sum(row["value"] for row in rows)
    profit = sum(row["profit"] for row in rows)
    return_rate = Decimal(total - current["initial_cash"]) * 100 / current["initial_cash"]
    return rows, total, profit, return_rate
