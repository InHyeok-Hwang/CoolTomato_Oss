from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app import create_app
from market import DEMO, FIRST_DAY


class HiddenInputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.values = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("type") == "hidden":
            self.values[attrs["name"]] = attrs["value"]


class AppTests(unittest.TestCase):
    def setUp(self):
        offline = patch("market.Market.stock_series", side_effect=OSError("offline"))
        offline.start()
        self.addCleanup(offline.stop)
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = {"TESTING": True, "DATABASE": Path(temp.name) / "app.sqlite3"}
        self.client = create_app(self.config).test_client()

    def form(self, url):
        return HiddenInputs(self.client.get(url).get_data(as_text=True)).values

    def start(self):
        return self.client.post("/setup", data={**self.form("/setup"), "scenario": "student", "start_day": FIRST_DAY}, follow_redirects=True)

    def test_four_pages_trade_chart_persistence_and_reset(self):
        setup = self.client.get("/", follow_redirects=True).get_data(as_text=True)
        self.assertIn("어떤 금액으로 시작할까요?", setup)
        self.assertIn("1,000,000원", self.start().get_data(as_text=True))
        order = self.form("/market?symbol=AAPL")
        self.assertEqual(order["source"], DEMO)
        result = self.client.post("/trade/AAPL", data={**order, "side": "buy", "quantity": "1"},
                                  follow_redirects=True)
        self.assertIn("모의 매수가 체결", result.get_data(as_text=True))
        self.assertIn("720,000원", result.get_data(as_text=True))
        chart = self.client.get("/api/chart/AAPL", query_string={"source": DEMO}).get_json()
        self.assertEqual(chart["values"][-1], 280_000)
        self.client = create_app(self.config).test_client()
        history = self.client.get("/history").get_data(as_text=True)
        self.assertIn(DEMO, history)
        self.assertIn("280,000원", history)
        sell = self.form("/market?symbol=AAPL")
        result = self.client.post("/trade/AAPL", data={**sell, "side": "sell", "quantity": "1"},
                                  follow_redirects=True)
        self.assertIn("모의 매도가 체결", result.get_data(as_text=True))
        reset = self.form("/dashboard")
        result = self.client.post("/reset", data={**reset, "confirm": "yes"}, follow_redirects=True)
        self.assertIn("어떤 금액으로 시작할까요?", result.get_data(as_text=True))
        self.start()
        self.assertIn("아직 체결된", self.client.get("/history").get_data(as_text=True))

    def test_tampered_price_and_missing_csrf_are_rejected(self):
        self.start()
        order = {**self.form("/market?symbol=AAPL"), "side": "buy", "quantity": "1"}
        for price in ("1", "sNaN"):
            with self.subTest(price=price):
                result = self.client.post("/trade/AAPL", data={**order, "price": price}, follow_redirects=True)
                self.assertIn("가격이나 출처가 바뀌었어요", result.get_data(as_text=True))
        self.assertEqual(self.client.post("/trade/AAPL", data={**order, "csrf": ""}).status_code, 400)
        self.assertIn("아직 체결된", self.client.get("/history").get_data(as_text=True))

    @patch("market.request_json", side_effect=OSError("offline"))
    def test_unavailable_coin_disables_order(self, _request_json):
        self.start()
        page = self.client.get("/market?symbol=BTC").get_data(as_text=True)
        self.assertIn("시세 조회 불가", page)
        self.assertNotIn("모의 매수", page)

    @patch("market.request_json")
    def test_coin_replay_advances_evaluates_and_finishes(self, request_json):
        request_json.return_value = [
            {"market": "KRW-BTC", "candle_date_time_utc": f"{day}T00:00:00", "trade_price": price}
            for day, price in (("2024-01-01", 50_000_000), ("2024-01-02", 40_000_000),
                               ("2024-01-09", 60_000_000), ("2024-02-08", 70_000_000))
        ]
        self.start()
        chart = self.client.get("/api/chart/BTC?day=2024-06-30").get_json()
        self.assertEqual(chart["labels"], ["2024-01-01"])
        order = {**self.form("/market?symbol=BTC"), "side": "buy", "quantity": "0.012"}
        self.assertIn("모의 매수가 체결", self.client.post("/trade/BTC", data=order,
                         follow_redirects=True).get_data(as_text=True))
        clock = self.form("/dashboard")
        response = self.client.post("/simulation", data={**clock, "action": "1"}, follow_redirects=True)
        page = response.get_data(as_text=True)
        for expected in ("2024-01-02", "880,000원", "-12.00%", "집중 기준에 해당", "45.5%"):
            self.assertIn(expected, page)
        self.assertIn("날짜가 변경", self.client.post("/trade/BTC", data=order,
                      follow_redirects=True).get_data(as_text=True))
        self.assertIn("날짜가 변경", self.client.post("/simulation", data={**clock, "action": "1"},
                      follow_redirects=True).get_data(as_text=True))
        for step, day in (("7", "2024-01-09"), ("30", "2024-02-08")):
            self.client.post("/simulation", data={**self.form("/dashboard"), "action": step})
            self.assertEqual(self.form("/dashboard")["day"], day)
        clock = self.form("/dashboard")
        self.client.post("/simulation", data={**clock, "action": "-1"})
        self.assertEqual(self.form("/dashboard")["day"], "2024-02-08")
        open_order = self.form("/market?symbol=BTC")
        final = self.client.post("/simulation", data={**clock, "action": "finish"}, follow_redirects=True)
        for expected in ("종합 결과", "1,240,000원", "+24.00%", "12.00%", "매수 1회"):
            self.assertIn(expected, final.get_data(as_text=True))
        self.assertIn("이미 종료", self.client.post("/trade/BTC",
                      data={**open_order, "side": "sell", "quantity": "0.012"},
                      follow_redirects=True).get_data(as_text=True))

    def test_invalid_start_and_last_date_cap(self):
        form = {**self.form("/setup"), "scenario": "custom", "amount": "345678"}
        invalid = self.client.post("/setup", data={**form, "start_day": "2024-02-31"})
        self.assertIn("시작 날짜를 지정", invalid.get_data(as_text=True))
        self.client.post("/setup", data={**form, "start_day": "2024-06-29"})
        self.client.post("/simulation", data={**self.form("/dashboard"), "action": "30"})
        self.assertEqual(self.form("/dashboard")["day"], "2024-06-30")
        self.assertNotIn("모의 매수", self.client.get("/market?symbol=AAPL").get_data(as_text=True))

    def test_reported_sunday_then_monday_demo_trade(self):
        self.client.post("/setup", data={**self.form("/setup"), "scenario": "student",
                                         "start_day": "2024-06-16"})
        page = self.client.get("/market?symbol=AAPL").get_data(as_text=True)
        for text in ("데모 데이터", "주말·휴장일", "내 자산에서 날짜 진행하기"):
            self.assertIn(text, page)
        self.assertNotIn('action="/trade/AAPL"', page)
        self.client.post("/simulation", data={**self.form("/dashboard"), "action": "1"})
        order = self.form("/market?symbol=AAPL")
        response = self.client.post("/trade/AAPL", data={**order, "side": "buy", "quantity": "1"},
                                    follow_redirects=True)
        self.assertIn("모의 매수가 체결", response.get_data(as_text=True))

    def test_real_stock_buy_sell_and_history(self):
        from decimal import Decimal
        from market import STOCK
        with patch("market.Market.stock_series", return_value={"2024-06-17": Decimal("250000")}):
            self.client.post("/setup", data={**self.form("/setup"), "scenario": "student",
                                             "start_day": "2024-06-17"})
            for side, message in (("buy", "모의 매수가 체결"), ("sell", "모의 매도가 체결")):
                order = self.form("/market?symbol=AAPL")
                self.assertEqual(order["source"], STOCK)
                result = self.client.post("/trade/AAPL", data={**order, "side": side, "quantity": "1"},
                                          follow_redirects=True)
                self.assertIn(message, result.get_data(as_text=True))
            self.assertIn("1,000,000원", result.get_data(as_text=True))
            self.assertIn(STOCK, self.client.get("/history").get_data(as_text=True))
            self.assertEqual(self.client.get("/api/chart/AAPL").get_json()["source"], STOCK)
