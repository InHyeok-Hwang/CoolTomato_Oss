from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app import create_app
from market import DEMO


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
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = {"TESTING": True, "DATABASE": Path(temp.name) / "app.sqlite3", "ALPHA_VANTAGE_API_KEY": ""}
        self.client = create_app(self.config).test_client()

    def form(self, url):
        return HiddenInputs(self.client.get(url).get_data(as_text=True)).values

    def start(self):
        return self.client.post("/setup", data={**self.form("/setup"), "scenario": "student"}, follow_redirects=True)

    def test_four_pages_trade_chart_persistence_and_reset(self):
        setup = self.client.get("/", follow_redirects=True).get_data(as_text=True)
        self.assertIn("어떤 금액으로 시작할까요?", setup)
        self.assertIn("1,000,000원", self.start().get_data(as_text=True))
        order = self.form("/market?symbol=AAPL")
        self.assertEqual(order["source"], DEMO)
        result = self.client.post("/trade/AAPL", data={**order, "side": "buy", "quantity": "1"},
                                  follow_redirects=True)
        self.assertIn("모의 매수가 체결", result.get_data(as_text=True))
        self.assertIn("710,000원", result.get_data(as_text=True))
        chart = self.client.get("/api/chart/AAPL", query_string={"source": DEMO}).get_json()
        self.assertEqual(chart["values"][-1], 290_000)
        self.client = create_app(self.config).test_client()
        history = self.client.get("/history").get_data(as_text=True)
        self.assertIn(DEMO, history)
        self.assertIn("290,000원", history)
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
