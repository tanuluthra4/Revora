import sqlite3
import pytest
from database.db import transaction


def test_commit_failure_rolls_back_caller_owned_connection():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        conn.execute("""
            CREATE TABLE parent (
                id INTEGER PRIMARY KEY
            )
        """)
        conn.execute("""
            CREATE TABLE child (
                parent_id INTEGER,
                FOREIGN KEY (parent_id)
                    REFERENCES parent(id)
                    DEFERRABLE INITIALLY DEFERRED
            )
        """)

        with pytest.raises(sqlite3.IntegrityError):
            with transaction(conn):
                conn.execute(
                    "INSERT INTO child (parent_id) VALUES (999)"
                )

        assert not conn.in_transaction
        assert conn.execute(
            "SELECT COUNT(*) FROM child"
        ).fetchone()[0] == 0

        # Caller-owned connection remains usable.
        conn.execute("INSERT INTO parent (id) VALUES (999)")

    finally:
        conn.close()


def test_failed_begin_does_not_rollback_existing_transaction():
    conn = sqlite3.connect(":memory:", isolation_level=None)

    try:
        conn.execute("CREATE TABLE items (value INTEGER)")
        conn.execute("BEGIN")
        conn.execute("INSERT INTO items VALUES (42)")

        with pytest.raises(sqlite3.OperationalError):
            with transaction(conn):
                pass

        # The transaction that existed before the call must survive.
        assert conn.in_transaction
        assert conn.execute(
            "SELECT COUNT(*) FROM items"
        ).fetchone()[0] == 1

        conn.execute("ROLLBACK")

    finally:
        conn.close()


def test_original_exception_is_preserved_and_work_is_rolled_back(tmp_path):
    class Boom(Exception):
        pass

    conn = sqlite3.connect(str(tmp_path / "t.db"), isolation_level=None)
    conn.execute("CREATE TABLE t (v INTEGER)")
    with pytest.raises(Boom):
        with transaction(conn):
            conn.execute("INSERT INTO t VALUES (1)")
            raise Boom()
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    conn.close()


def test_owned_connection_is_closed_on_success_and_on_failure(tmp_path, monkeypatch):
    import database.db as dbmod
    seen = []
    real = dbmod.get_db_connection

    def tracking(path=None):
        c = real(str(tmp_path / "o.db"))
        seen.append(c)
        return c

    monkeypatch.setattr(dbmod, "get_db_connection", tracking)
    with transaction() as c:
        c.execute("CREATE TABLE t (v INTEGER)")
    with pytest.raises(RuntimeError):
        with transaction() as c:
            c.execute("INSERT INTO t VALUES (1)")
            raise RuntimeError("fail")
    for c in seen:
        with pytest.raises(sqlite3.ProgrammingError):          # "Cannot operate on a closed database"
            c.execute("SELECT 1")
    check = real(str(tmp_path / "o.db"))
    assert check.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    check.close()


def test_two_writers_never_interleave(tmp_path):
    """BEGIN IMMEDIATE serialises writers: both increments land, none is lost."""
    import threading
    path = str(tmp_path / "c.db")
    setup = sqlite3.connect(path); setup.execute("CREATE TABLE n (v INTEGER)"); setup.execute("INSERT INTO n VALUES (0)")
    setup.commit(); setup.close()

    def bump():
        for _ in range(25):
            c = sqlite3.connect(path, timeout=10, isolation_level=None)
            with transaction(c):
                v = c.execute("SELECT v FROM n").fetchone()[0]
                c.execute("UPDATE n SET v = ?", (v + 1,))
            c.close()

    threads = [threading.Thread(target=bump) for _ in range(4)]
    [t.start() for t in threads]; [t.join() for t in threads]
    c = sqlite3.connect(path)
    assert c.execute("SELECT v FROM n").fetchone()[0] == 100
    c.close()
