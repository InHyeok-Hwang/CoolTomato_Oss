from datetime import date, timedelta

from db import transaction
from market import LAST_DAY
import trading


STEPS = {"1": 1, "7": 7, "30": 30}


def portfolio(connection, current, market, day):
    positions = connection.execute("SELECT * FROM positions ORDER BY symbol").fetchall()
    quotes = {row["symbol"]: market.quote(row["symbol"], day, valuation=True) for row in positions}
    return trading.portfolio(current, positions, quotes)


def update(connection, market, account_id, expected_day, action):
    if action != "finish" and action not in STEPS:
        raise trading.TradeError("1일, 7일, 30일 중에서 선택해 주세요.")
    with transaction(connection):
        current = trading.active_account(connection, account_id, expected_day)
        day = current["current_day"]
        finished = action == "finish"
        if not finished:
            if day == LAST_DAY:
                raise trading.TradeError("시뮬레이션의 마지막 날짜예요. 투자를 종료해 결과를 확인해 주세요.")
            day = min(LAST_DAY, (date.fromisoformat(day) + timedelta(days=STEPS[action])).isoformat())

        _, total, _, _ = portfolio(connection, current, market, day)
        if total is None:
            raise trading.TradeError("보유 종목 시세를 확인할 수 없어 자산 평가를 보류했어요.")
        connection.execute("UPDATE account SET current_day = ?, finished = ? WHERE id = ?",
                           (day, finished, current["id"]))
        # 종료 시 같은 날 거래가 더 있었다면 최종 평가액으로 갱신한다.
        connection.execute("INSERT INTO checkpoints VALUES (?, ?) ON CONFLICT(day) "
                           "DO UPDATE SET total = excluded.total", (day, total))
