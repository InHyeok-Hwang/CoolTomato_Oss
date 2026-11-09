from contextlib import contextmanager
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario TEXT NOT NULL,
    initial_cash INTEGER NOT NULL CHECK (initial_cash > 0),
    cash INTEGER NOT NULL CHECK (cash >= 0)
);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    units INTEGER NOT NULL CHECK (units > 0),
    cost INTEGER NOT NULL CHECK (cost >= 0)
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    units INTEGER NOT NULL CHECK (units > 0),
    unit_price TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount > 0),
    source TEXT NOT NULL,
    as_of TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    fx_rate TEXT,
    fx_as_of TEXT,
    traded_at TEXT NOT NULL
);
"""


def connect(path):
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def transaction(connection):
    # Lock before reading balances so concurrent orders cannot spend the same cash.
    connection.execute("BEGIN IMMEDIATE")
    with connection:
        yield
