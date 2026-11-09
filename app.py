from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import re
import secrets

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for

import db
from market import DEMO, SYMBOLS, UPBIT, Market
import trading


KST = timezone(timedelta(hours=9))


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=secrets.token_hex(32),
        DATABASE=Path(app.instance_path) / "portfolio.sqlite3",
        ALPHA_VANTAGE_API_KEY=os.environ.get("ALPHA_VANTAGE_API_KEY", "").strip(),
        TRUSTED_HOSTS=["localhost", "127.0.0.1"],
        MAX_CONTENT_LENGTH=8_192,
    )
    if config:
        app.config.update(config)
    database = Path(app.config["DATABASE"])
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = db.connect(database)
    try:
        connection.executescript(db.SCHEMA)
    finally:
        connection.close()
    market = Market(app.config["ALPHA_VANTAGE_API_KEY"])

    def connection_for_request():
        if "db" not in g:
            g.db = db.connect(database)
        return g.db

    @app.teardown_appcontext
    def close_db(_error):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    @app.before_request
    def validate_form():
        if request.method == "POST":
            expected = session.get("csrf", "")
            received = request.form.get("csrf", "")
            if not expected or not received.isascii() or not secrets.compare_digest(expected, received):
                abort(400, "화면을 새로고침한 뒤 다시 시도해 주세요.")

    @app.context_processor
    def template_values():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(24)
        return {"csrf": session["csrf"], "symbols": SYMBOLS, "demo_source": DEMO}

    @app.template_filter("money")
    def format_money(value):
        if value is None:
            return "—"
        number = f"{Decimal(value):,.8f}".rstrip("0").rstrip(".")
        return f"{number}원"

    @app.template_filter("quantity")
    def format_quantity(value):
        return f"{value:,.8f}".rstrip("0").rstrip(".")

    @app.template_filter("kst")
    def format_kst(value):
        if not value or "T" not in value:
            return value
        return datetime.fromisoformat(value).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")

    @app.get("/")
    def index():
        return redirect(url_for("dashboard"))

    @app.route("/setup", methods=["GET", "POST"])
    def setup_page():
        connection = connection_for_request()
        if trading.account(connection):
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            try:
                trading.setup(connection, request.form.get("scenario", ""), request.form.get("amount", ""))
            except trading.TradeError as error:
                flash(str(error), "error")
            else:
                return redirect(url_for("dashboard"))
        return render_template("setup.html", scenarios=trading.SCENARIOS, max_cash=trading.MAX_CASH)

    @app.get("/dashboard")
    def dashboard():
        connection = connection_for_request()
        with connection:
            connection.execute("BEGIN")
            current = trading.account(connection)
            if not current:
                return redirect(url_for("setup_page"))
            positions = connection.execute("SELECT * FROM positions ORDER BY symbol").fetchall()
        quotes = {position["symbol"]: market.quote(position["symbol"]) for position in positions}
        rows, total, profit, rate = trading.portfolio(current, positions, quotes)
        return render_template("dashboard.html", account=current, positions=rows, total=total,
                               profit=profit, rate=rate, has_demo=any(q and q.source == DEMO for q in quotes.values()),
                               scenario=trading.SCENARIOS[current["scenario"]][0])

    @app.get("/market")
    def market_page():
        connection = connection_for_request()
        current = trading.account(connection)
        if not current:
            return redirect(url_for("setup_page"))
        symbol = request.args.get("symbol", "BTC")
        if symbol not in SYMBOLS:
            abort(404)
        position = connection.execute("SELECT units FROM positions WHERE symbol = ?", (symbol,)).fetchone()
        held = trading.quantity(symbol, position["units"]) if position else Decimal(0)
        quote = market.quote(symbol)
        return render_template("market.html", account=current, symbol=symbol, instrument=SYMBOLS[symbol],
                               quote=quote, held=held, request_id=secrets.token_hex(16),
                               chart_source=quote.source if quote else UPBIT)

    @app.get("/api/chart/<symbol>")
    def chart_data(symbol):
        if symbol not in SYMBOLS or not trading.account(connection_for_request()):
            abort(404)
        chart = market.chart(symbol, request.args.get("source", ""))
        return jsonify(chart) if chart else (jsonify(error="가격 그래프를 조회할 수 없어요."), 503)

    @app.post("/trade/<symbol>")
    def trade(symbol):
        if symbol not in SYMBOLS:
            abort(404)
        form = request.form
        request_id = form.get("request_id", "")
        try:
            if not re.fullmatch(r"[0-9a-f]{32}", request_id):
                raise trading.TradeError("주문 화면을 새로고침해 주세요.")
            quote = market.quote(symbol)
            if quote is None:
                raise trading.TradeError("시세를 확인할 수 없어 거래를 잠시 중단했어요.")
            try:
                shown_price = Decimal(form.get("price", ""))
            except InvalidOperation:
                shown_price = None
            if (shown_price is None or not shown_price.is_finite() or quote.price != shown_price or
                    quote.source != form.get("source") or
                    str(quote.fx_rate or "") != form.get("fx_rate", "")):
                raise trading.TradeError("가격이나 출처가 바뀌었어요. 새 조건을 확인하고 다시 주문해 주세요.")
            trading.execute(connection_for_request(), quote, form.get("side", ""), form.get("quantity", ""),
                            form.get("account_id", ""), request_id)
        except trading.TradeError as error:
            flash(str(error), "error")
        else:
            message = "모의 매수가 체결됐어요." if form.get("side") == "buy" else "모의 매도가 체결됐어요."
            flash(message, "success")
        return redirect(url_for("market_page", symbol=symbol))

    @app.get("/history")
    def history():
        connection = connection_for_request()
        if not trading.account(connection):
            return redirect(url_for("setup_page"))
        trades = connection.execute("SELECT * FROM trades ORDER BY id DESC").fetchall()
        return render_template("history.html", trades=trades, to_quantity=trading.quantity)

    @app.post("/reset")
    def reset_page():
        if request.form.get("confirm") != "yes":
            flash("초기화 확인란을 선택해 주세요.", "error")
            return redirect(url_for("dashboard"))
        try:
            trading.reset(connection_for_request(), request.form.get("account_id", ""))
        except trading.TradeError as error:
            flash(str(error), "error")
            return redirect(url_for("dashboard"))
        flash("투자 계좌를 초기화했어요.", "success")
        return redirect(url_for("setup_page"))

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000)
