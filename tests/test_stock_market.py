from datetime import datetime, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch

from market import DEMO, STOCK, Market


def payload(symbol, currency, rows):
    return {"chart": {"error": None, "result": [{
        "meta": {"symbol": symbol, "currency": currency},
        "timestamp": [int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())
                      for day, _ in rows],
        "indicators": {"quote": [{"close": [value for _, value in rows]}]},
    }]}}


class StockMarketTests(unittest.TestCase):
    @patch("market.request_json")
    def test_real_history_fx_no_future_and_no_holiday_demo(self, request):
        request.side_effect = [
            payload("AAPL", "USD", [("2024-01-02", 100), ("2024-01-03", 110)]),
            payload("KRW=X", "KRW", [("2023-12-29", 1300), ("2024-01-03", 1400)]),
        ]
        market = Market()
        quote = market.quote("AAPL", "2024-01-02")
        self.assertEqual((quote.price, quote.source), (Decimal(130000), STOCK))
        self.assertEqual(market.quote("AAPL", "2024-01-03").price, 154000)
        self.assertIsNone(market.quote("AAPL", "2024-01-01"))
        self.assertEqual(market.series("AAPL")[1], STOCK)
        self.assertEqual(market.chart("AAPL", "2024-01-02")["labels"], ["2024-01-02"])
        self.assertEqual(market.quote("AAPL", "2024-01-06", valuation=True).as_of, "2024-01-03")
        self.assertEqual(request.call_count, 2)

    def test_failed_empty_or_malformed_history_uses_labeled_demo(self):
        invalid = [OSError("offline"), {}, {"chart": {"error": None, "result": []}},
                   payload("AAPL", "EUR", [("2024-01-02", 100)]),
                   payload("AAPL", "USD", [("2024-01-02", None)]),
                   payload("AAPL", "USD", [("2024-01-02", "NaN")]),
                   payload("MSFT", "USD", [("2024-01-02", 100)])]
        for response in invalid:
            with self.subTest(response=response), patch("market.request_json") as request:
                if isinstance(response, Exception):
                    request.side_effect = response
                else:
                    request.return_value = response
                market = Market()
                self.assertEqual(market.quote("AAPL", "2024-01-02").source, DEMO)
                self.assertEqual(market.chart("AAPL", "2024-01-02")["source"], DEMO)
                self.assertIsNone(market.quote("AAPL", "2024-01-06"))

    @patch("market.request_json")
    def test_missing_or_future_fx_falls_back_for_entire_series(self, request):
        for rates in ([("2024-01-03", 1300)], [("2023-12-01", 1300)]):
            with self.subTest(rates=rates):
                request.side_effect = [payload("AAPL", "USD", [("2024-01-02", 100)]),
                                       payload("KRW=X", "KRW", rates)]
                self.assertEqual(Market().quote("AAPL", "2024-01-02").source, DEMO)
