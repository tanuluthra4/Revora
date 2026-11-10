"""Backup and restore on throwaway databases only."""
import os
import sqlite3

import pytest

from database.backup import BackupError, backup_database, pre_migration_backup


def make_db(path, rows=3):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    c.executemany("INSERT INTO t (v) VALUES (?)", [(f"row{i}",) for i in range(rows)])
    c.commit(); c.close()


def rows(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT v FROM t ORDER BY id").fetchall()
    finally:
        c.close()


def test_backup_is_a_consistent_verified_copy_and_source_is_unchanged(tmp_path):
    src = str(tmp_path / "live.db"); make_db(src)
    before = open(src, "rb").read()
    out = backup_database(src, str(tmp_path / "b" / "copy.bak"))
    assert rows(out) == rows(src) and open(src, "rb").read() == before
    assert not [f for f in os.listdir(tmp_path / "b") if f.endswith(".partial")]


def test_backup_never_overwrites_an_existing_file(tmp_path):
    src = str(tmp_path / "live.db"); make_db(src)
    dest = str(tmp_path / "keep.bak"); open(dest, "wb").write(b"precious")
    with pytest.raises(BackupError, match="overwrite"):
        backup_database(src, dest)
    assert open(dest, "rb").read() == b"precious"


def test_backup_works_without_hard_links_and_still_never_overwrites(tmp_path, monkeypatch):
    src = str(tmp_path / "live.db"); make_db(src)
    def no_links(*a, **k):
        raise OSError(1, "Operation not permitted")
    monkeypatch.setattr(os, "link", no_links)
    out = backup_database(src, str(tmp_path / "nolink.bak"))
    assert rows(out) == rows(src)
    with pytest.raises(BackupError, match="overwrite"):
        backup_database(src, out)
    assert rows(out) == rows(src)
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".partial")]


def test_failed_copy_leaves_no_partial_destination(tmp_path, monkeypatch):
    src = str(tmp_path / "live.db"); make_db(src)
    monkeypatch.setattr(os, "link", lambda *a: (_ for _ in ()).throw(OSError(1, "nope")))
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError(28, "No space left")))
    with pytest.raises(BackupError):
        backup_database(src, str(tmp_path / "x.bak"))
    monkeypatch.setattr(os, "fsync", real_fsync)
    assert not os.path.exists(tmp_path / "x.bak")
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".partial")]


def test_missing_source_and_corrupt_source_are_clean_errors(tmp_path):
    with pytest.raises(BackupError, match="No database"):
        backup_database(str(tmp_path / "nope.db"), str(tmp_path / "x.bak"))
    bad = tmp_path / "bad.db"; bad.write_bytes(b"this is not a sqlite database" * 100)
    with pytest.raises(BackupError):
        backup_database(str(bad), str(tmp_path / "y.bak"))
    assert not os.path.exists(tmp_path / "y.bak")
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".partial")]


def test_backup_while_the_database_is_being_written(tmp_path):
    src = str(tmp_path / "live.db"); make_db(src, rows=200)
    writer = sqlite3.connect(src, isolation_level=None, timeout=10)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO t (v) VALUES ('uncommitted')")
    out = backup_database(src, str(tmp_path / "during.bak"))
    writer.execute("COMMIT"); writer.close()
    assert len(rows(out)) == 200                         # a consistent snapshot, never half a transaction
    assert sqlite3.connect(out).execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_restore_procedure_on_a_separate_database(tmp_path):
    """The documented restore: keep the damaged file, copy the backup into place."""
    live = str(tmp_path / "live.db"); make_db(live)
    good = backup_database(live, str(tmp_path / "good.bak"))
    c = sqlite3.connect(live); c.execute("DELETE FROM t"); c.commit(); c.close()      # disaster
    assert rows(live) == []
    os.replace(live, live + ".damaged")
    import shutil
    shutil.copyfile(good, live)
    assert rows(live) == [("row0",), ("row1",), ("row2",)]
    assert sqlite3.connect(live).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert rows(live + ".damaged") == []                 # the damaged file was kept, not destroyed


def test_pre_migration_backup_skips_empty_or_missing_databases(tmp_path):
    assert pre_migration_backup(str(tmp_path / "none.db"), 6) is None
    (tmp_path / "empty.db").write_bytes(b"")
    assert pre_migration_backup(str(tmp_path / "empty.db"), 6) is None
