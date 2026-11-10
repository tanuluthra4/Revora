"""Consistent SQLite backups (online backup API) and the pre-migration safety copy.

`sqlite3.Connection.backup` copies a live database page by page under SQLite's
own locking, so the copy is consistent even while the app is serving requests
(plain `cp` of a WAL database is not). Every copy is verified with
`PRAGMA integrity_check` before it is reported as good.
"""
import os
import sqlite3
import tempfile
from datetime import datetime, timezone


class BackupError(RuntimeError):
    """A backup could not be made or did not verify. Callers must stop."""


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def backup_database(source_path, dest_path):
    """Copy source_path to dest_path; returns dest_path. Never overwrites."""
    if not os.path.isfile(source_path):
        raise BackupError(f"No database at {source_path}.")

    directory = os.path.dirname(os.path.abspath(dest_path))
    partial = None
    src = dst = None

    try:
        os.makedirs(directory, exist_ok=True)

        # Fast rejection; os.link below provides the actual no-overwrite guarantee.
        if os.path.exists(dest_path):
            raise BackupError(
                f"Refusing to overwrite existing file {dest_path}."
            )

        # Unique temporary file in the same directory/filesystem.
        fd, partial = tempfile.mkstemp(
            prefix=".backup-",
            suffix=".partial",
            dir=directory,
        )
        os.close(fd)

        src = sqlite3.connect(
            f"file:{os.path.abspath(source_path)}?mode=ro",
            uri=True,
            timeout=10,
        )
        dst = sqlite3.connect(partial)

        src.backup(dst)

        result = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise BackupError(
                f"Backup failed its integrity check: {result}"
            )

        dst.close()
        dst = None

        src.close()
        src = None

        _publish_without_overwrite(partial, dest_path)

    except BackupError:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise BackupError(f"Backup failed: {exc}") from exc
    finally:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()
        if partial is not None and os.path.exists(partial):
            try:
                os.remove(partial)
            except OSError:
                pass

    return dest_path

def _publish_without_overwrite(partial, dest_path):
    """Move the finished copy into place; fails rather than replacing an existing file.

    A hard link is atomic. Some filesystems refuse hard links, so fall back to
    creating the destination exclusively (O_EXCL) and copying into it, which
    still cannot overwrite anything, and remove it again if the copy fails.
    """
    try:
        os.link(partial, dest_path)
        return
    except FileExistsError:
        raise BackupError(f"Refusing to overwrite existing file {dest_path}.") from None
    except OSError:
        pass
    try:
        fd = os.open(dest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise BackupError(f"Refusing to overwrite existing file {dest_path}.") from None
    try:
        with os.fdopen(fd, "wb") as out, open(partial, "rb") as src:
            while chunk := src.read(1024 * 1024):
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        try:
            os.remove(dest_path)
        except OSError:
            pass
        raise


def default_backup_path(database_path):
    folder = os.path.join(os.path.dirname(os.path.abspath(database_path)), "backups")
    return os.path.join(folder, f"{os.path.basename(database_path)}.{_stamp()}.bak")


def pre_migration_backup(database_path, target_version):
    """Safety copy taken before upgrading an existing database.

    Returns the path of the copy, or None when there is nothing to protect
    (no file yet, or an empty one). Raises BackupError on failure - the
    caller must then NOT migrate.
    """
    if not os.path.isfile(database_path) or os.path.getsize(database_path) == 0:
        return None
    base = f"{database_path}.pre-v{target_version}.bak"
    dest = base if not os.path.exists(base) else f"{database_path}.pre-v{target_version}-{_stamp()}.bak"
    return backup_database(database_path, dest)
