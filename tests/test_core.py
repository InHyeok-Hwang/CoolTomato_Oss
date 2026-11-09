from contextlib import closing
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import db
from market import DEMO, FIRST_DAY, UPBIT, Market, Quote, now
import trading


class TradingTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "test.sqlite3"
        self.connection = db.connect(self.path)
        self.addCleanup(self.connection.close)
        self.connection.executescript(db.SCHEMA)
        trading.setup(self.connection, "student", "", FIRST_DAY)
        self.account_id = str(trading.account(self.connection)["id"])

    def order(self, symbol, price, side, amount, request_id="order"):
        quote = Quote(symbol, Decimal(price), UPBIT if symbol == "BTC" else DEMO, FIRST_DAY, now())
        trading.execute(self.connection, quote, side, amount, self.account_id, request_id, FIRST_DAY)

    def test_stock_buy_and_partial_sell_preserve_cost_and_cash(self):
        self.order("AAPL", "100000", "buy", "3", "first")
        self.order("AAPL", "120000", "sell", "1", "second")
        with closing(db.connect(self.path)) as reopened:
            current = trading.account(reopened)
            positions = reopened.execute("SELECT * FROM positions").fetchall()
            quote = Quote("AAPL", Decimal(110_000), DEMO, now(), now())
            rows, total, profit, rate = trading.portfolio(current, positions, {"AAPL": quote})
            self.assertEqual(current["cash"], 820_000)
            self.assertEqual(rows[0]["cost"], 200_000)
            self.assertEqual((total, profit, rate), (1_040_000, 20_000, Decimal(4)))
            self.assertEqual(reopened.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 2)

    def test_coin_precision_and_full_sale(self):
        self.order("BTC", "100000000", "buy", "0.00000001", "first")
        self.order("BTC", "100000000", "sell", "0.00000001", "second")
        self.assertEqual(trading.account(self.connection)["cash"], 1_000_000)
        self.assertIsNone(self.connection.execute("SELECT * FROM positions").fetchone())

    def test_invalid_orders_leave_database_unchanged(self):
        for symbol, side, amount in (("AAPL", "buy", "11"), ("AAPL", "sell", "1"),
                                     ("AAPL", "buy", "1.5"), ("BTC", "buy", "0.123456789"),
                                     ("BTC", "buy", "0.00000001"), ("BTC", "buy", "NaN")):
            with self.subTest(symbol=symbol, side=side, amount=amount), self.assertRaises(trading.TradeError):
                self.order(symbol, "100000", side, amount)
        self.assertEqual(trading.account(self.connection)["cash"], 1_000_000)
        self.assertIsNone(self.connection.execute("SELECT * FROM trades").fetchone())

    def test_double_submission_is_rejected_and_reset_invalidates_old_order(self):
        self.order("AAPL", "100000", "buy", "1")
        with self.assertRaisesRegex(trading.TradeError, "이미 처리"):
            self.order("AAPL", "100000", "buy", "1")
        trading.reset(self.connection, self.account_id)
        self.assertIsNone(self.connection.execute("SELECT * FROM trades").fetchone())
        self.assertIsNone(self.connection.execute("SELECT * FROM positions").fetchone())
        self.assertIsNone(trading.account(self.connection))
        trading.setup(self.connection, "custom", "2500000", FIRST_DAY)
        self.assertEqual(trading.account(self.connection)["cash"], 2_500_000)
        with self.assertRaisesRegex(trading.TradeError, "계좌가 변경"):
            self.order("AAPL", "100000", "buy", "1")

    def test_missing_coin_price_does_not_count_as_zero(self):
        self.order("BTC", "100000000", "buy", "0.001")
        rows, total, profit, rate = trading.portfolio(
            trading.account(self.connection), self.connection.execute("SELECT * FROM positions"), {})
        self.assertIsNone(rows[0]["value"])
        self.assertEqual((total, profit, rate), (None, None, None))

    def test_trade_insert_failure_rolls_back_cash_and_position(self):
        self.connection.execute("""CREATE TRIGGER reject_trade BEFORE INSERT ON trades
                                 BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
        with self.assertRaises(db.sqlite3.IntegrityError):
            self.order("AAPL", "100000", "buy", "1")
        self.assertEqual(trading.account(self.connection)["cash"], 1_000_000)
        self.assertIsNone(self.connection.execute("SELECT * FROM positions").fetchone())



class MarketTests(unittest.TestCase):
    @patch("market.request_json")
    def test_upbit_historical_quote_chart_and_missing_day(self, request_json):
        request_json.return_value = [
            {"market": "KRW-BTC", "candle_date_time_utc": "2024-07-01T00:00:00", "trade_price": 1000},
            {"market": "KRW-BTC", "candle_date_time_utc": "2024-01-03T00:00:00", "trade_price": 200},
            {"market": "KRW-BTC", "candle_date_time_utc": "2024-01-01T00:00:00", "trade_price": 100},
        ]
        market = Market()
        self.assertEqual(market.quote("BTC", "2024-01-01").source, UPBIT)
        self.assertIsNone(market.quote("BTC", "2024-01-02"))
        self.assertEqual(market.quote("BTC", "2024-01-02", valuation=True).price, 100)
        self.assertEqual(market.chart("BTC", "2024-01-02")["values"], [100.0])
        self.assertEqual(market.chart("BTC", "2024-06-30")["values"], [100.0, 200.0])
        self.assertEqual(request_json.call_count, 1)
        self.assertEqual(request_json.call_args.args[1]["to"], "2024-07-01T00:00:00Z")

    def test_demo_stock_weekend_has_no_trade_and_uses_friday_valuation(self):
        market = Market()
        self.assertIsNone(market.quote("AAPL", "2024-01-06"))
        quote = market.quote("AAPL", "2024-01-06", valuation=True)
        self.assertEqual((quote.source, quote.as_of), (DEMO, "2024-01-05"))
        self.assertEqual(market.chart("AAPL", "2024-01-06")["labels"][-1], "2024-01-05")

    @patch("market.request_json", side_effect=OSError("offline"))
    def test_coin_outage_never_uses_demo_prices(self, _request_json):
        market = Market()
        self.assertIsNone(market.quote("BTC", FIRST_DAY))
        self.assertIsNone(market.chart("BTC", FIRST_DAY))
