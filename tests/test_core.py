from contextlib import closing
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import db
from market import DEMO, STOCK, UPBIT, Market, Quote, now
import trading


class TradingTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "test.sqlite3"
        self.connection = db.connect(self.path)
        self.addCleanup(self.connection.close)
        self.connection.executescript(db.SCHEMA)
        trading.setup(self.connection, "student", "")
        self.account_id = str(trading.account(self.connection)["id"])

    def order(self, symbol, price, side, amount, request_id="order"):
        quote = Quote(symbol, Decimal(price), UPBIT if symbol == "BTC" else DEMO, now(), now())
        trading.execute(self.connection, quote, side, amount, self.account_id, request_id)

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
        trading.setup(self.connection, "custom", "2500000")
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
    def test_upbit_current_price_and_history(self, request_json):
        request_json.side_effect = [
            [{"market": "KRW-BTC", "trade_timestamp": 1700000000000, "trade_price": 100_000_000}],
            [{"candle_date_time_kst": "2026-11-09T09:00:00", "trade_price": 101},
             {"candle_date_time_kst": "2026-11-08T09:00:00", "trade_price": 100}],
        ]
        market = Market("")
        quote = market.quote("BTC")
        self.assertEqual((quote.source, quote.price), (UPBIT, Decimal(100_000_000)))
        self.assertIs(market.quote("BTC"), quote)
        self.assertEqual(market.chart("BTC", UPBIT)["values"], [100.0, 101.0])
        self.assertEqual(request_json.call_count, 2)

    @patch("market.request_json")
    def test_stock_close_converts_to_won_and_chart_uses_usd(self, request_json):
        request_json.side_effect = [
            {"Time Series (Daily)": {"2026-11-06": {"4. close": "200.25"}}},
            {"Time Series FX (Daily)": {"2026-11-09": {"4. close": "1400.25"}}},
        ]
        market = Market("test-key")
        quote = market.quote("AAPL")
        self.assertEqual((quote.source, quote.price), (STOCK, Decimal("280400.0625")))
        self.assertEqual((quote.as_of, quote.fx_as_of), ("2026-11-06", "2026-11-09"))
        self.assertEqual(market.chart("AAPL", STOCK)["currency"], "USD")
        self.assertEqual(request_json.call_count, 2)

    @patch("market.request_json")
    def test_stock_missing_fx_switches_to_demo(self, request_json):
        request_json.side_effect = [
            {"Time Series (Daily)": {"2026-11-06": {"4. close": "200.25"}}},
            {"Note": "Rate limit"},
        ]
        market = Market("test-key")
        quote = market.quote("AAPL")
        chart = market.chart("AAPL", quote.source)
        self.assertEqual(quote.source, DEMO)
        self.assertEqual((chart["values"][-1], chart["currency"]), (290_000, "KRW"))

    @patch("market.request_json", side_effect=OSError("offline"))
    def test_coin_outage_never_uses_demo_prices(self, _request_json):
        market = Market("")
        self.assertIsNone(market.quote("BTC"))
        self.assertIsNone(market.chart("BTC", UPBIT))
        self.assertIsNone(market.chart("BTC", DEMO))
