from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import secrets
import os

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for

import db
import coach
from market import DEMO, FIRST_DAY, LAST_DAY, SYMBOLS, Market
import simulation
import trading


KST = timezone(timedelta(hours=9))


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        DATABASE=Path(app.instance_path) / "replay.sqlite3",
        TRUSTED_HOSTS=["localhost", "127.0.0.1"] + [host for host in
                       [os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")] if host],
        MAX_CONTENT_LENGTH=8_192,
        TEAM_MODE=os.environ.get("TEAM_MODE") == "1",
        VISITOR_DATABASE_DIR=os.environ.get("VISITOR_DATABASE_DIR", str(Path(app.instance_path) / "visitors")),
        EPHEMERAL_STORAGE=os.environ.get("EPHEMERAL_STORAGE") == "1",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("RENDER") == "true",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    if config:
        app.config.update(config)
    database = Path(app.config["DATABASE"])
    visitor_directory = Path(app.config["VISITOR_DATABASE_DIR"])
    if app.config["TEAM_MODE"]:
        if not (os.environ.get("SECRET_KEY") or (config or {}).get("SECRET_KEY")):
            raise ValueError("TEAM_MODE requires a persistent SECRET_KEY")
        visitor_directory.mkdir(parents=True, exist_ok=True)
    else:
        database.parent.mkdir(parents=True, exist_ok=True)
        connection = db.connect(database)
        try:
            connection.executescript(db.SCHEMA)
        finally:
            connection.close()
    market = Market()

    def connection_for_request():
        if "db" not in g:
            path = database
            if app.config["TEAM_MODE"]:
                visitor = session.get("visitor_id")
                if not isinstance(visitor, str) or not re.fullmatch(r"[0-9a-f]{64}", visitor):
                    visitor = secrets.token_hex(32)
                    session["visitor_id"] = visitor
                    session.permanent = True
                path = visitor_directory / f"{visitor}.sqlite3"
            g.db = db.connect(path)
            if app.config["TEAM_MODE"]:
                g.db.executescript(db.SCHEMA)
        return g.db

    @app.teardown_appcontext
    def close_db(_error):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    @app.after_request
    def private_responses(response):
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response

    @app.get("/healthz")
    def health():
        return jsonify(status="ok")

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
                trading.setup(connection, request.form.get("scenario", ""), request.form.get("amount", ""),
                              request.form.get("start_day", ""))
            except trading.TradeError as error:
                flash(str(error), "error")
            else:
                return redirect(url_for("dashboard"))
        return render_template("setup.html", scenarios=trading.SCENARIOS, max_cash=trading.MAX_CASH,
                               first_day=FIRST_DAY, last_start=(datetime.fromisoformat(LAST_DAY) -
                                                              timedelta(days=1)).date().isoformat())

    @app.get("/dashboard")
    def dashboard():
        connection = connection_for_request()
        with connection:
            connection.execute("BEGIN")
            current = trading.account(connection)
            if not current:
                return redirect(url_for("setup_page"))
            rows, total, profit, rate = simulation.portfolio(connection, current, market, current["current_day"])
            trades = connection.execute("SELECT side, symbol, simulated_day FROM trades").fetchall()
            checkpoints = connection.execute("SELECT day, total FROM checkpoints ORDER BY day").fetchall()
        return render_template("dashboard.html", account=current, positions=rows, total=total,
                               profit=profit, rate=rate, last_day=LAST_DAY, steps=simulation.STEPS,
                               has_demo=any(row["quote"] and row["quote"].source == DEMO for row in rows),
                               feedback=coach.analyze(current["cash"], rows, total, rate, trades, checkpoints),
                               checkpoints=checkpoints,
                               scenario=trading.SCENARIOS[current["scenario"]][0])

    @app.post("/simulation")
    def update_simulation():
        form = request.form
        try:
            simulation.update(connection_for_request(), market, form.get("account_id", ""),
                              form.get("day", ""), form.get("action", ""))
        except trading.TradeError as error:
            flash(str(error), "error")
        return redirect(url_for("dashboard"))

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
        quote = market.quote(symbol, current["current_day"])
        source = market.series(symbol)[1]
        return render_template("market.html", account=current, symbol=symbol, instrument=SYMBOLS[symbol],
                               quote=quote, source=source, held=held, request_id=secrets.token_hex(16))

    @app.get("/api/chart/<symbol>")
    def chart_data(symbol):
        current = trading.account(connection_for_request())
        if symbol not in SYMBOLS or not current:
            abort(404)
        chart = market.chart(symbol, current["current_day"])
        response = jsonify(chart) if chart else jsonify(error="가격 그래프를 조회할 수 없어요.")
        response.headers["Cache-Control"] = "no-store"
        return response, 200 if chart else 503

    @app.post("/trade/<symbol>")
    def trade(symbol):
        if symbol not in SYMBOLS:
            abort(404)
        form = request.form
        request_id = form.get("request_id", "")
        try:
            if not re.fullmatch(r"[0-9a-f]{32}", request_id):
                raise trading.TradeError("주문 화면을 새로고침해 주세요.")
            connection = connection_for_request()
            expected_day = form.get("day", "")
            current = trading.active_account(connection, form.get("account_id", ""), expected_day)
            quote = market.quote(symbol, current["current_day"])
            if quote is None:
                raise trading.TradeError("시세를 확인할 수 없어 거래를 잠시 중단했어요.")
            try:
                shown_price = Decimal(form.get("price", ""))
            except InvalidOperation:
                shown_price = None
            if (shown_price is None or not shown_price.is_finite() or quote.price != shown_price or
                    quote.source != form.get("source")):
                raise trading.TradeError("가격이나 출처가 바뀌었어요. 새 조건을 확인하고 다시 주문해 주세요.")
            trading.execute(connection, quote, form.get("side", ""), form.get("quantity", ""),
                            form.get("account_id", ""), request_id, expected_day)
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
