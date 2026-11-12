# Operations: persistence, backups, staged rollout and rollback

Status: **plan only.** Nothing in this repository changes the live Render service, its plan, its disk, its database or its deployment configuration. Every step marked **(owner approval)** needs the owner's explicit go-ahead.

## 1. Persistence decision (approved): paid web service + persistent disk + SQLite

Facts from Render's documentation (checked 2026-10-26; re-check before acting, and no prices were verified):
- A free web service's filesystem is ephemeral: the database is lost on every deploy or restart.
- Persistent disks need a paid web service, attach to a **single instance**, and mean **no zero-downtime deploys**.
- Render keeps a daily disk snapshot for 7 days; restoring a snapshot restores the **whole disk**.
- A free Postgres database expires after 30 days and has no backups; paid Postgres has point-in-time recovery. Postgres is not implemented here.

Settings (commented in `render.yaml`, not applied): `DATABASE_PATH=/var/data/pow_os.db`, disk mounted at `/var/data`, start command unchanged (one gunicorn worker, four threads, which is correct for SQLite).

## 2. Backups

### Automatic pre-migration copy
On start, if the database exists and a migration is pending, the app writes `<db>.pre-v6.bak` using SQLite's online backup API, verifies it with `PRAGMA integrity_check`, and **does not migrate if that fails**. An existing `.pre-v6.bak` is never overwritten (a timestamped name is used instead).

### On-demand copy
```bash
flask --app app backup-db                  # backups/<db>.<UTC time>.bak next to the database
flask --app app backup-db --out /var/data/backups/manual-1.bak
```
Consistent while the app is running; refuses to overwrite; verifies the copy. Run it from the Render shell. Keep copies **off the instance's only disk** as well (see 2.3).

### Monthly off-platform copy (procedure and limits)
1. Run `backup-db` on the service shell.
2. Download the resulting file to a machine you control (Render shell / SCP as your plan allows).
3. Store it somewhere that is not Render (encrypted storage you own). **The file contains password hashes, private reviewer notes and unshared reports: treat it as confidential, never commit it, never e-mail it.**
4. Test the copy once in a while: `sqlite3 copy.bak "PRAGMA integrity_check"` should print `ok`.

Limits: it is manual (a missed month is a month of potential loss); everything since the last copy is lost in a disaster; Render's own snapshots help only for 7 days and restore the whole disk; the copy is a point in time, so the audit chain of a restored database ends where the copy ended; SQLite on one instance is a single point of failure.

## 3. Restore
1. Stop writes: suspend the service **(owner approval)**.
2. Keep the damaged file: `mv pow_os.db pow_os.db.damaged` (also move `-wal` and `-shm`).
3. Copy the backup into place as `pow_os.db`.
4. Start the service. It will migrate if the copy is older than the code (a new `.pre-v6.bak` is made first).
5. Check `/healthz`, log in, open a request and confirm the integrity box reads "Integrity verified".
Whole-disk restore from a Render snapshot is the alternative and also reverts everything on the disk.

## 4. Staged rollout (staging first)
1. **Staging (owner approval to create):** a separate paid web service with its own disk and its own `SECRET_KEY`, **never sharing the production disk**. Deploy this branch there.
2. Copy a recent production backup to the staging disk (it holds real password hashes: restrict access, delete it afterwards). Start the app. Confirm `.pre-v6.bak` appeared and the log says it migrated to the latest version (v6).
3. Run `docs/E2E_CHECKLIST.md` on staging, including a replay of an old verified proof and its public link.
4. Take a production backup (`backup-db`) and download it off-platform **(owner approval)**.
5. Attach the disk and set `DATABASE_PATH` on production **(owner approval)**. The first start migrates; check the log line `Database backed up to ...`.
6. Smoke-test production: `/healthz`, a legacy `/proof/<token>`, log in, open an old request, create a review request.

## 5. Rollback
Migration 5 only adds tables and columns, so the previous release still opens a migrated database. **But it cannot display a request in the new `REVIEWED` state: its dashboard raises `ValueError: 'REVIEWED' is not a valid WorkflowState`** (reproduced against the real v4 code). Therefore:
- **Before any review-mode request exists:** redeploy the previous commit. Old verification flows, profiles, picker and `/proof/` links work (legacy share columns are kept in step).
- **After review-mode requests exist:** do not roll back the code over live data. Fix forward, or restore `<db>.pre-v6.bak` / a backup taken before the rollout, which **discards everything created after that point** (reports, findings, new accounts, new links).
- Review-mode links (`/r/<token>`) and reports are invisible to the previous release; review-mode requests that are not yet REVIEWED would show in it without a mode guard, which is another reason not to run both versions on the same data.

## Renaming the service, repository or database file (not done; needs your approval)
The product is called Revora, but three technical names still say `proof-of-work-os` / `pow_os`. They were left alone on purpose:
* **Render service name** (`render.yaml`): if the Blueprint is synced with a different name, Render may create a *new* service with a new, empty disk and database instead of renaming the old one. Rename it in the Render dashboard (Settings) instead, after taking a backup; the `onrender.com` address changes with the name.
* **GitHub repository** (`tanuluthra4/proof-of-work-os`, linked in the landing-page footer): rename it on GitHub, then update that one link in `templates/welcome.html` and the clone URL in the README. GitHub redirects the old address.
* **Database file `pow_os.db`**: renaming it would make the app open a new empty file. Keep the name, or move the file and `DATABASE_PATH` together during a planned, backed-up change.
