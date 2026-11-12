"""Safe, repeatable schema upgrades.

`schema.sql` is the original baseline and is idempotent (CREATE ... IF NOT
EXISTS). Everything added afterwards lives here as an ordered migration,
tracked with SQLite's `PRAGMA user_version`. The same code path upgrades an
existing database *and* builds a fresh one, so the two can never drift.

Migrations never drop tables or delete rows.
"""
import logging
from datetime import datetime, timezone

from database.db import transaction
from services.integrity import GENESIS_HASH, compute_hash

log = logging.getLogger(__name__)


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn, table, definition):
    name = definition.split()[0]
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _migration_1(conn):
    """Roles, evidence <-> workflow link, hash-chained audit trail."""
    _add_column(conn, "users", "role TEXT NOT NULL DEFAULT 'developer'")

    _add_column(conn, "workflow_instances", "round INTEGER NOT NULL DEFAULT 1")
    _add_column(conn, "workflow_instances", "chain_head TEXT")
    _add_column(conn, "workflow_instances", "transition_count INTEGER NOT NULL DEFAULT 0")

    for definition in (
        "seq INTEGER",
        "actor_id INTEGER",
        "actor_role TEXT",
        "evidence_digest TEXT",
        "prev_hash TEXT",
        "hash TEXT",
    ):
        _add_column(conn, "state_transitions", definition)

    _add_column(conn, "evidence",
                "workflow_id INTEGER REFERENCES workflow_instances(id) ON DELETE CASCADE")
    _add_column(conn, "evidence", "round INTEGER NOT NULL DEFAULT 1")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS blocked_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id INTEGER NOT NULL,
            actor_id INTEGER,
            event TEXT NOT NULL,
            from_state TEXT,
            code TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (workflow_id) REFERENCES workflow_instances(id) ON DELETE CASCADE
        )
        """
    )

    # Tasks that predate the workflow engine (or lost their workflow) get
    # exactly one, once, here - instead of on every dashboard page load.
    conn.execute(
        """
        INSERT INTO workflow_instances (user_id, task_id, current_state)
        SELECT t.user_id, t.id, 'CREATED'
        FROM tasks t
        WHERE NOT EXISTS (SELECT 1 FROM workflow_instances w WHERE w.task_id = t.id)
        """
    )

    _backfill_chain(conn)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_workflow ON evidence(workflow_id, round)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_blocked_workflow ON blocked_actions(workflow_id)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_transition_seq ON state_transitions(workflow_id, seq)"
    )

    duplicates = conn.execute(
        """
        SELECT task_id FROM workflow_instances
        WHERE task_id IS NOT NULL GROUP BY task_id HAVING COUNT(*) > 1
        """
    ).fetchall()
    if duplicates:
        # Never delete history to satisfy a constraint. Leave the index out and say so.
        log.warning("Duplicate workflows exist for tasks %s; one-workflow-per-task "
                    "index was NOT created.", [d["task_id"] for d in duplicates])
    else:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_task "
            "ON workflow_instances(task_id) WHERE task_id IS NOT NULL"
        )

    # The audit log is append-only at the database level. (Deleting a whole
    # workflow still cascades - the parent row is already gone by then.)
    conn.execute("DROP TRIGGER IF EXISTS state_transitions_no_update")
    conn.execute(
        """
        CREATE TRIGGER state_transitions_no_update
        BEFORE UPDATE ON state_transitions
        BEGIN
            SELECT RAISE(ABORT, 'state_transitions is append-only');
        END
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS state_transitions_no_delete")
    conn.execute(
        """
        CREATE TRIGGER state_transitions_no_delete
        BEFORE DELETE ON state_transitions
        WHEN EXISTS (SELECT 1 FROM workflow_instances WHERE id = OLD.workflow_id)
        BEGIN
            SELECT RAISE(ABORT, 'state_transitions is append-only');
        END
        """
    )


def _backfill_chain(conn):
    """Seal transitions recorded before the chain existed.

    Their original content is untouched; the chain simply starts being
    tamper-evident from the moment of migration. They are marked
    actor_role='legacy' so nobody mistakes them for chain-era records.
    """
    sealed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    workflows = conn.execute("SELECT id FROM workflow_instances").fetchall()

    for workflow in workflows:
        rows = conn.execute(
            "SELECT * FROM state_transitions WHERE workflow_id = ? ORDER BY id",
            (workflow["id"],),
        ).fetchall()
        if not rows or all(row["hash"] for row in rows):
            continue

        previous_hash = GENESIS_HASH
        rounds = 1
        for seq, row in enumerate(rows, start=1):
            record = {
                "workflow_id": row["workflow_id"],
                "seq": seq,
                "from_state": row["from_state"],
                "event": row["event"],
                "to_state": row["to_state"],
                "condition_result": row["condition_result"],
                "reason": row["reason"],
                "actor_id": None,
                "actor_role": "legacy",
                "evidence_digest": None,
                "created_at": row["created_at"] or sealed_at,
                "prev_hash": previous_hash,
            }
            record["hash"] = compute_hash(record)
            conn.execute(
                """
                UPDATE state_transitions
                SET seq = ?, actor_id = NULL, actor_role = 'legacy',
                    evidence_digest = NULL, prev_hash = ?, hash = ?, created_at = ?
                WHERE id = ?
                """,
                (seq, previous_hash, record["hash"], record["created_at"], row["id"]),
            )
            if row["event"] == "RESTART":
                rounds += 1
            previous_hash = record["hash"]

        conn.execute(
            "UPDATE workflow_instances SET chain_head = ?, transition_count = ?, round = ? "
            "WHERE id = ?",
            (previous_hash, len(rows), rounds, workflow["id"]),
        )


def _migration_2(conn):
    """Evidence is sent to one named reviewer; verified proofs can be shared by link."""
    _add_column(conn, "workflow_instances",
                "reviewer_id INTEGER REFERENCES users(id) ON DELETE SET NULL")
    _add_column(conn, "workflow_instances", "share_token TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_reviewer ON workflow_instances(reviewer_id)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_share_token "
        "ON workflow_instances(share_token) WHERE share_token IS NOT NULL"
    )


def _migration_3(conn):
    """Multiple independent reviewers, an approval policy, per-reviewer decisions.

    * review_assignments - one row per (workflow, round, reviewer); status keeps
      cancelled/reassigned reviewers in the record instead of deleting them.
    * review_decisions   - one immutable row per reviewer decision, pinned to the
      evidence digest (version) that reviewer actually saw.
    * workflow_instances.required_approvals - the approval policy (k of n).
    * state_transitions.meta - JSON sealed into the hash chain (reviewer policy,
      reassignment details, note digests).
    Nothing is dropped; no historical decision is invented.
    """
    _add_column(conn, "state_transitions", "meta TEXT")
    _add_column(conn, "workflow_instances", "required_approvals INTEGER NOT NULL DEFAULT 1")
    _add_column(conn, "workflow_instances", "share_revoked_at TIMESTAMP")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS review_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id INTEGER NOT NULL,
            round INTEGER NOT NULL,
            reviewer_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'assigned'
                CHECK (status IN ('assigned', 'cancelled')),
            assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            cancelled_at TIMESTAMP,
            UNIQUE (workflow_id, round, reviewer_id),
            FOREIGN KEY (workflow_id) REFERENCES workflow_instances(id) ON DELETE CASCADE,
            FOREIGN KEY (reviewer_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS review_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id INTEGER NOT NULL,
            round INTEGER NOT NULL,
            reviewer_id INTEGER NOT NULL,
            decision TEXT NOT NULL CHECK (decision IN ('approve', 'reject')),
            notes TEXT NOT NULL,
            private_notes TEXT,
            note_public INTEGER NOT NULL DEFAULT 0 CHECK (note_public IN (0, 1)),
            evidence_digest TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (workflow_id, round, reviewer_id),
            FOREIGN KEY (workflow_id) REFERENCES workflow_instances(id) ON DELETE CASCADE,
            FOREIGN KEY (reviewer_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_assign_reviewer ON review_assignments(reviewer_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_workflow ON review_decisions(workflow_id, round)")

    # Carry over single-reviewer assignments (migration 2). Decisions are NOT backfilled:
    # earlier approvals/rejections stay exactly where they were, in the hash chain.
    conn.execute(
        """
        INSERT OR IGNORE INTO review_assignments (workflow_id, round, reviewer_id)
        SELECT id, round, reviewer_id FROM workflow_instances WHERE reviewer_id IS NOT NULL
        """
    )

    # Decisions are append-only at the database level (their parent workflow may
    # still be deleted, which cascades).
    conn.execute("DROP TRIGGER IF EXISTS review_decisions_no_update")
    conn.execute(
        """
        CREATE TRIGGER review_decisions_no_update BEFORE UPDATE ON review_decisions
        BEGIN SELECT RAISE(ABORT, 'review_decisions is append-only'); END
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS review_decisions_no_delete")
    conn.execute(
        """
        CREATE TRIGGER review_decisions_no_delete BEFORE DELETE ON review_decisions
        WHEN EXISTS (SELECT 1 FROM workflow_instances WHERE id = OLD.workflow_id)
        BEGIN SELECT RAISE(ABORT, 'review_decisions is append-only'); END
        """
    )


def _migration_4(conn):
    """Reviewer profiles and discovery. Additive only: nothing is dropped or rewritten.

    The existing `profiles` table is reused (full_name = display name, headline,
    bio, portfolio_url, linkedin_url, profile_visibility). New nullable/defaulted
    columns hold the self-reported details and which optional parts are public.
    `profile_visibility` is 'private' (default, only the owner), 'unlisted'
    (public page by link, not in the directory) or 'listed' (directory + page).
    Skills and expertise areas live in `reviewer_tags` so they can be filtered in SQL.
    """
    for definition in (
        "experience_level TEXT",
        "work_history TEXT",
        "education TEXT",
        "github_url TEXT",
        "pub_bio INTEGER NOT NULL DEFAULT 1",
        "pub_experience INTEGER NOT NULL DEFAULT 1",
        "pub_work INTEGER NOT NULL DEFAULT 1",
        "pub_education INTEGER NOT NULL DEFAULT 1",
        "pub_links INTEGER NOT NULL DEFAULT 1",
        "updated_at TIMESTAMP",
    ):
        _add_column(conn, "profiles", definition)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reviewer_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('skill', 'expertise')),
            tag TEXT NOT NULL,
            label TEXT NOT NULL,
            UNIQUE (user_id, kind, tag)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reviewer_tags_lookup ON reviewer_tags(kind, tag, user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_profiles_user ON profiles(user_id)")
    # Every reviewer gets a (private) profile row so the editor always has one.
    conn.execute(
        """
        INSERT INTO profiles (user_id, full_name, profile_visibility)
        SELECT u.id, u.username, 'private' FROM users u
        WHERE u.role = 'reviewer' AND NOT EXISTS (SELECT 1 FROM profiles p WHERE p.user_id = u.id)
        """
    )


def _migration_5(conn):
    """Revora: versioned review requests, structured reports, findings, share links.

    Additive only. Nothing is dropped; no existing row is rewritten. Every
    workflow that exists now keeps review_mode = 'verification' and finishes
    under the old rules. See the specification, section 2.
    """
    _add_column(conn, "workflow_instances", "review_mode TEXT NOT NULL DEFAULT 'verification'")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS submission_versions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id   INTEGER NOT NULL REFERENCES workflow_instances(id) ON DELETE CASCADE,
            round         INTEGER NOT NULL,
            project_id    INTEGER NOT NULL REFERENCES projects(id),
            review_goal   TEXT NOT NULL CHECK (review_goal IN ('production', 'interview')),
            repo_url      TEXT NOT NULL,
            commit_sha    TEXT NOT NULL,
            demo_url      TEXT,
            tech_stack    TEXT NOT NULL,
            questions     TEXT,
            created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            locked_at     TIMESTAMP,
            UNIQUE (workflow_id, round)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS review_reports (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id      INTEGER NOT NULL REFERENCES workflow_instances(id) ON DELETE CASCADE,
            round            INTEGER NOT NULL,
            reviewer_id      INTEGER NOT NULL REFERENCES users(id),
            review_goal      TEXT NOT NULL,
            summary          TEXT NOT NULL,
            strengths        TEXT NOT NULL,
            unresolved       TEXT,
            assessed         TEXT NOT NULL,
            not_assessed     TEXT NOT NULL,
            interview_qs     TEXT,
            private_notes    TEXT,
            consent_ack_at   TIMESTAMP NOT NULL,
            consent_version  TEXT NOT NULL CHECK (length(consent_version) > 0),
            content_digest   TEXT NOT NULL,
            created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (workflow_id, round, reviewer_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS review_findings (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id        INTEGER NOT NULL REFERENCES review_reports(id) ON DELETE CASCADE,
            seq              INTEGER NOT NULL,
            severity         TEXT NOT NULL CHECK (severity IN ('critical', 'high', 'medium', 'low')),
            category         TEXT NOT NULL,
            title            TEXT NOT NULL,
            explanation      TEXT NOT NULL,
            suggested_fix    TEXT NOT NULL,
            reasoning        TEXT,
            file_path        TEXT,
            commit_ref       TEXT,
            repro_evidence   TEXT,
            prior_finding_id INTEGER REFERENCES review_findings(id),
            UNIQUE (report_id, seq)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS finding_updates (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            finding_id   INTEGER NOT NULL REFERENCES review_findings(id) ON DELETE CASCADE,
            seq          INTEGER NOT NULL,
            actor_id     INTEGER NOT NULL REFERENCES users(id),
            actor_role   TEXT NOT NULL CHECK (actor_role IN ('owner', 'reviewer')),
            from_status  TEXT NOT NULL,
            to_status    TEXT NOT NULL,
            note         TEXT NOT NULL,
            commit_ref   TEXT,
            created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            prev_hash    TEXT NOT NULL,
            hash         TEXT NOT NULL,
            UNIQUE (finding_id, seq)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS share_links (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id   INTEGER NOT NULL REFERENCES workflow_instances(id) ON DELETE CASCADE,
            round         INTEGER NOT NULL,
            kind          TEXT NOT NULL CHECK (kind IN ('proof', 'report')),
            token         TEXT NOT NULL UNIQUE,
            created_by    INTEGER REFERENCES users(id),
            created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            revoked_at    TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_share_active "
        "ON share_links(workflow_id, round, kind) WHERE revoked_at IS NULL"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reports_workflow ON review_reports(workflow_id, round)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_findings_report ON review_findings(report_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_updates_finding ON finding_updates(finding_id, seq)")

    # Append-only at the database level. A parent that is itself being deleted
    # (cascade) is allowed through, exactly like review_decisions.
    for table, parent_sql in (
        ("review_reports",
         "EXISTS (SELECT 1 FROM workflow_instances WHERE id = OLD.workflow_id)"),
        ("review_findings",
         "EXISTS (SELECT 1 FROM review_reports WHERE id = OLD.report_id)"),
        ("finding_updates",
         "EXISTS (SELECT 1 FROM review_findings WHERE id = OLD.finding_id)"),
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {table}_no_update")
        conn.execute(
            f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
        )
        conn.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete")
        conn.execute(
            f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
            f"WHEN {parent_sql} BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
        )

    # A submitted version is immutable.
    conn.execute("DROP TRIGGER IF EXISTS submission_versions_locked_update")
    conn.execute(
        """
        CREATE TRIGGER submission_versions_locked_update BEFORE UPDATE ON submission_versions
        WHEN OLD.locked_at IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'a submitted version is immutable'); END
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS submission_versions_locked_delete")
    conn.execute(
        """
        CREATE TRIGGER submission_versions_locked_delete BEFORE DELETE ON submission_versions
        WHEN OLD.locked_at IS NOT NULL
             AND EXISTS (SELECT 1 FROM workflow_instances WHERE id = OLD.workflow_id)
        BEGIN SELECT RAISE(ABORT, 'a submitted version is immutable'); END
        """
    )

    # Share links: never deleted; the only permitted change is revoking an active link.
    conn.execute("DROP TRIGGER IF EXISTS share_links_guard_update")
    conn.execute(
        """
        CREATE TRIGGER share_links_guard_update BEFORE UPDATE ON share_links
        WHEN OLD.revoked_at IS NOT NULL
             OR NEW.revoked_at IS NULL
             OR NEW.token != OLD.token OR NEW.workflow_id != OLD.workflow_id
             OR NEW.round != OLD.round OR NEW.kind != OLD.kind
        BEGIN SELECT RAISE(ABORT, 'share links can only be revoked'); END
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS share_links_no_delete")
    conn.execute(
        """
        CREATE TRIGGER share_links_no_delete BEFORE DELETE ON share_links
        WHEN EXISTS (SELECT 1 FROM workflow_instances WHERE id = OLD.workflow_id)
        BEGIN SELECT RAISE(ABORT, 'share links are never deleted'); END
        """
    )

    # Carry over existing public proof links. The legacy columns stay in place
    # (a rollback to the previous version still reads them) but are no longer
    # the source of truth.
    conn.execute(
        """
        INSERT OR IGNORE INTO share_links (workflow_id, round, kind, token, created_at, revoked_at)
        SELECT id, round, 'proof', share_token,
               COALESCE(completed_at, started_at, CURRENT_TIMESTAMP), share_revoked_at
        FROM workflow_instances WHERE share_token IS NOT NULL
        """
    )


def _migration_6(conn):
    """Close a hole in the share-link guard added by migration 5.

    The old trigger compared only some columns, and with `!=`, which is NULL
    (so "not changed") whenever either side is NULL. An UPDATE could therefore
    revoke a link and change id, created_by or created_at at the same time.
    The rule is now: a link may be revoked exactly once, and no other column may
    change in any way. Comparisons use IS NOT, which is NULL-safe. Idempotent;
    no row is touched.
    """
    conn.execute("DROP TRIGGER IF EXISTS share_links_guard_update")
    conn.execute(
        """
        CREATE TRIGGER share_links_guard_update BEFORE UPDATE ON share_links
        WHEN OLD.revoked_at IS NOT NULL
             OR NEW.revoked_at IS NULL
             OR NEW.id IS NOT OLD.id
             OR NEW.workflow_id IS NOT OLD.workflow_id
             OR NEW.round IS NOT OLD.round
             OR NEW.kind IS NOT OLD.kind
             OR NEW.token IS NOT OLD.token
             OR NEW.created_by IS NOT OLD.created_by
             OR NEW.created_at IS NOT OLD.created_at
        BEGIN SELECT RAISE(ABORT, 'share links can only be revoked'); END
        """
    )


MIGRATIONS = [
    (1, _migration_1),
    (2, _migration_2),
    (3, _migration_3),
    (4, _migration_4),
    (5, _migration_5),
    (6, _migration_6),
]
LATEST_VERSION = MIGRATIONS[-1][0]


def run_migrations(conn):
    """Apply every pending migration, each in its own transaction."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for target, migrate in MIGRATIONS:
        if version >= target:
            continue
        with transaction(conn):
            migrate(conn)
            conn.execute(f"PRAGMA user_version = {int(target)}")
        log.info("Applied database migration %s", target)
        version = target
    return version
