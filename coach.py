from decimal import Decimal


def analyze(cash, positions, total, rate, trades, checkpoints):
    buys = sum(trade["side"] == "buy" for trade in trades)
    sells = len(trades) - buys
    same_day_turns = len({(trade["simulated_day"], trade["symbol"]) for trade in trades
                          if trade["side"] == "buy"} &
                         {(trade["simulated_day"], trade["symbol"]) for trade in trades
                          if trade["side"] == "sell"})
    messages = [f"매수 {buys}회 · 매도 {sells}회 · 같은 날 양방향 거래 {same_day_turns}건(날짜·종목 조합)."]
    if not trades:
        messages.append("아직 체결된 거래가 없어요.")
    messages.append(f"보유 종목 {len(positions)}개. 두 종목 이상 보유는 분산의 출발점이에요. "
                    "종목 수만으로 위험을 판단하기는 어려워요.")
    if total is None:
        messages.append("평가 가능한 가격이 확보되면 현금 비중, 집중도, 수익률을 계산해요.")
    elif total:
        cash_share = Decimal(cash) * 100 / total
        messages.append(f"현금은 총자산의 {cash_share:.1f}%예요. "
                        + ("현금 비중이 낮아 추가 매수에 쓸 여유가 적어요." if cash_share < 10
                           else "다음 투자 결정을 할 때 쓸 수 있는 현금이에요."))
        if positions:
            largest = max(positions, key=lambda row: row["value"])
            share = Decimal(largest["value"]) * 100 / total
            flag = "집중 기준에 해당해요." if share >= 50 else "집중 기준인 50% 미만이에요."
            messages.append(f"가장 큰 종목 {largest['symbol']}은 총자산의 {share:.1f}%로, {flag}")
    if rate is not None:
        messages.append(f"초기 투자금 대비 누적 수익률은 {rate:+.2f}%예요.")
    if len(checkpoints) > 1:
        previous, latest = checkpoints[-2:]
        change = latest["total"] - previous["total"]
        messages.append(f"직전 평가일({previous['day']}) 대비 총자산이 {change:+,}원 변했어요.")
        peak = checkpoints[0]["total"]
        drawdown = Decimal(0)
        for checkpoint in checkpoints:
            peak = max(peak, checkpoint["total"])
            drawdown = max(drawdown, Decimal(peak - checkpoint["total"]) * 100 / peak)
        messages.append(f"기록한 평가 시점 기준 최대 낙폭은 {drawdown:.2f}%예요.")
    return messages
