import os
import sqlite3
from contextlib import contextmanager

from flask import current_app, has_app_context

from config import DB_PATH


def _database_path():
    if has_app_context():
        return current_app.config["DATABASE"]
    return DB_PATH


def get_db_connection(path=None):
    """Open a SQLite connection.

    The connection is in autocommit mode (isolation_level=None) so that
    transactions are always explicit - see `transaction()`.
    """
    path = path or _database_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction(conn=None):
    """Run a block inside BEGIN IMMEDIATE ... COMMIT."""
    owns_connection = conn is None
    if owns_connection:
        conn = get_db_connection()

    transaction_started = False

    try:
        conn.execute("BEGIN IMMEDIATE")
        transaction_started = True

        yield conn

        conn.execute("COMMIT")
        transaction_started = False

    except BaseException:
        if transaction_started and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    finally:
        if owns_connection:
            conn.close()
