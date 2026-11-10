"""share_links is append-only except for ONE permitted change: revoking an active link.

Every column is checked individually and combined with a revocation, because the
original guard let a revoking UPDATE also change id / created_by / created_at
(and `!=` is NULL, i.e. "unchanged", when created_by is NULL)."""
import sqlite3

import pytest

from app import init_db
from database.migrations import MIGRATIONS
from tests.test_migrations import SCHEMA

COLUMNS = {  # column -> a different value
    "id": 99, "workflow_id": 2, "round": 7, "kind": "proof", "token": "other-token",
    "created_by": 2, "created_at": "2001-01-01 00:00:00",
}


def seed(conn, created_by=1):
    conn.execute("INSERT OR IGNORE INTO users (id, username, email, password, role) VALUES (1,'a','a@x.com','x','developer')")
    conn.execute("INSERT OR IGNORE INTO users (id, username, email, password, role) VALUES (2,'b','b@x.com','x','reviewer')")
    for wid in (1, 2):
        conn.execute("INSERT OR IGNORE INTO workflow_instances (id, user_id, current_state) VALUES (?, 1, 'CREATED')", (wid,))
    conn.execute("INSERT INTO share_links (workflow_id, round, kind, token, created_by, created_at) "
                 "VALUES (1, 1, 'report', 'tok', ?, '2026-01-01 00:00:00')", (created_by,))
    conn.commit()


def row(conn):
    return tuple(conn.execute("SELECT * FROM share_links").fetchone())


@pytest.fixture(params=["fresh", "upgraded-from-v5"])
def conn(request, tmp_path):
    path = str(tmp_path / "t.db")
    if request.param == "fresh":
        init_db(path)
    else:                               # a database exactly as release v5 left it
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        c.executescript(open(SCHEMA, encoding="utf-8").read())
        c.execute("PRAGMA foreign_keys = ON")
        for version, migrate in MIGRATIONS:
            if version <= 5:
                migrate(c)
                c.execute(f"PRAGMA user_version = {version}")
        c.commit(); c.close()
        assert init_db(path) == MIGRATIONS[-1][0]      # the real upgrade path
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()


@pytest.mark.parametrize("column", sorted(COLUMNS))
def test_no_column_can_change_on_its_own(conn, column):
    seed(conn)
    before = row(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"UPDATE share_links SET {column} = ?", (COLUMNS[column],))
    assert row(conn) == before


@pytest.mark.parametrize("column", sorted(COLUMNS))
def test_revoking_cannot_smuggle_in_another_change(conn, column):
    seed(conn)
    before = row(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"UPDATE share_links SET revoked_at = CURRENT_TIMESTAMP, {column} = ?", (COLUMNS[column],))
    assert row(conn) == before                       # nothing, not even the revocation, was applied
    assert conn.execute("SELECT revoked_at FROM share_links").fetchone()[0] is None


def test_null_created_by_cannot_be_filled_in_while_revoking(conn):
    seed(conn, created_by=None)                      # `NEW != OLD` is NULL here, so the old guard passed
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE share_links SET revoked_at = CURRENT_TIMESTAMP, created_by = 2")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE share_links SET created_by = 2")
    assert conn.execute("SELECT created_by, revoked_at FROM share_links").fetchone() == (None, None)


def test_null_to_null_revocation_is_allowed(conn):
    seed(conn, created_by=None)
    conn.execute("UPDATE share_links SET revoked_at = CURRENT_TIMESTAMP")
    assert conn.execute("SELECT revoked_at FROM share_links").fetchone()[0] is not None


def test_revocation_is_allowed_exactly_once_and_is_one_way(conn):
    seed(conn)
    conn.execute("UPDATE share_links SET revoked_at = '2026-02-02 00:00:00'")
    for sql in ("UPDATE share_links SET revoked_at = NULL",                       # reactivate
                "UPDATE share_links SET revoked_at = '2027-01-01 00:00:00'",      # re-revoke / rewrite time
                "UPDATE share_links SET token = 'x'"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
    assert conn.execute("SELECT revoked_at FROM share_links").fetchone()[0] == "2026-02-02 00:00:00"


def test_links_are_never_deleted_and_the_workflow_cascade_still_works(conn):
    seed(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM share_links")
    conn.execute("DELETE FROM workflow_instances WHERE id = 1")          # deleting the owner's request cascades
    assert conn.execute("SELECT COUNT(*) FROM share_links").fetchone()[0] == 0


def test_migration_6_is_idempotent_and_touches_no_rows(tmp_path):
    path = str(tmp_path / "t.db")
    init_db(path)
    c = sqlite3.connect(path)
    seed(c)
    before = row(c)
    MIGRATIONS[-1][1](c); MIGRATIONS[-1][1](c)
    assert row(c) == before
    triggers = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE name LIKE 'share_links%' AND type='trigger'")]
    assert sorted(triggers) == ["share_links_guard_update", "share_links_no_delete"]
    c.close()
