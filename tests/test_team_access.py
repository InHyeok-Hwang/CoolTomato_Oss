from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app import create_app
from test_app import HiddenInputs


class TeamAccessTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.legacy = self.root / "replay.sqlite3"
        self.legacy.write_bytes(b"existing local data must remain untouched")
        self.config = {
            "TESTING": True, "TEAM_MODE": True, "SECRET_KEY": "test-only-fixed-secret",
            "DATABASE": self.legacy, "VISITOR_DATABASE_DIR": self.root / "visitors",
            "EPHEMERAL_STORAGE": True,
        }
        self.app = create_app(self.config)
        self.alice = self.app.test_client()
        self.bob = self.app.test_client()
        offline = patch("market.Market.stock_series", side_effect=OSError("test offline"))
        offline.start()
        self.addCleanup(offline.stop)

    def form(self, client, url):
        return HiddenInputs(client.get(url).get_data(as_text=True)).values

    def start(self, client, amount):
        client.post("/setup", data={**self.form(client, "/setup"), "scenario": "custom",
                                   "amount": str(amount), "start_day": "2024-06-17"})

    def test_visitors_trade_advance_and_reset_independently(self):
        self.start(self.alice, 1_000_000)
        self.start(self.bob, 5_000_000)
        order = self.form(self.alice, "/market?symbol=AAPL")
        response = self.alice.post("/trade/AAPL", data={**order, "side": "buy", "quantity": "1"},
                                   follow_redirects=True)
        self.assertIn("모의 매수가 체결", response.get_data(as_text=True))
        self.assertIn("아직 체결된", self.bob.get("/history").get_data(as_text=True))
        self.assertIn("5,000,000원", self.bob.get("/dashboard").get_data(as_text=True))
        # Another visitor's valid CSRF token is not valid for this browser.
        self.assertEqual(self.bob.post("/reset", data={**order, "confirm": "yes"}).status_code, 400)
        self.alice.post("/simulation", data={**self.form(self.alice, "/dashboard"), "action": "1"})
        self.assertEqual(self.form(self.bob, "/dashboard")["day"], "2024-06-17")
        self.bob.post("/reset", data={**self.form(self.bob, "/dashboard"), "confirm": "yes"})
        self.assertIn("AAPL", self.alice.get("/history").get_data(as_text=True))
        self.assertIn("어떤 금액", self.bob.get("/dashboard", follow_redirects=True).get_data(as_text=True))
        self.assertEqual(self.legacy.read_bytes(), b"existing local data must remain untouched")

    def test_cookie_survives_app_restart_but_new_browser_starts_fresh(self):
        self.start(self.alice, 123456)
        cookie = self.alice.get_cookie("session")
        restarted = create_app(self.config).test_client()
        restarted.set_cookie("session", cookie.value)
        self.assertIn("123,456원", restarted.get("/dashboard").get_data(as_text=True))
        fresh = create_app(self.config).test_client()
        self.assertIn("어떤 금액", fresh.get("/dashboard", follow_redirects=True).get_data(as_text=True))

    def test_health_creates_no_account_and_pages_are_private(self):
        response = self.alice.get("/healthz")
        self.assertEqual(response.get_json(), {"status": "ok"})
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(list((self.root / "visitors").iterdir()), [])
        page = self.alice.get("/setup")
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertIn("브라우저별로 분리", page.get_data(as_text=True))
        self.assertIn("초기화될 수 있어요", page.get_data(as_text=True))

    def test_invalid_visitor_id_cannot_escape_database_directory(self):
        with self.alice.session_transaction() as session:
            session["visitor_id"] = "../replay"
        self.alice.get("/setup")
        with self.alice.session_transaction() as session:
            self.assertRegex(session["visitor_id"], r"^[0-9a-f]{64}$")
        self.assertEqual(self.legacy.read_bytes(), b"existing local data must remain untouched")

    def test_public_host_and_secure_cookie_configuration(self):
        with patch.dict("os.environ", {"RENDER_EXTERNAL_HOSTNAME": "example.onrender.com", "RENDER": "true"}):
            client = create_app(self.config).test_client()
            self.assertEqual(client.get("/healthz", base_url="https://example.onrender.com").status_code, 200)
            self.assertEqual(client.get("/healthz", base_url="https://untrusted.example").status_code, 400)
            cookie = client.get("/setup", base_url="https://example.onrender.com").headers["Set-Cookie"]
            for attribute in ("Secure", "HttpOnly", "SameSite=Lax"):
                self.assertIn(attribute, cookie)

    def test_team_mode_requires_stable_secret(self):
        config = {**self.config}
        del config["SECRET_KEY"]
        with patch.dict("os.environ", {}, clear=True), self.assertRaisesRegex(ValueError, "SECRET_KEY"):
            create_app(config)
