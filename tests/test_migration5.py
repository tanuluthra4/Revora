"""Migration 5 and the backup tooling, on temporary databases built from the
real legacy schema (schema.sql + migrations 1..4 + hand-sealed chains).
Production data is never touched."""
import os
import sqlite3

import pytest
from click.testing import CliRunner

from app import create_app, init_db
from database.backup import BackupError, backup_database, pre_migration_backup
from database.db import get_db_connection
from database.migrations import LATEST_VERSION, MIGRATIONS
from services.integrity import verify_chain
from tests.test_migrations import SCHEMA, build_v2_database

NEW_TABLES = {"submission_versions", "review_reports", "review_findings",
              "finding_updates", "share_links"}


def build_v4_database(path):
    """A v4 database (the release before Revisely) with a verified legacy proof,
    a shared and a revoked link, an open request and a reviewer profile."""
    build_v2_database(path)                      # v2 with a sealed VERIFIED chain + share token
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA user_version = 2")
    for target, migrate in MIGRATIONS:
        if 3 <= target <= 4:
            migrate(conn)
            conn.execute(f"PRAGMA user_version = {target}")
    # a second workflow whose share link was revoked
    conn.execute("INSERT INTO tasks (user_id, title, status) VALUES (1, 'Old revoked', 'Completed')")
    conn.execute("INSERT INTO workflow_instances (user_id, task_id, current_state, result, share_token, "
                 "share_revoked_at) VALUES (1, 2, 'CREATED', NULL, 'revokedtoken', '2026-02-01 00:00:00')")
    conn.commit()
    conn.close()


def counts(path, tables):
    conn = sqlite3.connect(path)
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        conn.close()


OLD_TABLES = ("users", "tasks", "workflow_instances", "state_transitions", "evidence",
              "review_assignments", "review_decisions", "profiles", "reviewer_tags")


def test_v4_database_upgrades_without_losing_or_rewriting_rows(tmp_path):
    path = str(tmp_path / "v4.db")
    build_v4_database(path)
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == 4
    before = counts(path, OLD_TABLES)
    old_chain = sqlite3.connect(path).execute(
        "SELECT seq, event, hash, prev_hash, reason FROM state_transitions ORDER BY id").fetchall()

    assert init_db(path) == LATEST_VERSION == 6

    assert counts(path, OLD_TABLES) == before
    conn = get_db_connection(path)
    assert [tuple(r) for r in conn.execute(
        "SELECT seq, event, hash, prev_hash, reason FROM state_transitions ORDER BY id")] == \
        [tuple(r) for r in old_chain]
    assert verify_chain(conn, 1)["ok"]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert NEW_TABLES <= tables
    # every existing workflow stays in verification mode; nothing becomes a report
    assert {r[0] for r in conn.execute("SELECT review_mode FROM workflow_instances")} == {"verification"}
    for table in NEW_TABLES - {"share_links"}:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    conn.close()


def test_share_links_copied_with_revocation_preserved(tmp_path):
    path = str(tmp_path / "v4.db")
    build_v4_database(path)
    init_db(path)
    conn = get_db_connection(path)
    rows = {r["token"]: r for r in conn.execute("SELECT * FROM share_links")}
    assert set(rows) == {"legacytoken", "revokedtoken"}
    assert rows["legacytoken"]["revoked_at"] is None and rows["legacytoken"]["kind"] == "proof"
    assert rows["revokedtoken"]["revoked_at"] == "2026-02-01 00:00:00"
    # legacy columns stay in place for a rollback to the previous release
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(workflow_instances)")}
    assert {"share_token", "share_revoked_at", "review_mode"} <= cols
    conn.close()


def test_migration_is_repeatable_and_idempotent(tmp_path):
    path = str(tmp_path / "v4.db")
    build_v4_database(path)
    init_db(path)
    before = counts(path, OLD_TABLES + tuple(NEW_TABLES))
    assert init_db(path) == LATEST_VERSION
    assert counts(path, OLD_TABLES + tuple(NEW_TABLES)) == before


def test_fresh_database_has_no_backup_and_full_schema(tmp_path):
    path = str(tmp_path / "fresh.db")
    assert init_db(path) == LATEST_VERSION
    assert not [f for f in os.listdir(tmp_path) if ".bak" in f]
    conn = get_db_connection(path)
    assert NEW_TABLES <= {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()


def test_v2_and_v3_chains_survive_the_full_upgrade(tmp_path):
    path = str(tmp_path / "v2.db")
    build_v2_database(path)
    assert init_db(path) == LATEST_VERSION
    conn = get_db_connection(path)
    assert verify_chain(conn, 1)["ok"]
    conn.close()


def test_pre_migration_copy_is_made_and_is_a_valid_v4_database(tmp_path):
    path = str(tmp_path / "v4.db")
    build_v4_database(path)
    init_db(path)
    copy = path + ".pre-v6.bak"
    assert os.path.isfile(copy)
    c = sqlite3.connect(copy)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 4
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not (NEW_TABLES & names)               # it is the state BEFORE migration 5
    c.close()


def test_existing_copy_is_never_overwritten(tmp_path):
    path = str(tmp_path / "v4.db")
    build_v4_database(path)
    first = pre_migration_backup(path, 6)
    second = pre_migration_backup(path, 6)
    assert first != second and os.path.isfile(first) and os.path.isfile(second)


def test_no_migration_when_the_backup_fails(tmp_path, monkeypatch):
    path = str(tmp_path / "v4.db")
    build_v4_database(path)

    def boom(*a, **k):
        raise BackupError("disk full")
    monkeypatch.setattr("app.pre_migration_backup", boom)
    with pytest.raises(BackupError):
        init_db(path)
    c = sqlite3.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 4          # untouched
    assert "share_links" not in {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    c.close()


def test_failed_migration_rolls_back_completely(tmp_path, monkeypatch):
    import database.migrations as m
    path = str(tmp_path / "v4.db")
    build_v4_database(path)

    def failing(conn):
        m_original(conn)
        raise RuntimeError("simulated failure after the DDL")
    m_original = m._migration_5
    monkeypatch.setattr(m, "MIGRATIONS", m.MIGRATIONS[:4] + [(5, failing)])
    with pytest.raises(RuntimeError):
        init_db(path)
    c = sqlite3.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 4
    assert not (NEW_TABLES & {r[0] for r in c.execute("SELECT name FROM sqlite_master")})
    assert "review_mode" not in {r[1] for r in c.execute("PRAGMA table_info(workflow_instances)")}
    c.close()


def test_new_tables_are_append_only(tmp_path):
    path = str(tmp_path / "fresh.db")
    init_db(path)
    conn = get_db_connection(path)
    conn.execute("INSERT INTO users (username, email, password, role) VALUES ('a','a@x.com','x','developer')")
    conn.execute("INSERT INTO users (username, email, password, role) VALUES ('r','r@x.com','x','reviewer')")
    conn.execute("INSERT INTO projects (user_id, title) VALUES (1, 'p')")
    conn.execute("INSERT INTO workflow_instances (user_id, current_state) VALUES (1, 'CREATED')")
    conn.execute("INSERT INTO submission_versions (workflow_id, round, project_id, review_goal, repo_url, "
                 "commit_sha, tech_stack, locked_at) VALUES (1, 1, 1, 'production', 'u', 'abc1234', 'py', "
                 "CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO review_reports (workflow_id, round, reviewer_id, review_goal, summary, strengths, "
                 "assessed, not_assessed, consent_ack_at, consent_version, content_digest) "
                 "VALUES (1, 1, 2, 'production', 's', 's', 'a', 'n', CURRENT_TIMESTAMP, 'v1', 'd')")
    conn.execute("INSERT INTO review_findings (report_id, seq, severity, category, title, explanation, "
                 "suggested_fix) VALUES (1, 1, 'high', 'security', 't', 'e', 'f')")
    conn.execute("INSERT INTO share_links (workflow_id, round, kind, token) VALUES (1, 1, 'report', 'tok')")
    for sql in ("UPDATE submission_versions SET repo_url = 'x'", "DELETE FROM submission_versions",
                "UPDATE review_reports SET summary = 'x'", "DELETE FROM review_reports",
                "UPDATE review_findings SET title = 'x'", "DELETE FROM review_findings",
                "DELETE FROM share_links", "UPDATE share_links SET token = 'other'"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
    conn.execute("UPDATE share_links SET revoked_at = CURRENT_TIMESTAMP")      # revoking is allowed
    with pytest.raises(sqlite3.IntegrityError):                                # ... exactly once
        conn.execute("UPDATE share_links SET revoked_at = NULL")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO review_reports (workflow_id, round, reviewer_id, review_goal, summary, "
                     "strengths, assessed, not_assessed, consent_ack_at, consent_version, content_digest) "
                     "VALUES (1, 2, 2, 'production', 's', 's', 'a', 'n', CURRENT_TIMESTAMP, '', 'd')")
    with pytest.raises(sqlite3.IntegrityError):                                # consent is mandatory
        conn.execute("INSERT INTO review_reports (workflow_id, round, reviewer_id, review_goal, summary, "
                     "strengths, assessed, not_assessed, consent_version, content_digest) "
                     "VALUES (1, 2, 2, 'production', 's', 's', 'a', 'n', 'v1', 'd')")
    # deleting the whole workflow still cascades cleanly
    conn.execute("DELETE FROM workflow_instances WHERE id = 1")
    assert conn.execute("SELECT COUNT(*) FROM review_reports").fetchone()[0] == 0
    conn.close()


# ------------------------------------------------------------------ backup-db
def test_backup_command_writes_a_verified_copy(tmp_path):
    db = str(tmp_path / "live.db")
    app = create_app({"DATABASE": db, "TESTING": True, "SECRET_KEY": "k"})
    out = str(tmp_path / "copy.db")
    result = CliRunner().invoke(app.cli, ["backup-db", "--out", out])
    assert result.exit_code == 0, result.output
    assert "verified" in result.output
    c = sqlite3.connect(out)
    assert c.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION
    c.close()
    again = CliRunner().invoke(app.cli, ["backup-db", "--out", out])        # never overwrites
    assert again.exit_code != 0 and "overwrite" in again.output


def test_backup_of_a_missing_database_fails_cleanly(tmp_path):
    with pytest.raises(BackupError):
        backup_database(str(tmp_path / "nope.db"), str(tmp_path / "x.bak"))

def test_backup_never_overwrites_existing_destination(tmp_path):
    source = tmp_path / "source.db"
    destination = tmp_path / "existing.bak"

    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE sample (value INTEGER)")
    conn.execute("INSERT INTO sample VALUES (123)")
    conn.commit()
    conn.close()

    destination.write_text("DO NOT OVERWRITE")

    with pytest.raises(BackupError):
        backup_database(str(source), str(destination))

    assert destination.read_text() == "DO NOT OVERWRITE"


def test_backup_wraps_filesystem_errors(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE sample (value INTEGER)")
    conn.commit()
    conn.close()

    def fail_link(source, destination):
        raise OSError("simulated filesystem failure")

    def fail_open(*args, **kwargs):
        raise OSError("simulated filesystem failure")

    # A hard-link failure alone now falls back to an exclusive copy (see
    # test_backup_restore.py); the error must still surface when that fails too.
    monkeypatch.setattr("database.backup.os.link", fail_link)
    monkeypatch.setattr("database.backup.os.open", fail_open)

    with pytest.raises(BackupError, match="simulated filesystem failure"):
        backup_database(str(source), str(tmp_path / "copy.bak"))

    assert not (tmp_path / "copy.bak").exists()
    assert not list(tmp_path.glob("*.partial"))
